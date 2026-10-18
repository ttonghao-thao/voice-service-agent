"""Acceptance reporting must not conflate routing, arguments, sources and speech."""

import json
from pathlib import Path

import pytest

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


def test_unusable_voice_evidence_is_not_reported_as_retrieval_passed():
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

    assert result["retrieval"] == "insufficient_evidence"


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
