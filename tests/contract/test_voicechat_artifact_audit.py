"""Metadata-only fixtures: no model engine, weight loading, network, or tokenizer dependency."""

import importlib.util
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "voicechat_artifact_audit", Path(__file__).parents[2] / "scripts/audit_voicechat_artifacts.py"
)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def safetensors(path, shapes, dtype="BF16", metadata=None):
    offset = 0
    header = {}
    if metadata is not None:
        header["__metadata__"] = metadata
    for key, shape in shapes.items():
        length = audit.math.prod(shape) * audit.DTYPE_BYTES.get(dtype, 2)
        header[key] = {"shape": shape, "dtype": dtype, "data_offsets": [offset, offset + length]}
        offset += length
    raw = json.dumps(header).encode()
    with path.open("wb") as stream:
        stream.write(struct.pack("<Q", len(raw)))
        stream.write(raw)
        # Sparse payload length only: fixtures contain no real weights.
        stream.truncate(8 + len(raw) + offset)
    return header, len(raw)


@pytest.fixture
def artifact(tmp_path):
    model = tmp_path / "model"
    llm = model / "nano-v2-vllm"
    llm.mkdir(parents=True)
    config = {"architectures": ["NemotronHForCausalLM"], "hidden_size": 4, "vocab_size": 8,
              "custom_outputs": ["text_logits", "function_tokens"],
              "custom_input_specs": [{"name": "combined_embeds", "dim": 4, "dtype": "bfloat16"}],
              "pad_token_id": 0, "auto_map": {"AutoModel": "unused.RemoteModel"}}
    (llm / "config.json").write_text(json.dumps(config))
    safetensors(llm / "model.safetensors", {"stt_model.function_head.weight": [8, 4],
                                          "stt_model.lm_head.weight": [8, 4]}, metadata={"secret": "DO_NOT_EMIT"})
    safetensors(model / "embeddings.safetensors", {"embed_tokens.weight": [8, 4]})
    (model / "tokenizer").mkdir()
    package = tmp_path / "vllm"
    models = package / "model_executor/models"
    models.mkdir(parents=True)
    (package / "__init__.py").write_text("raise AssertionError('must never import vllm')\n")
    (models / "registry.py").write_text(
        'MODELS = {"NemotronHForCausalLM": ("nemotron_h", "NemotronHForCausalLM")}\n'
    )
    source = models / "nemotron_h.py"
    source.write_text("raise AssertionError('must never import model')\n"
                      "class NemotronHForCausalLM:\n"
                      "    def load_weights(self):\n"
                      "        return self.function_head.argmax()\n")
    args = SimpleNamespace(model_dir=str(model), llm_dir=None, index=None,
                           vllm_package_dir=str(package), vllm_model_source=str(source), check_tokenizer=False)
    return args, model, llm


class LocalTokenizer:
    unk_token_id = None
    is_fast = True

    def __init__(self, **overrides):
        self.vocab = {token: i + 1 for i, token in enumerate(audit.TOKENS)} | overrides
        self.bos_token, self.eos_token, self.pad_token = audit.TOKENS[:3]

    def get_vocab(self):
        return self.vocab

    def encode(self, token, add_special_tokens):
        assert add_special_tokens is False
        return [self.vocab[token]] if token in self.vocab else [0]

    def convert_ids_to_tokens(self, token_id):
        return next(key for key, value in self.vocab.items() if value == token_id)

    def __len__(self):
        return len(self.vocab)

    @property
    def bos_token_id(self):
        return self.vocab[self.bos_token]

    @property
    def eos_token_id(self):
        return self.vocab[self.eos_token]

    @property
    def pad_token_id(self):
        return self.vocab[self.pad_token]


