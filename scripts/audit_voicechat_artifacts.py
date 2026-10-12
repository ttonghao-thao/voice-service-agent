"""Read-only VoiceChat disk metadata audit; never load weights or a vLLM engine."""

import argparse
import ast
import contextlib
import importlib.metadata
import importlib.util
import json
import math
import os
import struct
import sys
from itertools import islice
from pathlib import Path

MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_HEADER_BYTES = 16 * 1024 * 1024
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_FILES = 128
MAX_TENSORS = 100_000
MAX_ITEMS = 20
MAX_OUTPUT_BYTES = 64 * 1024
TOKENS = ("<s>", "</s>", "<SPECIAL_12>", "<SPECIAL_20>", "<SPECIAL_21>")
SOURCE_MARKERS = (
    "function_head", "function_tokens", "function_logits", "use_function_head",
    "custom_outputs", "argmax", "pad_token_id", "WeightsMapper", "load_weights", "stt_model",
)
DTYPE_BYTES = {
    "BOOL": 1, "U8": 1, "I8": 1, "U16": 2, "I16": 2, "U32": 4, "I32": 4,
    "U64": 8, "I64": 8, "F16": 2, "BF16": 2, "F32": 4, "F64": 8,
    "F8_E4M3": 1, "F8_E5M2": 1, "F8_E4M3FN": 1, "F8_E8M0": 1,
}


class AuditError(ValueError):
    pass


def clipped(value, limit=200):
    return "".join(c if c.isprintable() else "?" for c in str(value))[:limit]


def check(status, reason, **data):
    return {"status": status, "reason": reason, **data}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AuditError("duplicate_json_key")
        result[key] = value
    return result


def bounded_json(raw):
    result = json.loads(raw, object_pairs_hook=unique_object,
                        parse_constant=lambda _: (_ for _ in ()).throw(AuditError("nonfinite_json")))
    if not isinstance(result, dict):
        raise AuditError("json_object_required")
    return result


