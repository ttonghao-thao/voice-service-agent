"""Acceptance reporting must not conflate routing, arguments, sources and speech."""

import json
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_native_cuekb_validation as entrypoint
from scripts.run_native_cuekb_validation import (
    backend_injection_line,
    case_line,
    classify,
    read_api_evidence,
    read_voicechat_evidence,
    summarize_api_log,
    summarize_cuekb_v2_log,
    summarize_voicechat_log,
)
from scripts.validate_native_cuekb import assess, load_cases, passed, summary_lines


@pytest.mark.parametrize("cancel_last", [False, True])
def test_backend_injection_summary_separates_received_from_completed_and_other_sequences(cancel_last):
    rows = []
    for sequence in (42, 420):
        for index in range(10):
            rows.extend([
                f"seq_id: {sequence} response_chars=322 Token length:93",
                f"Tool response buffered during speaking_ack (seq_id: {sequence}) (step: 20)",
                f"[seq {sequence}] Fast response-token injection started",
            ])
            if sequence == 42 and index == 9 and cancel_last:
                rows.append(f"[seq {sequence}] Response injection cancelled")
            else:
                rows.extend([
                    f"Function call state transition: process_response -> idle (seq_id: {sequence}) (step: 30)",
                    f"[seq {sequence}] Fast response-token injection done, state: idle",
                ])
    selected = read_voicechat_evidence(StringIO("\n".join(rows)))
    line = backend_injection_line(selected, "42")
    assert "received=10 buffered_ack=10 started=10" in line
    assert f"returned_idle={9 if cancel_last else 10} done_idle={9 if cancel_last else 10}" in line
    assert f"cancelled={int(cancel_last)} decoder_limit=0" in line
    assert f"status={'incomplete' if cancel_last else 'complete'}" in line
    assert "chars_min_max=322:322 tokens_min_max=93:93" in line
    assert "status=unverified" in backend_injection_line(selected, "99")


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
    assert result["cuekb_evidence_status"] == "sufficient"
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
            "cuekb_evidence_status": "sufficient",
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


def test_completed_audio_does_not_override_failed_source_check():
    row = {"expected_tool": "search_knowledge_base", "answer_complete": "passed", "selection": "passed",
           "arguments": "manual_review_required", "retrieval": "passed", "voice_evidence": "passed",
           "speech_transport": "passed", "reason_code": "VOICE_UNSUPPORTED_NUMBER"}
    assert not passed({"status": "observed", "cases": [row]})
    assert classify({"status": "observed", "cases": [row]})[0] == "ANSWER_CONTENT_CHECK"


def test_single_case_selection_preserves_original_wav_and_rejects_unknown_id():
    path = Path(__file__).parents[2] / "artifacts/native-cuekb/cases.json"
    all_cases = load_cases(path)
    selected = load_cases(path, "black_pig_crisp_heart")
    assert len(selected) == 1
    assert selected[0] == next(c for c in all_cases if c["case_id"] == "black_pig_crisp_heart")
    with pytest.raises(ValueError, match="Unknown case_id"):
        load_cases(path, "not-a-case")
    followup = load_cases(path, "golden_yolk", "greeting")
    assert [case["case_id"] for case in followup] == ["golden_yolk", "greeting"]
    assert followup == [next(case for case in all_cases if case["case_id"] == name)
                        for name in ("golden_yolk", "greeting")]
    with pytest.raises(ValueError, match="requires --case-id"):
        load_cases(path, followup_case_id="greeting")


@pytest.mark.parametrize("error", ["VOICE_SESSION_ROTATION_REQUIRED", "VOICE_SESSION_EXPIRED"])
def test_explicit_lifetime_error_does_not_get_classified_as_asr_failure(error):
    assert classify({"cases": [{"expected_tool": "search_knowledge_base",
                               "input_item_ids": [], "portal_error": error}]})[0] == "VOICE_SESSION_LIMIT"


