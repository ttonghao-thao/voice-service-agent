"""Project authoritative CueKB evidence into complete, readable model groups.

Ranking/provenance stay in citations. Product scope is never inferred from a
query, document tag, nearby hit, or score. Optional neighbors are not required
to synthesize an answer and do not consume the native function-channel budget.
"""

from app.evidence_types import CueKBApplicability

# Registered policy is shared by initial admission and the per-input merger so
# evidence always leaves room for the real terminal result, not just raw text.
ANSWER_INSTRUCTIONS = (
    "Use all excerpts returned for this question, including earlier results. Answer every requested "
    "detail or group unconfirmed details in one brief statement. Keep facts with their product and conditions. "
)
FINAL_INSTRUCTIONS = "Search finished. Do not call another tool. Answer now."


class IncompleteEvidence(ValueError):
    pass


def compact_applicability(scope):
    """Keep readable scope and attribution type; no entity IDs or duplicated spans."""
    result = {
        "status": scope.status,
        "subjects": [
            {key: value for key, value in {
                "display_name": subject.display_name,
                "product_model": subject.product_model,
                "software_version": subject.software_version,
                "source_kind": subject.source_kind,
            }.items() if value}
            for subject in scope.subjects
        ],
        "conditions": [{"text": condition.text, "source_kind": condition.source_kind}
                       for condition in scope.conditions],
    }
    if scope.reason_codes:
        result["reason_codes"] = list(scope.reason_codes)
    return result


def evidence_item(citation):
    """Return one indivisible fact + required context + applicability group.

    Legacy citations remain readable, but V2 never uses the old primary-chunk
    echo rule to certify that missing required context is safe to discard.
    """
    item = {"citation_id": citation.citation_id, "text": citation.content,
            "title": citation.title, "document_id": citation.document_id,
            "chunk_id": citation.chunk_id, "version_id": citation.version_id,
            "context": citation.context, "conditions": citation.metadata}
    if citation.cuekb_schema_version != "2":
        return item
    if citation.dependency_issues or citation.context_state not in ("complete", "truncated", "disabled"):
        raise IncompleteEvidence("unresolved")
    if citation.block_kind == "heading":
        raise IncompleteEvidence("heading")
    parts = citation.context_parts
    primary = [part for part in parts if str(part.get("chunk_id")) == citation.chunk_id]
    if parts and (len(primary) != 1 or primary[0].get("source_text") != citation.content):
        raise IncompleteEvidence("unresolved")
    required = [part for part in parts
                if part.get("required") and str(part.get("chunk_id")) != citation.chunk_id]
    if citation.context_state == "disabled" and required:
        raise IncompleteEvidence("unresolved")
    # These are independent, complete source chunks, never the truncated
    # display-only context string. Keep original order and complete conditions.
    item.update(
        context="\n\n".join(part["source_text"] for part in required) or None,
        conditions={},
        applicability=compact_applicability(citation.applicability),
        context_state=citation.context_state,
        required_context_chunks=len(required),
    )
    if citation.fact_kind != "statement":
        item["fact_kind"] = citation.fact_kind
    if required:
        # Required parts carry their own scope. Flattened text under the main
        # fact's resolved product used to hide unknown/conflicting attribution.
        # Raw spans and link_provenance stay in Citation for audit, not speech.
        item["required_context"] = [
            {"text": part["source_text"], "role": part.get("role"),
             "applicability": compact_applicability(CueKBApplicability.model_validate(part.get("applicability") or {}))}
            for part in required
        ]
    return item


def model_source(item):
    source = {"text": item["text"]}
    for key in ("title", "context", "conditions", "applicability", "fact_kind"):
        value = item.get(key)
        if key == "context" and item.get("required_context"):
            value = item["required_context"]
        if key == "title" and value == "Source excerpt":
            continue
        if value and value != item["text"]:
            source[key] = value
    return source


def scope_text(item):
    """Facts explicitly delivered in scope are also available to lexical checks."""
    scope = item.get("applicability") or {}
    return " ".join([
        *(str(subject.get(key) or "") for subject in scope.get("subjects", [])
          for key in ("display_name", "product_model", "software_version")),
        *(condition["text"] for condition in scope.get("conditions", [])),
    ])
