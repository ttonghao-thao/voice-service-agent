"""Exact final function-channel admission, measured by the running VoiceChat.

Never truncates source statements or conditions. The provider's raw assessment
remains auditable; only whole groups may be withheld from the spoken packet.
"""

import copy
import json
from dataclasses import dataclass

from app.agent_runtime.native_cuekb import compact_voice_result
from app.contracts import DomainError


@dataclass(frozen=True)
class VoicePacket:
    text: str
    token_count: int
    tokenizer_id: str
    retained_ids: tuple[str, ...] = ()
    omitted_ids: tuple[str, ...] = ()


def serialize_packet(result):
    return json.dumps(compact_voice_result(result), ensure_ascii=True, separators=(",", ":"))


async def measure_packet(result, measure, *, max_tokens, max_bytes):
    text = serialize_packet(result)
    if len(text.encode()) > max_bytes:
        raise DomainError("VOICE_EVIDENCE_BUDGET", "The complete tool result exceeds its byte budget.", 502)
    receipt = await measure(text)
    if receipt["token_count"] > max_tokens:
        raise DomainError("VOICE_EVIDENCE_BUDGET", "The complete tool result exceeds its token budget.", 502)
    return VoicePacket(text, receipt["token_count"], receipt["tokenizer_id"])


def _atomic_groups(ids, conflicts):
    """Contradictory groups travel together; omission must not resolve a conflict."""
    bundles = [{group_id} for group_id in ids]
    for conflict in conflicts:
        connected = set(conflict["evidence_ids"])
        touched = [bundle for bundle in bundles if connected & bundle]
        if touched:
            connected.update(set.union(*touched))
            bundles = [bundle for bundle in bundles if bundle not in touched]
            bundles.append(connected)
    return sorted(bundles, key=lambda bundle: min(ids.index(group_id) for group_id in bundle))


async def fit_voice_result(ready, result, measure, *, max_tokens, max_bytes):
    """Measure each candidate exactly, admit whole groups, then update observer.

    ``ready`` is updated in place only after a measured packet fits. The answer
    observer must read its admitted citations after its answer future resolves.
    At most seven preflights are needed for the contract's five evidence groups.
    No CueKB retrieval or synthesis request is performed here.
    """
    if ready.envelope.get("schema_version") != "cuekb-evidence-v2.1":
        return await measure_packet(result, measure, max_tokens=max_tokens, max_bytes=max_bytes)
    envelope = ready.envelope
    original_items = envelope["items"]
    original_citations = ready.citations
    model_items = result["evidence"]
    ids = [item["id"] for item in model_items]
    if len(ids) != len(original_items) or len(ids) != len(original_citations):
        raise DomainError("VOICE_PROTOCOL_ERROR", "Voice evidence identities are inconsistent.", 502)
    supports = envelope.get("group_supports", {})
    facets = envelope.get("facets", [])
    provider_decision = envelope["decision"]
    identity = None
    measured_cache = {}

    async def candidate(packet):
        nonlocal identity
        text = serialize_packet(packet)
        if len(text.encode()) > max_bytes:
            return None
        if text not in measured_cache:
            receipt = await measure(text)
            if identity is not None and receipt["tokenizer_id"] != identity:
                raise DomainError("VOICE_PROTOCOL_ERROR", "Voice tokenizer changed during evidence admission.", 502)
            identity = receipt["tokenizer_id"]
            measured_cache[text] = receipt
        receipt = measured_cache[text]
        return VoicePacket(text, receipt["token_count"], receipt["tokenizer_id"]) if receipt["token_count"] <= max_tokens else None

    def projection(selected):
        packet = copy.deepcopy(result)
        packet["evidence"] = [item for item in model_items if item["id"] in selected]
        covered = {facet for group_id in selected for facet in supports.get(group_id, [])}
        missing = [facet["text"] for facet in facets
                   if facet["support"] not in ("direct", "entailed") or facet["id"] not in covered]
        packet["missing"] = missing or result.get("missing", [])
        if provider_decision == "answer" and packet["missing"]:
            packet["decision"] = "partial_answer" if selected else "insufficient"
        elif provider_decision == "partial_answer" and not selected:
            packet["decision"] = "insufficient"
        if not selected:
            # A readable whole-question limit message is bounded independently
            # of long facet text. Exact omissions remain in API audit below.
            packet["decision"] = "insufficient"
            packet["missing"] = ["Requested details could not fit the voice evidence budget."]
            packet.pop("clarification", None)
            packet["instructions"] = "Say: I cannot confirm the requested information from the available documentation. Do not call tools."
        return packet

    chosen_result = copy.deepcopy(result)
    fitted = await candidate(chosen_result)
    budget_limited = fitted is None
    selected = set(ids)
    if fitted is None:
        selected = set()
        fitted = None
        for bundle in _atomic_groups(ids, envelope.get("conflicts", [])):
            proposed = selected | bundle
            packet = projection(proposed)
            measured = await candidate(packet)
            if measured is not None:
                selected, fitted, chosen_result = proposed, measured, packet
        if fitted is None:
            chosen_result = projection(set())
            fitted = await candidate(chosen_result)
        if fitted is None:
            raise DomainError("VOICE_EVIDENCE_BUDGET", "The terminal voice result cannot fit the configured budget.", 502)
    retained = tuple(group_id for group_id in ids if group_id in selected)
    omitted = tuple(group_id for group_id in ids if group_id not in selected)
    envelope.setdefault("provider_decision", provider_decision)
    envelope.setdefault("provider_model_view", copy.deepcopy(envelope["model_view"]))
    envelope["decision"] = chosen_result["decision"]
    envelope["clarification"] = copy.deepcopy(chosen_result.get("clarification"))
    envelope["model_view"] = {key: copy.deepcopy(chosen_result[key]) for key in ("decision", "evidence", "missing")}
    envelope["model_view"]["search"] = "finished"
    envelope["items"] = [item for group_id, item in zip(ids, original_items, strict=True) if group_id in selected]
    ready.citations = [citation for group_id, citation in zip(ids, original_citations, strict=True) if group_id in selected]
    envelope["wire_admission"] = {"retained_ids": list(retained), "omitted_ids": list(omitted),
                                  "token_count": fitted.token_count, "tokenizer_id": fitted.tokenizer_id,
                                  "token_budget": max_tokens, "byte_count": len(fitted.text.encode()), "budget_limited": budget_limited,
                                  "serialization": "nemo-tool-response-v1"}
    return VoicePacket(fitted.text, fitted.token_count, fitted.tokenizer_id, retained, omitted)
