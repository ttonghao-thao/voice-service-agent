import pytest
from app.task_context import (
    bind_arguments,
    empty_state,
    observe,
    resolved_request,
    snapshot,
    task_status,
    values,
)


def source(item, revision=0):
    return {"input_item_id": item, "epoch": 1, "request_revision": revision, "channel": "voice"}


def test_user_correction_is_sourced_and_invalidates_the_previous_version():
    first = observe({}, "My model is AX100, software version 2.1.", source("first"))
    assert values(first) == {"product_model": "AX100", "software_version": "2.1"}
    second = observe(first, "Actually use AX200 instead.", source("correction", 1))
    assert values(second) == {"product_model": "AX200"}
    assert second["conditions"]["product_model"]["source"]["input_item_id"] == "correction"
    assert second["changes"][-2]["field"] == "software_version"
    assert second["changes"][-2]["reason"] == "model_changed"
    assert values(first)["software_version"] == "2.1"  # Inputs/snapshots do not share mutable state.


@pytest.mark.parametrize("text", [
    'For example, my model is AX200.', 'If my model is AX200, what happens?',
    'Someone said "my model is AX200".', 'Compare model AX200 and AX300.',
])
def test_hypothetical_and_quoted_conditions_do_not_override_the_user(text):
    initial = observe({}, "My model is AX100.", source("first"))
    result = observe(initial, text, source("later"))
    assert values(result) == {"product_model": "AX100"}


@pytest.mark.parametrize("text", ["My model is AX200 or AX300.", "Wrong model.", "Not this model.",
                                  "My model is not AX100.", "I no longer use AX100."])
def test_ambiguous_or_retracted_model_clarifies_and_removes_old_version(text):
    initial = observe({}, "My model is AX100, version 2.1.", source("first"))
    result = observe(initial, text, source("later"))
    assert values(result) == {}
    assert result["unresolved"] == ["product_model"]


def test_arguments_cannot_restore_a_corrected_model_or_fabricate_a_version():
    state = observe({}, "Actually use AX200 instead.", source("correction"))
    state, errors = bind_arguments(state, {"product_model": "AX100", "software_version": "3.0"},
                                   "Actually use AX200 instead.", state["inputs"][0]["source"])
    assert set(errors) == {"product_model", "software_version"}
    assert values(state) == {"product_model": "AX200"}


def test_same_final_input_is_idempotent_and_canceled_task_is_not_implicitly_resumed():
    state = observe(empty_state(), "Find the AX100 connection limit", source("first"))
    state["inputs"][0].update(kind="knowledge", turn_id="old-task")
    assert observe(state, "Find the AX100 connection limit", source("first")) == state
    state = task_status(state, "old-task", "canceled", "user_cancelled")
    state = observe(state, "What about it?", source("next"))
    task = snapshot(state, "What about it?", state["inputs"][-1]["source"], turn_id="new-task",
                    revision=2, epoch=1, kb_ids=[], deadline_at="deadline", parent_task_id=None)
    assert task["original_request"] == task["resolved_request"] == "What about it?"
    assert task["evidence_policy"] == "Only evidence retrieved for this task is authoritative."


def test_context_never_truncates_a_full_length_final_input_to_fit_cuekb():
    state = observe({}, "My model is AX100.", source("first"))
    text = "x" * 2000
    assert resolved_request(text, state) == text
    assert values(state)["product_model"] == "AX100"


def test_current_correction_does_not_send_obsolete_conditions_back_to_retrieval():
    state = observe({}, "Find the limit for model AX100, version 2.1.", source("first"))
    state["inputs"][0].update(kind="knowledge", turn_id="first-task")
    state = observe(state, "Actually use AX200 instead.", source("second", 1))
    task = snapshot(state, "Actually use AX200 instead.", state["inputs"][-1]["source"],
                    turn_id="second-task", revision=2, epoch=1, kb_ids=[], deadline_at=None, parent_task_id="first-task")
    assert "AX100" not in task["resolved_request"] and "2.1" not in task["resolved_request"]
    assert task["original_request"] == "Actually use AX200 instead."
    assert task["recent_inputs"][0]["text"] == "Find the limit for model AX100, version 2.1."


def test_comparison_keeps_user_profile_as_background_without_forcing_a_single_filter():
    state = observe({}, "My model is AX100, version 2.1.", source("first"))
    question = "Compare models AX200 and AX300."
    state = observe(state, question, source("second"))
    task = snapshot(state, question, state["inputs"][-1]["source"], turn_id="comparison", revision=1,
                    epoch=1, kb_ids=[], deadline_at=None, parent_task_id=None)
    assert not task["conditions"] and task["resolved_request"] == question
    assert task["background_conditions"]["product_model"]["value"] == "AX100"
    assert values(state) == {"product_model": "AX100", "software_version": "2.1"}