@pytest.mark.parametrize("status,citations", [(None, 0), ("failed", 0), ("answered", 0)])
def test_audio_completion_requires_committed_portal_answer_and_sources(status, citations):
    row = {"expected_tool": "search_knowledge_base", "answer_complete": "passed", "selection": "passed",
           "arguments": "passed", "retrieval": "passed", "voice_evidence": "passed",
           "speech_transport": "passed", "reason_code": None, "input_item_ids": ["i1"],
           "answer_status": status, "answer_citation_count": citations}
    report = {"status": "observed", "cases": [row]}
    assert not passed(report)
    assert classify(report)[0] == "PORTAL_ANSWER_COMMIT"


def test_condition_diagnostics_keep_required_conditions_and_source_restrictions():
    from app.agent_runtime.evidence import EvidenceGate
    source = SimpleNamespace(citation_id="C1", metadata={"product_model": "AX"},
                             content="Product AX must not be opened.", context="Use only indoors unless supervised.")
    failures = EvidenceGate.missing_conditions("Open it.", [source], {"software_version": "v2"})
    assert {f["rule"] for f in failures} == {"source_metadata", "source_signal", "required_condition"}
    assert {f["value"] for f in failures} == {"AX", "negation", "only", "unless", "v2"}
    assert EvidenceGate.check_spoken("Open it.", [source]) == "VOICE_MISSING_CONDITION"


def test_condition_diagnostics_remain_hidden_after_knowledge_scope_revocation():
    from app.api.routes import _record_for_principal
    from app.config import Settings
    from app.contracts import Principal, now
    kb = "00000000-0000-4000-8000-000000000001"
    record = SimpleNamespace(kind="native_answer_check", epoch=1, source_id="turn", created_at=now(),
                             payload={"_authorized_kb_ids": [kb], "condition_failures": [{"value": "private"}]})
    user = Principal(user_id="operator", roles={"customer"}, scopes=set(), knowledge_base_ids=set())
    assert _record_for_principal(record, user, Settings(_env_file=None)) is None
    user = user.model_copy(update={"knowledge_base_ids": {kb}})
    assert _record_for_principal(record, user, Settings(_env_file=None))["payload"] == {
        "condition_failures": [{"value": "private"}]}


def test_budget_summary_reports_post_terminal_failure_instead_of_only_result_mismatch():
    log = "\n".join([
        "voice_search_state conversation_id=c1 input_item_id=i1 call_id=k1 retrieval_executions=1 "
        "retrieval_limit=2 duplicate_queries=0 terminal_result_submitted=0 cumulative_sources=1",
        "voice_search_state conversation_id=c1 input_item_id=i1 call_id=k2 retrieval_executions=1 "
        "retrieval_limit=2 duplicate_queries=1 terminal_result_submitted=1 cumulative_sources=1",
        "voice_search_post_terminal conversation_id=c1 input_item_id=i1 call_id=k3",
    ])
    lines, ok = summarize_api_log(read_api_evidence(StringIO(log)))
    assert not ok
    assert lines[-2] == ("api_search_budget inputs=1 retrieval_executions=1 duplicate_queries=1 "
                         "terminal_results_submitted=1 post_terminal_calls=1 queued_calls_settled=0 result_injections=0")
    assert lines[-1] == "api_diagnostic=failed fault=TOOL_LOOP_AFTER_TERMINAL"


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
        "arguments=unverified retrieval=unverified cuekb_evidence=- "
        "voice_evidence=- evidence_filter=- "
        "speech=passed answer_complete=unverified extra_answers=0 quiet_observation=unverified answer_status=- answer_sources=0 "
        "retrieval_executions=0 retrieval_limit=- "
        "duplicate_queries=0 terminal_results_submitted=0 post_terminal_calls=0 queued_calls_settled=0 tool_batches_submitted=0 cumulative_sources=0 "
        "latest_retrieval=- reason=- "
        "portal_error=- elapsed_ms=1200 input_ids=input-1 trace_ids=-"
    )


