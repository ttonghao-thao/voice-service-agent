"""Read-only, CPU-only inspection of supplied VoiceChat vLLM deployment files.

Reads safetensors headers, never tensor payloads or executable model code. This
can find packaging mismatches, but cannot attest the running model or its quality.
"""

import argparse
import hashlib
import json
import math
import struct
from pathlib import Path

MAX_METADATA_BYTES = 32 * 1024 * 1024
SPECIAL_TOKENS = ("<SPECIAL_20>", "<SPECIAL_21>", "<SPECIAL_22>")
PAPER_FUSION_WEIGHTS = {"user_audio": 1.0, "agent_text": 1.0, "function": 2.0}
FUSION_FIELDS = {
    "user_audio": "duplex_user_channel_weight",
    "agent_text": "duplex_text_channel_weight",
    "function": "duplex_function_channel_weight",
}


def read_json(path):
    path = Path(path)
    if path.stat().st_size > MAX_METADATA_BYTES:
        raise ValueError("Metadata file exceeds 32 MiB")
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def checkpoint_header(path):
    path = Path(path)
    with path.open("rb") as source:
        prefix = source.read(8)
        if len(prefix) != 8:
            raise ValueError("Invalid safetensors length prefix")
        size = struct.unpack("<Q", prefix)[0]
        if not 0 < size <= MAX_METADATA_BYTES or size + 8 > path.stat().st_size:
            raise ValueError("Invalid or oversized safetensors header")
        raw = source.read(size)
    header = json.loads(raw)
    if not isinstance(header, dict):
        raise ValueError("Invalid safetensors header")
    relevant = {}
    for name, metadata in header.items():
        if name == "__metadata__":
            continue
        if any(name == suffix or name.endswith("." + suffix) for suffix in (
            "function_head.weight", "lm_head.weight", "embed_tokens.weight",
        )):
            relevant[name] = {"shape": metadata["shape"], "dtype": metadata["dtype"]}
    return {"file": path.name, "file_bytes": path.stat().st_size,
        "header_sha256": hashlib.sha256(raw).hexdigest(), "header_bytes": size,
        "tensor_count": len(header) - int("__metadata__" in header), "selected_tensors": relevant}


def inspect_fusion(config_path):
    """Compare supplied config with paper §2.2; never change inference weights.

    Defaults mirror the public 097dfe9 wrapper, not an attestation of a deployed
    NIM backend. Gated/concat fusion cannot be judged by three scalar weights.
    """
    result = {"paper_weight_alignment": "unverified", "paper_weights": PAPER_FUSION_WEIGHTS.copy(),
        "weights_in_pinned_wrapper": None, "defaulted_fields": [],
        "limitations": ["Config-only comparison against arXiv:2609.21967v2 section 2.2 and Speech 097dfe9.",
            "Runtime overrides, loaded heads, other fusion inputs and actual embeddings are not verified."]}
    if config_path is None:
        result["reason"] = "VOICECHAT_CONFIG_NOT_SUPPLIED"
        return result
    config, digest = read_json(config_path)
    result["config_sha256"] = digest
    stt = config
    for key in ("model", "stt", "model"):
        stt = stt.get(key) if isinstance(stt, dict) else None
    if not isinstance(stt, dict):
        result["reason"] = "EXPECTED_MODEL_STT_MODEL"
        return result
    fields = (*FUSION_FIELDS.values(), "fuse_method", "use_function_head")
    result["declared"] = {key: stt[key] for key in fields if key in stt}
    result["defaulted_fields"] = [key for key in fields if key not in stt]
    if stt.get("fuse_method") not in (None, "add"):
        result["reason"] = "NON_ADDITIVE_FUSION_REQUIRES_RUNTIME_REVIEW"
        return result
    enabled = stt.get("use_function_head", True)
    weights = {channel: stt.get(key, 1.0) for channel, key in FUSION_FIELDS.items()}
    if type(enabled) is not bool or any(type(value) not in (int, float) or not math.isfinite(value)
                                       for value in weights.values()):
        result["reason"] = "INVALID_FUSION_VALUE"
        return result
    if not enabled:
        weights["function"] = 0.0
    result["weights_in_pinned_wrapper"] = weights
    result["paper_weight_alignment"] = "matches" if weights == PAPER_FUSION_WEIGHTS else "differs"
    return result