def test_valid_metadata_does_not_claim_running_capability_or_emit_content(artifact):
    args, _, _ = artifact
    report = audit.audit(args)
    checks = report["checks"]
    assert checks["config"]["status"] == checks["weights"]["status"] == "verified"
    assert checks["function_head_presence"]["status"] == "verified"
    assert checks["vllm_registry"]["registry_entries"][0]["literal_mapping"] == [
        "nemotron_h", "NemotronHForCausalLM"]
    assert checks["vllm_model_source"]["markers"]["function_head"] == [4]
    assert all(checks[key]["status"] == "unknown" for key in ("runtime_class", "head_loaded", "tool_execution"))
    assert report["capability_status"] == "unknown"
    output = json.dumps(report)
    assert "DO_NOT_EMIT" not in output and "must never import" not in output
    assert len(output.encode()) < audit.MAX_OUTPUT_BYTES


def test_reads_only_prefix_and_header_not_tensor_payload(artifact, monkeypatch):
    _, _, llm = artifact
    path = llm / "model.safetensors"
    with path.open("rb") as stream:
        raw_header_size = struct.unpack("<Q", stream.read(8))[0]
    original = Path.open
    reads = []

    class HeaderReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, count):
            reads.append(count)
            assert self.stream.tell() + count <= 8 + raw_header_size
            return self.stream.read(count)

    monkeypatch.setattr(Path, "open", lambda path, mode: HeaderReader(original(path, mode)))
    audit.read_header(path)
    assert reads == [8, raw_header_size]


def test_multiple_indices_require_explicit_selection_and_ignore_unreferenced_files(artifact):
    args, _, llm = artifact
    header, _ = audit.read_header(llm / "model.safetensors")
    index = {"weight_map": dict.fromkeys(header, "model.safetensors")}
    for name in ("a.safetensors.index.json", "b.safetensors.index.json"):
        (llm / name).write_text(json.dumps(index))
    (llm / "unused.safetensors").write_bytes(b"not read")
    assert audit.audit(args)["checks"]["weights"]["reason"] == "multiple_indices_select_with_index"
    args.index = "a.safetensors.index.json"
    assert audit.audit(args)["checks"]["weights"]["status"] == "verified"


@pytest.mark.parametrize("shard", ["../outside.safetensors", "/outside.safetensors",
                                  "subdir/../model.safetensors"])
def test_index_rejects_escaping_shard_paths(artifact, shard):
    args, _, llm = artifact
    (llm / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {"w": shard}}))
    assert audit.audit(args)["checks"]["weights"]["status"] == "failed"


def test_symlink_shard_escape_is_rejected(artifact, tmp_path):
    args, _, llm = artifact
    outside = tmp_path / "outside.safetensors"
    safetensors(outside, {"w": [1]})
    (llm / "model.safetensors").unlink()
    (llm / "model.safetensors").symlink_to(outside)
    assert audit.audit(args)["checks"]["weights"]["reason"] == "artifact_path_outside_directory"


def test_duplicate_json_and_duplicate_tensors_are_not_verified(artifact):
    args, _, llm = artifact
    (llm / "model.safetensors.index.json").write_text(
        '{"weight_map":{"w":"model.safetensors","w":"model.safetensors"}}')
    assert audit.audit(args)["checks"]["weights"]["reason"] == "duplicate_json_key"
    (llm / "model.safetensors.index.json").unlink()
    safetensors(llm / "duplicate.safetensors", {"stt_model.function_head.weight": [8, 4]})
    assert audit.audit(args)["checks"]["weights"]["reason"] == "duplicate_tensor_across_shards"


def test_index_missing_tensor_mapping_is_failed(artifact):
    args, _, llm = artifact
    (llm / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"different": "model.safetensors"}}))
    assert audit.audit(args)["checks"]["weights"]["reason"] == "index_header_mapping_mismatch"


@pytest.mark.parametrize("raw", [b"short", struct.pack("<Q", audit.MAX_HEADER_BYTES + 1),
                                 struct.pack("<Q", 20) + b"{}"])
def test_truncated_and_oversized_headers_are_failed(artifact, raw):
    args, _, llm = artifact
    (llm / "model.safetensors").write_bytes(raw)
    assert audit.audit(args)["checks"]["weights"]["status"] == "failed"


