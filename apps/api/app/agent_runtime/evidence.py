"""Conservative evidence acceptance and executable checks, not semantic proof."""

import re

from app.contracts import PresentationContract, printable_ascii

PORTAL_SPEECH = "Please read the written answer in the portal for the complete conditions."


def contains_condition(text, value):
    return bool(re.search(r"(?<![A-Za-z0-9_.-])" + re.escape(value) + r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9])", text, re.I))


def presentation_contract(mode, conditions=None):
    return PresentationContract(mode=mode, required_conditions={
        key: value for key, value in (conditions or {}).items()
        if key in ("product_model", "software_version") and isinstance(value, str) and value
    })


class EvidenceGate:
    @staticmethod
    def evaluate(result, ctx):
        if ctx.tool_errors or result.get("status") == "failed":
            return "failed"
        if result.get("status") == "needs_clarification":
            return "clarify"
        if result.get("status") == "not_found" or not result.get("hits"):
            return "no_evidence"
        if (
            result.get("status") != "ok"
            or result.get("evidence_status") != "sufficient"
            or result.get("scope_limited")
            or result.get("hits_omitted")
            or any(hit.get("context_truncated") or hit.get("context_omitted") for hit in result["hits"])
        ):
            return "reason"
        return "ready"

    @staticmethod
    def check_spoken(text, evidence):
        if not text.strip() or len(text) > 8000 or not printable_ascii(text):
            return "VOICE_ANSWER_INVALID"
        references = set(re.findall(r"\[(C\d+)\]", text))
        if references - {item.citation_id for item in evidence}:
            return "RAG_INVALID_CITATION"
        source = " ".join(item.content + " " + (item.context or "") + " " + " ".join(
            str(item.metadata.get(key, "")) for key in ("product_model", "software_version"))
            for item in evidence)
        # Reject new numeric facts; this deliberately does not certify paraphrases.
        def numbers(value):
            return set(re.findall(r"(?<![A-Za-z0-9])\d+(?:\.\d+)*(?:%|[A-Za-z]+)?", value))
        if numbers(re.sub(r"\[C\d+\]", "", text)) - numbers(source):
            return "VOICE_UNSUPPORTED_NUMBER"
        def quantities(value):
            return set(re.findall(r"\b\d+(?:\.\d+)?\s*(?:ms|seconds?|minutes?|hours?|days?|"
                                  r"mbps|gbps|mhz|ghz|volts?|watts?|meters?|metres?|degrees?)\b", value, re.I))
        if {q.casefold() for q in quantities(text)} - {q.casefold() for q in quantities(source)}:
            return "VOICE_UNSUPPORTED_UNIT"
        for item in evidence:
            for key in ("product_model", "software_version"):
                value = item.metadata.get(key)
                if isinstance(value, str) and value and not contains_condition(text, value):
                    return "VOICE_MISSING_CONDITION"
        signals = (r"\b(?:not|never|cannot|can't|doesn't|don't)\b", r"\bonly\b", r"\bunless\b")
        if any(re.search(signal, source, re.I) and not re.search(signal, text, re.I) for signal in signals):
            return "VOICE_MISSING_CONDITION"
        return None

    @staticmethod
    def check_presentation(text, contract, evidence=(), approved_text=None):
        if contract.mode == "verbatim":
            # Punctuation/case may change; numbers and their attached units may not.
            def normalize(value):
                return re.sub(r"\s+", " ", re.sub(r"[,.!?;:]($|\s)", r"\1", value.casefold())).strip()
            if not text.strip() or not printable_ascii(text) or normalize(text) != normalize(approved_text or ""):
                return "VOICE_SPEECH_MISMATCH"
            return None
        code = EvidenceGate.check_spoken(text, evidence)
        if code:
            return code
        if any(not contains_condition(text, value) for value in contract.required_conditions.values()):
            return "VOICE_MISSING_CONDITION"
        return None

    @staticmethod
    def prepare(bundle, conditions=None):
        """Short speech must retain known conditions or direct to the full answer."""
        contract = presentation_contract("verbatim", conditions)
        if not printable_ascii(bundle.speech_text) or bundle.speech_language != "en-US":
            bundle.speech_text, bundle.speech_language = PORTAL_SPEECH, "en-US"
        if bundle.status == "answered" and bundle.citations:
            if (EvidenceGate.check_spoken(bundle.speech_text, bundle.citations)
                or any(not contains_condition(bundle.speech_text, value)
                       for value in contract.required_conditions.values())):
                bundle.speech_text = PORTAL_SPEECH
        bundle.presentation = contract
        return bundle
