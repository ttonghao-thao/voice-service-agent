import hashlib
import json
import wave
from pathlib import Path

import pytest

from scripts.inspect_input_prefix import digest, inspect

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "artifacts/native-cuekb"
CID = "11111111-1111-1111-1111-111111111111"


def window(case="greeting", texts=None):
    config = next(row for row in json.loads((FIXTURES / "cases.json").read_text()) if row["case_id"] == case)
    if texts is None:
        texts = [config["expected_transcript"]]
    with wave.open(str(FIXTURES / config["wav"]), "rb") as source:
        pcm = source.readframes(96000).ljust(192000, b"\0")
    events = [{"event": "input.prefix", "stream": stream, "sample_rate": rate,
               "samples": rate * 4, "format": "pcm16_le", "sha256": value}
              for stream, rate, value in (("decoded", 24000, hashlib.sha256(pcm).hexdigest()),
                                           ("resampled", 16000, "a" * 64), ("submitted", 16000, "a" * 64))]
    api, saved, merged = [], [], ""
    for index, text in enumerate(texts):
        item_id = f"input-{index}"
        merged += text
        api.append(f"voice_input_transcribed conversation_id={CID} input_item_id={item_id} "
                   f"text_chars={len(text)} text_sha256={digest(text)}")
        saved.append({"item_id": item_id, "text": text})
        events.append({"event": "asr.completed", "input_item_id": item_id,
                       "chars": len(text), "sha256": digest(text), "source_chars_total": len(merged),
                       "source_sha256_total": digest(merged)})
    return api, events, saved


def report(api, events, saved, case="greeting", **kwargs):
    logs = ["2026-10-22T00:00:00Z " + row for row in api]
    logs += ["2026-10-22T00:00:01Z voice_ws_diagnostic " + json.dumps({"sequence_id": 42, **event})
             for event in events]
    return inspect("\n".join(logs), CID, case, saved, FIXTURES, **kwargs)


@pytest.mark.parametrize("case", ["greeting", "general_tech"])
def test_complete_prefix_checks_only_fixed_transcription_not_business(case):
    result = report(*window(case), case=case)
    assert result["diagnostic"]["status"] == "passed"
    assert result["diagnostic"]["focus"] == "input_prefix_only_not_answer_acceptance"
    assert result["audio"]["source_to_decoded"] == "passed"


@pytest.mark.parametrize("case,text", [("greeting", "o, how are you today?"), ("general_tech", "TCP and UDP")])
def test_short_raw_asr_with_intact_boundary_hashes_points_upstream(case, text):
    result = report(*window(case, [text]), case=case)
    assert result["diagnostic"]["fault"] == "UPSTREAM_ASR_PREFIX_MISMATCH"
    assert result["transcript"]["source_to_final"] == "passed"
    assert result["transcript"]["voicechat_to_api"] == "passed"
    assert result["transcript"]["api_to_saved"] == "passed"


def test_prefix_split_into_two_input_items_is_not_reported_missing():
    result = report(*window(texts=["Hello, ", "how are you today?"]))
    assert result["diagnostic"]["status"] == "passed"
    assert result["transcript"]["input_items"] == 2


@pytest.mark.parametrize("stream,fault", [("decoded", "INPUT_AUDIO_MISMATCH"),
                                          ("submitted", "VOICECHAT_INPUT_QUEUE_MISMATCH")])
def test_audio_boundary_mismatch_is_not_misdiagnosed_as_recognizer(stream, fault):
    api, events, saved = window(texts=["o, how are you today?"])
    next(e for e in events if e.get("stream") == stream)["sha256"] = "b" * 64
    assert report(api, events, saved)["diagnostic"]["fault"] == fault


def test_asr_to_api_mismatch_is_separate_from_database_corruption():
    api, events, saved = window()
    events[-1]["sha256"] = digest("different source")
    assert report(api, events, saved)["diagnostic"]["fault"] == "TRANSCRIPT_HANDOFF_MISMATCH"
    api, events, saved = window()
    saved[0]["text"] = "stored without prefix"
    assert report(api, events, saved)["diagnostic"]["fault"] == "TRANSCRIPT_STORAGE_MISMATCH"


@pytest.mark.parametrize("change", ["missing_prefix", "missing_hash", "duplicate_stream", "unknown_sequence", "truncated"])
def test_incomplete_evidence_never_becomes_pass_or_invented_root_cause(change):
    api, events, saved = window()
    if change == "missing_prefix":
        events = [e for e in events if e.get("stream") != "submitted"]
    elif change == "missing_hash":
        api[0] = api[0].split(" text_sha256")[0]
    elif change == "duplicate_stream":
        events.append(dict(events[0]))
    elif change == "unknown_sequence":
        events[-1]["input_item_id"] = "unrelated"
    result = report(api, events, saved, records_truncated=change == "truncated")
    assert result["diagnostic"]["status"] == "unverified"


def test_unrelated_session_is_ignored_and_output_is_bounded():
    api, events, saved = window(texts=["x" * 5000])
    api.append("voice_input_transcribed conversation_id=22222222-2222-2222-2222-222222222222 "
               "input_item_id=other text_sha256=secret")
    events.append({"event": "input.prefix", "sequence_id": 99, "stream": "decoded", "sha256": "secret"})
    result = report(api, events, saved)
    assert "secret" not in json.dumps(result)
    assert len(result["transcript"]["texts"][0]["text"]) == 160
    assert result["transcript"]["texts"][0]["truncated"] is True
