"""Conservative evidence acceptance and executable checks, not semantic proof."""

import re

from app.contracts import PresentationContract, printable_ascii

PORTAL_SPEECH = "Please read the written answer in the portal for the complete conditions."


_NUMBER_WORDS = dict(zip(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(),
    range(20), strict=True))
_NUMBER_WORDS.update(dict(zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10), strict=True)))
_NUMBER_WORDS.update(hundred=100, thousand=1000, million=1000000)
_NUMBER_PATTERN = re.compile(r"\b(?:" + "|".join(_NUMBER_WORDS) + r")(?:[ -]+(?:" + "|".join(_NUMBER_WORDS) + r"))*\b", re.I)


def normalize_numbers(text):
    """English integer words used by speech must receive the same numeric check."""
    def convert(match):
        total = current = 0
        for word in re.split(r"[ -]+", match.group().lower()):
            if word == "and":
                continue
            value = _NUMBER_WORDS[word]
            if value == 100:
                current = max(1, current) * value
            elif value >= 1000:
                total += max(1, current) * value
                current = 0
            else:
                current += value
        return str(total + current)
    digits = "|".join(list(_NUMBER_WORDS)[:10])
    # Consume fractional digits before cardinal grouping (seven five is .75,
    # not the cardinal sum twelve).
    text = re.sub(r"\bpoint ((?:" + digits + r")(?:[ -]+(?:" + digits + r"))*)\b",
                  lambda match: "point " + "".join(str(_NUMBER_WORDS[word])
                      for word in re.split(r"[ -]+", match[1].lower())), text, flags=re.I)
    # Cardinal "and" joins a scale (one hundred and five), not a list
    # of independent facts (forty-five and three must not become 48).
    text = re.sub(r"\b(hundred|thousand|million)\s+and\s+(?=(?:" + "|".join(_NUMBER_WORDS) + r")\b)",
                  r"\1 ", text, flags=re.I)
    text = _NUMBER_PATTERN.sub(convert, text)
    text = re.sub(r"\b(\d+) point (\d+)\b", r"\1.\2", text, flags=re.I)
    return re.sub(r"(\d)\s+percent\b", r"\1%", text, flags=re.I)



def contains_condition(text, value):
    return bool(re.search(r"(?<![A-Za-z0-9_.-])" + re.escape(value) + r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9])", text, re.I))


def presentation_contract(mode, conditions=None):
    return PresentationContract(mode=mode, required_conditions={
        key: value for key, value in (conditions or {}).items()
        if key in ("product_model", "software_version") and isinstance(value, str) and value
    })