def read_json(path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise AuditError("json_size_limit")
    return bounded_json(raw)


def inside(root, path):
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise AuditError("artifact_path_outside_directory")
    return resolved


def capture(operation):
    try:
        return operation()
    except (OSError, ValueError, RuntimeError, RecursionError, OverflowError, SyntaxError) as exc:
        reason = str(exc) if isinstance(exc, AuditError) else type(exc).__name__
        return check("failed", clipped(reason))


def read_header(path):
    """Read exactly prefix + header; stat and offsets validate payload bounds, never its bytes."""
    with path.open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        prefix = stream.read(8)
        if len(prefix) != 8:
            raise AuditError("safetensors_prefix_truncated")
        header_size = struct.unpack("<Q", prefix)[0]
        if not 0 < header_size <= min(MAX_HEADER_BYTES, size - 8):
            raise AuditError("safetensors_header_bounds")
        raw = stream.read(header_size)
        if len(raw) != header_size:
            raise AuditError("safetensors_header_truncated")
    if not raw.startswith(b"{"):
        raise AuditError("safetensors_header_not_object_prefix")
    header = bounded_json(raw)
    metadata = header.get("__metadata__", {})
    if not isinstance(metadata, dict) or any(not isinstance(v, str) for v in metadata.values()):
        raise AuditError("safetensors_invalid_metadata")
    header.pop("__metadata__", None)
    if not header or len(header) > MAX_TENSORS:
        raise AuditError("safetensors_tensor_count")
    payload_size = size - 8 - header_size
    intervals = []
    unknown_dtypes = set()
    for name, meta in header.items():
        if not isinstance(name, str) or not 0 < len(name) <= 1024 or not isinstance(meta, dict):
            raise AuditError("safetensors_tensor_metadata")
        shape, offsets, dtype = meta.get("shape"), meta.get("data_offsets"), meta.get("dtype")
        if (not isinstance(shape, list) or len(shape) > 16
                or any(type(d) is not int or not 0 <= d < 2**63 for d in shape)
                or not isinstance(dtype, str) or not 0 < len(dtype) <= 32
                or not isinstance(offsets, list) or len(offsets) != 2
                or any(type(n) is not int for n in offsets)
                or not 0 <= offsets[0] <= offsets[1] <= payload_size):
            raise AuditError("safetensors_invalid_shape_dtype_offsets")
        if dtype in DTYPE_BYTES:
            if offsets[1] - offsets[0] != math.prod(shape) * DTYPE_BYTES[dtype]:
                raise AuditError("safetensors_shape_byte_mismatch")
        else:
            unknown_dtypes.add(dtype)
        intervals.append(tuple(offsets))
    end = 0
    for start, stop in sorted(intervals):
        if start != end:
            raise AuditError("safetensors_overlap_or_hole")
        end = stop
    if end != payload_size:
        raise AuditError("safetensors_unindexed_payload")
    return header, unknown_dtypes


def audit_config(llm):
    config = read_json(inside(llm, llm / "config.json"))
    for key in ("hidden_size", "vocab_size"):
        if type(config.get(key)) is not int or config[key] <= 0:
            raise AuditError("config_dimensions_missing_or_invalid")
    architectures = config.get("architectures", [])
    outputs = config.get("custom_outputs", [])
    if (not isinstance(architectures, list) or not all(isinstance(x, str) for x in architectures)
            or not isinstance(outputs, list) or not all(isinstance(x, str) for x in outputs)):
        raise AuditError("config_declaration_type")
    inputs = config.get("custom_input_specs", [])
    if not isinstance(inputs, list) or not all(isinstance(x, dict) for x in inputs):
        raise AuditError("config_input_spec_type")
    combined = [x for x in inputs if x.get("name") == "combined_embeds"]
    valid = (bool(architectures) and "function_tokens" in outputs and "text_logits" in outputs
             and len(combined) == 1 and type(combined[0].get("dim")) is int
             and combined[0]["dim"] == config["hidden_size"]
             and isinstance(combined[0].get("dtype"), str) and bool(combined[0]["dtype"]))
    roles = {k: config[k] for k in ("bos_token_id", "eos_token_id", "pad_token_id")
             if type(config.get(k)) is int}
    return check("verified" if valid else "failed", "config_declarations_only",
                 hidden_size=config["hidden_size"], vocab_size=config["vocab_size"], token_ids=roles,
                 architectures=[clipped(x) for x in architectures[:MAX_ITEMS]],
                 custom_outputs=[clipped(x) for x in outputs[:MAX_ITEMS]],
                 combined_embeds=[{k: clipped(x[k]) if isinstance(x.get(k), str) else x.get(k)
                                   for k in ("name", "dim", "dtype") if isinstance(x.get(k), (str, int))}
                                  for x in combined[:MAX_ITEMS]],
                 use_function_head=config.get("use_function_head")
                 if type(config.get("use_function_head")) is bool else None,
                 auto_map_declared=isinstance(config.get("auto_map"), dict))


def source_metadata(path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise AuditError("source_size_limit")
    tree = ast.parse(raw, filename=str(path))
    markers = {marker: set() for marker in SOURCE_MARKERS}
    classes = []
    registry_entries = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            classes.append({"name": clipped(node.name), "line": node.lineno})
        value = (node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute)
                 else node.name if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 else node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else "")
        for marker in SOURCE_MARKERS:
            if marker in value:
                markers[marker].add(node.lineno)
        if isinstance(node, ast.Dict):
            for key, value_node in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "NemotronHForCausalLM":
                    entry = {"line": key.lineno, "literal_mapping": None}
                    with contextlib.suppress(ValueError, TypeError):
                        mapping = ast.literal_eval(value_node)
                        if isinstance(mapping, (list, tuple)) and len(mapping) <= 4:
                            entry["literal_mapping"] = [clipped(x) for x in mapping if isinstance(x, str)]
                    registry_entries.append(entry)
    return check("verified", "disk_source_metadata_only", path=clipped(path, 300),
                 classes=classes[:MAX_ITEMS],
                 markers={k: sorted(v)[:MAX_ITEMS] for k, v in markers.items() if v},
                 registry_entries=registry_entries[:MAX_ITEMS],
                 runtime_class_verified=False)


def audit_vllm(args):
    if args.vllm_package_dir:
        path = Path(args.vllm_package_dir).resolve()
        if not path.is_dir():
            raise AuditError("vllm_package_directory_missing")
        root = str(path)
        return check("verified", "explicit_disk_package_not_running_environment",
                     package_roots=[clipped(root, 300)], version=None, _roots=[root])
    # Top-level lookup only: nested find_spec can import parent packages.
    spec = importlib.util.find_spec("vllm")
    if spec is None or not spec.submodule_search_locations:
        return check("unknown", "vllm_not_visible_to_this_python", package_roots=[])
    version = None
    with contextlib.suppress(importlib.metadata.PackageNotFoundError):
        version = importlib.metadata.version("vllm")
    return check("verified", "python_package_location_only",
                 package_roots=[clipped(x, 300) for x in list(spec.submodule_search_locations)[:MAX_ITEMS]],
                 version=clipped(version) if version else None,
                 _roots=list(spec.submodule_search_locations)[:MAX_ITEMS])


def select_weights(llm, index):
    indices = sorted(islice(llm.glob("*.safetensors.index.json"), 2)) if not index else []
    if index:
        index_path = inside(llm, Path(index) if Path(index).is_absolute() else llm / index)
    elif len(indices) > 1:
        raise AuditError("multiple_indices_select_with_index")
    else:
        index_path = inside(llm, indices[0]) if indices else None
    weight_map = None
    if index_path:
        weight_map = read_json(index_path).get("weight_map")
        if (not isinstance(weight_map, dict) or not 0 < len(weight_map) <= MAX_TENSORS
                or any(not isinstance(k, str) or not 0 < len(k) <= 1024
                       or not isinstance(v, str) or not v or Path(v).is_absolute()
                       or ".." in Path(v).parts
                       or Path(v).suffix != ".safetensors" for k, v in weight_map.items())):
            raise AuditError("invalid_weight_map")
        files = [(name, inside(llm, llm / name)) for name in sorted(set(weight_map.values()))]
    else:
        paths = list(islice(llm.glob("*.safetensors"), MAX_FILES + 1))
        if len(paths) > MAX_FILES:
            raise AuditError("weight_file_count_missing_or_over_limit")
        files = [(str(path.relative_to(llm)), inside(llm, path)) for path in sorted(paths)]
    if not 0 < len(files) <= MAX_FILES:
        raise AuditError("weight_file_count_missing_or_over_limit")
    if len({path for _, path in files}) != len(files):
        raise AuditError("duplicate_shard_path")
    return files, weight_map


def tensor_summary(header, filename):
    return [{"key": clipped(key), "file": clipped(filename), "dtype": meta["dtype"], "shape": meta["shape"]}
            for key, meta in header.items()
            if any(part in key for part in ("function_head", "lm_head", "embed_tokens"))]


def audit_weights(llm, index):
    files, weight_map = select_weights(llm, index)
    seen, relevant, unknown_dtypes = {}, [], set()
    file_rows = []
    for name, path in files:
        header, unsupported = read_header(path)
        unknown_dtypes.update(unsupported)
        file_rows.append({"file": clipped(name), "tensor_count": len(header)})
        if len(seen) + len(header) > MAX_TENSORS:
            raise AuditError("total_tensor_count_limit")
        for key in header:
            if key in seen:
                raise AuditError("duplicate_tensor_across_shards")
            seen[key] = name
        relevant.extend(tensor_summary(header, name))
    if weight_map is not None and seen != weight_map:
        raise AuditError("index_header_mapping_mismatch")
    head_count = sum("function_head" in name for name in seen)
    return check("unknown" if unknown_dtypes else "verified", "headers_and_index_only",
                 indexed=weight_map is not None, file_count=len(files), tensor_count=len(seen),
                 files=file_rows[:MAX_ITEMS], function_head_key_count=head_count,
                 tensors=relevant[:MAX_ITEMS], omitted_tensor_summaries=max(0, len(relevant) - MAX_ITEMS),
                 unsupported_dtypes=sorted(unknown_dtypes)[:MAX_ITEMS])


def audit_embeddings(model):
    header, unknown_dtypes = read_header(inside(model, model / "embeddings.safetensors"))
    if "embed_tokens.weight" not in header:
        raise AuditError("backend_embed_tokens_weight_missing")
    return check("unknown" if unknown_dtypes else "verified", "backend_embedding_header_only",
                 tensors=tensor_summary(header, "embeddings.safetensors")[:MAX_ITEMS],
                 unsupported_dtypes=sorted(unknown_dtypes)[:MAX_ITEMS])


@contextlib.contextmanager
def offline_tokenizer_environment():
    values = {key: os.environ.get(key) for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")}
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        os.environ.update({key: "1" for key in values})
        yield
    finally:
        sys.dont_write_bytecode = previous
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def audit_tokenizer(model, loader=None):
    path = inside(model, model / "tokenizer")
    if not path.is_dir():
        raise AuditError("bundled_tokenizer_missing")
    with offline_tokenizer_environment(), contextlib.redirect_stdout(sys.stderr):
        if loader is None:
            try:
                from transformers import AutoTokenizer
            except ImportError:
                return check("unknown", "optional_transformers_unavailable")
            loader = AutoTokenizer.from_pretrained
        try:
            tok = loader(str(path), local_files_only=True, use_fast=True, trust_remote_code=False)
        except Exception as exc:
            return check("unknown", "offline_tokenizer_load_error", error_type=type(exc).__name__)
        vocab = tok.get_vocab()
        if (not isinstance(vocab, dict) or not vocab
                or any(not isinstance(k, str) or type(v) is not int or v < 0 for k, v in vocab.items())):
            raise AuditError("invalid_tokenizer_vocabulary")
        entries = []
        for token in TOKENS:
            ids = tok.encode(token, add_special_tokens=False)
            valid = (token in vocab and isinstance(ids, list) and len(ids) == 1
                     and type(ids[0]) is int and ids[0] == vocab[token] and ids[0] != tok.unk_token_id
                     and tok.convert_ids_to_tokens(ids[0]) == token)
            entries.append({"token": token, "single_known_token": valid,
                            "id": ids[0] if valid else None})
        valid = all(entry["single_known_token"] for entry in entries)
        valid = valid and len({entry["id"] for entry in entries}) == len(TOKENS)
        if valid:
            tok.bos_token, tok.eos_token, tok.pad_token = TOKENS[:3]
        roles = {"bos": tok.bos_token_id, "eos": tok.eos_token_id, "pad": tok.pad_token_id} if valid else {}
        valid = valid and all(type(value) is int and value == vocab[token]
                              for value, token in zip(roles.values(), TOKENS[:3], strict=True))
        return check("verified" if valid else "failed", "local_tokenizer_encoding_only",
                     tokens=entries, max_vocab_id=max(vocab.values()), tokenizer_length=len(tok),
                     tokenizer_class=clipped(type(tok).__name__), is_fast=bool(tok.is_fast),
                     role_ids=roles if valid else {})


def tokenizer_ranges(tokenizer, weights, embedding, config):
    if tokenizer["status"] != "verified":
        return check("unknown", "tokenizer_encoding_not_verified")
    hidden = config.get("hidden_size")
    candidates = []
    for section in (weights, embedding):
        for tensor in section.get("tensors", []):
            if (tensor["key"].endswith(".weight") and len(tensor["shape"]) == 2
                    and tensor["dtype"] in ("F16", "BF16", "F32", "F64")
                    and tensor["shape"][1] == hidden):
                candidates.append(tensor)
    if (weights["status"] != "verified" or embedding["status"] != "verified"
            or weights.get("omitted_tensor_summaries", 0)
            or not any("function_head" in x["key"] for x in candidates)
            or not any(x["file"] == "embeddings.safetensors" for x in candidates)):
        return check("unknown", "complete_unquantized_head_and_embedding_layout_not_established")
    within = all(tokenizer["max_vocab_id"] < tensor["shape"][0] for tensor in candidates)
    return check("verified" if within else "failed", "ids_vs_selected_full_matrix_rows_only",
                 max_vocab_id=tokenizer["max_vocab_id"],
                 rows=[{"key": x["key"], "rows": x["shape"][0]} for x in candidates[:MAX_ITEMS]])


def audit(args, tokenizer_loader=None):
    model = Path(args.model_dir).resolve()
    llm = Path(args.llm_dir).resolve() if args.llm_dir else (model / "nano-v2-vllm").resolve()
    checks = {}
    checks["config"] = capture(lambda: audit_config(llm))
    checks["vllm_package"] = capture(lambda: audit_vllm(args))
    roots = checks["vllm_package"].pop("_roots", [])
    checks["vllm_registry"] = (capture(lambda: source_metadata(Path(roots[0]) / "model_executor/models/registry.py"))
                               if len(roots) == 1 else check("unknown", "single_package_root_not_established"))
    checks["vllm_model_source"] = (capture(lambda: source_metadata(Path(args.vllm_model_source).resolve()))
                                   if args.vllm_model_source else check("unknown", "select_model_source_explicitly"))
    checks["weights"] = capture(lambda: audit_weights(llm, args.index))
    checks["backend_embeddings"] = capture(lambda: audit_embeddings(model))
    checks["function_head_presence"] = (
        check("verified", "header_names_include_function_head")
        if checks["weights"].get("function_head_key_count", 0)
        else check("unknown", "function_head_not_established_from_validated_headers"))
    checks["tokenizer"] = (capture(lambda: audit_tokenizer(model, tokenizer_loader))
                            if args.check_tokenizer else check("unknown", "optional_tokenizer_check_not_requested"))
    checks["tokenizer_matrix_ranges"] = tokenizer_ranges(
        checks["tokenizer"], checks["weights"], checks["backend_embeddings"], checks["config"])
    checks["runtime_class"] = check("unknown", "disk_registry_and_auto_map_do_not_prove_running_class")
    checks["head_loaded"] = check("unknown", "headers_do_not_prove_weight_loading_or_values")
    checks["tool_execution"] = check("unknown", "no_inference_or_real_tool_roundtrip_performed")
    return {"schema_version": 1, "scope": "disk_metadata_only", "capability_status": "unknown",
            "python": clipped(sys.executable, 300), "model_dir": clipped(model, 300), "llm_dir": clipped(llm, 300),
            "summary": {status: sum(row["status"] == status for row in checks.values())
                        for status in ("verified", "unknown", "failed")}, "checks": checks}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--model-dir", required=True, help="Explicit actual VoiceChat model version directory")
    result.add_argument("--llm-dir", help="Explicit LLM artifact directory; default: MODEL_DIR/nano-v2-vllm")
    result.add_argument("--vllm-package-dir", help="Explicit disk package root; otherwise inspect this Python")
    result.add_argument("--vllm-model-source", help="Explicit model .py selected after reading registry")
    result.add_argument("--index", help="Select one index inside LLM_DIR when multiple indices exist")
    result.add_argument("--check-tokenizer", action="store_true", help="Use existing transformers locally/offline")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        report = audit(args)
    except (OSError, ValueError, RuntimeError) as exc:
        report = {"scope": "disk_metadata_only", "capability_status": "unknown",
                  "checks": {"audit": check("failed", type(exc).__name__)}}
    output = json.dumps(report, ensure_ascii=True, allow_nan=False, indent=2)
    if len(output.encode()) > MAX_OUTPUT_BYTES:
        report = {"scope": "disk_metadata_only", "capability_status": "unknown",
                  "checks": {"audit": check("failed", "report_output_limit")}}
        output = json.dumps(report)
    print(output)
    return int(any(row["status"] == "failed" for row in report["checks"].values()))


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
