"""Function-history reuse must not drop new facts or cross an input boundary."""

from app.agent_runtime.direct import EvidenceReady
from app.agent_runtime.native_input import NativeInputSearch


def ready(*items):
    return EvidenceReady({"items": list(items), "partial": False,
                          "search_state": "finished", "instructions": "Answer from all returned excerpts."},
                         [], 0, "test")


def test_batch_construction_does_not_mark_excerpts_delivered_and_conditions_remain_distinct():
    search = NativeInputSearch(2)
    first = {"text": "Store for five days only when sealed.", "conditions": {"product_model": "AX"}}
    second = {"text": first["text"], "conditions": {"product_model": "BX"}}
    search.voice_result(ready(first))  # A call in a still-unsubmitted batch.
    wire = search.voice_result(ready(first, second))
    assert wire["sources"] == [first, second]
    search.remember_delivery(wire)
    assert search.voice_result(ready(first, second))["sources"] == []
    third = {"text": "Consume within two hours after opening.", "conditions": {"product_model": "AX"}}
    assert search.voice_result(ready(first, second, third))["sources"] == [third]
    assert NativeInputSearch(2).voice_result(ready(first))["sources"] == [first]


def test_reviewed_cumulative_evidence_is_not_replaced_by_its_smaller_wire_update():
    search = NativeInputSearch(2)
    initial = {"text": "AX has eighteen pastry layers."}
    added = {"text": "AX costs twenty dollars only in store."}
    search.remember_delivery(search.voice_result(ready(initial)))
    cumulative = ready(initial, added)
    assert search.voice_result(cumulative)["sources"] == [added]
    assert cumulative.envelope["items"] == [initial, added]
