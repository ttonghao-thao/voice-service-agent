import hashlib
import importlib.util
import json
import struct
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts/analyze_voicechat_capture.py"
SPEC = importlib.util.spec_from_file_location("voicechat_capture_analysis", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def make_capture(tmp_path, *, mismatch=False, rnnt_match=True, bias=False, tool=False):
    root = tmp_path / "sequence-42"
    root.mkdir()
    pcm = b"".join(struct.pack("<f", -0.5 + index / 2559) for index in range(2560))
    resampled = b"".join(struct.pack("<f", struct.unpack("<f", pcm[index:index + 4])[0] + 0.00001)
                         for index in range(0, len(pcm), 4))
    quantized = b"".join(
        struct.pack("<f", (((int(value * 32768.0) + 32768) % 65536) - 32768) / 32768.0)
        for (value,) in struct.iter_unpack("<f", resampled)
    )
    (root / "soxr-output-16k.f32le").write_bytes(resampled)
    (root / "triton-submitted-16k.f32le").write_bytes(quantized)
    (root / "triton-received-16k.f32le").write_bytes(
        quantized[:-4] + (b"\0\0\0\0" if mismatch else quantized[-4:])
    )
    summary = {
        "sequence_id": 42,
        "files": {"triton-submitted-16k": {"samples": 2560}},
    }
    (root / "ws-summary.json").write_text(json.dumps(summary))
    events = [
        {"sequence_id": 42, "event": "encoder.window", "step": 0,
         "encoded_shape": [1, 2, 4], "asr_shape": [1, 2, 3]},
        {"sequence_id": 42, "event": "tensor", "name": "encoded-selected", "step": 0,
         "frame_index": 0, "shape": [1, 1, 4], "sha256": "a", "l2": 1.0},
        {"sequence_id": 42, "event": "tensor", "name": "asr-selected", "step": 0,
         "frame_index": 0, "shape": [1, 1, 3], "sha256": "b", "l2": 1.0},
        {"sequence_id": 42, "event": "tensor", "name": "combined-embeds", "step": 0,
         "frame_index": 0, "shape": [1, 1, 4], "sha256": "c", "l2": 1.0},
        {"sequence_id": 42, "event": "encoder.window", "step": 1,
         "encoded_shape": [1, 2, 4], "asr_shape": [1, 2, 3]},
        {"sequence_id": 42, "event": "tensor", "name": "encoded-selected", "step": 1,
         "frame_index": 1, "shape": [1, 1, 4], "sha256": "z", "l2": 2.0},
        {"sequence_id": 42, "event": "tensor", "name": "asr-selected", "step": 1,
         "frame_index": 1, "shape": [1, 1, 3], "sha256": "d", "l2": 1.0},
        {"sequence_id": 42, "event": "tensor", "name": "combined-embeds", "step": 1,
         "frame_index": 1, "shape": [1, 1, 4], "sha256": "e", "l2": 2.0},
        {"sequence_id": 42, "event": "rnnt.compare", "step": 0, "frame_index": 0,
         "manual_tokens": [1], "reference_tokens": [1 if rnnt_match else 2],
         "matches": rnnt_match, "punctuation_bias_enabled": bias},
        {"sequence_id": 42, "event": "llm.output", "step": 0,
         "custom_output_keys": ["function_tokens", "text_logits"],
         "function_kind": "sotc" if tool else "pad", "function_scores_available": False},
        {"sequence_id": 42, "event": "llm.output", "step": 1,
         "custom_output_keys": ["function_tokens", "text_logits"],
         "function_kind": "eotc" if tool else "pad", "function_scores_available": False},
    ]
    (root / "model-events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))
    return root


def test_complete_capture_routes_all_pad_to_runtime_head_or_template(tmp_path):
    report = MODULE.analyze(make_capture(tmp_path))
    assert report["summary"] == {"verified": 6, "unknown": 0, "failed": 0}
    assert report["checks"]["transport"]["reason"] == "resampled_quantization_and_triton_pcm_exact_match"
    assert report["checks"]["rnnt_reference"]["reason"] == "manual_and_reference_greedy_match"
    assert report["checks"]["function_runtime"]["counts"]["pad"] == 2
    assert report["next_action"].startswith("M27:")


def test_transport_mismatch_is_the_first_repair_branch(tmp_path):
    report = MODULE.analyze(make_capture(tmp_path, mismatch=True, rnnt_match=False))
    assert report["checks"]["transport"]["status"] == "failed"
    assert report["checks"]["rnnt_reference"]["status"] == "failed"
    assert report["next_action"].startswith("R1:")


def test_punctuation_bias_makes_greedy_equivalence_inconclusive(tmp_path):
    report = MODULE.analyze(make_capture(tmp_path, bias=True))
    assert report["checks"]["rnnt_reference"] == {
        "status": "unknown",
        "reason": "punctuation_bias_must_be_disabled_for_equivalence",
        "comparisons": 1,
    }
    assert report["next_action"].startswith("M25:")


def test_native_tool_boundaries_advance_to_real_acceptance(tmp_path):
    report = MODULE.analyze(make_capture(tmp_path, tool=True))
    assert report["checks"]["function_runtime"]["reason"] == "native_tool_boundary_observed"
    assert report["next_action"].startswith("M28:")


def test_event_input_limits_are_enforced(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text("{}\n" * (MODULE.MAX_EVENTS + 1))
    try:
        MODULE.read_events(path)
    except ValueError as exc:
        assert "too many events" in str(exc)
    else:
        raise AssertionError("expected bounded event input failure")


def test_incremental_patch_manifest_is_chained_to_d21_and_capture_stays_opt_in():
    root = Path(__file__).parents[2]
    patch = root / "deploy/voicechat/isolated-diagnostics.patch"
    manifest = json.loads((root / "deploy/voicechat/isolated-diagnostics-source-manifest.json").read_text())
    prior = json.loads((root / "deploy/voicechat/diagnostics-source-manifest.json").read_text())
    assert manifest["patch_sha256"] == hashlib.sha256(patch.read_bytes()).hexdigest()
    for path, entry in manifest["files"].items():
        assert entry["before_sha256"] == prior[path]["after_sha256"]
        assert entry["after_sha256"] != entry["before_sha256"]
    text = patch.read_text()
    assert "GreedyRNNTInfer" in text and "partial_hypotheses" in text
    assert "VOICECHAT_CAPTURE_SEQUENCE_ID" in text and "VOICECHAT_CAPTURE_DIR" in text
    dockerfile = (root / "deploy/voicechat/Dockerfile").read_text()
    assert "VOICECHAT_CAPTURE_DIR" not in dockerfile and "VOICECHAT_CAPTURE_SEQUENCE_ID" not in dockerfile
