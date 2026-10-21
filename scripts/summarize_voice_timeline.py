"""Read timestamped Docker logs; print only one conversation's transport timeline.

No model, database, or network calls. Input may concatenate API and VoiceChat logs.
Docker timestamps mark log observation, not GPU execution or physical audibility.
"""

import argparse
import json
import re
import sys

API_EVENTS = (
    "voice_input_started", "voice_input_transcribed", "voice_tool_call_received",
    "voice_bridge_started", "cuekb_response_validated", "voice_tool_result_submitted",
    "voice_bridge_finished", "voice_bridge_failed", "voice_batch_result_submitted",
    "voice_batch_call_settled", "voice_audio_first_received",
    "voice_audio_first_sent", "voice_response_completed", "voice_search_post_terminal",
)
FIELDS = (
    "conversation_id", "input_item_id", "call_id", "turn_id", "response_id", "tool",
    "batch_id", "calls", "injections", "trace_id", "retrieval_status", "hit_count", "duration_ms", "phase", "samples", "status",
)
BACKEND = {
    "response_chars=": "backend.result_received",
    "Tool response buffered during speaking_ack": "backend.buffered_ack",
    "Fast response-token injection started": "backend.inject_started",
    "process_response -> idle": "backend.returned_idle",
    "Fast response-token injection done": "backend.inject_done",
    "Response injection cancelled": "backend.inject_cancelled",
}


def summarize(lines, conversation_id):
    api, voice, backend, inputs, sequences = [], [], [], set(), set()
    for line in lines:
        timestamp = re.match(r"^(\d{4}-\d\d-\d\dT\S+Z)\s", line)
        at = timestamp[1] if timestamp else "unknown"
        event = next((name for name in API_EVENTS if name + " " in line), None)
        if event:
            fields = dict(re.findall(r"\b(" + "|".join(FIELDS) + r")=([A-Za-z0-9_.:,/-]+)", line))
            if fields.get("conversation_id") != conversation_id:
                continue
            fields.pop("conversation_id", None)
            if fields.get("input_item_id") not in (None, "None"):
                inputs.add(fields["input_item_id"])
            api.append({"at": at, "event": event, **fields})
        elif "voice_ws_diagnostic " in line:
            try:
                entry = json.loads(line.split("voice_ws_diagnostic ", 1)[1])
            except ValueError:
                continue
            voice.append((at, entry))
        elif any(marker in line for marker in (*BACKEND, "System prompt received for sequence", "Function call decoded from model for sequence")):
            backend.append((at, line))
        if len(api) + len(voice) + len(backend) > 10000:
            raise ValueError("Too many diagnostic events; narrow the time window")
    call_ids = {row["call_id"] for row in api if "call_id" in row}
    response_ids = {row["response_id"] for row in api if "response_id" in row}
    for _, entry in voice:
        matched = (entry.get("input_item_id") in inputs or entry.get("call_id") in call_ids
                   or entry.get("response_id") in response_ids)
        if matched and entry.get("sequence_id") is not None:
            sequences.add(str(entry["sequence_id"]))
    rows = list(api)
    model_calls = 0
    prompts = []
    for at, entry in voice:
        if str(entry.get("sequence_id")) not in sequences:
            continue
        if entry.get("event") in ("function.parsed", "function.call", "function.result", "function.batch_result"):
            rows.append({"at": at, "event": "voicechat." + entry["event"],
                         **{k: entry[k] for k in ("sequence_id", "input_item_id", "call_id", "native_batch_id", "native_batch_size", "output_index", "calls", "tool", "at_ms") if k in entry}})
    for at, line in backend:
        match = re.search(r"(?:for sequence |\[seq |seq_id: )(\d+)(?=[:\]\s])", line)
        if not match or match[1] not in sequences:
            continue
        sequence = match[1]
        if "System prompt received" in line:
            fields = dict(re.findall(r"\b(tool_names|chars|sha256)=([A-Za-z0-9_,.-]+)", line))
            prompts.append({"sequence_id": sequence, **fields})
        elif "Function call decoded from model" in line:
            model_calls += 1
            names = re.search(r"tool_names=([A-Za-z0-9_,.-]+)", line)
            rows.append({"at": at, "event": "model.function", "sequence_id": sequence,
                         "tools": names[1] if names else "unknown"})
        else:
            event = next((event for marker, event in BACKEND.items() if marker in line), None)
            if event:
                rows.append({"at": at, "event": event, "sequence_id": sequence})
    header = {"conversation_id": conversation_id, "input_item_ids": sorted(inputs),
              "sequence_ids": sorted(sequences), "session_match": "passed" if sequences else "unverified",
              "model_function_count": model_calls if sequences else None,
              "api_tool_calls": sum(r["event"] == "voice_tool_call_received" for r in api),
              "backend_prompts": prompts, "voicechat_diagnostic_events": len(voice),
              "backend_log_candidates": len(backend), "timestamp_basis": "docker_log_observed_utc"}
    rows.sort(key=lambda row: row["at"])
    header["truncated"] = len(rows) > 120
    return header, rows[:120]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conversation-id", required=True)
    args = parser.parse_args()
    try:
        header, rows = summarize(sys.stdin, args.conversation_id)
    except ValueError as exc:
        print("timeline=unavailable detail=" + str(exc))
        return 1
    print("timeline=" + json.dumps(header, separators=(",", ":")))
    for row in rows:
        print("stage=" + json.dumps(row, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
