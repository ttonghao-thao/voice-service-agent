import math
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from app.contracts import Citation, StrictModel
from app.evidence_types import (
    ContextOmission,
    ContextRole,
    ContextState,
    CueKBApplicability,
    CueKBSourceSpan,
    EvidenceModel,
    RankingMethod,
    SourceAnchor,
)
from pydantic import Field, FiniteFloat, field_validator, model_validator


class CueKBSearchInput(StrictModel):
    query: str = Field(min_length=1, max_length=2000)
    product_model: str | None = Field(default=None, min_length=1, max_length=120)
    software_version: str | None = Field(default=None, min_length=1, max_length=120)


class CueKBSearchFilters(StrictModel):
    document_ids: list[UUID] = Field(default_factory=list, max_length=100)
    product_model: str | None = None
    software_version: str | None = None
    category: str | None = None


RelationType = Literal[
    "belongs_to", "adjacent_to", "alias_of", "revises", "replaces", "references", "depends_on", "applies_to"
]


class CueKBRelationQuery(StrictModel):
    entity_ids: list[UUID] = Field(default_factory=list, max_length=20)
    types: list[RelationType] = Field(default_factory=list, max_length=8)
    direction: Literal["outgoing", "incoming", "both"] = "outgoing"
    at: datetime | None = None

    @model_validator(mode="after")
    def aware_time(self):
        if self.at is not None and self.at.tzinfo is None:
            raise ValueError("relation time requires a timezone")
        return self


class CueKBSearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=2000)
    kb_ids: list[UUID] = Field(min_length=1, max_length=50)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    mode: Literal["auto", "exact", "hybrid", "related"] = "auto"
    top_k: int = Field(default=5, ge=1, le=20)
    filters: CueKBSearchFilters = Field(default_factory=CueKBSearchFilters)
    include_context: bool = True
    relations: CueKBRelationQuery = Field(default_factory=CueKBRelationQuery)
    max_evidence_bytes: int = Field(default=48000, ge=4096, le=128000)
    include_diagnostics: bool = False

    @model_validator(mode="after")
    def unique_kbs(self):
        if len(set(self.kb_ids)) != len(self.kb_ids):
            raise ValueError("knowledge base IDs must not contain duplicates")
        return self


class CueKBContextPart(EvidenceModel):
    chunk_id: UUID
    source_text: str
    anchor: SourceAnchor
    title_path: list[str]
    source_spans: list[CueKBSourceSpan] = Field(default_factory=list, max_length=64)
    role: ContextRole | None = None
    required: bool = False


class CueKBRelation(EvidenceModel):
    relation_id: UUID
    subject_id: UUID
    object_id: UUID
    relation_type: RelationType
    conditions: dict[str, Any] = Field(default_factory=dict)
    stance: Literal["supports", "refutes"]
    chunk_id: UUID


class CueKBSearchHit(EvidenceModel):
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    rank: int = Field(ge=1, le=20)
    source_text: str = Field(min_length=1)
    context: str | None = None
    title_path: list[str] = Field(default_factory=list)
    anchor: SourceAnchor = Field(default_factory=SourceAnchor)
    metadata: dict[str, Any] = Field(default_factory=dict)
    retrieval_sources: list[str] = Field(default_factory=list)
    document_name: str | None = None
    content_sha256: str | None = None
    score: FiniteFloat
    score_type: Literal["rrf", "rerank"]
    stage_scores: dict[str, FiniteFloat] = Field(default_factory=dict)
    applicability: CueKBApplicability = Field(default_factory=CueKBApplicability)
    source_spans: list[CueKBSourceSpan] = Field(default_factory=list, max_length=64)
    processing_profile: str = Field(min_length=1)
    source_unit_id: str | None = Field(default=None, max_length=200)
    block_kind: Literal["text", "list", "table", "heading"] = "text"
    context_parts: list[CueKBContextPart] = Field(default_factory=list, max_length=21)
    context_state: ContextState
    context_omissions: list[ContextOmission] = Field(default_factory=list, max_length=4)
    context_truncated: bool = False
    relations: list[CueKBRelation] = Field(default_factory=list, max_length=50)

    @field_validator("score", "stage_scores", mode="before")
    @classmethod
    def numeric_scores(cls, value):
        values = value.values() if isinstance(value, dict) else (value,)
        if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in values):
            raise ValueError("scores must be finite numbers")
        return value


class CueKBCandidateCounts(StrictModel):
    keyword: int = Field(default=0, ge=0, le=100)
    vector: int = Field(default=0, ge=0, le=100)
    relation: int = Field(default=0, ge=0, le=50)
    union: int = Field(default=0, ge=0, le=250)
    authoritative: int = Field(default=0, ge=0, le=250)
    duplicate_removed: int = Field(default=0, ge=0, le=250)
    reranked: int = Field(default=0, ge=0, le=100)
    returned: int = Field(default=0, ge=0, le=20)


class CueKBOmissionCounts(StrictModel):
    scope_excluded: int = Field(default=0, ge=0)
    dependency_unresolved: int = Field(default=0, ge=0)
    candidate_limit: int = Field(default=0, ge=0)
    rerank_window_limit: int = Field(default=0, ge=0)
    payload_budget_exhausted: int = Field(default=0, ge=0)
    stale_candidate: int = Field(default=0, ge=0)
    heading_only: int = Field(default=0, ge=0)
    context_budget_omitted: int = Field(default=0, ge=0)


