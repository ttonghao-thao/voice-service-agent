"""Strict V2 contract checks against an upstream public *synthetic* example.

Fixture provenance: ttonghao-thao/CueKB at
 dac382e282f12ae1c1b89366d5c058725d9f13c6,
 docs/examples/search-v2-response.json. This is not production retrieval evidence.
"""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from app.contracts import Citation
from app.tools.schemas import CueKBSearchRequest, CueKBSearchResponse
from pydantic import ValidationError

FIXTURE = Path(__file__).parents[1] / "fixtures/cuekb-v2-response.json"
KB = "11111111-1111-4111-8111-111111111111"


def response():
    return json.loads(FIXTURE.read_text())


def test_upstream_public_v2_example_preserves_attribution_and_required_context():
    result = CueKBSearchResponse.model_validate(response())
    assert result.schema_version == "2"
    assert result.evidence_status == "unassessed"
    assert result.ranking_method == "keyword_rrf"
    hit = result.hits[0]
    assert hit.score == pytest.approx(1 / 61)
    assert hit.applicability.status == "resolved"
    assert hit.applicability.subjects[0].display_name == "Product A"
    assert hit.context_parts[0].required is True
    assert hit.context_parts[0].source_spans[0].text == hit.source_text
    assert result.diagnostics.reranker.configured is False
    assert result.diagnostics.reranker.executed is False


@pytest.mark.parametrize("path", [
    (), ("hits", 0), ("hits", 0, "applicability"),
    ("hits", 0, "applicability", "subjects", 0), ("hits", 0, "context_parts", 0),
    ("hits", 0, "source_spans", 0), ("diagnostics",), ("diagnostics", "counts"),
])
def test_undeclared_fields_are_not_silently_discarded(path):
    data = response()
    target = data
    for part in path:
        target = target[part]
    target["undeclared_field"] = "not silently ignored"
    with pytest.raises(ValidationError):
        CueKBSearchResponse.model_validate(data)


@pytest.mark.parametrize("field", ["schema_version", "ranking_method", "ranking_version", "diagnostics"])
def test_missing_v2_identity_or_diagnostics_is_not_inferred(field):
    data = response()
    del data[field]
    with pytest.raises(ValidationError):
        CueKBSearchResponse.model_validate(data)


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), -float("inf"), "0.8"])
@pytest.mark.parametrize("field", ["score", "stage_scores"])
def test_scores_are_actual_finite_numbers(bad, field):
    data = response()
    data["hits"][0][field] = {"rrf": bad} if field == "stage_scores" else bad
    with pytest.raises(ValidationError):
        CueKBSearchResponse.model_validate(data)


@pytest.mark.parametrize("fault", ["rank", "ascending_score", "score_type", "status"])
def test_inconsistent_rank_score_or_status_is_rejected(fault):
    data = response()
    if fault == "rank":
        data["hits"][0]["rank"] = 2
    elif fault == "ascending_score":
        extra = deepcopy(data["hits"][0])
        extra.update(rank=2, chunk_id="55555555-5555-4555-8555-555555555556", score=1.0)
        data["hits"].append(extra)
    elif fault == "score_type":
        data["hits"][0]["score_type"] = "rerank"
    else:
        data["retrieval_status"] = "not_found"
    with pytest.raises(ValidationError):
        CueKBSearchResponse.model_validate(data)


def test_empty_degraded_and_not_found_remain_distinct():
    data = response()
    data.update(hits=[], retrieval_status="degraded", degraded_reasons=["vector_unavailable"])
    result = CueKBSearchResponse.model_validate(data)
    assert result.retrieval_status == "degraded" and not result.hits
    data.update(retrieval_status="not_found", degraded_reasons=[])
    assert CueKBSearchResponse.model_validate(data).retrieval_status == "not_found"


def test_verbatim_source_whitespace_survives_wire_and_citation_validation():
    data = response()
    source = "  Input voltage: 12 V.\nDo not exceed 30 C.\n"
    hit = data["hits"][0]
    hit["source_text"] = source
    hit["context"] = source
    hit["source_spans"][0]["text"] = source
    hit["context_parts"][0]["source_text"] = source
    hit["context_parts"][0]["source_spans"][0]["text"] = source
    result = CueKBSearchResponse.model_validate(data)
    assert result.hits[0].source_text == source
    assert result.hits[0].source_spans[0].text == source
    citation = citation_for(result)
    assert citation.content == source
    assert citation.context_parts[0]["source_text"] == source


def citation_for(result):
    hit = result.hits[0]
    return Citation(
        citation_id="C1", document_id=str(hit.document_id), chunk_id=str(hit.chunk_id),
        title=" / ".join(hit.title_path), version_id=str(hit.version_id), content=hit.source_text,
        context_parts=[part.model_dump(mode="json") for part in hit.context_parts],
        trace_id=str(result.trace_id), retrieval_id=str(result.trace_id), rank=hit.rank,
        cuekb_schema_version="2", document_name=hit.document_name, content_sha256=hit.content_sha256,
        score=hit.score, score_type=hit.score_type, applicability=hit.applicability,
        source_spans=hit.source_spans, processing_profile=hit.processing_profile,
        source_unit_id=hit.source_unit_id, block_kind=hit.block_kind, context_state=hit.context_state,
        context_omissions=hit.context_omissions, ranking_method=result.ranking_method,
        ranking_version=result.ranking_version,
    )


def test_v2_citation_round_trip_retains_evidence_not_internal_ids_as_conditions():
    result = CueKBSearchResponse.model_validate(response())
    citation = Citation.model_validate(citation_for(result).model_dump(mode="json"))
    assert citation.cuekb_schema_version == "2"
    assert citation.applicability.subjects[0].display_name == "Product A"
    assert citation.context_state == "complete"
    assert citation.context_parts[0]["required"] is True
    assert citation.metadata == {}


def test_historical_citation_remains_readable_without_inventing_v2_attribution():
    citation = Citation.model_validate({
        "citation_id": "C1", "document_id": "historical-document", "chunk_id": "historical-chunk",
        "title": "Historical excerpt", "version_id": "historical-version", "content": "Known fact.",
        "trace_id": "historical-trace", "retrieval_id": "historical-trace", "rank": 1,
    })
    assert citation.cuekb_schema_version == "1"
    assert citation.applicability.status == "unknown"
    assert citation.context_state is None
    assert citation.score is None


def test_resolved_applicability_requires_a_subject():
    data = response()
    data["hits"][0]["applicability"]["subjects"] = []
    with pytest.raises(ValidationError):
        CueKBSearchResponse.model_validate(data)


def test_v2_request_has_explicit_budgets_and_rejects_undeclared_fields():
    request = CueKBSearchRequest(
        query="Product A shipping, shelf life and storage", kb_ids=[KB],
        max_evidence_bytes=8192, include_diagnostics=False, request_id="bounded-request",
    )
    assert request.top_k == 5
    assert request.max_evidence_bytes == 8192
    assert request.relations.entity_ids == []
    for extras in ({"original_question": "not supported"}, {"kb_ids": [KB, KB]},
                   {"max_evidence_bytes": 4095}, {"relations": {"at": "2026-10-23T00:00:00"}}):
        with pytest.raises(ValidationError):
            CueKBSearchRequest.model_validate({"query": "Product A", "kb_ids": [KB], **extras})
