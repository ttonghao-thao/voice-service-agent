"""Send authorized English WAVs through the deployed portal's real voice path.

One conversation/voice connection for sequential cases, with continuous silence
after each WAV. Tool execution stays in the application; no synthetic results.
Reports separate routing, argument, retrieval and speech observations. Actual
speech faithfulness and listening always require review.
"""

import argparse
import asyncio
import base64
import hashlib
import json
import time
import wave
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from app.contracts import now
from websockets.asyncio.client import connect

from scripts.probe_voicechat import read_wav
from scripts.verify_deployment import validate_voice_policy


def load_cases(path, case_id=None):
    path = Path(path)
    cases = json.loads(path.read_text())
    if not isinstance(cases, list) or not 1 <= len(cases) <= 20:
        raise ValueError("Provide 1-20 cases")
    seen = set()
    for case in cases:
        if not isinstance(case.get("case_id"), str) or not case["case_id"] or case["case_id"] in seen:
            raise ValueError("Case IDs must be nonempty and unique")
        seen.add(case["case_id"])
        if "expected_tool" not in case or case["expected_tool"] not in (None, "search_knowledge_base"):
            raise ValueError(
                f"Case {case['case_id']!r}: expected_tool must be null or "
                f"search_knowledge_base; got {case.get('expected_tool')!r}"
            )
        terms = case.get("query_terms", [])
        if not isinstance(terms, list) or any(not isinstance(term, str) or not term.strip() for term in terms):
            raise ValueError("query_terms must contain nonempty strings")
    if case_id is not None:
        cases = [case for case in cases if case["case_id"] == case_id]
        if not cases:
            raise ValueError(f"Unknown case_id: {case_id!r}")
    for case in cases:
        case["wav"] = str((path.parent / case["wav"]).resolve())
        frames, digest = read_wav(case["wav"])
        if len(frames) > 375:
            raise ValueError("Each case must be at most 30 seconds")
        case["frames"], case["input_sha256"] = frames, digest
    return cases