@pytest.mark.parametrize("meta", [
    {"shape": [1], "dtype": "BF16", "data_offsets": [0, 1]},
    {"shape": [-1], "dtype": "BF16", "data_offsets": [0, 2]},
    {"shape": [True], "dtype": "BF16", "data_offsets": [0, 2]},
    {"shape": [1], "dtype": "BF16", "data_offsets": [1, 3]},
])
def test_invalid_shape_offsets_and_payload_bounds_are_failed(artifact, meta):
    args, _, llm = artifact
    raw = json.dumps({"w": meta}).encode()
    (llm / "model.safetensors").write_bytes(struct.pack("<Q", len(raw)) + raw + b"\0\0")
    assert audit.audit(args)["checks"]["weights"]["status"] == "failed"


def test_missing_head_and_unknown_dtype_remain_unknown(artifact):
    args, _, llm = artifact
    safetensors(llm / "model.safetensors", {"stt_model.lm_head.weight": [8, 4]}, dtype="PACKED_FUTURE")
    report = audit.audit(args)["checks"]
    assert report["weights"]["status"] == "unknown"
    assert report["function_head_presence"]["status"] == "unknown"


def test_missing_files_and_syntax_error_are_bounded_failures(artifact):
    args, model, llm = artifact
    (llm / "config.json").unlink()
    (model / "embeddings.safetensors").unlink()
    Path(args.vllm_model_source).write_text("invalid syntax here !!!")
    checks = audit.audit(args)["checks"]
    assert all(checks[name]["status"] == "failed" for name in ("config", "backend_embeddings", "vllm_model_source"))


def test_source_comments_are_not_implementation_evidence(artifact):
    args, _, _ = artifact
    Path(args.vllm_model_source).write_text("# function_head argmax load_weights\nclass OnlyComment: pass\n")
    assert audit.audit(args)["checks"]["vllm_model_source"]["markers"] == {}


def test_top_level_vllm_lookup_does_not_import_nested_model(artifact, monkeypatch):
    args, _, _ = artifact
    args.vllm_package_dir = None
    calls = []

    def find_spec(name):
        calls.append(name)
        assert name == "vllm"
        return None

    monkeypatch.setattr(audit.importlib.util, "find_spec", find_spec)
    assert audit.audit(args)["checks"]["vllm_package"]["status"] == "unknown"
    assert calls == ["vllm"]


def test_offline_tokenizer_ids_and_ranges_allow_config_pad_difference(artifact):
    args, _, _ = artifact
    args.check_tokenizer = True

    def loader(path, **kwargs):
        assert kwargs == {"local_files_only": True, "use_fast": True, "trust_remote_code": False}
        assert audit.os.environ["HF_HUB_OFFLINE"] == audit.os.environ["TRANSFORMERS_OFFLINE"] == "1"
        assert path.endswith("/tokenizer")
        return LocalTokenizer()

    report = audit.audit(args, tokenizer_loader=loader)["checks"]
    assert report["tokenizer"]["status"] == report["tokenizer_matrix_ranges"]["status"] == "verified"
    assert report["config"]["token_ids"]["pad_token_id"] == 0
    assert report["tokenizer"]["role_ids"]["pad"] == 3


def test_tokenizer_out_of_matrix_range_is_failed(artifact):
    args, _, _ = artifact
    args.check_tokenizer = True
    report = audit.audit(args, tokenizer_loader=lambda *a, **k: LocalTokenizer(extra=9))["checks"]
    assert report["tokenizer_matrix_ranges"]["status"] == "failed"


def test_multi_token_special_marker_is_failed(artifact):
    args, _, _ = artifact
    args.check_tokenizer = True
    tok = LocalTokenizer()
    tok.encode = lambda *a, **k: [1, 2]
    assert audit.audit(args, tokenizer_loader=lambda *a, **k: tok)["checks"]["tokenizer"]["status"] == "failed"


