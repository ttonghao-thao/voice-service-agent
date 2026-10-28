"""Closed CueKB V2 evidence types shared by wire contracts and stored citations.

Checked against CueKB 8a5aacce1184b95cacd391347183491a0f075d9f. Evidence text is
never stripped or rewritten during validation: exact source identity matters.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class SourceAnchor(EvidenceModel):
    page: int | None = Field(default=None, ge=1)
    heading_path: list[str] = Field(default_factory=list)
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)
    bbox: dict[str, float | str] | None = None


class CueKBSourceSpan(EvidenceModel):
    label: str = Field(default="text", min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=32768)
    anchor: SourceAnchor = Field(default_factory=SourceAnchor)
    parser_item_id: str | None = Field(default=None, max_length=500)
    span_id: str | None = Field(default=None, max_length=100)
    row_index: int | None = Field(default=None, ge=0)
    column_index: int | None = Field(default=None, ge=0)


class CueKBApplicableSubject(EvidenceModel):
    entity_id: UUID | None = None
    display_name: str = Field(min_length=1, max_length=200)
    product_model: str | None = Field(default=None, min_length=1, max_length=200)
    software_version: str | None = Field(default=None, min_length=1, max_length=200)
    source_kind: Literal["curated", "reviewed_alias", "parser"] = "curated"
    provenance_id: str | None = Field(default=None, max_length=500)
    source_spans: list[CueKBSourceSpan] = Field(default_factory=list, max_length=20)


class CueKBApplicableCondition(EvidenceModel):
    text: str = Field(min_length=1, max_length=2000)
    source_spans: list[CueKBSourceSpan] = Field(default_factory=list, max_length=20)
    source_kind: Literal["curated", "parser"] = "curated"
    provenance_id: str | None = Field(default=None, max_length=500)


class CueKBApplicability(EvidenceModel):
    reason_codes: list[Literal["missing_binding", "no_reviewed_match", "structure_missing", "scope_conflict"]] = Field(default_factory=list, max_length=4)
    status: Literal["resolved", "unknown", "ambiguous"] = "unknown"
    subjects: list[CueKBApplicableSubject] = Field(default_factory=list, max_length=8)
    conditions: list[CueKBApplicableCondition] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def resolved_has_subject(self):
        if self.status == "resolved" and not self.subjects:
            raise ValueError("resolved applicability requires a subject")
        return self


CueKBFactKind = Literal["statement", "specification", "comparison", "bundle", "general"]
CueKBDependencyIssue = Literal[
    "unmatched_reference", "ambiguous_reference", "dependency_limit_exceeded", "dependency_cycle", "unsafe_dependency"
]
ContextRole = Literal["attribution", "table_header", "condition", "exception", "continuation"]
ContextState = Literal["disabled", "complete", "truncated", "unresolved"]
ContextOmission = Literal[
    "budget_omitted", "candidate_limit_reached", "dependency_unresolved", "unsafe_scope_excluded"
]
RankingMethod = Literal["keyword_rrf", "rrf", "rrf_related", "rrf_then_rerank"]