def assess(case, history, seen_inputs, output_samples):
    """Lexical argument checks do not certify semantic equivalence or audio."""
    records = history.get("records", [])
    turns = [item for item in history.get("items", []) if item.get("input_item_id") in seen_inputs]
    args = [r["payload"] for r in records if r["kind"] == "native_tool_arguments"
            and r["payload"].get("input_item_id") in seen_inputs]
    errors = [r["payload"] for r in records if r["kind"] == "native_tool_error"
              and r["payload"].get("input_item_id") in seen_inputs]
    searches = [r["payload"] for r in records if r["kind"] == "native_cuekb_result"
                and r["payload"].get("input_item_id") in seen_inputs]
    evidence_checks = [r["payload"] for r in records if r["kind"] == "native_cuekb_voice_evidence"
                       and r["payload"].get("input_item_id") in seen_inputs]
    search_states = [r["payload"] for r in records if r["kind"] == "native_search_state"
                     and r["payload"].get("input_item_id") in seen_inputs]
    limits = [r for r in records if r["kind"] == "native_search_limit"
              and r["payload"].get("input_item_id") in seen_inputs]
    search_state = search_states[-1] if search_states else {}
    actual = [t["selected_tool"] for t in turns]
    if not actual:
        actual = [entry["name"] for entry in [*args, *errors]]
    wanted = [] if case["expected_tool"] is None else [case["expected_tool"]]
    selection = "passed" if seen_inputs and set(actual) == set(wanted) else "failed" if seen_inputs else "unverified"
    queries = [entry["arguments"]["query"] for entry in args]
    terms = case.get("query_terms", [])
    argument_status = ("failed" if any(e.get("stage", "arguments") == "arguments" for e in errors) else "passed" if len(queries) == 1 and terms
        and all(term.casefold() in queries[0].casefold() for term in terms)
        else "manual_review_required" if queries else "not_applicable" if not wanted else "unverified")
    answered_turn = next((t for t in reversed(turns) if t.get("answer")), {})
    answer = answered_turn.get("answer")
    answer_check = next((r["payload"] for r in reversed(records)
                         if r["kind"] == "native_answer_check"
                         and r.get("source_id") == answered_turn.get("id")), None)
    reason = answer.get("reason_code") if answer else None
    if searches:
        source = searches[-1]
        retrieval = ("failed" if source.get("status") == "failed" else
            "needs_clarification" if source.get("status") == "needs_clarification" else
            "insufficient_evidence" if source.get("status") == "not_found" or source.get("evidence_status") in ("insufficient", "conflicting") else
            "passed" if source.get("hit_count", 0) > 0 else "insufficient_evidence")
    else:
        retrieval = ("insufficient_evidence" if reason in ("CUEKB_NO_EVIDENCE", "CUEKB_EVIDENCE_UNSAFE") else
            "needs_clarification" if reason == "CUEKB_NEEDS_CLARIFICATION" else
            "failed" if reason and reason.startswith(("CUEKB_", "TOOL_")) else
            "passed" if answer and answer.get("citations") else
            "not_applicable" if not wanted and not actual else "unverified")
    evidence_audit = evidence_checks[-1] if evidence_checks else None
    # Empty supplemental retrievals do not erase admitted, cumulative evidence.
    # Only the recorded cumulative bundle can establish this; not hit counts.
    if search_state.get("cumulative_sources", 0) > 0 and retrieval == "insufficient_evidence":
        retrieval = "passed"
    voice_evidence_status = (
        "not_applicable" if not wanted else
        "passed" if search_state.get("cumulative_sources", 0) > 0 else
        "passed" if evidence_audit and evidence_audit.get("selected_count", 0) > 0 else
        "unavailable" if reason == "CUEKB_VOICE_EVIDENCE_UNAVAILABLE" else
        "unverified"
    )
    return {"selection": selection, "actual_tools": actual, "arguments": argument_status,
            "answer_status": (answer or {}).get("status"),
            "answer_citation_count": len((answer or {}).get("citations", [])),
            "answer_check": answer_check,
            "retrieval": retrieval,
            "cuekb_evidence_status": searches[-1].get("evidence_status") if searches else None,
            "voice_evidence": voice_evidence_status,
            "retrieval_executions": search_state.get("retrieval_executions", len(searches)),
            "retrieval_limit": search_state.get("retrieval_limit"),
            "duplicate_queries": search_state.get("duplicate_queries", 0),
            "terminal_results_submitted": sum(bool(s.get("terminal_result_submitted")) for s in search_states),
            "post_terminal_calls": len(limits),
            "cumulative_sources": search_state.get("cumulative_sources", 0),
            "latest_retrieval_status": searches[-1].get("status") if searches else None,
            "voice_evidence_filter": ({key: evidence_audit.get(key) for key in (
                "candidate_count", "selected_count", "rejected_context_count",
                "rejected_truncated_context_count", "rejected_omitted_context_count",
                "accepted_truncated_source_count", "accepted_omitted_source_count",
                "rejected_non_ascii_count", "rejected_limit_count", "rejected_budget_count",
            )} if evidence_audit else None), "reason_code": reason,
            "speech_transport": "passed" if output_samples else "failed",
            "retrieval_relevance": "manual_review_required" if wanted else "not_applicable",
            "input_transcription": "manual_review_required",
            "spoken_faithfulness": "manual_review_required",
            "query_sha256": [hashlib.sha256(query.encode()).hexdigest() for query in queries],
            "source_trace_ids": sorted({c["trace_id"] for c in (answer or {}).get("citations", [])}
                | {s["trace_id"] for s in searches if s.get("trace_id")}),
            "presentation_reason": reason if reason and reason.startswith("VOICE_") else None}