def test_evidence_filter_summary_splits_truncated_and_omitted_context():
    line = case_line({
        "case_id": "product", "expected_tool": "search_knowledge_base",
        "actual_tools": ["search_knowledge_base"], "selection": "passed",
        "arguments": "manual_review_required", "retrieval": "passed",
        "cuekb_evidence_status": "sufficient", "voice_evidence": "passed",
        "voice_evidence_filter": {
            "candidate_count": 5, "selected_count": 2,
            "accepted_truncated_source_count": 1,
            "rejected_truncated_context_count": 1,
            "accepted_omitted_source_count": 1,
            "rejected_omitted_context_count": 2,
            "rejected_non_ascii_count": 0, "rejected_limit_count": 0,
            "rejected_budget_count": 0,
        },
        "speech_transport": "passed", "reason_code": None, "portal_error": None,
        "elapsed_ms": 1200, "input_item_ids": ["input-1"], "source_trace_ids": ["trace-1"],
    })

    assert "truncated_ok:1,truncated_rejected:1" in line
    assert "omitted_ok:1,omitted_rejected:2" in line


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


def test_single_entrypoint_classifies_post_tool_answer_timeout():
    row = {
        "expected_tool": "search_knowledge_base",
        "actual_tools": ["search_knowledge_base"], "selection": "passed",
        "arguments": "manual_review_required", "retrieval": "passed",
        "voice_evidence": "passed", "speech_transport": "passed",
        "reason_code": "VOICE_ANSWER_TIMEOUT", "portal_error": "VOICE_TOOL_REQUIRED",
        "input_item_ids": ["input-1"],
    }

    assert classify({"status": "partial", "error_type": "ValueError", "cases": [row]}) == (
        "VOICE_RESPONSE_COMPLETION", "provider_response_or_api_relay"
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
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.call",'
        '"action":"queued","input_item_id":"i1"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.result"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"output.sent",'
        '"kind":"response.output_audio.done"}',
    ))

    lines, ok = summarize_voicechat_log(log, "search_knowledge_base")

    assert ok and len(lines) == 6
    assert lines[1] == (
        f"backend_prompt sequence=42 tools=search_knowledge_base chars=6123 "
        f"sha256={digest} match=passed"
    )
    assert lines[2] == (
        "model_function status=passed count=1 unique_sequences=42 tools=search_knowledge_base"
    )
    assert lines[3] == (
        "voicechat_events function_parsed=1 new_calls=1 new_call_inputs=1 function_result=1 "
        "duplicates_coalesced=0 results_replayed=0 duplicate_acks_suppressed=0 replay_limit=0 "
        "response_timeouts=0 audio_done=1"
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
    assert lines[-1] == "voicechat_diagnostic=failed fault=MODEL_FUNCTION_ABSENT scope=tool_transport"


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
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.call",'
        '"action":"queued","input_item_id":"i1"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.parsed"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.duplicate",'
        '"action":"coalesced"}',
        "Function call response timed out after 64 frames, resetting to idle",
        "Function call decoded from model for sequence 42: tool_names=search_knowledge_base",
    ))

    lines, ok = summarize_voicechat_log(log, "search_knowledge_base")

    assert not ok
    assert "count=2 unique_sequences=42" in lines[2]
    assert "response_timeouts=1" in lines[3]
    assert lines[-1] == "voicechat_diagnostic=failed fault=LEGACY_TOOL_REPLAY scope=tool_transport"


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
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.call",'
        '"action":"queued","input_item_id":"i1"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.result"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.parsed"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.call",'
        '"action":"queued","input_item_id":"i2"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.result"}',
    ))

    lines, ok = summarize_voicechat_log(log, "search_knowledge_base")

    assert ok
    assert "count=2 unique_sequences=42" in lines[2]
    assert lines[-1] == "voicechat_diagnostic=passed fault=NONE scope=tool_transport"


