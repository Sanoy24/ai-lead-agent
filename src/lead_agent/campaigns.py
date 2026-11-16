"""Reactivation campaigns: re-contact past leads and customers who went quiet.

The business imports a list of people who previously got in touch or bought
from it. The agent sends each one a short opener through the follow-up
scheduler (so sending is throttled and never happens in quiet hours), and any
reply continues as a normal conversation.

Safeguards: the business must confirm the contacts have a prior relationship,
anyone who ever opted out is never contacted, and people already in a recent
conversation are skipped.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import (
    BaseModel,
    EmailStr,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from lead_agent.channels.sms import phone_key
from lead_agent.db import Lead, LeadEvent, LeadSource, LeadStatus, utcnow

MAX_CONTACTS = 2000
RECENT_ACTIVITY = timedelta(days=30)
"""People with any activity this recent are already in a conversation."""


class ContactRow(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    service_interest: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("name", "phone", "service_interest", "note", "email", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("phone")
    @classmethod
    def _enough_digits(cls, value: str | None) -> str | None:
        if value is not None and len(phone_key(value)) < 7:
            raise ValueError("not a valid phone number")
        return value

    @model_validator(mode="after")
    def _reachable(self) -> ContactRow:
        if not self.email and not self.phone:
            raise ValueError("needs an email address or a phone number")
        return self


class SkippedRow(BaseModel):
    row: int
    """1-based position in the uploaded list."""
    reason: str


class ImportResult(BaseModel):
    campaign: str
    queued: int
    skipped: list[SkippedRow]


class CampaignStats(BaseModel):
    campaign: str
    total: int
    waiting: int
    """Imported, opener not sent yet."""
    contacted: int
    replied: int
    qualified: int
    booked: int
    opted_out: int


class ReactivationService:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def import_contacts(
        self, campaign: str, contacts: list[dict[str, Any]], consent_basis: str
    ) -> ImportResult:
        campaign = campaign.strip()[:64]
        skipped: list[SkippedRow] = []
        now = utcnow()
        with self._sessions.begin() as session:
            known = _known_contacts(session)
            seen: set[str] = set()
            queued = 0
            for index, raw in enumerate(contacts[:MAX_CONTACTS], start=1):
                try:
                    row = ContactRow.model_validate(raw)
                except ValidationError as exc:
                    skipped.append(SkippedRow(row=index, reason=_first_error(exc)))
                    continue
                keys = _keys(row.email, row.phone)
                if keys & known.opted_out:
                    skipped.append(SkippedRow(row=index, reason="opted out earlier"))
                    continue
                if keys & known.recent:
                    skipped.append(SkippedRow(row=index, reason="already in a recent conversation"))
                    continue
                if keys & seen:
                    skipped.append(SkippedRow(row=index, reason="duplicate in this list"))
                    continue
                seen |= keys
                lead = Lead(
                    source=LeadSource.REACTIVATION,
                    source_detail=campaign,
                    name=row.name,
                    email=row.email.lower() if row.email else None,
                    phone=row.phone,
                    service_interest=row.service_interest,
                    consent=True,
                    consent_text=f'Reactivation campaign "{campaign}": {consent_basis}',
                    status=LeadStatus.NEW,
                    outreach_pending=True,
                    next_follow_up_at=now,  # The scheduler sends the opener.
                )
                lead.events.append(
                    LeadEvent(
                        type="lead.created",
                        detail={
                            "source": LeadSource.REACTIVATION,
                            "campaign": campaign,
                            "note": row.note,
                            "consent": True,
                        },
                    )
                )
                session.add(lead)
                queued += 1
        if len(contacts) > MAX_CONTACTS:
            skipped.append(
                SkippedRow(row=MAX_CONTACTS + 1, reason=f"only {MAX_CONTACTS} contacts per upload")
            )
        return ImportResult(campaign=campaign, queued=queued, skipped=skipped)

    def stats(self) -> list[CampaignStats]:
        with self._sessions() as session:
            leads = session.scalars(
                select(Lead).where(Lead.source == LeadSource.REACTIVATION)
            ).all()
        groups: dict[str, Counter[str]] = {}
        for lead in leads:
            counts = groups.setdefault(lead.source_detail or "Untitled", Counter())
            counts["total"] += 1
            counts["waiting"] += lead.outreach_pending
            counts["contacted"] += not lead.outreach_pending
            counts["replied"] += lead.last_inbound_at is not None
            counts["qualified"] += lead.status == LeadStatus.QUALIFIED
            counts["booked"] += lead.status == LeadStatus.BOOKED
            counts["opted_out"] += lead.status == LeadStatus.OPTED_OUT
        return [
            CampaignStats(
                campaign=name,
                total=c["total"],
                waiting=c["waiting"],
                contacted=c["contacted"],
                replied=c["replied"],
                qualified=c["qualified"],
                booked=c["booked"],
                opted_out=c["opted_out"],
            )
            for name, c in sorted(groups.items())
        ]


class _Known:
    def __init__(self) -> None:
        self.opted_out: set[str] = set()
        self.recent: set[str] = set()


def _keys(email: str | None, phone: str | None) -> set[str]:
    keys = set()
    if email:
        keys.add(f"e:{email.lower()}")
    if phone:
        keys.add(f"p:{phone_key(phone)}")
    return keys


def _known_contacts(session: Session) -> _Known:
    known = _Known()
    cutoff = utcnow() - RECENT_ACTIVITY
    rows = session.execute(
        select(Lead.email, Lead.phone, Lead.status, Lead.opted_out_at, Lead.updated_at)
    ).all()
    for email, phone, status, opted_out_at, updated_at in rows:
        keys = _keys(email, phone)
        if status == LeadStatus.OPTED_OUT or opted_out_at is not None:
            known.opted_out |= keys
        elif updated_at is not None and _aware(updated_at) >= cutoff:
            known.recent |= keys
    return known


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    field = ".".join(str(p) for p in error["loc"])
    message = str(error["msg"]).removeprefix("Value error, ")
    return f"{field}: {message}" if field else message
