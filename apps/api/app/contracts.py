import base64
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator


def uid() -> str:
    return str(uuid4())


def now() -> datetime:
    return datetime.now(UTC)


def printable_ascii(value: str) -> bool:
    return all(char in "\n\r" or " " <= char <= "~" for char in value)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Principal(StrictModel):
    user_id: str
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()
    knowledge_base_ids: tuple[str, ...] = ()
    expires_at: float | None = None

    @field_validator("knowledge_base_ids")
    @classmethod
    def valid_knowledge_base_ids(cls, value: tuple[str, ...]):
        try:
            parsed = tuple(str(UUID(item)) for item in value)
        except ValueError as exc:
            raise ValueError("knowledge_base_ids must contain UUID values") from exc
        if len(set(parsed)) != len(parsed):
            raise ValueError("knowledge_base_ids must not contain duplicates")
        return parsed


class MessageInput(StrictModel):
    text: str = Field(min_length=1, max_length=2000)


class BridgeArguments(StrictModel):
    user_request: str = Field(min_length=1, max_length=2000)


class NanoBridgeArguments(BridgeArguments):
    query: str = Field(min_length=1, max_length=2000)
    product_model: str | None = Field(default=None, min_length=1, max_length=120)
    software_version: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("user_request", "query", "product_model", "software_version")
    @classmethod
    def ascii_strings(cls, value):
        if value is not None and not printable_ascii(value):
            raise ValueError("Voice arguments require printable ASCII")
        return value


class ConversationInput(StrictModel):
    title: str = Field(default="New conversation", min_length=1, max_length=100)
    locale: Literal["en-US"] = "en-US"


class InterruptInput(StrictModel):
    expected_epoch: int = Field(ge=0)


class TaskControlInput(StrictModel):
    expected_epoch: int = Field(ge=0)
    expected_revision: int = Field(ge=0)


class StopPlaybackInput(TaskControlInput):
    response_id: str | None = Field(default=None, min_length=1, max_length=128)


class Citation(StrictModel):
    citation_id: str
    document_id: str
    chunk_id: str
    title: str
    source_uri: str | None = None
    version_id: str
    business_version: str | None = None
    updated_at: str | None = None
    content: str
    context: str | None = None
    context_parts: list[dict[str, Any]] = Field(default_factory=list)
    context_truncated: bool = False
    context_omitted: bool = False
    hits_omitted: int = Field(default=0, ge=0)
    relations: list[dict[str, Any]] = Field(default_factory=list)
    trace_id: str
    retrieval_id: str
    retrieval_status: Literal["ok", "degraded", "not_found", "needs_clarification"] = "ok"
    evidence_status: Literal["unassessed", "sufficient", "insufficient", "conflicting"] = "unassessed"
    degraded_reasons: tuple[str, ...] = ()
    scope_limited: bool = False
    content_revisions: dict[str, int] = Field(default_factory=dict)
    rank: int = Field(ge=1)
    title_path: tuple[str, ...] = ()
    anchor: dict[str, Any] = Field(default_factory=dict)
    retrieval_sources: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    # The exact server-authorized KB scope used for this retrieval. It is not
    # supplied by the browser and is used to redact history after revocation.
    authorized_kb_ids: tuple[str, ...] = ()
    is_mock: bool = False


class AgentAnswer(StrictModel):
    status: Literal["answered", "needs_clarification", "insufficient_evidence", "failed"]
    display_text: str = Field(max_length=8000)
    speech_text: str = Field(max_length=160)
    citation_ids: list[str]


class AnswerBundle(StrictModel):
    answer_id: str = Field(default_factory=uid)
    answer_origin: Literal["business_runtime", "voicechat"] = "business_runtime"
    evidence_role: Literal["cited_sources", "retrieved_context"] = "cited_sources"
    authorized_kb_ids: list[str] | None = None
    status: Literal[
        "answered", "needs_clarification", "insufficient_evidence", "failed", "canceled", "voice_completed"
    ]
    display_text: str
    speech_text: str
    speech_language: str = "en-US"
    citations: list[Citation] = Field(default_factory=list)
    cards: list[dict[str, Any]] = Field(default_factory=list)
    reason_code: str | None = None
    is_mock: bool = False


