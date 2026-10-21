import pytest
from app.contracts import DomainError
from app.voice.batches import ToolBatches
from app.voice.provider import normalize


def member(index=0, size=2, **overrides):
    event = {"type": "response.function_call_arguments.done", "call_id": f"c{index}",
             "response_id": "r", "name": "search_knowledge_base", "arguments": '{"query":"x"}',
             "native_batch_id": "b", "native_batch_size": size, "output_index": index, **overrides}
    return normalize(event).payload


def test_explicit_batch_preserves_receipt_state_and_order():
    batches = ToolBatches()
    assert batches.append(member(), "input", False) is None
    batch = batches.append(member(1), "input", True)
    assert batch.after_terminal is False
    assert [c["call_id"] for c in batch.calls] == ["c0", "c1"]
    assert not batches.pending
    with pytest.raises(DomainError, match="VOICE_PROTOCOL_ERROR"):
        batches.append(member(), "input", False)


@pytest.mark.parametrize("overrides", [
    {"native_batch_size": True}, {"native_batch_size": 17}, {"native_batch_size": 0},
    {"native_batch_id": ""}, {"output_index": -1}, {"output_index": 2}, {"output_index": False},
])
def test_invalid_wire_batch_metadata_is_rejected(overrides):
    with pytest.raises(DomainError):
        member(**overrides)


def test_mixed_inputs_and_missing_members_fail_explicitly():
    batches = ToolBatches()
    batches.append(member(), "input", False)
    with pytest.raises(DomainError, match="VOICE_PROTOCOL_ERROR"):
        batches.append(member(1), "other", False)
    batches.pending["b"].started -= 6
    with pytest.raises(DomainError, match="VOICE_PROTOCOL_ERROR"):
        batches.check_timeout()


def test_no_timing_based_coalescing_of_legacy_calls():
    batches = ToolBatches()
    payload = {"call_id": "c", "name": "search_knowledge_base", "arguments": '{}'}
    assert batches.append(payload, "input", True).after_terminal is True
    assert not batches.pending
