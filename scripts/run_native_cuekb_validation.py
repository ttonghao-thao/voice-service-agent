"""Run a selected or complete Native CueKB deployment check from the API image.

The command is intended to be started with one host-side ``docker exec``.  It
checks the deployed API contract and the image-bundled inputs before sending
the selected real WAV cases through the portal. Detailed JSON and reply audio stay
under /tmp; stdout is a bounded set of fields used for fault localization.
"""

import argparse
import asyncio
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
from app.config import Settings

from scripts.validate_native_cuekb import load_cases, passed, run
from scripts.verify_deployment import validate_ready, validate_voice_policy

PROMPT_RE = re.compile(
    r"Prompt prepared: tools_count=(\d+) tool_names=(\S+) "
    r"prompt_chars=(\d+) prompt_sha256=([0-9a-f]{64})"
)
BACKEND_PROMPT_RE = re.compile(
    r"System prompt received for sequence (\d+): tool_names=(\S+) "
    r"chars=(\d+) sha256=([0-9a-f]{64})"
)
MODEL_FUNCTION_RE = re.compile(
    r"Function call decoded from model for sequence (\d+): "
    r"tool_names=([A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*)"
)
API_TOOL_RECEIVED_RE = re.compile(
    r"voice_tool_call_received conversation_id=(\S+) call_id=(\S+) tool=(\S+) input_item_id=(\S+)"
)
API_BRIDGE_STARTED_RE = re.compile(r"voice_bridge_started conversation_id=(\S+) call_id=(\S+)")
API_BRIDGE_FINISHED_RE = re.compile(
    r"voice_bridge_finished conversation_id=(\S+) turn_id=(\S+) call_id=(\S+) status=(\S+)"
)
API_BRIDGE_FAILED_RE = re.compile(
    r"voice_bridge_failed conversation_id=(\S+) turn_id=(\S+) call_id=(\S+) exception_type=(\S+)"
)
API_RESULT_SUBMITTED_RE = re.compile(
    r"voice_tool_result_submitted conversation_id=(\S+) turn_id=(\S+) call_id=(\S+)"
)
API_UNBOUND_RE = re.compile(r"voice_unbound_tool_settled conversation_id=(\S+) call_id=(\S+)")
API_ACK_RESTORED_RE = re.compile(
    r"voice_tool_ack_continuation_restored conversation_id=(\S+) call_id=(\S+) response_id=(\S+)"
)
API_CUEKB_RE = re.compile(
    r"cuekb_response_validated conversation_id=(\S+) turn_id=(\S+) trace_id=(\S+) "
    r"retrieval_status=(\S+) hit_count=(\d+) duration_ms=(\d+)"
)
API_RESPONSE_COMPLETED_RE = re.compile(
    r"voice_response_completed conversation_id=(\S+) response_id=(\S+) input_item_id=(\S+) phase=(\S+) samples=(\d+)"
)
API_SEARCH_STATE_RE = re.compile(
    r"voice_search_state conversation_id=(\S+) input_item_id=(\S+) call_id=(\S+) "
    r"retrieval_executions=(\d+) retrieval_limit=(\d+) duplicate_queries=(\d+) "
    r"terminal_result_submitted=(\d+) cumulative_sources=(\d+)"
)
API_POST_TERMINAL_RE = re.compile(
    r"voice_search_post_terminal conversation_id=(\S+) input_item_id=(\S+) call_id=(\S+)"
)
VOICECHAT_LOG_MARKERS = (
    "Prompt prepared:",
    "System prompt received for sequence",
    "Function call decoded from model for sequence",
    '"event": "function.parsed"',
    '"event":"function.parsed"',
    '"event": "function.call"',
    '"event":"function.call"',
    '"event": "function.result"',
    '"event":"function.result"',
    '"event": "function.duplicate"',
    '"event":"function.duplicate"',
    '"kind": "response.output_audio.done"',
    '"kind":"response.output_audio.done"',
    "Function call response timed out",
    "response_chars=",
    "Tool response buffered during speaking_ack",
    "Fast response-token injection started",
    "process_response -> idle",
    "Fast response-token injection done",
    "Response injection cancelled",
    "Max decoder steps",
)
API_LOG_MARKERS = (
    "voice_search_state",
    "voice_search_post_terminal",
    "voice_tool_call_received",
    "voice_bridge_started",
    "voice_bridge_finished",
    "voice_bridge_failed",
    "voice_tool_result_submitted",
    "voice_unbound_tool_settled",
    "voice_tool_ack_continuation_restored",
    "cuekb_response_validated",
    "voice_response_completed",
)


