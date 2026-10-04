"""Conservative evidence acceptance and executable checks, not semantic proof."""

import re

from app.contracts import printable_ascii


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
            return set(re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)*(?:%|[A-Za-z]+)?", value))
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
                if isinstance(value, str) and value and value.casefold() not in text.casefold():
                    return "VOICE_MISSING_CONDITION"
        if re.search(r"\b(?:not|never|cannot|only|unless)\b", source, re.I) and not re.search(
            r"\b(?:not|never|cannot|only|unless|can't|doesn't|don't)\b", text, re.I
        ):
            return "VOICE_MISSING_CONDITION"
        return None
