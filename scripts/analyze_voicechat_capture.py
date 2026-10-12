#!/usr/bin/env python3
"""Analyze one explicitly authorized isolated VoiceChat capture without loading a model."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path

MAX_EVENT_BYTES = 32 * 1024 * 1024
MAX_EVENTS = 8192
MAX_PCM_BYTES = 64 * 1024 * 1024


def check(status: str, reason: str, **fields) -> dict:
    return {"status": status, "reason": reason, **fields}


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def read_events(path: Path) -> list[dict]:
    if path.stat().st_size > MAX_EVENT_BYTES:
        raise ValueError("model-events.jsonl exceeds the bounded analyzer input")
    events = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            if len(events) >= MAX_EVENTS:
                raise ValueError("model-events.jsonl contains too many events")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"model-events.jsonl:{line_number} is not an object")
            events.append(value)
    return events


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def compare_transport(root: Path, summary: dict) -> dict:
    resampled = root / "soxr-output-16k.f32le"
    submitted = root / "triton-submitted-16k.f32le"
    received = root / "triton-received-16k.f32le"
    if not resampled.is_file() or not submitted.is_file() or not received.is_file():
        return check("unknown", "resampled_submitted_or_received_pcm_missing")
    if any(path.stat().st_size > MAX_PCM_BYTES for path in (resampled, submitted, received)):
        return check("failed", "pcm_file_exceeds_analyzer_limit")
    if resampled.stat().st_size % 4 or submitted.stat().st_size % 4 or received.stat().st_size % 4:
        return check("failed", "pcm_file_not_float32_aligned")
    resampled_payload = resampled.read_bytes()
    submitted_payload = submitted.read_bytes()
    expected = bytearray()
    for (value,) in struct.iter_unpack("<f", resampled_payload):
        integer = int(value * 32768.0)
        integer = ((integer + 32768) % 65536) - 32768
        expected.extend(struct.pack("<f", integer / 32768.0))
    if len(expected) > len(submitted_payload):
        return check(
            "failed",
            "resampled_pcm_not_fully_submitted",
            resampled_samples=len(expected) // 4,
            submitted_samples=len(submitted_payload) // 4,
        )
    expected.extend(b"\0" * (len(submitted_payload) - len(expected)))
    if bytes(expected) != submitted_payload:
        return check(
            "failed",
            "resampled_to_submitted_pcm_mismatch",
            resampled_samples=len(resampled_payload) // 4,
            submitted_samples=len(submitted_payload) // 4,
        )
    submitted_hash = digest(submitted)
    received_hash = digest(received)
    samples = submitted.stat().st_size // 4
    declared = summary.get("files", {}).get("triton-submitted-16k", {}).get("samples")
    if declared != samples:
        return check("failed", "ws_summary_sample_count_mismatch", submitted_samples=samples, declared=declared)
    if submitted.stat().st_size != received.stat().st_size or submitted_hash != received_hash:
        return check(
            "failed",
            "triton_pcm_content_mismatch",
            submitted_samples=samples,
            received_samples=received.stat().st_size // 4,
            submitted_sha256=submitted_hash,
            received_sha256=received_hash,
        )
    return check(
        "verified",
        "resampled_quantization_and_triton_pcm_exact_match",
        resampled_samples=len(resampled_payload) // 4,
        submitted_samples=samples,
        sha256=submitted_hash,
    )


def compare_encoder_mapping(events: list[dict]) -> dict:
    windows = {int(event["step"]): event for event in events if event.get("event") == "encoder.window"}
    selected = [
        event for event in events
        if event.get("event") == "tensor" and event.get("name") in {"encoded-selected", "asr-selected"}
    ]
    if not windows or not selected:
        return check("unknown", "encoder_window_or_selected_frame_missing")
    failures = []
    for event in selected:
        step = int(event.get("step", -1))
        frame = int(event.get("frame_index", -1))
        window = windows.get(step) or max((item for key, item in windows.items() if key <= step), default=None,
                                          key=lambda item: int(item["step"]))
        if window is None:
            failures.append({"step": step, "reason": "window_missing"})
            continue
        shape_name = "encoded_shape" if event["name"] == "encoded-selected" else "asr_shape"
        shape = window.get(shape_name)
        if not isinstance(shape, list) or len(shape) != 3 or not 0 <= frame < int(shape[1]):
            failures.append({"step": step, "frame_index": frame, "shape": shape})
    if failures:
        return check("failed", "selected_frame_outside_encoder_window", failures=failures[:20])
    steps = [int(event["step"]) for event in selected if event["name"] == "asr-selected"]
    if steps != sorted(steps) or len(steps) != len(set(steps)):
        return check("failed", "asr_selected_steps_not_strictly_monotonic", steps=steps[:50])
    return check("verified", "selected_frames_in_range_and_monotonic", windows=len(windows), frames=len(selected))


def compare_rnnt(events: list[dict]) -> dict:
    errors = [event for event in events if event.get("event") == "rnnt.reference_error"]
    comparisons = [event for event in events if event.get("event") == "rnnt.compare"]
    if errors:
        return check("failed", "reference_greedy_error", error_types=sorted({e.get("error_type") for e in errors}))
    if not comparisons:
        return check("unknown", "rnnt_comparison_missing")
    if any(event.get("punctuation_bias_enabled") for event in comparisons):
        return check("unknown", "punctuation_bias_must_be_disabled_for_equivalence", comparisons=len(comparisons))
    mismatches = [
        {"step": event.get("step"), "frame_index": event.get("frame_index"),
         "manual_count": len(event.get("manual_tokens", [])),
         "reference_count": len(event.get("reference_tokens", []))}
        for event in comparisons if not event.get("matches")
    ]
    if mismatches:
        return check("failed", "manual_and_reference_greedy_diverged", mismatches=mismatches[:20])
    return check("verified", "manual_and_reference_greedy_match", comparisons=len(comparisons))


def compare_combined_embeds(events: list[dict]) -> dict:
    tensors = [
        event for event in events
        if event.get("event") == "tensor" and event.get("name") == "combined-embeds"
    ]
    audio_tensors = [
        event for event in events
        if event.get("event") == "tensor" and event.get("name") == "encoded-selected"
    ]
    if len(tensors) < 2 or len(audio_tensors) < 2:
        return check(
            "unknown",
            "fewer_than_two_encoded_or_combined_steps",
            encoded_steps=len(audio_tensors),
            combined_steps=len(tensors),
        )
    hashes = {event.get("sha256") for event in tensors}
    audio_hashes = {event.get("sha256") for event in audio_tensors}
    nonzero = sum(float(event.get("l2", 0.0)) > 0.0 for event in tensors)
    audio_nonzero = sum(float(event.get("l2", 0.0)) > 0.0 for event in audio_tensors)
    if not nonzero or not audio_nonzero:
        return check(
            "failed",
            "encoded_or_combined_embeds_are_all_zero",
            encoded_nonzero_steps=audio_nonzero,
            combined_nonzero_steps=nonzero,
        )
    if len(audio_hashes) < 2:
        return check("failed", "encoded_audio_did_not_change", steps=len(audio_tensors))
    if len(hashes) < 2:
        return check("failed", "combined_embeds_did_not_change", steps=len(tensors))
    return check(
        "verified",
        "encoded_audio_and_combined_embeds_change_across_steps",
        steps=len(tensors),
        encoded_unique_hashes=len(audio_hashes),
        combined_unique_hashes=len(hashes),
    )


def compare_function_runtime(events: list[dict]) -> dict:
    outputs = [event for event in events if event.get("event") == "llm.output"]
    if not outputs:
        return check("unknown", "llm_custom_outputs_missing")
    missing = [
        event.get("step") for event in outputs
        if not {"text_logits", "function_tokens"}.issubset(set(event.get("custom_output_keys", [])))
    ]
    if missing:
        return check("failed", "required_custom_outputs_missing", steps=missing[:20])
    counts = {kind: 0 for kind in ("pad", "sotc", "eotc", "other")}
    for event in outputs:
        kind = event.get("function_kind", "other")
        counts[kind if kind in counts else "other"] += 1
    reason = "native_tool_boundary_observed" if counts["sotc"] and counts["eotc"] else "custom_output_observed_without_tool_boundary"
    return check(
        "verified",
        reason,
        counts=counts,
        steps=len(outputs),
        score_steps=sum(bool(event.get("function_scores_available")) for event in outputs),
    )


def choose_next_action(checks: dict) -> str:
    order = (
        ("transport", "R1: repair WS/resampling/Triton transport at the first byte mismatch"),
        ("encoder_mapping", "M26: repair encoder frame selection and process each valid frame once"),
        ("rnnt_reference", "M26: repair manual RNNT predictor/blank/symbol state against NeMo greedy"),
        ("combined_embeds", "M26: repair perception to combined_embeds propagation"),
        ("function_runtime", "M27: repair missing runtime custom function outputs"),
    )
    for key, action in order:
        if checks[key]["status"] == "failed":
            return action
    if any(value["status"] == "unknown" for value in checks.values()):
        return "M25: repeat isolated capture until every prerequisite check is conclusive"
    counts = checks["function_runtime"].get("counts", {})
    if not counts.get("sotc"):
        return "M27: inspect running function head scores/loading, then run the single-variable template comparison"
    return "M28: run the real native-tool to CueKB to authorized-speech and playback acceptance matrix"


def analyze(root: Path) -> dict:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("capture path must be a directory")
    summary_path = root / "ws-summary.json"
    events_path = root / "model-events.jsonl"
    if not summary_path.is_file() or not events_path.is_file():
        raise ValueError("capture requires ws-summary.json and model-events.jsonl")
    summary = read_json(summary_path)
    events = read_events(events_path)
    sequence_id = summary.get("sequence_id")
    event_sequences = {event.get("sequence_id") for event in events}
    sequence_check = (
        check("verified", "single_sequence_correlated", sequence_id=sequence_id)
        if event_sequences == {sequence_id}
        else check("failed", "sequence_id_mismatch", ws_sequence_id=sequence_id,
                   model_sequence_ids=sorted(str(value) for value in event_sequences))
    )
    checks = {
        "sequence": sequence_check,
        "transport": compare_transport(root, summary),
        "encoder_mapping": compare_encoder_mapping(events),
        "rnnt_reference": compare_rnnt(events),
        "combined_embeds": compare_combined_embeds(events),
        "function_runtime": compare_function_runtime(events),
    }
    statuses = {name: result["status"] for name, result in checks.items()}
    return {
        "schema_version": 1,
        "scope": "offline_isolated_capture_analysis",
        "sequence_id": sequence_id,
        "checks": checks,
        "summary": {status: sum(value == status for value in statuses.values())
                    for status in ("verified", "unknown", "failed")},
        "next_action": choose_next_action(checks),
        "acceptance_boundary": "This report does not prove real CueKB, authorized speech, playback, or listening.",
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("capture_dir", type=Path, help="sequence-N directory created by isolated capture")
    result.add_argument("--output", type=Path, help="optional JSON report path")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        report = analyze(args.capture_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        report = {"schema_version": 1, "scope": "offline_isolated_capture_analysis",
                  "checks": {"input": check("failed", type(exc).__name__)}}
    payload = json.dumps(report, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    statuses = [value["status"] for value in report.get("checks", {}).values()]
    if "failed" in statuses:
        return 1
    if "unknown" in statuses:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
