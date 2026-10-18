"""Acceptance reporting must not conflate routing, arguments, sources and speech."""

import json
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_native_cuekb_validation as entrypoint
from scripts.run_native_cuekb_validation import (
    case_line,
    classify,
    read_api_evidence,
    read_voicechat_evidence,
    summarize_api_log,
    summarize_voicechat_log,
)
from scripts.validate_native_cuekb import assess, load_cases, passed, summary_lines


def test_committed_cases_use_current_public_tool_name():
    root = Path(__file__).parents[2]
    cases = json.loads((root / "artifacts/native-cuekb/cases.json").read_text())

    assert {case["expected_tool"] for case in cases} <= {None, "search_knowledge_base"}


def test_case_validation_identifies_legacy_tool_name(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([
        {"case_id": "legacy", "wav": "unused.wav", "expected_tool": "search_cuekb"}
    ]))

    with pytest.raises(
        ValueError,
        match="Case 'legacy': expected_tool must be null or search_knowledge_base; got 'search_cuekb'",
    ):
        load_cases(path)


def test_unusable_voice_evidence_is_separate_from_successful_retrieval():
    case = {"expected_tool": "search_knowledge_base", "query_terms": ["Black Pig"]}
    records = [
        {"kind": "native_tool_arguments", "payload": {
            "name": "search_knowledge_base", "input_item_id": "i1",
            "arguments": {"query": "Black Pig"},
        }},
        {"kind": "native_cuekb_result", "payload": {
            "input_item_id": "i1", "status": "ok", "evidence_status": "sufficient",
            "hit_count": 1, "trace_id": "trace-1",
        }},
    ]
    history = {"records": records, "items": [{
        "input_item_id": "i1", "selected_tool": "search_knowledge_base",
        "answer": {"reason_code": "CUEKB_VOICE_EVIDENCE_UNAVAILABLE", "citations": []},
    }]}

    result = assess(case, history, {"i1"}, 1920)

    assert result["retrieval"] == "passed"
    assert result["voice_evidence"] == "unavailable"


def test_console_summary_is_bounded_and_exit_requires_product_retrieval():
    report = {
        "status": "observed",
        "capabilities": {"native_tools": ["search_knowledge_base"]},
        "cases": [{
            "case_id": "product", "expected_tool": "search_knowledge_base",
            "status": "observed", "selection": "passed",
            "actual_tools": ["search_knowledge_base"], "arguments": "manual_review_required",
            "retrieval": "passed", "reason_code": None, "speech_transport": "passed",
            "elapsed_ms": 1200, "input_items_observed": 1, "output_samples": 1920,
            "source_trace_ids": ["trace-1"],
        }],
    }

    lines = list(summary_lines(report))

    assert len(lines) == 2
    assert lines[0] == (
        "report_status=observed error_type=None cases=1 "
        "native_tools=['search_knowledge_base']"
    )
    assert "case=product" in lines[1]
    assert "retrieval=passed" in lines[1]
    assert "traces=1" in lines[1]
    assert passed(report)
    report["cases"][0]["retrieval"] = "insufficient_evidence"
    assert not passed(report)


def test_single_entrypoint_classifies_missing_native_tool_with_bounded_evidence():
    row = {
        "case_id": "product", "expected_tool": "search_knowledge_base",
        "actual_tools": [], "selection": "failed", "arguments": "unverified",
        "retrieval": "unverified", "speech_transport": "passed",
        "reason_code": None, "portal_error": None, "elapsed_ms": 1200,
        "input_item_ids": ["input-1"], "source_trace_ids": [],
    }

    fault, focus = classify({"status": "observed", "cases": [row]})
    line = case_line(row)

    assert (fault, focus) == (
        "TOOL_NOT_CALLED", "voicechat_prompt_or_model_function_channel"
    )
    assert line == (
        "case=product expected=search_knowledge_base actual=- selection=failed "
        "arguments=unverified retrieval=unverified voice_evidence=- evidence_filter=- "
        "speech=passed reason=- "
        "portal_error=- elapsed_ms=1200 input_ids=input-1 trace_ids=-"
    )


