"""Lead intake: validation, duplicate detection and storage."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from lead_agent.channels.sms import phone_key
from lead_agent.db import (
    Direction,
    Lead,
    LeadEvent,
    LeadMessage,
    LeadSource,
    LeadStatus,
    utcnow,
)

SMS_CONSENT_TEXT = "Texted our business number. They can opt out at any time by replying STOP."
TELEGRAM_CONSENT_TEXT = (
    "Started a conversation with our Telegram bot. They can opt out at any time with /stop."
)

_PHONE = re.compile(r"^\+?[0-9(][0-9 ().-]{5,24}$")


class LeadIntake(BaseModel):
    """A new enquiry, from the website form or a webhook."""

    name: str = Field(min_length=1, max_length=120)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    message: str = Field(default="", max_length=2000)
    service_interest: str | None = Field(default=None, max_length=120)
    timezone: str | None = Field(default=None, max_length=64)
    consent: bool = Field(
        description="The lead explicitly agreed to be contacted. Required: leads without "
        "consent are rejected."
    )

    @field_validator("name", "message", "service_interest")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value

    @field_validator("phone")
    @classmethod
    def _valid_phone(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        if not _PHONE.fullmatch(value.strip()):
            raise ValueError("Enter a valid phone number.")
        return re.sub(r"[ ().-]", "", value.strip())

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str | None) -> str | None:
        if not value:
            return None
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            return None  # Unknown zones fall back to the business's default.
        return value

    @model_validator(mode="after")
    def _contact_and_consent(self) -> LeadIntake:
        if not self.email and not self.phone:
            raise ValueError("Provide an email address or a phone number.")
        if not self.consent:
            raise ValueError("The lead must agree to be contacted.")
        return self


@dataclass(frozen=True, slots=True)
class IntakeResult:
    lead_id: str
    created: bool
    """False when the submission was added to an existing recent lead."""


@dataclass(frozen=True, slots=True)
class LeadRef:
    id: str
    status: LeadStatus


class LeadService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        consent_text: str,
        duplicate_window: timedelta = timedelta(hours=24),
    ) -> None:
        self._sessions = sessions
        self._consent_text = consent_text
        self._duplicate_window = duplicate_window

    @property
    def sessions(self) -> sessionmaker[Session]:
        return self._sessions

    def receive(
        self, intake: LeadIntake, source: LeadSource, source_detail: str | None = None
    ) -> IntakeResult:
        email = intake.email.lower() if intake.email else None
        with self._sessions.begin() as session:
            existing = self._recent_lead(session, email, intake.phone)
            if existing is not None:
                if intake.message:
                    existing.messages.append(
                        LeadMessage(
                            direction=Direction.INBOUND, channel=source, content=intake.message
                        )
                    )
                existing.last_inbound_at = utcnow()
                existing.events.append(
                    LeadEvent(type="lead.resubmitted", detail={"source": source})
                )
                return IntakeResult(existing.id, created=False)

            lead = Lead(
                source=source,
                source_detail=source_detail,
                name=intake.name,
                email=email,
                phone=intake.phone,
                service_interest=intake.service_interest,
                timezone=intake.timezone,
                consent=True,
                consent_text=self._consent_text,
                status=LeadStatus.NEW,
                last_inbound_at=utcnow(),
            )
            if intake.message:
                lead.messages.append(
                    LeadMessage(direction=Direction.INBOUND, channel=source, content=intake.message)
                )
            lead.events.append(
                LeadEvent(
                    type="lead.created",
                    detail={"source": source, "source_detail": source_detail, "consent": True},
                )
            )
            session.add(lead)
            session.flush()
            return IntakeResult(lead.id, created=True)

    def telegram_lead(self, chat_id: str) -> LeadRef | None:
        """The most recent lead for a Telegram chat, if there is one."""
        with self._sessions() as session:
            lead = session.scalars(
                select(Lead)
                .where(Lead.telegram_chat_id == chat_id)
                .order_by(Lead.created_at.desc())
                .limit(1)
            ).first()
            return LeadRef(lead.id, LeadStatus(lead.status)) if lead else None

    def email_lead(self, sender: str, lead_id: str | None = None) -> str | None:
        """The lead an email reply belongs to: the one named in the thread, or the
        most recent lead with the sender's address."""
        sender = sender.lower()
        with self._sessions() as session:
            if lead_id is not None:
                lead = session.get(Lead, lead_id)
                if lead is not None and lead.email == sender:
                    return lead.id
            return session.scalars(
                select(Lead.id)
                .where(Lead.email == sender)
                .order_by(Lead.created_at.desc())
                .limit(1)
            ).first()

    def sms_lead(self, phone: str) -> LeadRef | None:
        """The most recent lead with this phone number, however it was written."""
        key = phone_key(phone)
        if len(key) < 7:
            return None
        with self._sessions() as session:
            candidates = session.scalars(
                select(Lead)
                .where(Lead.phone.like(f"%{key[-4:]}"))
                .order_by(Lead.created_at.desc())
                .limit(50)
            ).all()
            lead = next((c for c in candidates if c.phone and phone_key(c.phone) == key), None)
            return LeadRef(lead.id, LeadStatus(lead.status)) if lead else None

    def start_sms_lead(self, phone: str, message: str) -> str:
        """Create a lead for someone who texted the business number.

        Texting the business is the person's own request to talk, which is
        recorded as their consent. Replying STOP opts them out.
        """
        with self._sessions.begin() as session:
            lead = Lead(
                source=LeadSource.SMS,
                name=phone,
                phone=phone,
                consent=True,
                consent_text=SMS_CONSENT_TEXT,
                status=LeadStatus.NEW,
                last_inbound_at=utcnow(),
            )
            lead.messages.append(
                LeadMessage(
                    direction=Direction.INBOUND, channel=LeadSource.SMS, content=message[:2000]
                )
            )
            lead.events.append(
                LeadEvent(
                    type="lead.created",
                    detail={"source": LeadSource.SMS, "source_detail": None, "consent": True},
                )
            )
            session.add(lead)
            session.flush()
            return lead.id

    def start_telegram_lead(self, chat_id: str, name: str, message: str | None = None) -> str:
        """Create a lead for someone who started a chat with the Telegram bot.

        Writing to the bot is the person's own request to talk, which is recorded
        as their consent. ``/stop`` opts them out.
        """
        with self._sessions.begin() as session:
            lead = Lead(
                source=LeadSource.TELEGRAM,
                name=name.strip()[:120] or "Telegram user",
                telegram_chat_id=chat_id,
                consent=True,
                consent_text=TELEGRAM_CONSENT_TEXT,
                status=LeadStatus.NEW,
                last_inbound_at=utcnow(),
            )
            if message:
                lead.messages.append(
                    LeadMessage(
                        direction=Direction.INBOUND,
                        channel=LeadSource.TELEGRAM,
                        content=message[:2000],
                    )
                )
            lead.events.append(
                LeadEvent(
                    type="lead.created",
                    detail={"source": LeadSource.TELEGRAM, "source_detail": None, "consent": True},
                )
            )
            session.add(lead)
            session.flush()
            return lead.id

    def _recent_lead(self, session: Session, email: str | None, phone: str | None) -> Lead | None:
        conditions = []
        if email:
            conditions.append(Lead.email == email)
        if phone:
            conditions.append(Lead.phone == phone)
        if not conditions:
            return None
        since = utcnow() - self._duplicate_window
        return session.scalars(
            select(Lead)
            .where(
                or_(*conditions),
                Lead.created_at >= since,
                Lead.status != LeadStatus.OPTED_OUT,
            )
            .order_by(Lead.created_at.desc())
            .limit(1)
        ).first()