class KnowledgeResultView(StrictModel):
    kind: Literal["knowledge"] = "knowledge"
    result_id: str = Field(default_factory=uid)
    directive: Literal["answer_from_evidence", "ask_clarification", "report_insufficient", "report_failure"]
    retrieval_status: Literal["ok", "degraded", "not_found", "needs_clarification"] | None
    evidence_status: Literal["unassessed", "sufficient", "insufficient", "conflicting"] | None
    citations: list[Citation] = Field(default_factory=list)
    degraded_reasons: list[str] = Field(default_factory=list)
    scope_limited: bool = False
    application_limited: bool = False
    hits_omitted: int = Field(default=0, ge=0)
    reason_code: str | None = None
    message: str = Field(default="", max_length=500)
    is_mock: bool = False
    trace_id: str | None = None


class KnowledgeBundle(KnowledgeResultView):
    authorized_kb_ids: list[str]
    tool_version: int | None

    def public_view(self):
        return KnowledgeResultView.model_validate(
            self.model_dump(exclude={"authorized_kb_ids", "tool_version"})
        )


ACTIVE_TURN_STATUSES = frozenset({"running", "awaiting_voice"})


class PortalEvent(StrictModel):
    type: str
    event_id: str = Field(default_factory=uid)
    conversation_id: str
    epoch: int
    request_revision: int = 0
    turn_id: str | None = None
    server_seq: int = 0
    timestamp: datetime = Field(default_factory=now)
    payload: dict[str, Any] = Field(default_factory=dict)


class EmptyEventPayload(StrictModel):
    message: str | None = Field(default=None, max_length=500)


class PlaybackClearPayload(EmptyEventPayload):
    response_id: str | None = Field(default=None, min_length=1, max_length=128)


class SessionReadyPayload(StrictModel):
    sample_rate: Literal[24000]
    format: Literal["pcm16"]
    chunk_ms: Literal[80]
    is_mock: bool


class TranscriptPayload(StrictModel):
    item_id: str = Field(min_length=1, max_length=128)
    response_id: str | None = Field(default=None, min_length=1, max_length=128)
    text: str = Field(max_length=100000)


class SpeechTextPayload(StrictModel):
    response_id: str = Field(min_length=1, max_length=128)
    item_id: str | None = Field(default=None, min_length=1, max_length=128)
    segment_index: int = Field(default=0, ge=0)
    phase: Literal["status", "answer"] = "answer"
    text: str = Field(max_length=100000)


class AudioDeltaPayload(StrictModel):
    response_id: str = Field(min_length=1, max_length=128)
    item_id: str | None = Field(default=None, min_length=1, max_length=128)
    audio: str = Field(min_length=1, max_length=64000)

    @field_validator("audio")
    @classmethod
    def pcm16_audio(cls, value: str):
        try:
            raw = base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise ValueError("audio must be valid base64") from exc
        if len(raw) % 2 or len(raw) > 48000:
            raise ValueError("audio must be a bounded PCM16 chunk")
        return value


class ResponseDonePayload(StrictModel):
    response_id: str = Field(min_length=1, max_length=128)
    item_id: str | None = Field(default=None, min_length=1, max_length=128)
    phase: Literal["status", "answer"] = "answer"


class InputStatePayload(StrictModel):
    state: Literal["speaking", "quiet"]


class ToolStartedPayload(StrictModel):
    message: str = Field(min_length=1, max_length=500)
    user_text: str | None = Field(default=None, max_length=2000)


class PortalErrorPayload(StrictModel):
    code: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=500)
    retryable: bool
    trace_id: str = Field(min_length=1, max_length=128)


class _ServerEventBase(StrictModel):
    event_id: str = Field(default_factory=uid)
    conversation_id: str = Field(min_length=1, max_length=128)
    epoch: int = Field(ge=0)
    request_revision: int = Field(default=0, ge=0)
    turn_id: str | None = Field(default=None, max_length=128)
    server_seq: int = Field(ge=0)
    timestamp: datetime = Field(default_factory=now)


class SessionReadyEvent(_ServerEventBase):
    type: Literal["portal.session.ready"]
    payload: SessionReadyPayload


class TranscriptDeltaEvent(_ServerEventBase):
    type: Literal["portal.transcript.delta", "portal.transcript.done"]
    payload: TranscriptPayload