class EvidenceGate:
    @staticmethod
    def missing_conditions(text, evidence, required=None):
        """Explain lexical failures; these checks are not semantic proof."""
        failures = []
        for item in evidence:
            for key in ("product_model", "software_version"):
                value = item.metadata.get(key)
                if isinstance(value, str) and value and not contains_condition(text, value):
                    failures.append({"rule": "source_metadata", "citation_id": item.citation_id,
                                     "field": key, "value": value})
        signals = {"negation": r"\b(?:not|never|cannot|can't|doesn't|don't)\b",
                   "only": r"\bonly\b", "unless": r"\bunless\b"}
        for name, pattern in signals.items():
            if re.search(pattern, text, re.I):
                continue
            for item in evidence:
                fields = {"content": item.content, "context": item.context or "",
                          **{key: str(item.metadata.get(key, ""))
                             for key in ("product_model", "software_version")}}
                matched = False
                for key, value in fields.items():
                    found = re.search(pattern, value, re.I)
                    if found:
                        failures.append({"rule": "source_signal", "citation_id": item.citation_id,
                                         "field": key, "value": name,
                                         "excerpt": value[max(0, found.start() - 60):found.end() + 100]})
                        matched = True
                        break
                if matched:
                    break
        for key, value in (required or {}).items():
            if not contains_condition(text, value):
                failures.append({"rule": "required_condition", "field": key, "value": value})
        return failures

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
        text, source = normalize_numbers(text), normalize_numbers(source)
        # Reject new numeric facts; this deliberately does not certify paraphrases.
        def numbers(value):
            return set(re.findall(r"(?<![A-Za-z0-9])\d+(?:\.\d+)*", value))
        if numbers(re.sub(r"\[C\d+\]", "", text)) - numbers(source):
            return "VOICE_UNSUPPORTED_NUMBER"
        def quantities(value):
            aliases = {
                "ms": "ms", "millisecond": "ms", "milliseconds": "ms",
                "second": "second", "seconds": "second", "minute": "minute", "minutes": "minute",
                "hour": "hour", "hours": "hour", "day": "day", "days": "day",
                "g": "gram", "gram": "gram", "grams": "gram", "kg": "kilogram", "kilograms": "kilogram",
                "v": "volt", "volt": "volt", "volts": "volt", "w": "watt", "watt": "watt", "watts": "watt",
                "a": "amp", "amp": "amp", "amps": "amp", "ampere": "amp", "amperes": "amp",
                "meter": "meter", "meters": "meter", "metre": "meter", "metres": "meter",
                "degree": "degree", "degrees": "degree", "connection": "connection", "connections": "connection",
                "mbps": "mbps", "gbps": "gbps", "mhz": "mhz", "ghz": "ghz", "%": "%",
                "usd": "usd", "dollars": "usd", "$": "usd", "rmb": "cny", "cny": "cny",
            }
            units = "|".join(re.escape(unit) for unit in sorted(aliases, key=len, reverse=True))
            suffix = re.findall(r"\b(\d+(?:\.\d+)?)\s*(" + units + r")(?![A-Za-z])", value, re.I)
            prefix = re.findall(r"(?<![A-Za-z])(" + units + r")\s*(\d+(?:\.\d+)?)\b", value, re.I)
            return {(number, aliases[unit.casefold()]) for number, unit in suffix} | {
                (number, aliases[unit.casefold()]) for unit, number in prefix}
        if quantities(text) - quantities(source):
            return "VOICE_UNSUPPORTED_UNIT"
        # Do not allow a number present elsewhere in the source to migrate
        # into a different explicit condition (e.g. sealed vs after opening).
        for condition in (r"after opening|once opened", r"sealed|unopened"):
            claimed = [part for part in re.split(r"[.;!?]", text)
                       if re.search(condition, part, re.I)]
            supported = [part for part in re.split(r"[.;!?]", source)
                         if re.search(condition, part, re.I)]
            if claimed and supported:
                scoped_source = " ".join(supported)
                for claim in claimed:
                    if numbers(claim) - numbers(scoped_source):
                        return "VOICE_MISSING_CONDITION"
                    if (re.search(r"refrigerat", scoped_source, re.I)
                        and re.search(r"room temperature|unrefrigerated", claim, re.I)):
                        return "VOICE_MISSING_CONDITION"
        if EvidenceGate.missing_conditions(text, evidence):
            return "VOICE_MISSING_CONDITION"
        return None

    @staticmethod
    def check_disposition(text, decision):
        """No-evidence outcomes authorize a refusal or clarification, not facts."""
        code = EvidenceGate.check_spoken(text, [])
        if code:
            return code
        clauses = [clause.strip() for clause in re.split(r"(?<=[.!?])\s+|;|\bbut\b|\bhowever\b", text, flags=re.I)
                   if clause.strip()]
        refusal = re.compile(r"\b(?:cannot|can't|could not|unable to|do not know|don't know|no verified|no reliable|insufficient|not enough)\b", re.I)
        clarify = re.compile(r"^(?:(?:which|what)\b.*\?|(?:can|could) you (?:specify|clarify|provide|select)\b|please (?:specify|clarify|provide|select)\b)", re.I)
        if decision not in {"insufficient", "clarify"} or not clauses or any(
            not refusal.search(clause) and not clarify.search(clause) for clause in clauses
        ):
            return "VOICE_ANSWER_UNGROUNDED"
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
