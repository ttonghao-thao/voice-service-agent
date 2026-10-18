"""Acceptance reporting must not conflate routing, arguments, sources and speech."""

import json
from pathlib import Path

import pytest

from scripts.validate_native_cuekb import assess, load_cases


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