async def run(args):
    if not 1 <= args.case_timeout <= 180:
        raise ValueError("case-timeout must be 1-180 seconds")
    cases = load_cases(args.cases, getattr(args, "case_id", None))
    parts = urlsplit(args.base_url)
    origin = args.origin or urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    report = {"tested_at": now().isoformat(), "mode": "deployed_portal", "synthetic_tool_results": False,
        "cases": [], "status": "partial", "limitations": [
            "WAV delivery and protocol observations do not prove input comprehension or actual playback.",
            "Query term checks are lexical; meaning, missing conditions and spoken facts need human review.",
            "This sequential probe does not certify tool-wait interruption, microphone AEC or latency targets."]}
    cid = None
    try:
        async with httpx.AsyncClient(base_url=args.base_url, timeout=10, follow_redirects=False) as http:
            response = await http.get("/api/v1/capabilities")
            response.raise_for_status()
            caps = response.json()
            validate_voice_policy(caps, cuekb_only=True)
            report["capabilities"] = {key: caps.get(key) for key in (
                "provider", "native_tools", "native_tool_revision", "qa_toolset_version", "answer_policy")}
            created = await http.post("/api/v1/conversations", json={"title": "Native CueKB voice validation"})
            created.raise_for_status()
            conversation = created.json()
            cid = conversation["id"]
            report["conversation_id"] = cid
            http.headers["Authorization"] = "Bearer " + conversation["access_token"]
            issued_response = await http.post(f"/api/v1/conversations/{cid}/voice-sessions", json={})
            issued_response.raise_for_status()
            issued = issued_response.json()
            report["wait_interaction"] = issued["wait_interaction"]
            endpoint = urlunsplit(("wss" if parts.scheme == "https" else "ws", parts.netloc, "", "", "")) + issued["ws_url"]
            try:
                async with connect(endpoint, origin=origin, max_size=131072) as ws:
                    ready = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if ready["type"] != "portal.session.ready":
                        raise ValueError("Voice session was not ready")
                    seq = 0
                    for case in cases:
                        row = {"case_id": case["case_id"], "expected_tool": case["expected_tool"],
                               "input_sha256": case["input_sha256"], "status": "partial"}
                        report["cases"].append(row)
                        started = time.monotonic()
                        seen_inputs, audio = set(), bytearray()
                        samples = 0
                        response_samples, answer_responses = {}, {}
                        latest_case_input = None
                        row["answer_complete"] = "unverified"
                        primary_sent = asyncio.Event()
                        answer_ended = False

                        async def send(frames=case["frames"], finished=primary_sent):
                            nonlocal seq
                            index = 0
                            while True:
                                frame = frames[index] if index < len(frames) else bytes(3840)
                                frame_seq = seq
                                seq += 1
                                await ws.send(json.dumps({"type": "portal.audio.append", "epoch": issued["epoch"],
                                    "seq": frame_seq, "payload": {"format": "pcm16", "sample_rate": 24000,
                                                             "audio": base64.b64encode(frame).decode()}}))
                                index += 1
                                if index == len(frames):
                                    finished.set()
                                await asyncio.sleep(0.08)

                        sender = asyncio.create_task(send())
                        try:
                            async with asyncio.timeout(args.case_timeout):
                                while True:
                                    if answer_ended and primary_sent.is_set():
                                        break
                                    try:
                                        event = json.loads(await asyncio.wait_for(ws.recv(), 0.1))
                                    except TimeoutError:
                                        continue
                                    body = event["payload"]
                                    if (event["type"] == "portal.input.state" and body.get("state") == "speaking"):
                                        # A pause may split one WAV into multiple native inputs.
                                        # An earlier answer must not finish the later question.
                                        answer_ended = False
                                        row["answer_complete"] = "unverified"
                                        latest_case_input = None
                                    elif event["type"] == "portal.transcript.done":
                                        if body["item_id"] != latest_case_input:
                                            answer_ended = False
                                            row["answer_complete"] = "unverified"
                                        latest_case_input = body["item_id"]
                                        seen_inputs.add(body["item_id"])
                                    elif event["type"] == "portal.audio.delta":
                                        raw = base64.b64decode(body["audio"], validate=True)
                                        samples += len(raw) // 2
                                        rid = body["response_id"]
                                        response_samples[rid] = response_samples.get(rid, 0) + len(raw) // 2
                                        if args.audio_dir and len(audio) + len(raw) <= 16 * 1024 * 1024:
                                            audio.extend(raw)
                                    elif event["type"] == "portal.error":
                                        row["portal_error"] = body["code"]
                                        raise ValueError("Portal voice error")
                                    elif (event["type"] == "portal.speech_text.done" and body.get("phase") == "answer"
                                          and body.get("input_item_id") in seen_inputs and body.get("text", "").strip()):
                                        answer_responses[body["response_id"]] = body["input_item_id"]
                                    elif (event["type"] == "portal.audio.done" and body.get("phase") == "answer"
                                          and latest_case_input is not None
                                          and answer_responses.get(body["response_id"]) == latest_case_input
                                          and response_samples.get(body["response_id"], 0) > 0):
                                        # Never stop on a response completed while the WAV
                                        # still contains unprocessed speech (e.g. Golden Yolk).
                                        answer_ended = primary_sent.is_set()
                                        row["answer_complete"] = "passed" if answer_ended else "unverified"
                                        row["answer_response_id"] = body["response_id"]
                                        row["answer_samples"] = response_samples[body["response_id"]]
                                        if not primary_sent.is_set():
                                            row["answer_ended_before_full_input"] = True
                            # Leave inference advancing while the canonical tool answer commits.
                            history = None
                            for _ in range(30):
                                response = await http.get(f"/api/v1/conversations/{cid}/messages")
                                response.raise_for_status()
                                history = response.json()
                                pending = [t for t in history.get("items", []) if t.get("input_item_id") in seen_inputs and not t.get("answer")]
                                if not pending:
                                    break
                                await asyncio.sleep(0.05)
                            row.update(assess(case, history, seen_inputs, samples))
                            row["status"] = "observed"
                        except Exception:
                            try:
                                response = await http.get(f"/api/v1/conversations/{cid}/messages")
                                response.raise_for_status()
                                row.update(assess(case, response.json(), seen_inputs, samples))
                            except Exception:
                                row["audit_status"] = "unavailable"
                            raise
                        finally:
                            sender.cancel()
                            await asyncio.gather(sender, return_exceptions=True)
                            row["elapsed_ms"] = round((time.monotonic() - started) * 1000)
                            row["primary_fully_sent"] = primary_sent.is_set()
                            row["input_item_ids"] = sorted(seen_inputs)
                            row["input_items_observed"] = len(seen_inputs)
                            row["output_samples"] = samples
                            row["output_sha256"] = hashlib.sha256(audio).hexdigest() if audio else None
                            if audio:
                                directory = Path(args.audio_dir)
                                await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
                                target = directory / f"case-{len(report['cases']):02d}.wav"
                                with wave.open(str(target), "wb") as wav:
                                    wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                                    wav.writeframes(audio)
                                row["output_wav"] = str(target)
                    report["status"] = "observed"
            finally:
                if cid:
                    await http.delete(f"/api/v1/conversations/{cid}")
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    output = Path(args.output)
    await asyncio.to_thread(output.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(output.write_text, json.dumps(report, indent=2) + "\n")
    return report


def summary_lines(report):
    capabilities = report.get("capabilities", {})
    yield (
        f"report_status={report.get('status')} error_type={report.get('error_type')} "
        f"cases={len(report.get('cases', []))} native_tools={capabilities.get('native_tools')}"
    )
    for case in report.get("cases", []):
        yield " ".join((
            f"case={case.get('case_id')}",
            f"status={case.get('status')}",
            f"selection={case.get('selection')}",
            f"tools={case.get('actual_tools')}",
            f"arguments={case.get('arguments')}",
            f"retrieval={case.get('retrieval')}",
            f"voice_evidence={case.get('voice_evidence')}",
            f"reason={case.get('reason_code')}",
            f"speech={case.get('speech_transport')}",
            f"elapsed_ms={case.get('elapsed_ms')}",
            f"inputs={case.get('input_items_observed')}",
            f"samples={case.get('output_samples')}",
            f"portal_error={case.get('portal_error')}",
            f"traces={len(case.get('source_trace_ids', []))}",
        ))


def passed(report):
    if report.get("status") != "observed" or not report.get("cases"):
        return False
    for row in report["cases"]:
        expected_tool = row.get("expected_tool")
        expected_retrieval = "not_applicable" if expected_tool is None else "passed"
        expected_voice_evidence = "not_applicable" if expected_tool is None else "passed"
        if (
            row.get("reason_code") is not None
            or (expected_tool and "answer_status" in row and (
                row["answer_status"] != "answered" or row.get("answer_citation_count", 0) == 0))
            or row.get("post_terminal_calls", 0) > 0
            or (row.get("retrieval_limit") is not None
                and row.get("retrieval_executions", 0) > row["retrieval_limit"])
            or row.get("answer_complete", "passed") != "passed"
            or
            row.get("selection") != "passed"
            or row.get("arguments") in ("failed", "unverified")
            or row.get("retrieval") != expected_retrieval
            or row.get("voice_evidence", expected_voice_evidence) != expected_voice_evidence
            or row.get("speech_transport") != "passed"
        ):
            return False
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Portal origin, using its trusted TLS certificate")
    parser.add_argument("--origin", help="Configured PUBLIC_ORIGIN if different from base-url")
    parser.add_argument("--cases", required=True, help="JSON list with case_id, wav, expected_tool, optional query_terms")
    parser.add_argument("--case-id", help="Run only this case, on a new conversation/connection")
    parser.add_argument("--case-timeout", type=float, default=90)
    parser.add_argument("--audio-dir", help="Optional authorized output recordings for listening")
    parser.add_argument("--output", default="artifacts/native-cuekb-validation.json")
    parser.add_argument("--print-report", action="store_true", help="Print full JSON instead of the compact summary")
    args = parser.parse_args()
    report = asyncio.run(run(args))
    if args.print_report:
        print(json.dumps(report, indent=2))
    else:
        print("\n".join(summary_lines(report)))
    raise SystemExit(0 if passed(report) else 1)
