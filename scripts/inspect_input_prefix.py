"""Locate missing prefixes using bounded fingerprints; never call a model.

Matched hashes prove only their named boundary, not resampling quality, encoder
frame selection, Nano comprehension, or physical playback.
"""

import hashlib
import json
import re
import wave
from pathlib import Path

from scripts.summarize_voice_timeline import summarize

PREFIX_SECONDS = 4
CASES = ("greeting", "general_tech")


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def words(text):
    return re.findall(r"[a-z0-9]+", text.casefold())


def comparison(left, right):
    if left is None or right is None:
        return "unverified"
    return "passed" if left == right else "failed"


def inspect(text, conversation_id, case_id, transcripts, fixture_dir, records_truncated=False):
    if case_id not in CASES:
        raise ValueError("Input prefix inspection only supports greeting and general_tech")
    fixtures = Path(fixture_dir)
    case = next(row for row in json.loads((fixtures / "cases.json").read_text()) if row["case_id"] == case_id)
    with wave.open(str(fixtures / case["wav"]), "rb") as source:
        rate = source.getframerate()
        if (rate, source.getnchannels(), source.getsampwidth()) != (24000, 1, 2):
            raise ValueError("The prefix fixture must be 24 kHz mono PCM16")
        expected_audio = source.readframes(rate * PREFIX_SECONDS).ljust(rate * PREFIX_SECONDS * 2, b"\0")
    header, _ = summarize(text.splitlines(), conversation_id)
    sequences = header["sequence_ids"]
    events, api = [], []
    for line in text.splitlines():
        if "voice_ws_diagnostic " in line:
            try:
                entry = json.loads(line.split("voice_ws_diagnostic ", 1)[1])
            except ValueError:
                continue
            if str(entry.get("sequence_id")) in sequences:
                events.append(entry)
        if "voice_input_transcribed " in line:
            fields = dict(re.findall(r"\b(conversation_id|input_item_id|text_chars|text_sha256)=([a-zA-Z0-9_-]+)", line))
            if fields.get("conversation_id") == conversation_id:
                api.append(fields)
    prefixes = {}
    for entry in events:
        if entry.get("event") != "input.prefix":
            continue
        stream = entry.get("stream")
        expected_rate = 24000 if stream == "decoded" else 16000
        if (entry.get("sample_rate") == expected_rate and entry.get("samples") == expected_rate * PREFIX_SECONDS
                and entry.get("format") == "pcm16_le" and re.fullmatch(r"[a-f0-9]{64}", str(entry.get("sha256")))):
            prefixes.setdefault(stream, []).append(entry.get("sha256"))

    def fingerprint(stream):
        entries = prefixes.get(stream, [])
        return entries[0] if len(entries) == 1 else None

    scoped = len(sequences) == 1 and not header["truncated"]
    audio = {
        "case": case_id, "conversation_id": conversation_id, "sequence_ids": sequences,
        "seconds": PREFIX_SECONDS,
        "source_to_decoded": comparison(hashlib.sha256(expected_audio).hexdigest(), fingerprint("decoded")),
        "resampled_to_submitted": comparison(fingerprint("resampled"), fingerprint("submitted")),
        "dropped_input_chunks": max((e.get("dropped_input_chunks", 0) for e in events), default=None),
    }
    completed = [e for e in events if e.get("event") == "asr.completed"]
    saved = {row["item_id"]: row["text"] for row in transcripts[:8]}
    server = {row.get("input_item_id"): row for row in completed}
    ids = [row.get("input_item_id") for row in api]
    complete = (scoped and not records_truncated and 0 < len(api) <= 8 and len(set(ids)) == len(ids)
                and len(server) == len(completed) == len(api) == len(saved) == len(transcripts)
                and set(ids) == set(server) == set(saved))

    def check_pairs(pairs):
        if not complete:
            return "unverified"
        values = [comparison(left, right) for left, right in pairs]
        return "failed" if "failed" in values else "unverified" if "unverified" in values else "passed"

    voicechat_to_api = check_pairs([
        (server.get(row.get("input_item_id"), {}).get("sha256"), row.get("text_sha256")) for row in api
    ])
    api_to_saved = check_pairs([
        (row.get("text_sha256"), digest(saved[row["input_item_id"]]) if row.get("input_item_id") in saved else None)
        for row in api
    ])
    merged = "".join(saved.get(item_id, "") for item_id in ids)
    source_to_final = comparison(completed[-1].get("source_sha256_total"), digest(merged)) if complete else "unverified"
    observed = " ".join(saved.get(item_id, "") for item_id in ids)
    expected = words(case["expected_transcript"])
    expected_prefix = words(case["expected_prefix"])
    actual = words(observed)
    transcript = {
        "case": case_id, "expected": case["expected_transcript"],
        "prefix": comparison(expected_prefix, actual[:len(expected_prefix)]) if complete else "unverified",
        "full_text": comparison(expected, actual) if complete else "unverified",
        "source_to_final": source_to_final, "voicechat_to_api": voicechat_to_api, "api_to_saved": api_to_saved,
        "raw_asr_chars": completed[-1].get("source_chars_total") if completed else None,
        "saved_chars": sum(len(row["text"]) for row in transcripts[:8]),
        "input_items": len(ids), "records_truncated": records_truncated,
        "texts": [{"item_id": row["item_id"], "text": row["text"][:160], "truncated": len(row["text"]) > 160}
                  for row in transcripts[:8]],
    }
    checks = (audio["source_to_decoded"], audio["resampled_to_submitted"],
              source_to_final, voicechat_to_api, api_to_saved)
    fault, focus = "PASS", "input_prefix_only_not_answer_acceptance"
    if not scoped:
        fault, focus = "SESSION_UNVERIFIED", "matching_sequence_or_log_window"
    elif audio["source_to_decoded"] == "failed":
        fault, focus = "INPUT_AUDIO_MISMATCH", "fixture_or_client_to_voicechat_decode"
    elif audio["resampled_to_submitted"] == "failed":
        fault, focus = "VOICECHAT_INPUT_QUEUE_MISMATCH", "voicechat_resampling_to_triton_submission"
    elif "unverified" in checks or not complete:
        fault, focus = "INCOMPLETE_EVIDENCE", "missing_fingerprint_or_transcript_boundary"
    elif api_to_saved == "failed":
        fault, focus = "TRANSCRIPT_STORAGE_MISMATCH", "api_to_saved_transcription"
    elif source_to_final == "failed" or voicechat_to_api == "failed":
        fault, focus = "TRANSCRIPT_HANDOFF_MISMATCH", "raw_asr_to_final_transcription"
    elif transcript["prefix"] == "failed":
        fault, focus = "UPSTREAM_ASR_PREFIX_MISMATCH", "voicechat_resampling_encoder_or_rnnt"
    elif transcript["full_text"] == "failed":
        fault, focus = "TRANSCRIPT_CONTENT_MISMATCH", "remaining_transcription_words"
    return {"audio": audio, "transcript": transcript,
            "diagnostic": {"case": case_id, "status": "passed" if fault == "PASS" else "unverified"
                           if fault in ("INCOMPLETE_EVIDENCE", "SESSION_UNVERIFIED") else "failed",
                           "fault": fault, "focus": focus}}