@pytest.mark.parametrize(("row", "fault"), [
    ({"portal_error": "VOICE_TOOL_UNKNOWN"}, "UNKNOWN_TOOL_NAME"),
    ({"selection": "passed", "arguments": "failed"}, "TOOL_ARGUMENTS"),
    ({"selection": "passed", "arguments": "manual_review_required",
      "retrieval": "insufficient_evidence"}, "CUEKB_RETRIEVAL"),
    ({"selection": "passed", "arguments": "manual_review_required",
      "retrieval": "passed", "voice_evidence": "unavailable"}, "CUEKB_VOICE_EVIDENCE"),
])
def test_single_entrypoint_classifies_next_diagnostic_stage(row, fault):
    product = {
        "expected_tool": "search_knowledge_base", "actual_tools": ["search_knowledge_base"],
        "selection": "passed", "arguments": "manual_review_required", "retrieval": "passed",
        "speech_transport": "passed", "input_item_ids": ["input-1"], **row,
    }
    assert classify({"status": "partial", "cases": [product]})[0] == fault


def test_single_entrypoint_does_not_blame_tool_selection_without_product_input():
    row = {
        "expected_tool": "search_knowledge_base", "actual_tools": [],
        "selection": "unverified", "arguments": "unverified", "retrieval": "unverified",
        "speech_transport": "failed", "input_item_ids": [], "input_items_observed": 0,
    }

    assert classify({"status": "partial", "cases": [row]}) == (
        "INPUT_NOT_OBSERVED", "voicechat_audio_or_asr_transport"
    )


def test_voicechat_log_summary_compares_prompt_chain_without_printing_raw_log():
    digest = "a" * 64
    log = "\n".join((
        "private prefix Prompt prepared: tools_count=1 tool_names=search_knowledge_base "
        f"prompt_chars=6123 prompt_sha256={digest}",
        "System prompt received for sequence 42: tool_names=search_knowledge_base "
        f"chars=6123 sha256={digest}",
        "Function call decoded from model for sequence 42: "
        "tool_names=search_knowledge_base private suffix",
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.parsed"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.result"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"output.sent",'
        '"kind":"response.output_audio.done"}',
    ))

    lines, ok = summarize_voicechat_log(log, "search_knowledge_base")

    assert ok and len(lines) == 5
    assert lines[1] == (
        f"backend_prompt sequence=42 tools=search_knowledge_base chars=6123 "
        f"sha256={digest} match=passed"
    )
    assert lines[2] == (
        "model_function status=passed count=1 unique_sequences=42 tools=search_knowledge_base"
    )
    assert lines[3] == (
        "voicechat_events function_parsed=1 function_result=1 "
        "duplicates_coalesced=0 response_timeouts=0 audio_done=1"
    )
    assert "private" not in "\n".join(lines)


def test_voicechat_log_summary_marks_absent_model_function_as_fault():
    digest = "b" * 64
    log = "\n".join((
        "Prompt prepared: tools_count=1 tool_names=search_knowledge_base "
        f"prompt_chars=6000 prompt_sha256={digest}",
        "System prompt received for sequence 7: tool_names=search_knowledge_base "
        f"chars=6000 sha256={digest}",
    ))

    lines, ok = summarize_voicechat_log(log, "search_knowledge_base")

    assert not ok
    assert lines[-1] == "voicechat_diagnostic=failed fault=MODEL_FUNCTION_ABSENT"


def test_voicechat_log_input_discards_unrelated_lines_before_summary():
    source = StringIO(
        "private unrelated payload\n"
        "Prompt prepared: tools_count=1 tool_names=search_knowledge_base "
        f"prompt_chars=1 prompt_sha256={'c' * 64}\n"
        'voice_ws_diagnostic {"event":"function.result"}\n'
    )

    evidence = read_voicechat_evidence(source)

    assert "private unrelated" not in evidence
    assert "Prompt prepared" in evidence
    assert "function.result" in evidence