class CueKBPayloadSizes(StrictModel):
    source_text_chars: int = Field(default=0, ge=0)
    source_text_bytes: int = Field(default=0, ge=0)
    context_chars: int = Field(default=0, ge=0)
    context_bytes: int = Field(default=0, ge=0)
    context_parts_chars: int = Field(default=0, ge=0)
    context_parts_bytes: int = Field(default=0, ge=0)
    hits_bytes: int = Field(default=2, ge=2)


class CueKBBackendCalls(StrictModel):
    keyword: int = Field(default=0, ge=0)
    embedding: int = Field(default=0, ge=0)
    vector: int = Field(default=0, ge=0)
    relation: int = Field(default=0, ge=0)
    rerank: int = Field(default=0, ge=0)


class CueKBRerankerDiagnostics(StrictModel):
    configured: bool
    executed: bool
    skipped_reason: Literal[
        "exact_path", "reranker_service_unconfigured", "rerank_disabled", "model_client_unconfigured",
        "not_enough_candidates", "insufficient_remaining_budget"
    ] | None = None


class CueKBSearchDiagnostics(StrictModel):
    counts: CueKBCandidateCounts = Field(default_factory=CueKBCandidateCounts)
    omissions: CueKBOmissionCounts = Field(default_factory=CueKBOmissionCounts)
    sizes: CueKBPayloadSizes = Field(default_factory=CueKBPayloadSizes)
    backend_calls: CueKBBackendCalls = Field(default_factory=CueKBBackendCalls)
    attempt_count: int = Field(default=1, ge=1, le=2)
    reranker: CueKBRerankerDiagnostics
    active_generation: str | None = None
    processing_profiles: list[str] = Field(default_factory=list, max_length=250)
    algorithm_revision: Literal["evidence-ranking-v2.1"] = "evidence-ranking-v2.1"
    keyword_limit: int = Field(ge=1, le=100)
    vector_limit: int = Field(ge=0, le=100)
    relation_limit: int = Field(ge=0, le=50)


class CueKBSearchResponse(EvidenceModel):
    # Required, never infer V2 from an older response with missing fields.
    schema_version: Literal["2"]
    trace_id: UUID
    request_id: str | None = None
    retrieval_status: Literal["ok", "degraded", "not_found", "needs_clarification"]
    evidence_status: Literal["unassessed", "sufficient", "insufficient", "conflicting"] = "unassessed"
    degraded_reasons: list[str] = Field(default_factory=list)
    scope_limited: bool = False
    content_revisions: dict[UUID, int]
    timings_ms: dict[str, float]
    retrieval_path: str = "keyword"
    executed_stages: list[str] = Field(default_factory=list)
    skipped_stages: list[dict[str, str]] = Field(default_factory=list)
    ranking_method: RankingMethod
    ranking_version: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    diagnostics: CueKBSearchDiagnostics
    hits: list[CueKBSearchHit] = Field(max_length=20)

    @model_validator(mode="after")
    def consistent_status(self):
        if self.retrieval_status in ("not_found", "needs_clarification") and self.hits:
            raise ValueError("non-result retrieval statuses cannot contain hits")
        if any(not math.isfinite(value) or value < 0 for value in self.timings_ms.values()):
            raise ValueError("timings_ms must contain finite non-negative values")
        if [hit.rank for hit in self.hits] != list(range(1, len(self.hits) + 1)):
            raise ValueError("hit ranks must be contiguous and ordered")
        if any(left.score < right.score for left, right in zip(self.hits, self.hits[1:], strict=False)):
            raise ValueError("hit scores must be in descending order")
        expected = "rerank" if self.ranking_method == "rrf_then_rerank" else "rrf"
        if any(hit.score_type != expected for hit in self.hits):
            raise ValueError("score types must match the declared ranking method")
        return self


class WeatherInput(StrictModel):
    place: str = Field(min_length=1, max_length=200)
    date: str | None = Field(default=None, max_length=32)
    units: Literal["metric", "imperial"] = "metric"


class Place(StrictModel):
    id: str
    name: str
    country_code: str
    timezone: str


class PlacesResponse(StrictModel):
    places: list[Place] = Field(max_length=10)


class Temperature(StrictModel):
    value: float = Field(allow_inf_nan=False)
    unit: Literal["C", "F"]


class WeatherSource(StrictModel):
    provider: str
    is_mock: bool


class WeatherResponse(StrictModel):
    status: Literal["ok"]
    place: Place
    kind: Literal["current", "forecast"]
    valid_at: str
    fetched_at: str
    temperature: Temperature
    condition: str = Field(max_length=500)
    source: WeatherSource
    stale: bool


class WeatherClarification(StrictModel):
    status: Literal["needs_clarification"]
    message: str
    places: list[Place] = Field(default_factory=list)
    is_mock: bool = False


class CueKBToolResult(StrictModel):
    cuekb_schema_version: Literal["2"] = "2"
    ranking_method: RankingMethod | None = None
    ranking_version: str | None = None
    diagnostics: CueKBSearchDiagnostics | None = None
    adapter_omissions: dict[str, int] = Field(default_factory=dict)
    status: Literal["ok", "degraded", "not_found", "needs_clarification"]
    evidence_status: Literal["unassessed", "sufficient", "insufficient", "conflicting"]
    hits: list[Citation] = Field(default_factory=list)
    trace_id: str
    retrieval_id: str
    degraded_reasons: list[str] = Field(default_factory=list)
    scope_limited: bool = False
    content_revisions: dict[str, int] = Field(default_factory=dict)
    timings_ms: dict[str, float] = Field(default_factory=dict)
    retrieval_path: str
    executed_stages: list[str] = Field(default_factory=list)
    skipped_stages: list[dict[str, str]] = Field(default_factory=list)
    hits_omitted: int = 0