def inspect(config_path, tokenizer_path, checkpoints, voicechat_config_path=None):
    config, config_hash = read_json(config_path)
    tokenizer, tokenizer_hash = read_json(tokenizer_path)
    headers = [checkpoint_header(path) for path in checkpoints]
    # Added tokens must be atomic. A vocabulary entry alone does not prove that
    # the runtime tokenizer encodes the entire special string as one token.
    added = {token: [] for token in SPECIAL_TOKENS}
    for entry in tokenizer.get("added_tokens", []):
        if entry.get("content") in added:
            added[entry["content"]].append(entry.get("id"))
    ids = [values[0] for values in added.values() if len(values) == 1]
    token_ids_ok = len(ids) == 3 and all(type(value) is int and value >= 0 for value in ids) and len(set(ids)) == 3
    tensors = [(name, data) for header in headers for name, data in header["selected_tensors"].items()]
    heads = [data for name, data in tensors if name.endswith("function_head.weight")]
    hidden_size = config.get("hidden_size")
    vocab_size = config.get("vocab_size")
    head_shape_ok = bool(heads) and all(data["shape"] == [vocab_size, hidden_size] for data in heads)
    inputs = config.get("custom_input_specs", [])
    combined = [item for item in inputs if isinstance(item, dict) and item.get("name") == "combined_embeds"]
    outputs = config.get("custom_outputs", [])
    outputs_valid = isinstance(outputs, list) and all(isinstance(value, str) for value in outputs)
    output_names = set(outputs) if outputs_valid else set()
    # The deployed backend directly reads these two outputs. function_logits
    # is optional: capture uses .get() and records unavailable scores separately.
    missing_required = sorted({"text_logits", "function_tokens"} - output_names) if outputs_valid else None
    return {
        "scope": "supplied_artifacts_only",
        "config_sha256": config_hash, "tokenizer_sha256": tokenizer_hash,
        "checkpoints": headers, "special_token_added_ids": added,
        "voicechat_fusion": inspect_fusion(voicechat_config_path),
        "function_output_declarations": {"declared": outputs, "missing_required": missing_required},
        "checks": {
            "combined_embeds_contract": "passed" if len(combined) == 1 and combined[0].get("dim") == hidden_size
                and type(hidden_size) is int and hidden_size > 0 else "mismatch",
            "function_outputs_contract": "passed" if outputs_valid and not missing_required else "mismatch",
            "function_scores_declared": ("passed" if "function_logits" in output_names else "missing")
                if outputs_valid else "unverified",
            "function_head_in_supplied_headers": "present" if heads else "missing",
            "function_head_shape": "passed" if head_shape_ok else "mismatch",
            "atomic_special_token_entries": "passed" if token_ids_ok else "unverified",
            "special_ids_within_head": "passed" if token_ids_ok and head_shape_ok
                and all(value < vocab_size for value in ids) else "unverified",
        },
        "limitations": [
            "Supply every converted vLLM checkpoint shard; missing in this report only means missing in supplied files.",
            "Header hashes are not weight hashes; payload values, trained weights and loading were not verified.",
            "Added token entries do not prove the actual runtime tokenizer or its encode/decode behavior.",
            "This does not test RNNT, audio, live combined_embeds or returned function_tokens.",
            "function_logits is optional diagnostic output; its declaration does not prove runtime score availability.",
        ],
    }


def summary_lines(report):
    yield "artifact_checks=" + json.dumps(report["checks"], sort_keys=True, separators=(",", ":"))
    fusion = report["voicechat_fusion"]
    yield (
        f"fusion_status={fusion.get('status')} "
        f"paper_weight_alignment={fusion.get('paper_weight_alignment')}"
    )
    yield (
        f"checkpoint_count={len(report['checkpoints'])} "
        f"missing_required_outputs={report['function_output_declarations'].get('missing_required')}"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Converted vLLM LLM config.json")
    parser.add_argument("--tokenizer", required=True, help="The corresponding tokenizer.json")
    parser.add_argument("--checkpoint", action="append", required=True, help="Safetensors file; repeat for all shards")
    parser.add_argument("--voicechat-config", help="Original VoiceChat config.json with model.stt.model fusion settings")
    parser.add_argument("--output", default="artifacts/voicechat-artifacts.json")
    parser.add_argument("--print-report", action="store_true", help="Print full JSON instead of the compact summary")
    args = parser.parse_args()
    report = inspect(args.config, args.tokenizer, args.checkpoint, args.voicechat_config)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    if args.print_report:
        print(json.dumps(report, indent=2))
    else:
        print("\n".join(summary_lines(report)))


if __name__ == "__main__":
    main()