def test_voicechat_log_summary_filters_other_sequences_and_trailing_log_quote():
    digest = "f" * 64
    log = "\n".join((
        "Prompt prepared: tools_count=1 tool_names=search_knowledge_base "
        f"prompt_chars=7000 prompt_sha256={digest}",
        'System prompt received for sequence 42: tool_names=search_knowledge_base '
        f'chars=7000 sha256={digest}"',
        'Function call decoded from model for sequence 41: tool_names=old_tool"',
        'Function call decoded from model for sequence 42: tool_names=search_knowledge_base"',
        'Function call decoded from model for sequence 42: tool_names=search_knowledge_base"',
        'voice_ws_diagnostic {"sequence_id":41,"event":"function.parsed"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.parsed"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.call",'
        '"action":"queued","input_item_id":"i1"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.parsed"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.result"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.duplicate",'
        '"action":"replayed_result"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.duplicate",'
        '"action":"ack_suppressed"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.result"}',
    ))

    lines, ok = summarize_voicechat_log(log, "search_knowledge_base")

    assert not ok
    assert lines[-1] == "voicechat_diagnostic=failed fault=LEGACY_TOOL_REPLAY scope=tool_transport"
    assert lines[2] == (
        "model_function status=passed count=2 unique_sequences=42 "
        "tools=search_knowledge_base"
    )
    assert lines[3].startswith(
        "voicechat_events function_parsed=2 new_calls=1 new_call_inputs=1 "
        "function_result=2 duplicates_coalesced=0 results_replayed=1 "
        "duplicate_acks_suppressed=1 replay_limit=0"
    )


def test_voicechat_log_summary_fails_after_one_cached_result_replay():
    digest = "1" * 64
    log = "\n".join((
        "Prompt prepared: tools_count=1 tool_names=search_knowledge_base "
        f"prompt_chars=7000 prompt_sha256={digest}",
        "System prompt received for sequence 42: tool_names=search_knowledge_base "
        f"chars=7000 sha256={digest}",
        *["Function call decoded from model for sequence 42: tool_names=search_knowledge_base"] * 3,
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.parsed"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.call",'
        '"action":"queued","input_item_id":"i1"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.result"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.parsed"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.duplicate",'
        '"action":"replayed_result"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.parsed"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.duplicate",'
        '"action":"replay_limit"}',
    ))

    lines, ok = summarize_voicechat_log(log, "search_knowledge_base")

    assert not ok
    assert "results_replayed=1 duplicate_acks_suppressed=0 replay_limit=1" in lines[3]
    assert lines[-1] == "voicechat_diagnostic=failed fault=LEGACY_TOOL_REPLAY scope=tool_transport"


def test_voicechat_log_summary_requires_duplicate_ack_suppression():
    digest = "2" * 64
    log = "\n".join((
        "Prompt prepared: tools_count=1 tool_names=search_knowledge_base "
        f"prompt_chars=7000 prompt_sha256={digest}",
        "System prompt received for sequence 42: tool_names=search_knowledge_base "
        f"chars=7000 sha256={digest}",
        *["Function call decoded from model for sequence 42: "
          "tool_names=search_knowledge_base"] * 2,
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.parsed"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.call",'
        '"action":"queued","input_item_id":"i1"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.result"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.parsed"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.duplicate",'
        '"action":"replayed_result"}',
    ))

    lines, ok = summarize_voicechat_log(log, "search_knowledge_base")

    assert not ok
    assert lines[-1] == "voicechat_diagnostic=failed fault=LEGACY_TOOL_REPLAY scope=tool_transport"


def test_voicechat_log_summary_requires_cached_replay_delivery():
    digest = "3" * 64
    log = "\n".join((
        "Prompt prepared: tools_count=1 tool_names=search_knowledge_base "
        f"prompt_chars=7000 prompt_sha256={digest}",
        "System prompt received for sequence 42: tool_names=search_knowledge_base "
        f"chars=7000 sha256={digest}",
        *["Function call decoded from model for sequence 42: "
          "tool_names=search_knowledge_base"] * 2,
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.parsed"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.call",'
        '"action":"queued","input_item_id":"i1"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.result"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.parsed"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.duplicate",'
        '"action":"replayed_result"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.duplicate",'
        '"action":"ack_suppressed"}',
    ))

    lines, ok = summarize_voicechat_log(log, "search_knowledge_base")

    assert not ok
    assert lines[-1] == "voicechat_diagnostic=failed fault=LEGACY_TOOL_REPLAY scope=tool_transport"


