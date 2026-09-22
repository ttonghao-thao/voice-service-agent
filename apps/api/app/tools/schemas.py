import math
from typing import Any, Literal
from uuid import UUID

from app.contracts import Citation, StrictModel
from pydantic import Field, model_validator


class CueKBSearchInput(StrictModel):
    query: str = Field(min_length=1, max_length=2000)
    product_model: str | None = Field(default=None, min_length=1, max_length=120)
    software_version: str | None = Field(default=None, min_length=1, max_length=120)


class CueKBSearchFilters(StrictModel):
    document_ids: list[UUID] = Field(default_factory=list)
    product_model: str | None = None
    software_version: str | None = None


class CueKBSearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=2000)
    kb_ids: list[UUID] = Field(min_length=1)
    mode: Literal["auto", "exact", "hybrid", "related"] = "auto"
    top_k: int = Field(default=5, ge=1, le=20)
    filters: CueKBSearchFilters = Field(default_factory=CueKBSearchFilters)
    include_context: bool = True


class SourceAnchor(StrictModel):
    page: int | None = Field(default=None, ge=1)
    heading_path: list[str] = Field(default_factory=list)
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)
    bbox: dict[str, float | str] | None = None


class CueKBContextPart(StrictModel):
    chunk_id: UUID
    source_text: str
    anchor: SourceAnchor
    title_path: list[str]


class CueKBRelation(StrictModel):
    relation_id: UUID
    subject_id: UUID
    object_id: UUID
    relation_type: Literal[
        "belongs_to",
        "adjacent_to",
        "alias_of",
        "revises",
        "replaces",
        "references",
        "depends_on",
        "applies_to",
    ]
    conditions: dict[Literal["product_model", "software_version"], str]
    stance: Literal["supports", "refutes"]
    chunk_id: UUID


class CueKBSearchHit(StrictModel):
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    rank: int = Field(ge=1)
    source_text: str = Field(min_length=1)
    context: str | None = None
    title_path: list[str] = Field(default_factory=list)
    anchor: SourceAnchor = Field(default_factory=SourceAnchor)
    metadata: dict[str, Any] = Field(default_factory=dict)
    retrieval_sources: list[str] = Field(default_factory=list)
    context_parts: list[CueKBContextPart] = Field(default_factory=list)
    context_truncated: bool = False
    relations: list[CueKBRelation] = Field(default_factory=list)


class CueKBSearchResponse(StrictModel):
    trace_id: UUID
    retrieval_status: Literal["ok", "degraded", "not_found", "needs_clarification"]
    evidence_status: Literal["unassessed", "sufficient", "insufficient", "conflicting"] = "unassessed"
    degraded_reasons: list[str] = Field(default_factory=list)
    scope_limited: bool = False
    content_revisions: dict[UUID, int]
    timings_ms: dict[str, float]
    retrieval_path: str = "keyword"
    executed_stages: list[str] = Field(default_factory=list)
    skipped_stages: list[dict[str, str]] = Field(default_factory=list)
    hits: list[CueKBSearchHit] = Field(max_length=20)

    @model_validator(mode="after")
    def consistent_status(self):
        if self.retrieval_status in ("not_found", "needs_clarification") and self.hits:
            raise ValueError("non-result retrieval statuses cannot contain hits")
        if any(not math.isfinite(value) or value < 0 for value in self.timings_ms.values()):
            raise ValueError("timings_ms must contain finite non-negative values")
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