def test_voicechat_log_summary_flags_repeated_calls_without_tool_result():
    digest = "d" * 64
    log = "\n".join((
        "Prompt prepared: tools_count=1 tool_names=search_knowledge_base "
        f"prompt_chars=7000 prompt_sha256={digest}",
        "System prompt received for sequence 42: tool_names=search_knowledge_base "
        f"chars=7000 sha256={digest}",
        "Function call decoded from model for sequence 42: tool_names=search_knowledge_base",
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.parsed"}',
        "Function call response timed out after 64 frames, resetting to idle",
        "Function call decoded from model for sequence 42: tool_names=search_knowledge_base",
    ))

    lines, ok = summarize_voicechat_log(log, "search_knowledge_base")

    assert not ok
    assert "count=2 unique_sequences=42" in lines[2]
    assert "response_timeouts=1" in lines[3]
    assert lines[-1] == "voicechat_diagnostic=failed fault=TOOL_RESULT_NOT_RECEIVED"


def test_voicechat_log_summary_allows_sequential_tool_cases_on_one_sequence():
    digest = "e" * 64
    log = "\n".join((
        "Prompt prepared: tools_count=1 tool_names=search_knowledge_base "
        f"prompt_chars=7000 prompt_sha256={digest}",
        "System prompt received for sequence 42: tool_names=search_knowledge_base "
        f"chars=7000 sha256={digest}",
        "Function call decoded from model for sequence 42: tool_names=search_knowledge_base",
        "Function call decoded from model for sequence 42: tool_names=search_knowledge_base",
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.parsed"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.result"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.parsed"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.result"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.duplicate"}',
    ))

    lines, ok = summarize_voicechat_log(log, "search_knowledge_base")

    assert ok
    assert "count=2 unique_sequences=42" in lines[2]
    assert lines[-1] == "voicechat_diagnostic=passed fault=NONE"


def test_voicechat_log_summary_filters_other_sequences_and_trailing_log_quote():
    digest = "f" * 64
    log = "\n".join((
        "Prompt prepared: tools_count=1 tool_names=search_knowledge_base "
        f"prompt_chars=7000 prompt_sha256={digest}",
        'System prompt received for sequence 42: tool_names=search_knowledge_base '
        f'chars=7000 sha256={digest}"',
        'Function call decoded from model for sequence 41: tool_names=old_tool"',
        'Function call decoded from model for sequence 42: tool_names=search_knowledge_base"',
        'voice_ws_diagnostic {"sequence_id":41,"event":"function.parsed"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.parsed"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.result"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.duplicate"}',
    ))

    lines, ok = summarize_voicechat_log(log, "search_knowledge_base")

    assert ok
    assert lines[2] == (
        "model_function status=passed count=1 unique_sequences=42 "
        "tools=search_knowledge_base"
    )
    assert lines[3].startswith(
        "voicechat_events function_parsed=1 function_result=1 duplicates_coalesced=1"
    )


def test_api_log_summary_reports_only_lifecycle_counts_and_trace():
    log = "\n".join((
        "voice_tool_call_received conversation_id=c1 call_id=k1 "
        "tool=search_knowledge_base input_item_id=i1",
        "voice_bridge_started conversation_id=c1 call_id=k1",
        "cuekb_response_validated conversation_id=c1 turn_id=t1 trace_id=z1 "
        "retrieval_status=ok hit_count=1 duration_ms=25 service_total_ms=20",
        "voice_bridge_finished conversation_id=c1 turn_id=t1 call_id=k1 "
        "status=insufficient_evidence",
        "voice_tool_result_submitted conversation_id=c1 turn_id=t1 call_id=k1",
        "private query and response body",
    ))

    evidence = read_api_evidence(StringIO(log))
    lines, ok = summarize_api_log(evidence)

    assert ok
    assert lines[0] == (
        "api_tool_flow conversations=c1 received=1 unique_calls=1 bridges_started=1 "
        "bridges_finished=1 bridges_failed=0 unbound=0 results_submitted=1"
    )
    assert lines[1] == "api_cuekb responses=1 statuses=ok hits=1 traces=z1 durations_ms=25"
    assert lines[2] == "api_diagnostic=passed fault=NONE"
    assert "private" not in evidence