def test_tokenizer_loading_error_remains_unknown_and_restores_offline_environment(artifact, monkeypatch):
    args, _, _ = artifact
    args.check_tokenizer = True
    monkeypatch.setenv("HF_HUB_OFFLINE", "original")

    def loader(*args, **kwargs):
        raise RuntimeError("private path DO_NOT_EMIT")

    report = audit.audit(args, tokenizer_loader=loader)
    assert report["checks"]["tokenizer"]["status"] == "unknown"
    assert audit.os.environ["HF_HUB_OFFLINE"] == "original"
    assert "DO_NOT_EMIT" not in json.dumps(report)


def test_cli_emits_json_and_does_not_treat_unknown_capability_as_failure(artifact, capsys):
    args, _, _ = artifact
    assert audit.main(["--model-dir", args.model_dir, "--vllm-package-dir", args.vllm_package_dir,
                       "--vllm-model-source", args.vllm_model_source]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["capability_status"] == "unknown" and report["summary"]["unknown"] > 0


def test_missing_referenced_shard_and_external_index_are_failed(artifact, tmp_path):
    args, _, llm = artifact
    index = llm / "model.safetensors.index.json"
    index.write_text(json.dumps({"weight_map": {"w": "missing.safetensors"}}))
    assert audit.audit(args)["checks"]["weights"]["status"] == "failed"
    args.index = str(tmp_path / "external.safetensors.index.json")
    assert audit.audit(args)["checks"]["weights"]["reason"] == "artifact_path_outside_directory"


def test_duplicate_header_keys_and_invalid_metadata_are_failed(artifact):
    args, _, llm = artifact
    path = llm / "model.safetensors"
    raw = b'{"w":{},"w":{}}'
    path.write_bytes(struct.pack("<Q", len(raw)) + raw)
    assert audit.audit(args)["checks"]["weights"]["reason"] == "duplicate_json_key"
    safetensors(path, {"w": [1]}, metadata="not a metadata object")
    assert audit.audit(args)["checks"]["weights"]["reason"] == "safetensors_invalid_metadata"


def test_nonstandard_head_layout_keeps_matrix_comparison_unknown(artifact):
    args, _, llm = artifact
    args.check_tokenizer = True
    safetensors(llm / "model.safetensors", {"stt_model.function_head.weight": [4, 8]})
    checks = audit.audit(args, tokenizer_loader=lambda *a, **k: LocalTokenizer())["checks"]
    assert checks["function_head_presence"]["status"] == "verified"
    assert checks["tokenizer_matrix_ranges"]["status"] == "unknown"


def test_missing_explicit_package_and_source_size_limit_are_failed(artifact, monkeypatch):
    args, _, _ = artifact
    args.vllm_package_dir = str(Path(args.vllm_package_dir) / "does-not-exist")
    monkeypatch.setattr(audit, "MAX_SOURCE_BYTES", 8)
    checks = audit.audit(args)["checks"]
    assert checks["vllm_package"]["reason"] == "vllm_package_directory_missing"
    assert checks["vllm_model_source"]["reason"] == "source_size_limit"


def test_output_budget_fails_explicitly_instead_of_printing_oversized_report(artifact, monkeypatch, capsys):
    args, _, _ = artifact
    monkeypatch.setattr(audit, "MAX_OUTPUT_BYTES", 256)
    assert audit.main(["--model-dir", args.model_dir, "--vllm-package-dir", args.vllm_package_dir]) == 1
    output = capsys.readouterr().out
    assert len(output.encode()) <= 256
    assert json.loads(output)["checks"]["audit"]["reason"] == "report_output_limit"


def test_weight_file_budget_is_bounded(artifact, monkeypatch):
    args, _, llm = artifact
    monkeypatch.setattr(audit, "MAX_FILES", 1)
    safetensors(llm / "extra.safetensors", {"other": [1]})
    assert audit.audit(args)["checks"]["weights"]["reason"] == "weight_file_count_missing_or_over_limit"