def csv(values):
    return ",".join(str(value) for value in values) if values else "-"


def safe_detail(value):
    return " ".join(str(value).split())[:240]


def evidence_filter_summary(row):
    value = row.get("voice_evidence_filter")
    if not value:
        return "-"
    return ",".join((
        f"candidates:{value.get('candidate_count', 0)}",
        f"selected:{value.get('selected_count', 0)}",
        f"truncated_ok:{value.get('accepted_truncated_source_count', 0)}",
        f"truncated_rejected:{value.get('rejected_truncated_context_count', 0)}",
        f"omitted_ok:{value.get('accepted_omitted_source_count', 0)}",
        f"omitted_rejected:{value.get('rejected_omitted_context_count', 0)}",
        f"non_ascii:{value.get('rejected_non_ascii_count', 0)}",
        f"limit:{value.get('rejected_limit_count', 0)}",
        f"budget:{value.get('rejected_budget_count', 0)}",
    ))


def classify(report):
    cases = report.get("cases", [])
    if report.get("error_type") in ("ConnectError", "ConnectionError") or not cases:
        return "PORTAL_CONNECT", "api_portal_connectivity"
    if any(row.get("portal_error") == "VOICE_TOOL_UNKNOWN" for row in cases):
        return "UNKNOWN_TOOL_NAME", "api_tool_name_validation"
    if any(row.get("portal_error") == "VOICE_TOOL_LOOP" or row.get("post_terminal_calls", 0) for row in cases):
        return "TOOL_LOOP_AFTER_TERMINAL", "provider_answer_after_search_finished"
    if any(row.get("portal_error") in ("VOICE_SESSION_ROTATION_REQUIRED", "VOICE_SESSION_EXPIRED") for row in cases):
        return "VOICE_SESSION_LIMIT", "api_voice_session_lifetime"
    if any(row.get("reason_code") in (
        "VOICE_MISSING_CONDITION", "VOICE_UNSUPPORTED_NUMBER", "VOICE_UNSUPPORTED_UNIT",
        "VOICE_ANSWER_INVALID", "RAG_INVALID_CITATION", "VOICE_SPEECH_MISMATCH",
    ) for row in cases):
        return "ANSWER_CONTENT_CHECK", "delivered_evidence_and_spoken_answer"
    product = [row for row in cases if row.get("expected_tool") is not None]
    if any(
        not row.get("input_item_ids") and not row.get("input_items_observed")
        for row in product
    ):
        return "INPUT_NOT_OBSERVED", "voicechat_audio_or_asr_transport"
    if any(row.get("selection") != "passed" and not row.get("actual_tools") for row in product):
        return "TOOL_NOT_CALLED", "voicechat_prompt_or_model_function_channel"
    if any(row.get("selection") != "passed" for row in cases):
        return "WRONG_TOOL_SELECTION", "voicechat_selection"
    if any(row.get("arguments") in ("failed", "unverified") for row in product):
        return "TOOL_ARGUMENTS", "voicechat_function_arguments"
    if any(row.get("retrieval") != "passed" for row in product):
        return "CUEKB_RETRIEVAL", "api_cuekb_trace"
    if any(row.get("voice_evidence") not in (None, "passed") for row in product):
        return "CUEKB_VOICE_EVIDENCE", "api_voice_evidence_filter"
    if (
        report.get("error_type") == "TimeoutError"
        or any(row.get("answer_complete", "passed") != "passed" for row in cases)
        or any(row.get("reason_code") == "VOICE_ANSWER_TIMEOUT" for row in product)
        or any(row.get("portal_error") == "VOICE_TOOL_REQUIRED" for row in product)
        or any(
            row.get("speech_transport") != "passed" for row in cases
        )
    ):
        return "VOICE_RESPONSE_COMPLETION", "provider_response_or_api_relay"
    if any("answer_status" in row and (
        row["answer_status"] != "answered" or row.get("answer_citation_count", 0) == 0
    ) for row in product):
        return "PORTAL_ANSWER_COMMIT", "api_answer_status_and_sources"
    if passed(report):
        return "PASS", "human_transcription_and_spoken_faithfulness"
    return "INCOMPLETE", "report_json"


