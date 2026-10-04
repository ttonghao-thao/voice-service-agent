import copy

import pytest

from scripts.score_voice_evaluation import summarize

VERSIONS = {"application": "app-v1", "voice": "voice-v1", "cuekb": "kb-v1",
            "text_model": "text-v1", "context_schema": "task-context-v1"}
MANIFEST = {"cases": [{"case_id": "C01", "tool_steps": {"question": "lookup_knowledge"},
                       "required_metrics": ["tool_selection_correct", "context_correct"]}]}


def result(**kwargs):
    return {"case_id": "C01", "real_service": False, "evidence_mode": "simulated_independent_apis",
            "versions": dict(VERSIONS), "context_correct": True, "status": "completed",
            "tool_observations": [{"step_id": "question", "actual_tool": "lookup_knowledge"}], **kwargs}


def test_simulation_cannot_inflate_real_model_accuracy():
    real = summarize([result()], MANIFEST)
    assert real["eligible_cases"] == 0 and not real["complete"]
    assert real["metrics"]["context_correct"]["rate"] is None
    assert real["coverage"]["missing_case_ids"] == ["C01"]
    simulated = summarize([result()], MANIFEST, mode="simulation")
    assert simulated["complete"] and simulated["metrics"]["context_correct"]["rate"] == 1


def test_missing_observation_is_not_reported_as_a_model_missing_tool():
    report = summarize([result(tool_observations=[])], MANIFEST, mode="simulation")
    assert not report["complete"]
    assert report["tool_errors"]["missing_observations"] == 1
    assert report["tool_errors"]["missing_tool"] == 0
    assert report["metrics"]["tool_selection_correct"]["evaluated"] == 0


@pytest.mark.parametrize("expected,actual,category", [
    ("lookup_knowledge", None, "missing_tool"),
    (None, "lookup_knowledge", "unexpected_tool"),
    ("lookup_knowledge", "reason_over_knowledge", "wrong_tool"),
])
def test_tool_error_categories_have_separate_observed_denominators(expected, actual, category):
    manifest = copy.deepcopy(MANIFEST)
    manifest["cases"][0]["tool_steps"]["question"] = expected
    report = summarize([result(tool_observations=[{"step_id": "question", "actual_tool": actual}])],
                       manifest, mode="simulation")
    assert report["tool_errors"][category] == report["tool_errors"]["observed_steps"] == 1
    assert report["metrics"]["tool_selection_correct"]["passed"] == 0


def test_corpus_reports_missing_cases_and_metrics_instead_of_counting_them_as_passed():
    manifest = copy.deepcopy(MANIFEST)
    manifest["cases"].append({**manifest["cases"][0], "case_id": "C02"})
    report = summarize([result(context_correct=None)], manifest, mode="simulation")
    assert report["coverage"]["missing_case_ids"] == ["C02"]
    assert report["coverage"]["missing_metrics"]["C01"] == ["context_correct"]
    assert not report["complete"]


def test_versions_and_failed_runs_prevent_a_complete_claim():
    manifest = copy.deepcopy(MANIFEST)
    manifest["cases"].append({**manifest["cases"][0], "case_id": "C02"})
    second = result(case_id="C02", versions={**VERSIONS, "voice": "different-voice"})
    report = summarize([result(), second], manifest, mode="simulation")
    assert report["versions"]["group_count"] == 2 and not report["complete"]
    report = summarize([result(status="failed")], MANIFEST, mode="simulation")
    assert report["coverage"]["failed_case_ids"] == ["C01"] and not report["complete"]


@pytest.mark.parametrize("rows", [[result(), result()], [result(stale_result_leaks=-1)],
    [result(tool_observations=[{"step_id": "question", "actual_tool": None}] * 2)]])
def test_invalid_results_are_rejected(rows):
    with pytest.raises(ValueError):
        summarize(rows, MANIFEST, mode="simulation")