def test_api_log_summary_rejects_multiple_ack_grant_restores_for_one_call():
    log = "\n".join((
        "voice_tool_call_received conversation_id=c1 call_id=k1 "
        "tool=search_knowledge_base input_item_id=i1",
        "voice_bridge_started conversation_id=c1 call_id=k1",
        "cuekb_response_validated conversation_id=c1 turn_id=u1 trace_id=t1 "
        "retrieval_status=ok hit_count=3 duration_ms=20",
        "voice_tool_result_submitted conversation_id=c1 turn_id=u1 call_id=k1",
        "voice_tool_ack_continuation_restored conversation_id=c1 call_id=k1 response_id=r1",
        "voice_tool_ack_continuation_restored conversation_id=c1 call_id=k1 response_id=r2",
    ))

    lines, ok = summarize_api_log(log)

    assert not ok
    assert "ack_grants_restored=2" in lines[0]
    assert lines[-1] == "api_diagnostic=failed fault=ACK_GRANT_INVARIANT"


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
        "voice_response_completed conversation_id=c1 response_id=r1 input_item_id=i1 phase=answer samples=1920",
        "private query and response body",
    ))

    evidence = read_api_evidence(StringIO(log))
    lines, ok = summarize_api_log(evidence)

    assert ok
    assert lines[0] == (
        "api_tool_flow conversations=c1 received=1 inputs=1 duplicate_inputs=0 "
        "unique_calls=1 bridges_started=1 "
        "bridges_finished=1 bridges_failed=0 unbound=0 results_submitted=1 "
        "ack_grants_restored=0 answers_completed=1"
    )
    assert lines[1] == "api_cuekb responses=1 statuses=ok hits=1 traces=z1 durations_ms=25"
    assert lines[-1] == "api_diagnostic=passed fault=NONE"
    assert "private" not in evidence


def test_v2_diagnostic_summary_is_bounded_and_never_echoes_evidence_or_unknown_fields():
    diagnostic = {
        "schema_version": "2", "ranking_method": "rrf", "trace_id": "00000000-0000-4000-8000-000000000001",
        "service_hits": 5, "adapter_retained": 3, "adapter_omitted": 2,
        "scope_counts": {"resolved": 2, "unknown": 1, "ambiguous": 0, "private": "secret"},
        "required_parts": 4, "unresolved_groups": 1, "service_hits_bytes": 9000,
        "adapter_bytes": 7000, "service_total_ms": 102.5,
        "omission_counts": {"payload_budget_exhausted": 1, "adapter": {"payload_budget": 2},
                            "source_text": "secret evidence"},
        "reranker": {"configured": False, "executed": False, "token": "secret"},
        "source_text": "secret evidence", "stage_scores": {"query": "secret query"},
    }
    text = "\n".join("cuekb_v2_diagnostic conversation_id=c1 data=" + json.dumps(diagnostic)
                     for _ in range(6))
    evidence = read_api_evidence(StringIO(text))
    lines = summarize_cuekb_v2_log(evidence)
    assert len(lines) == 5
    row = json.loads(lines[0].split("=", 1)[1])
    assert row["schema_version"] == "2" and row["ranking_method"] == "rrf"
    assert row["scope_counts"] == {"resolved": 2, "unknown": 1, "ambiguous": 0}
    assert row["reranker"] == {"configured": False, "executed": False}
    assert row["omission_counts"] == {"payload_budget_exhausted": 1, "adapter": {"payload_budget": 2}}
    assert json.loads(lines[-1].split("=", 1)[1]) == {"shown": 4, "omitted": 2, "malformed": 0}
    assert "secret" not in "\n".join(lines)


def test_v2_diagnostic_bad_rows_do_not_echo_original_content():
    lines = summarize_cuekb_v2_log('cuekb_v2_diagnostic conversation_id=c1 data={"secret":broken}')
    assert lines == ['cuekb_v2_summary={"shown":0,"omitted":0,"malformed":1}']
    assert summarize_cuekb_v2_log("no v2 diagnostic") == []


def test_v2_http_error_summary_keeps_status_trace_without_upstream_detail():
    trace = "00000000-0000-4000-8000-000000000001"
    log = f"cuekb_http_error conversation_id=c1 turn_id=t1 status=409 trace_id={trace}\nprivate upstream detail"
    selected = read_api_evidence(StringIO(log))
    assert "private" not in selected
    assert summarize_cuekb_v2_log(selected) == [f'cuekb_http_error={{"status":409,"trace_id":"{trace}"}}']


