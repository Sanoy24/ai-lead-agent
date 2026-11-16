"""Database models: leads, their messages, and an audit trail of events."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from sqlalchemy import JSON, ForeignKey, Index, String, Text, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)
from sqlalchemy.pool import StaticPool


def utcnow() -> datetime:
    return datetime.now(UTC)


def _uuid() -> str:
    return uuid.uuid4().hex


class LeadStatus(StrEnum):
    NEW = "new"
    CONTACTED = "contacted"
    QUALIFYING = "qualifying"
    QUALIFIED = "qualified"
    BOOKED = "booked"
    NURTURING = "nurturing"
    LOST = "lost"
    OPTED_OUT = "opted_out"


class LeadSource(StrEnum):
    FORM = "form"
    WEBHOOK = "webhook"
    TELEGRAM = "telegram"


class Direction(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSON}


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (Index("ix_leads_email_created", "email", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    source: Mapped[str] = mapped_column(String(16))
    source_detail: Mapped[str | None] = mapped_column(String(64), default=None)
    """Where a webhook lead came from, e.g. "facebook" or "typeform"."""
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(254), default=None)
    phone: Mapped[str | None] = mapped_column(String(32), default=None)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    service_interest: Mapped[str | None] = mapped_column(String(120), default=None)
    timezone: Mapped[str | None] = mapped_column(String(64), default=None)

    consent: Mapped[bool] = mapped_column(default=False)
    consent_text: Mapped[str | None] = mapped_column(Text, default=None)
    """The exact wording the lead agreed to, kept for compliance."""

    status: Mapped[str] = mapped_column(String(16), default=LeadStatus.NEW, index=True)
    score: Mapped[int | None] = mapped_column(default=None)
    answers: Mapped[dict[str, Any]] = mapped_column(default=dict)
    """Qualification answers collected during the conversation."""

    first_response_at: Mapped[datetime | None] = mapped_column(default=None)
    last_inbound_at: Mapped[datetime | None] = mapped_column(default=None)
    last_outbound_at: Mapped[datetime | None] = mapped_column(default=None)
    next_follow_up_at: Mapped[datetime | None] = mapped_column(default=None, index=True)
    follow_ups_sent: Mapped[int] = mapped_column(default=0)
    ai_paused: Mapped[bool] = mapped_column(default=False)
    """True when a person has taken over the conversation."""
    opted_out_at: Mapped[datetime | None] = mapped_column(default=None)
    crm_contact_id: Mapped[str | None] = mapped_column(String(64), default=None)

    messages: Mapped[list[LeadMessage]] = relationship(
        back_populates="lead", order_by="LeadMessage.id", cascade="all, delete-orphan"
    )
    events: Mapped[list[LeadEvent]] = relationship(
        back_populates="lead", order_by="LeadEvent.id", cascade="all, delete-orphan"
    )


class LeadMessage(Base):
    __tablename__ = "lead_messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), index=True)
    direction: Mapped[str] = mapped_column(String(8))
    channel: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    lead: Mapped[Lead] = relationship(back_populates="messages")


class LeadEvent(Base):
    """Append-only audit trail: everything that happened to a lead and why."""

    __tablename__ = "lead_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), index=True)
    type: Mapped[str] = mapped_column(String(48))
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    lead: Mapped[Lead] = relationship(back_populates="events")


def make_engine(database_url: str) -> Engine:
    if database_url.startswith("sqlite"):
        if database_url == "sqlite://" or ":memory:" in database_url:
            return create_engine(
                database_url, connect_args={"check_same_thread": False}, poolclass=StaticPool
            )
        Path(database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            database_url, connect_args={"check_same_thread": False, "timeout": 15}
        )
        event.listen(engine, "connect", _configure_sqlite)
        return engine
    return create_engine(database_url, pool_pre_ping=True)


def _configure_sqlite(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
