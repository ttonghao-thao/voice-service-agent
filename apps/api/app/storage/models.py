from datetime import datetime
from typing import Any

from app.contracts import now, uid
from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    access_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(100))
    locale: Mapped[str] = mapped_column(String(20), default="en-US")
    epoch: Mapped[int] = mapped_column(Integer, default=0)
    request_revision: Mapped[int] = mapped_column(Integer, default=0)
    current_turn: Mapped[str | None] = mapped_column(String(36))
    voice_session_id: Mapped[str | None] = mapped_column(String(36))
    event_seq: Mapped[int] = mapped_column(Integer, default=0)
    history: Mapped[list] = mapped_column(JSON, default=list)
    slots: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[str] = mapped_column(Text, default="")
    tool_config_version: Mapped[str] = mapped_column(String(32), default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Turn(Base):
    __tablename__ = "turns"
    __table_args__ = (UniqueConstraint("conversation_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    epoch: Mapped[int] = mapped_column(Integer)
    request_revision: Mapped[int] = mapped_column(Integer)
    parent_task_id: Mapped[str | None] = mapped_column(String(36))
    native_call_id: Mapped[str | None] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(180))
    request_hash: Mapped[str] = mapped_column(String(64))
    user_text: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), default="running")
    cancellation_reason: Mapped[str | None] = mapped_column(String(64))
    delivery_status: Mapped[str] = mapped_column(String(32), default="pending_validation")
    output_suppressed: Mapped[bool] = mapped_column(Boolean, default=False)
    answer: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("conversation_id", "server_seq"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    server_seq: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Record(Base):
    __tablename__ = "conversation_records"
    __table_args__ = (UniqueConstraint("conversation_id", "epoch", "kind", "source_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    epoch: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(40))
    source_id: Mapped[str] = mapped_column(String(180))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ToolConfig(Base):
    __tablename__ = "tool_configs"
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ToolRun(Base):
    __tablename__ = "tool_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    turn_id: Mapped[str] = mapped_column(String(36))
    epoch: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(40))
    duration_ms: Mapped[int] = mapped_column(Integer)
    evidence: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AdminAudit(Base):
    __tablename__ = "admin_audits"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(80))
    details: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