class SpeechTextEvent(_ServerEventBase):
    type: Literal["portal.speech_text.delta", "portal.speech_text.done"]
    payload: SpeechTextPayload


class AudioDeltaEvent(_ServerEventBase):
    type: Literal["portal.audio.delta"]
    payload: AudioDeltaPayload


class AudioDoneEvent(_ServerEventBase):
    type: Literal["portal.audio.done"]
    payload: ResponseDonePayload


class InputStateEvent(_ServerEventBase):
    type: Literal["portal.input.state"]
    payload: InputStatePayload


class ToolStartedEvent(_ServerEventBase):
    type: Literal["portal.tool.started"]
    payload: ToolStartedPayload


class AnswerFinalEvent(_ServerEventBase):
    type: Literal["portal.answer.final"]
    payload: AnswerBundle


class KnowledgeReadyEvent(_ServerEventBase):
    turn_id: str = Field(min_length=1, max_length=128)
    type: Literal["portal.knowledge.ready"]
    payload: KnowledgeResultView


class PlaybackClearEvent(_ServerEventBase):
    type: Literal["portal.playback.clear"]
    payload: PlaybackClearPayload


class SessionEndedEvent(_ServerEventBase):
    type: Literal["portal.session.ended"]
    payload: EmptyEventPayload


class ErrorEvent(_ServerEventBase):
    type: Literal["portal.error"]
    payload: PortalErrorPayload


PortalServerEvent = Annotated[
    SessionReadyEvent
    | TranscriptDeltaEvent
    | SpeechTextEvent
    | AudioDeltaEvent
    | AudioDoneEvent
    | InputStateEvent
    | ToolStartedEvent
    | AnswerFinalEvent
    | KnowledgeReadyEvent
    | PlaybackClearEvent
    | SessionEndedEvent
    | ErrorEvent,
    Field(discriminator="type"),
]
portal_server_event_adapter = TypeAdapter(PortalServerEvent)


class PortalAudioPayload(StrictModel):
    format: Literal["pcm16"]
    sample_rate: Literal[24000]
    audio: str = Field(min_length=1, max_length=5120)

    @field_validator("audio")
    @classmethod
    def exact_frame(cls, value: str):
        try:
            raw = base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise ValueError("audio must be valid base64") from exc
        if len(raw) != 3840:
            raise ValueError("audio must contain exactly 80ms of PCM16")
        return value


class PlaybackAckPayload(StrictModel):
    response_id: str = Field(min_length=1, max_length=128)
    played_samples: int = Field(ge=0)


class PlaybackStopPayload(StrictModel):
    response_id: str | None = Field(default=None, min_length=1, max_length=128)


class _ClientEventBase(StrictModel):
    epoch: int = Field(ge=0)


class PortalAudioAppend(_ClientEventBase):
    type: Literal["portal.audio.append"]
    event_id: str | None = Field(default=None, min_length=1, max_length=128)
    seq: int = Field(ge=0)
    payload: PortalAudioPayload


class PortalPlaybackAck(_ClientEventBase):
    type: Literal["portal.playback.ack"]
    payload: PlaybackAckPayload


class PortalPlaybackStop(_ClientEventBase):
    type: Literal["portal.playback.stop"]
    payload: PlaybackStopPayload = Field(default_factory=PlaybackStopPayload)


class PortalInterrupt(_ClientEventBase):
    type: Literal["portal.interrupt"]
    payload: EmptyEventPayload = Field(default_factory=EmptyEventPayload)


class PortalSessionClose(_ClientEventBase):
    type: Literal["portal.session.close"]
    payload: EmptyEventPayload = Field(default_factory=EmptyEventPayload)


PortalClientEvent = Annotated[
    PortalAudioAppend | PortalPlaybackAck | PortalPlaybackStop | PortalInterrupt | PortalSessionClose,
    Field(discriminator="type"),
]
portal_client_event_adapter = TypeAdapter(PortalClientEvent)


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, retryable: bool = False):
        self.code, self.message, self.status, self.retryable = code, message, status, retryable
        super().__init__(code)

    def payload(self):
        return {"code": self.code, "message": self.message, "retryable": self.retryable, "trace_id": uid()}