def case_line(row):
    return " ".join((
        f"case={row.get('case_id')}",
        f"expected={row.get('expected_tool') or '-'}",
        f"actual={csv(row.get('actual_tools', []))}",
        f"selection={row.get('selection')}",
        f"arguments={row.get('arguments')}",
        f"retrieval={row.get('retrieval')}",
        f"cuekb_evidence={row.get('cuekb_evidence_status') or '-'}",
        f"voice_evidence={row.get('voice_evidence') or '-'}",
        f"evidence_filter={evidence_filter_summary(row)}",
        f"speech={row.get('speech_transport')}",
        f"answer_complete={row.get('answer_complete', 'unverified')}",
        f"answer_status={row.get('answer_status') or '-'}",
        f"answer_sources={row.get('answer_citation_count', 0)}",
        f"retrieval_executions={row.get('retrieval_executions', 0)}",
        f"retrieval_limit={row.get('retrieval_limit') or '-'}",
        f"duplicate_queries={row.get('duplicate_queries', 0)}",
        f"terminal_results_submitted={row.get('terminal_results_submitted', 0)}",
        f"post_terminal_calls={row.get('post_terminal_calls', 0)}",
        f"cumulative_sources={row.get('cumulative_sources', 0)}",
        f"latest_retrieval={row.get('latest_retrieval_status') or '-'}",
        f"reason={row.get('reason_code') or '-'}",
        f"portal_error={row.get('portal_error') or '-'}",
        f"elapsed_ms={row.get('elapsed_ms')}",
        f"input_ids={csv(row.get('input_item_ids', []))}",
        f"trace_ids={csv(row.get('source_trace_ids', []))}",
    ))


def voicechat_diagnostics(text, sequence_id):
    entries = []
    for line in text.splitlines():
        marker = "voice_ws_diagnostic "
        if marker not in line:
            continue
        try:
            entry = json.loads(line.split(marker, 1)[1])
        except ValueError:
            continue
        if str(entry.get("sequence_id")) != str(sequence_id):
            continue
        entries.append(entry)
    return entries


def diagnostic_matches(entries, **wanted):
    return [entry for entry in entries
            if all(entry.get(key) == value for key, value in wanted.items())]


