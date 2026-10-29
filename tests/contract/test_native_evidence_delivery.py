"""Function-history reuse must not drop new facts or cross an input boundary."""

from app.agent_runtime.direct import EvidenceReady
from app.agent_runtime.native_cuekb import compact_voice_result
from app.agent_runtime.native_input import NativeInputSearch


def test_wire_compaction_is_lossless_and_preserves_dedup_and_distinct_scopes():
    import copy
    import json

    scope = {"status": "resolved", "subjects": [{"display_name": "Product AX", "source_kind": "curated"}],
             "conditions": [{"text": "Only when refrigerated", "source_kind": "curated"}]}
    items = [{"text": text, "title": "Product AX", "applicability": scope,
              "context": [{"text": "Do not freeze.", "applicability": {"status": "unknown"}}]}
             for text in ("Store for five days.", "Consume within two hours after opening.", "Ships in sealed packaging.")]
    search = NativeInputSearch(2)
    full = search.voice_result(ready(*items))
    untouched = copy.deepcopy(full)
    wire = compact_voice_result(full)
    restored = [{**wire.get("source_defaults", {}), **source} for source in wire["sources"]]
    assert restored == full["sources"] and full == untouched
    assert len(json.dumps(wire)) < len(json.dumps(full)) * .8
    search.remember_delivery(full)
    assert not search.voice_result(ready(*items))["sources"]
    mixed = copy.deepcopy(full)
    mixed["sources"][1]["applicability"] = {"status": "ambiguous", "subjects": [], "conditions": []}
    compact = compact_voice_result(mixed)
    assert "applicability" not in compact.get("source_defaults", {})
    assert [{**compact.get("source_defaults", {}), **source} for source in compact["sources"]] == mixed["sources"]


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
