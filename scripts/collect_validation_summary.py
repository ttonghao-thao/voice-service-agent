"""Read one bounded log window and saved answers; never start a model or CueKB call."""

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime

from app.config import Settings
from app.storage.models import Record, Turn
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts.inspect_input_prefix import CASES
from scripts.inspect_input_prefix import inspect as inspect_prefix
from scripts.run_native_cuekb_validation import (
    backend_injection_line,
    summarize_api_log,
    summarize_cuekb_v2_log,
)
from scripts.summarize_voice_timeline import summarize

CID = re.compile(r"\bconversation_id=([a-f0-9-]{36})(?=\s|$)")
MAX_BYTES = 8 * 1024 * 1024


def emit(kind, data):
    print(kind + "=" + json.dumps(data, ensure_ascii=True, separators=(",", ":")))


def clipped(text, limit):
    value = str(text or "")
    return {"text": value[:limit], "truncated": len(value) > limit}


def milliseconds(start, end):
    try:
        return round((datetime.fromisoformat(end.replace("Z", "+00:00"))
                      - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds() * 1000)
    except (ValueError, TypeError, AttributeError):
        return None


def log_summary(text, conversation_id):
    header, rows = summarize(text.splitlines(), conversation_id)
    emit("session", {key: header[key] for key in (
        "conversation_id", "input_item_ids", "sequence_ids", "session_match",
        "model_function_count", "api_tool_calls", "backend_prompts", "truncated",
    )})
    scoped = "\n".join(line for line in text.splitlines()
                       if (match := CID.search(line)) and match[1] == conversation_id)
    lines, _ = summarize_api_log(scoped)
    for line in lines[:3]:
        print(line[:1600] + (f" omitted_chars={len(line) - 1600}" if len(line) > 1600 else ""))
    # A missing call is normal for greeting/general QA, not a diagnostic failure.
    print(lines[3] if header["api_tool_calls"] else "api_diagnostic=observed fault=NO_TOOL_CALL_OBSERVED")
    for line in summarize_cuekb_v2_log(scoped):
        print(line)
    for sequence in header["sequence_ids"][:4]:
        print(backend_injection_line(text, sequence))
    # Correlate answer audio by response ID and phase, never use ACK as answer audio.
    inputs = list(dict.fromkeys(row["input_item_id"] for row in rows
                              if row.get("input_item_id") not in (None, "None")))
    for input_id in inputs[:4]:
        subset = [row for row in rows if row.get("input_item_id") == input_id]
        completed = [row for row in subset if row["event"] == "voice_response_completed"
                     and row.get("phase") == "answer"]
        calls = [row for row in subset if row["event"] == "voice_tool_call_received"]
        call_ids = {row.get("call_id") for row in calls}
        results = [row for row in rows if row["event"] == "voice_tool_result_submitted"
                   and row.get("call_id") in call_ids]
        # Exclude replies before a supplementary search, then measure the FIRST
        # formal reply after the final result. Never report the fifth reply's
        # first packet as the initial silent wait.
        audio_by_response = {row.get("response_id"): row["at"] for row in subset
                             if row["event"] == "voice_audio_first_sent"}
        final_replies = []
        for row in completed:
            elapsed = milliseconds(results[-1]["at"], audio_by_response.get(row.get("response_id"))) if results else 0
            if elapsed is not None and elapsed >= 0:
                final_replies.append(row)
        response_ids = {final_replies[0].get("response_id")} if final_replies else set()
        audio = [row for row in subset if row["event"] == "voice_audio_first_sent"
                 and row.get("response_id") in response_ids]
        texts = [row for row in subset if row["event"] == "voice_text_first_sent"
                 and row.get("response_id") in response_ids]
        transcribed = [row for row in subset if row["event"] == "voice_input_transcribed"]
        # Report timestamps too: intervals may include a second search/ACK, not just inference.
        stamps = {"transcribed": transcribed[0]["at"] if transcribed else None,
                  "first_call": calls[0]["at"] if calls else None,
                  "last_result": results[-1]["at"] if results else None,
                  "answer_audio": audio[0]["at"] if audio else None,
                  "answer_text": texts[0]["at"] if texts else None,
                  "answer_done": final_replies[0]["at"] if final_replies else None}
        emit("input_timing", {"input_item_id": input_id, **stamps,
                              "answer_responses": len(completed),
                              "additional_answers_after_result": max(0, len(final_replies) - 1),
                              "last_answer_done": completed[-1]["at"] if completed else None,
                              "question_to_first_answer_ms": milliseconds(stamps["transcribed"], stamps["answer_audio"]),
                              "question_to_call_ms": milliseconds(stamps["transcribed"], stamps["first_call"]),
                              "last_result_to_audio_ms": milliseconds(stamps["last_result"], stamps["answer_audio"])
                              if stamps["last_result"] and stamps["answer_audio"]
                              and stamps["last_result"] <= stamps["answer_audio"] else None,
                              "text_before_done_ms": milliseconds(stamps["answer_text"], stamps["answer_done"]),
                              "basis": "server_log_not_speaker", "incomplete": header["truncated"]})
    # Bounded injection milestones expose ACK wait separately from injection work.
    milestones = [r for r in rows if r["event"].startswith("backend.")]
    emit("backend_stages", {"rows": milestones[:18], "omitted": max(0, len(milestones) - 18)})


async def saved_answers(conversation_id, window_start, window_end):
    engine = create_async_engine(Settings().database_url)
    try:
        async with async_sessionmaker(engine)() as db:
            records = list(await db.scalars(select(Record).where(
                Record.conversation_id == conversation_id,
                Record.created_at >= window_start, Record.created_at <= window_end,
                Record.kind.in_(("user_transcript", "voicechat_transcript", "voice_session_end")),
            ).order_by(Record.created_at).limit(17)))
            turns = list(await db.scalars(select(Turn).where(
                Turn.conversation_id == conversation_id,
                Turn.created_at >= window_start, Turn.created_at <= window_end,
            ).order_by(Turn.created_at.desc()).limit(9)))
            queries = list(await db.scalars(select(Record).where(
                Record.conversation_id == conversation_id,
                Record.created_at >= window_start, Record.created_at <= window_end,
                Record.kind == "native_tool_arguments",
            ).order_by(Record.created_at).limit(9)))
            checks = list(await db.scalars(select(Record).where(
                Record.conversation_id == conversation_id,
                Record.created_at >= window_start, Record.created_at <= window_end,
                Record.kind.in_(("native_answer_check", "native_tool_batch", "native_cuekb_voice_evidence")),
            ).order_by(Record.created_at).limit(13)))
        emit("saved", {"conversation_id": conversation_id, "records_shown": min(len(records), 16),
                       "turns_shown": min(len(turns), 8), "records_truncated": len(records) > 16,
                       "turns_truncated": len(turns) > 8,
                       "queries_shown": min(len(queries), 8), "queries_truncated": len(queries) > 8})
        for record in records[:16]:
            payload = record.payload
            emit("record", {"kind": record.kind, "epoch": record.epoch,
                            "input_item_id": payload.get("input_item_id", payload.get("item_id")),
                            "response_id": payload.get("response_id"),
                            "status": payload.get("status"), **clipped(payload.get("text"), 1200)})
        for turn in reversed(turns[:8]):
            answer = turn.answer or {}
            citations = (turn.evidence or {}).get("citations", [])
            emit("turn", {"input_item_id": turn.input_item_id, "call_id": turn.native_call_id,
                          "tool": turn.selected_tool, "status": turn.status,
                          "phase": turn.execution_phase, "cancel": turn.cancellation_reason,
                          "answer_status": answer.get("status"), "reason": answer.get("reason_code"),
                          "answer_sources": len(answer.get("citations") or []),
                          "retrieved_sources": len(citations),
                          "traces": sorted({c.get("trace_id", "") for c in citations})[:4]})
        for record in queries[:8]:
            emit("query", {"input_item_id": record.payload.get("input_item_id"),
                           "call_id": record.source_id,
                           **clipped(record.payload.get("arguments", {}).get("query"), 500)})
        for record in checks[:12]:
            payload = record.payload
            if record.kind == "native_answer_check":
                failures = payload.get("condition_failures", [])
                emit("answer_check", {"input_item_id": payload.get("input_item_id"),
                                      "response_id": payload.get("response_id"),
                                      "reason": payload.get("reason_code"),
                                      "condition_failures": failures[:3],
                                      "omitted": max(0, len(failures) - 3)})
            elif record.kind == "native_tool_batch":
                emit("evidence_delivery", {key: payload.get(key) for key in (
                    "input_item_id", "new_sources", "retained_sources", "result_injections", "payload_bytes")
                    if payload.get(key) is not None})
            else:
                emit("voice_evidence", {key: payload.get(key) for key in (
                    "input_item_id", "cuekb_schema_version", "candidate_count", "selected_count", "selected_payload_bytes",
                    "required_context_chunks", "scope_counts", "rejected_unresolved_count", "rejected_heading_count",
                    "omitted_optional_context_count", "rejected_budget_count",
                    "rejected_non_ascii_count", "rejected_limit_count") if payload.get(key) is not None})
        if len(checks) > 12:
            emit("answer_checks_truncated", {"shown": 12})
        # Keep the admitted view (including conditions), not excluded surrounding context.
        # Samples are bounded diagnostics and do not certify semantic faithfulness.
        latest = next((t for t in turns if t.answer and t.evidence), None)
        items = (latest.evidence if latest else {}).get("envelope", {}).get("items", [])
        samples, remaining = [], 6000
        for item in items[:8]:
            value = {key: item[key] for key in ("citation_id", "text", "title", "context", "conditions",
                     "applicability", "context_state", "required_context_chunks")
                     if item.get(key)}
            encoded = json.dumps(value, ensure_ascii=True)
            if len(encoded) > remaining:
                break
            samples.append(value)
            remaining -= len(encoded)
        emit("admitted_evidence", {"input_item_id": latest.input_item_id if latest else None,
                                   "total": len(items), "items": samples,
                                   "omitted": len(items) - len(samples), "coverage": "manual_review_required"})
    finally:
        await engine.dispose()


async def saved_prefix(text, conversation_id, window_start, window_end, case_id):
    engine = create_async_engine(Settings().database_url)
    try:
        async with async_sessionmaker(engine)() as db:
            records = list(await db.scalars(select(Record).where(
                Record.conversation_id == conversation_id,
                Record.created_at >= window_start, Record.created_at <= window_end,
                Record.kind == "user_transcript",
            ).order_by(Record.created_at).limit(9)))
        transcripts = [{"item_id": row.payload["item_id"], "text": row.payload["text"]} for row in records[:8]]
        return inspect_prefix(text, conversation_id, case_id, transcripts, "/app/validation/native-cuekb",
                              records_truncated=len(records) > 8)
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conversation-id")
    parser.add_argument("--window-start", type=datetime.fromisoformat)
    parser.add_argument("--window-end", type=datetime.fromisoformat)
    parser.add_argument("--skip-db", action="store_true", help="Log-only offline analysis")
    parser.add_argument("--input-prefix-case", choices=CASES, help="Inspect only this fixed WAV's input boundaries")
    args = parser.parse_args()
    if not args.skip_db and (not args.window_start or not args.window_end):
        parser.error("--window-start and --window-end are required for saved answers")
    if args.input_prefix_case and args.skip_db:
        parser.error("Input prefix inspection requires saved transcripts")
    text = sys.stdin.read(MAX_BYTES + 1)
    if len(text.encode()) > MAX_BYTES:
        print("collection=failed reason=LOG_WINDOW_TOO_LARGE")
        return 1
    ids = [args.conversation_id] if args.conversation_id else sorted(set(CID.findall(text)))
    if not ids or len(ids) > 4:
        print(f"collection=unavailable reason=SESSION_COUNT conversations={len(ids)}")
        return 1
    status, diagnostic_status = 0, 0
    for conversation_id in ids:
        try:
            if args.input_prefix_case:
                report = asyncio.run(saved_prefix(text, conversation_id, args.window_start, args.window_end,
                                                  args.input_prefix_case))
                for kind in ("audio", "transcript", "diagnostic"):
                    emit("prefix_" + kind, report[kind])
                if report["diagnostic"]["status"] != "passed":
                    diagnostic_status = 1
            else:
                log_summary(text, conversation_id)
            if not args.skip_db and not args.input_prefix_case:
                asyncio.run(saved_answers(conversation_id, args.window_start, args.window_end))
        except Exception as exc:
            print(f"collection=unavailable conversation_id={conversation_id} error_type={type(exc).__name__}")
            status = 1
    print(f"collection={'complete' if status == 0 else 'incomplete'} conversations={len(ids)} business_acceptance=unassessed")
    return max(status, diagnostic_status)


if __name__ == "__main__":
    raise SystemExit(main())