def backend_injection_line(text, sequence_id):
    # Backend messages use both '[seq N]' and 'seq_id: N'. Never mix
    # warmup/other clients into the selected prompt sequence's counts.
    pattern = re.compile(r"(?:\[seq |seq_id: )" + re.escape(str(sequence_id)) + r"(?=\]|\s|\))")
    rows = [line for line in text.splitlines() if pattern.search(line)] if sequence_id is not None else []
    markers = {
        "received": "response_chars=", "buffered_ack": "Tool response buffered during speaking_ack",
        "started": "Fast response-token injection started", "returned_idle": "process_response -> idle",
        "done_idle": "Fast response-token injection done, state: idle", "cancelled": "Response injection cancelled",
        "decoder_limit": "Max decoder steps",
    }
    counts = {key: sum(marker in line for line in rows) for key, marker in markers.items()}
    sizes = [tuple(map(int, match.groups())) for line in rows
             if (match := re.search(r"response_chars=(\d+).*Token length:(\d+)", line))]
    status = ("unverified" if not any(counts.values()) else
              "complete" if counts["received"] > 0
              and counts["received"] == counts["returned_idle"] == counts["done_idle"]
              and not counts["cancelled"] and not counts["decoder_limit"] else "incomplete")
    chars = f"{min(s[0] for s in sizes)}:{max(s[0] for s in sizes)}" if sizes else "-"
    tokens = f"{min(s[1] for s in sizes)}:{max(s[1] for s in sizes)}" if sizes else "-"
    return (f"backend_injection sequence={sequence_id or '-'} status={status} "
            + " ".join(f"{key}={value}" for key, value in counts.items())
            + f" chars_min_max={chars} tokens_min_max={tokens}")


def summarize_voicechat_log(text, expected_tool):
    prompts = PROMPT_RE.findall(text)
    backend = BACKEND_PROMPT_RE.findall(text)
    decoded_all = MODEL_FUNCTION_RE.findall(text)
    prompt = prompts[-1] if prompts else None
    received = backend[-1] if backend else None
    decoded = ([row for row in decoded_all if row[0] == received[0]]
               if received else decoded_all)
    prompt_match = (
        "passed" if prompt and received and prompt[0] == "1"
        and prompt[1] == expected_tool and prompt[1:] == received[1:]
        else "failed" if prompt and received else "unverified"
    )
    decoded_names = [name for _, name in decoded]
    decoded_sequences = [sequence for sequence, _ in decoded]
    sequence_id = received[0] if received else None
    diagnostics = voicechat_diagnostics(text, sequence_id)
    parsed = diagnostic_matches(diagnostics, event="function.parsed")
    parsed_count = sum(entry.get("calls", 1) for entry in parsed)
    new_calls = diagnostic_matches(diagnostics, event="function.call", action="queued")
    new_call_count = len(new_calls)
    new_call_input_count = len({entry.get("input_item_id") for entry in new_calls
                                if entry.get("input_item_id")})
    result_count = len(diagnostic_matches(diagnostics, event="function.result"))
    coalesced_count = len(diagnostic_matches(
        diagnostics, event="function.duplicate", action="coalesced"
    ))
    replayed_count = len(diagnostic_matches(
        diagnostics, event="function.duplicate", action="replayed_result"
    ))
    replay_limit_count = len(diagnostic_matches(
        diagnostics, event="function.duplicate", action="replay_limit"
    ))
    suppressed_ack_count = len(diagnostic_matches(
        diagnostics, event="function.duplicate", action="ack_suppressed"
    ))
    duplicate_count = coalesced_count + replayed_count
    audio_done_count = len(diagnostic_matches(
        diagnostics, event="output.sent", kind="response.output_audio.done"
    ))
    model_function = (
        "passed" if expected_tool in decoded_names
        else "wrong_name" if decoded_names else "absent"
    )
    lines = [
        (
            f"voicechat_prompt count={prompt[0]} tools={prompt[1]} chars={prompt[2]} sha256={prompt[3]}"
            if prompt else "voicechat_prompt absent"
        ),
        (
            f"backend_prompt sequence={received[0]} tools={received[1]} "
            f"chars={received[2]} sha256={received[3]} match={prompt_match}"
            if received else "backend_prompt absent match=unverified"
        ),
        (
            f"model_function status={model_function} "
            f"count={len(decoded)} unique_sequences={csv(dict.fromkeys(decoded_sequences))} "
            f"tools={csv(dict.fromkeys(decoded_names))}"
        ),
        (
            f"voicechat_events function_parsed={parsed_count} "
            f"new_calls={new_call_count} new_call_inputs={new_call_input_count} "
            f"function_result={result_count} "
            f"duplicates_coalesced={coalesced_count} results_replayed={replayed_count} "
            f"duplicate_acks_suppressed={suppressed_ack_count} replay_limit={replay_limit_count} "
            f"response_timeouts={text.count('Function call response timed out')} "
            f"audio_done={audio_done_count}"
        ),
    ]
    if prompt_match != "passed":
        result = ("failed", "PROMPT_CHAIN")
    elif model_function != "passed":
        result = ("failed", "MODEL_FUNCTION_" + model_function.upper())
    elif parsed_count == 0:
        result = ("failed", "FUNCTION_EVENT_NOT_PARSED")
    elif new_call_count == 0:
        result = ("failed", "FUNCTION_CALL_NOT_QUEUED")
    elif duplicate_count or suppressed_ack_count or replay_limit_count:
        result = ("failed", "LEGACY_TOOL_REPLAY")
    elif new_call_count != parsed_count:
        result = ("failed", "FUNCTION_FORWARDING_GAP")
    elif result_count == 0:
        result = ("failed", "TOOL_RESULT_NOT_RECEIVED")
    elif result_count != new_call_count:
        result = ("failed", "TOOL_RESULT_COUNT_MISMATCH")
    elif text.count("Function call response timed out"):
        result = ("failed", "TOOL_RESULT_WAIT_TIMEOUT")
    else:
        result = ("passed", "NONE")
    lines.append(backend_injection_line(text, sequence_id))
    lines.append(f"voicechat_diagnostic={result[0]} fault={result[1]} scope=tool_transport")
    return lines, result[0] == "passed"