async def test_single_entrypoint_prints_only_preflight_window_cases_and_classification(
    monkeypatch, capsys,
):
    cases = [{"case_id": name} for name in ("greeting", "general", "product-1", "product-2")]
    capabilities = {
        "provider": "nvidia", "qa_execution_mode": "dual_tools",
        "answer_policy": "general_qa", "native_tools": ["search_knowledge_base"],
    }
    rows = [{
        "case_id": case["case_id"],
        "expected_tool": "search_knowledge_base" if case["case_id"].startswith("product") else None,
        "actual_tools": [], "selection": "failed", "arguments": "unverified",
        "retrieval": "unverified", "speech_transport": "passed", "elapsed_ms": 1,
        "input_item_ids": [case["case_id"] + "-input"], "source_trace_ids": [],
    } for case in cases]

    async def fake_preflight(_args, _settings):
        return cases, capabilities

    async def fake_run(_args):
        return {"status": "observed", "conversation_id": "conversation-1", "cases": rows}

    monkeypatch.setattr(entrypoint, "preflight", fake_preflight)
    monkeypatch.setattr(entrypoint, "run", fake_run)
    args = SimpleNamespace(output="/tmp/report.json")

    result = await entrypoint.execute(args)
    lines = capsys.readouterr().out.splitlines()

    assert result == 1
    assert len(lines) == 7
    assert lines[0].startswith("preflight=passed provider=nvidia")
    assert "conversation_id=conversation-1" in lines[1]
    assert lines[-1].startswith("validation=failed fault=TOOL_NOT_CALLED")


@pytest.mark.parametrize("scenario,expected", [
    ("missing_call", ("failed", "unverified", "unverified", "passed")),
    ("bad_arguments", ("passed", "failed", "unverified", "failed")),
    ("search_failed", ("passed", "passed", "failed", "passed")),
    ("no_evidence", ("passed", "passed", "insufficient_evidence", "passed")),
    ("speech_failed", ("passed", "passed", "unverified", "passed")),
])
def test_diagnostic_stages_do_not_masquerade_as_tool_selection_failure(scenario, expected):
    case = {"expected_tool": "search_knowledge_base", "query_terms": ["AX"]}
    records = [{"kind": "native_tool_arguments", "payload": {"name": "search_knowledge_base",
        "input_item_id": "i1", "arguments": {"query": "Configure Product AX"}}}]
    turn = {"input_item_id": "i1", "selected_tool": "search_knowledge_base", "answer": None}
    history = {"records": records, "items": [turn]}
    samples = 1920
    if scenario == "missing_call":
        history = {"records": [], "items": []}
    elif scenario == "bad_arguments":
        records[0] = {"kind": "native_tool_error", "payload": {"name": "search_knowledge_base",
            "input_item_id": "i1", "reason_code": "VOICE_TOOL_ARGUMENTS"}}
        history["items"] = []
        samples = 0
    else:
        code = {"search_failed": "CUEKB_AUTH_FAILED", "no_evidence": "CUEKB_NO_EVIDENCE",
                "speech_failed": "VOICE_UNSUPPORTED_NUMBER"}[scenario]
        turn["answer"] = {"reason_code": code, "citations": []}
    result = assess(case, history, {"i1"}, samples)
    assert tuple(result[key] for key in ("selection", "arguments", "retrieval", "speech_transport")) == expected
    assert result["spoken_faithfulness"] == "manual_review_required"