def test_v2_operational_summary_does_not_treat_counts_or_malformed_method_as_evidence():
    raw = {"schema_version": "secret", "ranking_method": ["secret"],
           "adapter_retained": True, "scope_counts": {"resolved": -1}, "service_total_ms": float("nan")}
    line = "cuekb_v2_diagnostic conversation_id=c1 data=" + json.dumps(raw)
    row = json.loads(summarize_cuekb_v2_log(line)[0].split("=", 1)[1])
    assert row["ranking_method"] == row["schema_version"] == "unknown"
    assert row["adapter_retained"] is None and row["scope_counts"] == {"resolved": None}
    assert "service_total_ms" not in row
    assert "secret" not in json.dumps(row)


@pytest.mark.parametrize(("reason", "fault"), [
    ("CUEKB_CONFIGURATION_ERROR", "CUEKB_CONFIGURATION"),
    ("CUEKB_INDEX_UPGRADE_REQUIRED", "CUEKB_CONFIGURATION"),
    ("CUEKB_CONFIGURATION_CHANGED", "CUEKB_CONFIGURATION"),
    ("CUEKB_INDEX_MAINTENANCE", "CUEKB_CONFIGURATION"),
    ("CUEKB_CONTRACT_ERROR", "CUEKB_CONTRACT"),
    ("TOOL_BAD_RESPONSE", "CUEKB_CONTRACT"),
    ("TOOL_UNAVAILABLE", "CUEKB_DEPENDENCY"),
    ("CUEKB_TIMEOUT", "CUEKB_DEPENDENCY"),
    ("CUEKB_RETRIEVAL_UNAVAILABLE", "CUEKB_RETRIEVAL_LIMITED"),
])
def test_v2_configuration_and_contract_failures_are_not_reported_as_no_evidence(reason, fault):
    assert classify({"cases": [{"reason_code": reason, "expected_tool": "search_knowledge_base",
                               "input_item_ids": ["i1"]}]})[0] == fault


def test_api_log_summary_allows_one_call_for_each_product_input():
    log = "\n".join((
        "voice_tool_call_received conversation_id=c1 call_id=k1 "
        "tool=search_knowledge_base input_item_id=i1",
        "voice_bridge_started conversation_id=c1 call_id=k1",
        "cuekb_response_validated conversation_id=c1 turn_id=t1 trace_id=z1 "
        "retrieval_status=ok hit_count=3 duration_ms=25",
        "voice_tool_result_submitted conversation_id=c1 turn_id=t1 call_id=k1",
        "voice_tool_call_received conversation_id=c1 call_id=k2 "
        "tool=search_knowledge_base input_item_id=i2",
        "voice_bridge_started conversation_id=c1 call_id=k2",
        "cuekb_response_validated conversation_id=c1 turn_id=t2 trace_id=z2 "
        "retrieval_status=ok hit_count=3 duration_ms=26",
        "voice_tool_result_submitted conversation_id=c1 turn_id=t2 call_id=k2",
        "voice_response_completed conversation_id=c1 response_id=r1 input_item_id=i1 phase=answer samples=1920",
        "voice_response_completed conversation_id=c1 response_id=r2 input_item_id=i2 phase=answer samples=1920",
    ))

    lines, ok = summarize_api_log(log)

    assert ok
    assert "received=2 inputs=2 duplicate_inputs=0 unique_calls=2" in lines[0]


def test_api_log_summary_rejects_a_call_without_a_result():
    log = "\n".join((
        "voice_tool_call_received conversation_id=c1 call_id=k1 "
        "tool=search_knowledge_base input_item_id=i1",
        "voice_tool_call_received conversation_id=c1 call_id=k2 "
        "tool=search_knowledge_base input_item_id=i1",
        "voice_bridge_started conversation_id=c1 call_id=k1",
        "cuekb_response_validated conversation_id=c1 turn_id=t1 trace_id=z1 "
        "retrieval_status=ok hit_count=3 duration_ms=25",
        "voice_tool_result_submitted conversation_id=c1 turn_id=t1 call_id=k1",
    ))

    lines, ok = summarize_api_log(log)

    assert not ok
    assert "received=2 inputs=1 duplicate_inputs=1 unique_calls=2" in lines[0]
    assert lines[-1] == "api_diagnostic=failed fault=TOOL_RESULT_COUNT_MISMATCH"