def read_voicechat_evidence(stream):
    selected = []
    for line in stream:
        if any(marker in line for marker in VOICECHAT_LOG_MARKERS):
            selected.append(line)
            if len(selected) > 4096:
                raise ValueError("VoiceChat diagnostic window contains more than 4096 relevant lines")
    return "".join(selected)


def summarize_api_log(text):
    received = API_TOOL_RECEIVED_RE.findall(text)
    started = API_BRIDGE_STARTED_RE.findall(text)
    finished = API_BRIDGE_FINISHED_RE.findall(text)
    failed = API_BRIDGE_FAILED_RE.findall(text)
    submitted = API_RESULT_SUBMITTED_RE.findall(text)
    unbound = API_UNBOUND_RE.findall(text)
    ack_restored = API_ACK_RESTORED_RE.findall(text)
    cuekb = API_CUEKB_RE.findall(text)
    completed = API_RESPONSE_COMPLETED_RE.findall(text)
    search_states = API_SEARCH_STATE_RE.findall(text)
    post_terminal = API_POST_TERMINAL_RE.findall(text)
    last_search = {(row[0], row[1]): row for row in search_states}
    answer_inputs = {(row[0], row[2]) for row in completed if row[3] == "answer" and int(row[4]) > 0}
    conversation_ids = dict.fromkeys(
        row[0]
        for rows in (received, started, finished, failed, submitted, unbound, ack_restored, cuekb)
        for row in rows
    )
    unique_calls = dict.fromkeys(
        [row[1] for row in received]
        + [row[1] for row in started]
        + [row[2] for row in finished]
        + [row[2] for row in failed]
        + [row[2] for row in submitted]
        + [row[1] for row in unbound]
        + [row[1] for row in ack_restored]
    )
    received_inputs = dict.fromkeys((row[0], row[3]) for row in received)
    duplicate_inputs = len(received) - len(received_inputs)
    lines = [
        (
            f"api_tool_flow conversations={csv(conversation_ids)} received={len(received)} "
            f"inputs={len(received_inputs)} duplicate_inputs={duplicate_inputs} "
            f"unique_calls={len(unique_calls)} bridges_started={len(started)} "
            f"bridges_finished={len(finished)} bridges_failed={len(failed)} "
            f"unbound={len(unbound)} results_submitted={len(submitted)} "
            f"ack_grants_restored={len(ack_restored)}"
            f" answers_completed={len(answer_inputs)}"
        ),
        (
            f"api_cuekb responses={len(cuekb)} statuses={csv(dict.fromkeys(row[3] for row in cuekb))} "
            f"hits={csv(row[4] for row in cuekb)} traces={csv(row[2] for row in cuekb)} "
            f"durations_ms={csv(row[5] for row in cuekb)}"
        ),
    ]
    lines.append(
        f"api_search_budget inputs={len(last_search)} "
        f"retrieval_executions={sum(int(row[3]) for row in last_search.values())} "
        f"duplicate_queries={sum(int(row[5]) for row in last_search.values())} "
        f"terminal_results_submitted={sum(int(row[6]) for row in search_states)} "
        f"post_terminal_calls={len(post_terminal)}"
    )
    if post_terminal:
        result = ("failed", "TOOL_LOOP_AFTER_TERMINAL")
    elif any(int(row[3]) > int(row[4]) for row in search_states):
        result = ("failed", "SEARCH_BUDGET_EXCEEDED")
    elif not received:
        result = ("failed", "TOOL_EVENT_ABSENT")
    elif not started:
        result = ("failed", "BRIDGE_NOT_STARTED")
    elif failed:
        result = ("failed", "BRIDGE_FAILED")
    elif not cuekb:
        result = ("failed", "CUEKB_RESPONSE_ABSENT")
    elif not submitted:
        result = ("failed", "TOOL_RESULT_NOT_SUBMITTED")
    elif {(row[0], row[1]) for row in received} != {(row[0], row[2]) for row in submitted}:
        result = ("failed", "TOOL_RESULT_COUNT_MISMATCH")
    elif len(ack_restored) > len(received) or len({row[1] for row in ack_restored}) != len(ack_restored):
        result = ("failed", "ACK_GRANT_INVARIANT")
    elif unbound:
        result = ("failed", "UNBOUND_TOOL_EVENTS")
    elif not set(received_inputs) <= answer_inputs:
        result = ("failed", "TOOL_ANSWER_NOT_COMPLETED")
    else:
        result = ("passed", "NONE")
    lines.append(f"api_diagnostic={result[0]} fault={result[1]}")
    return lines, result[0] == "passed"


