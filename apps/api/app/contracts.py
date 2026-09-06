from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def uid() -> str:
    return str(uuid4())


def now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Principal(StrictModel):
    user_id: str
    tenant_id: str
    scopes: frozenset[str] = frozenset()
    knowledge_base_ids: tuple[str, ...] = ()
    expires_at: float | None = None


class MessageInput(StrictModel):
    text: str = Field(min_length=1, max_length=2000)


class BridgeArguments(StrictModel):
    user_request: str = Field(min_length=1, max_length=2000)


class ConversationInput(StrictModel):
    title: str = Field(default="新会话", min_length=1, max_length=100)
    locale: Literal["zh-CN"] = "zh-CN"


class InterruptInput(StrictModel):
    expected_epoch: int = Field(ge=0)


class Citation(StrictModel):
    citation_id: str
    document_id: str
    chunk_id: str
    title: str
    source_uri: str | None = None
    version: str
    updated_at: str
    content: str
    retrieval_id: str
    is_mock: bool = False


class AgentAnswer(StrictModel):
    status: Literal["answered", "needs_clarification", "insufficient_evidence", "failed"]
    display_text: str = Field(max_length=8000)
    speech_text: str = Field(max_length=160)
    citation_ids: list[str]


class AnswerBundle(StrictModel):
    answer_id: str = Field(default_factory=uid)
    status: Literal["answered", "needs_clarification", "insufficient_evidence", "failed", "canceled"]
    display_text: str
    speech_text: str
    speech_language: str = "zh-CN"
    citations: list[Citation] = Field(default_factory=list)
    cards: list[dict[str, Any]] = Field(default_factory=list)
    reason_code: str | None = None
    is_mock: bool = False


class PortalEvent(StrictModel):
    type: str
    event_id: str = Field(default_factory=uid)
    conversation_id: str
    epoch: int
    turn_id: str | None = None
    server_seq: int = 0
    timestamp: datetime = Field(default_factory=now)
    payload: dict[str, Any] = Field(default_factory=dict)


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, retryable: bool = False):
        self.code, self.message, self.status, self.retryable = code, message, status, retryable
        super().__init__(code)

    def payload(self):
        return {"code": self.code, "message": self.message, "retryable": self.retryable, "trace_id": uid()}