async def test_single_entrypoint_prints_only_preflight_window_cases_and_classification(
    monkeypatch, capsys, tmp_path,
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
    args = SimpleNamespace(output=tmp_path / "report.json")

    result = await entrypoint.execute(args)
    lines = capsys.readouterr().out.splitlines()

    assert result == 1
    assert len(lines) == 7
    assert lines[0].startswith("preflight=passed provider=nvidia")
    assert "conversation_id=conversation-1" in lines[1]
    assert lines[-1].startswith("validation=failed fault=TOOL_NOT_CALLED")
    stored = json.loads(args.output.read_text())
    assert f"window_start={stored['window_start']} window_end={stored['window_end']}" in lines[1]


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


def test_voicechat_batch_summary_counts_members_separately_from_injections():
    digest = "a" * 64
    log = "\n".join((
        "Prompt prepared: tools_count=1 tool_names=search_knowledge_base "
        f"prompt_chars=6000 prompt_sha256={digest}",
        "System prompt received for sequence 42: tool_names=search_knowledge_base "
        f"chars=6000 sha256={digest}",
        "Function call decoded from model for sequence 42: "
        "tool_names=search_knowledge_base,search_knowledge_base",
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.parsed","calls":2}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.call","action":"queued","input_item_id":"i1"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.call","action":"queued","input_item_id":"i1"}',
        'voice_ws_diagnostic {"sequence_id":42,"event":"function.result","calls":2,"native_batch_id":"b1"}',
    ))
    lines, ok = summarize_voicechat_log(log, "search_knowledge_base")
    assert ok
    assert "function_parsed=2 new_calls=2 new_call_inputs=1 function_result=1" in lines[3]
    assert "tools=search_knowledge_base" in lines[2]
    lines, ok = summarize_voicechat_log(log.replace(
        "tool_names=search_knowledge_base,search_knowledge_base",
        "tool_names=search_knowledge_base,unknown_tool",
    ), "search_knowledge_base")
    assert not ok and "MODEL_FUNCTION_WRONG_NAME" in lines[-1]
    lines, ok = summarize_voicechat_log(log.replace('"calls":2,"native_batch_id"', '"calls":1,"native_batch_id"'), "search_knowledge_base")
    assert not ok and "TOOL_RESULT_COUNT_MISMATCH" in lines[-1]


def test_voice_payload_compacts_wrapping_without_cutting_evidence():
    from types import SimpleNamespace

    from app.agent_runtime.native_cuekb import voice_result
    from app.agent_runtime.native_input import ANSWER_INSTRUCTIONS, FINAL_INSTRUCTIONS

    passages = [
        "Product AX may be shipped only within the city; keep refrigerated at 2-5 C.",
        "After opening, consume within 24 hours. Do not freeze.",
        "Shelf life is 7 days only when unopened and refrigerated.",
    ]
    items = [{"text": text, "title": "Source excerpt", "context": None, "conditions": {}}
             for text in passages]
    items[1].update(title="Opened package", conditions={"product_model": "AX"})
    items[2]["context"] = "This shelf life applies only to the sealed package."
    ready = SimpleNamespace(envelope={"items": items, "partial": False,
        "search_state": "finished", "instructions": ANSWER_INSTRUCTIONS + FINAL_INSTRUCTIONS})
    payload = voice_result(ready)
    assert [item["text"] for item in payload["sources"]] == passages
    assert payload["sources"][1]["title"] == "Opened package"
    assert payload["sources"][1]["conditions"] == {"product_model": "AX"}
    assert payload["sources"][2]["context"] == items[2]["context"]
    # Generic titles carry no facts; no source or qualification is removed to save bytes.
    assert "Source excerpt" not in json.dumps(payload)
    assert payload["partial"] == "false" and payload["search_state"] == "finished"