def read_api_evidence(stream):
    selected = []
    for line in stream:
        if any(marker in line for marker in API_LOG_MARKERS):
            selected.append(line)
            if len(selected) > 4096:
                raise ValueError("API diagnostic window contains more than 4096 relevant lines")
    return "".join(selected)


async def preflight(args, settings):
    cases = load_cases(args.cases, getattr(args, "case_id", None))
    async with httpx.AsyncClient(base_url=args.base_url, timeout=5, follow_redirects=False) as client:
        ready_response = await client.get("/health/ready")
        ready_response.raise_for_status()
        ready = ready_response.json()
        validate_ready(ready, settings.enabled_tool_names, require_text=False)
        capabilities_response = await client.get("/api/v1/capabilities")
        capabilities_response.raise_for_status()
        capabilities = capabilities_response.json()
        validate_voice_policy(capabilities, cuekb_only=True)
    return cases, capabilities


async def execute(args):
    settings = Settings()
    started = datetime.now(UTC).isoformat()
    try:
        cases, capabilities = await preflight(args, settings)
    except Exception as exc:
        print(f"preflight=failed error={type(exc).__name__} detail={safe_detail(exc)!r}")
        print("validation=failed fault=PREFLIGHT focus=api_configuration_or_image exit_code=2")
        return 2

    print(
        "preflight=passed "
        f"provider={capabilities.get('provider')} "
        f"mode={capabilities.get('qa_execution_mode')} "
        f"policy={capabilities.get('answer_policy')} "
        f"tools={csv(capabilities.get('native_tools', []))} cases={len(cases)} "
        f"retrieval_limit={settings.voice_max_retrievals_per_input}"
    )
    report = await run(args)
    finished = datetime.now(UTC).isoformat()
    report.update(window_start=started, window_end=finished)
    await asyncio.to_thread(Path(args.output).write_text, json.dumps(report, indent=2) + "\n")
    print(
        f"window_start={started} window_end={finished} "
        f"conversation_id={report.get('conversation_id') or '-'} report={args.output}"
    )
    for row in report.get("cases", []):
        print(case_line(row))
        check = row.get("answer_check") or {}
        if check.get("condition_failures"):
            print("answer_check=" + json.dumps({
                "case": row["case_id"], "basis": check.get("evidence_basis"),
                "failures": check["condition_failures"][:3],
                "omitted": max(0, len(check["condition_failures"]) - 3),
            }, ensure_ascii=True))
    fault, focus = classify(report)
    exit_code = 0 if fault == "PASS" else 1
    print(
        f"validation={'passed' if exit_code == 0 else 'failed'} "
        f"fault={fault} focus={focus} error_type={report.get('error_type') or '-'} "
        f"exit_code={exit_code}"
    )
    return exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summarize-voicechat-log", action="store_true",
                        help="Read a bounded docker-log window from stdin and print five diagnostic lines")
    parser.add_argument("--summarize-api-log", action="store_true",
                        help="Read a bounded API docker-log window from stdin and print three diagnostic lines")
    parser.add_argument("--inspect-answer", metavar="CONVERSATION_ID",
                        help="Read a saved answer and compare original/delivered evidence checks; no voice test")
    parser.add_argument("--input-item-id", help="Exact input ID for --inspect-answer")
    parser.add_argument("--expected-tool", default="search_knowledge_base")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--origin", help="Defaults to the API container PUBLIC_ORIGIN")
    parser.add_argument("--cases", default="/app/validation/native-cuekb/cases.json")
    parser.add_argument("--case-id", help="Run one image-bundled case on a fresh connection")
    parser.add_argument("--case-timeout", type=float, default=90)
    parser.add_argument("--audio-dir", default="/tmp/native-cuekb/replies")
    parser.add_argument("--output", default="/tmp/native-cuekb/report.json")
    args = parser.parse_args()
    if sum(bool(mode) for mode in (args.summarize_voicechat_log, args.summarize_api_log, args.inspect_answer)) > 1:
        parser.error("Select only one diagnostic mode")
    if args.inspect_answer:
        if not args.input_item_id:
            parser.error("--inspect-answer requires --input-item-id")
        from scripts.inspect_native_answer import inspect_answer
        try:
            result = asyncio.run(inspect_answer(Settings(), args.inspect_answer, args.input_item_id))
        except ValueError as exc:
            print(f"answer_inspection=unavailable detail={safe_detail(exc)!r}")
            raise SystemExit(2) from exc
        except Exception as exc:
            print(f"answer_inspection=unavailable error_type={type(exc).__name__}")
            raise SystemExit(2) from exc
        print("answer_inspection=" + json.dumps(result, ensure_ascii=True))
        return
    if args.input_item_id:
        parser.error("--input-item-id requires --inspect-answer")
    if args.summarize_voicechat_log or args.summarize_api_log:
        try:
            evidence = (read_voicechat_evidence(sys.stdin) if args.summarize_voicechat_log
                        else read_api_evidence(sys.stdin))
        except ValueError as exc:
            component = "voicechat" if args.summarize_voicechat_log else "api"
            print(f"{component}_diagnostic=failed fault=LOG_WINDOW detail={safe_detail(exc)!r}")
            raise SystemExit(2) from exc
        lines, ok = (summarize_voicechat_log(evidence, args.expected_tool)
                     if args.summarize_voicechat_log else summarize_api_log(evidence))
        print("\n".join(lines))
        raise SystemExit(0 if ok else 1)
    if args.origin is None:
        args.origin = Settings().public_origin
    raise SystemExit(asyncio.run(execute(args)))


if __name__ == "__main__":
    main()
