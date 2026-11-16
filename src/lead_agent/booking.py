"""Bookings from Cal.com: mark leads booked, rescheduled or cancelled.

Booking links carry the lead's id (``metadata[lead_id]``), which Cal.com sends
back in its webhooks. If it's missing, the attendee's email is used instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from lead_agent.db import Lead, LeadEvent, LeadStatus

logger = logging.getLogger(__name__)

BOOKED = {"BOOKING_CREATED", "BOOKING_RESCHEDULED"}
CANCELLED = {"BOOKING_CANCELLED"}


class Attendee(BaseModel):
    model_config = ConfigDict(extra="ignore")

    email: str = ""
    name: str = ""
    time_zone: str | None = Field(default=None, alias="timeZone")


class Booking(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    uid: str = Field(default="", max_length=64)
    start_time: datetime | None = Field(default=None, alias="startTime")
    attendees: list[Attendee] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    reschedule_uid: str | None = Field(default=None, alias="rescheduleUid")


class CalcomWebhook(BaseModel):
    model_config = ConfigDict(extra="ignore")

    trigger_event: str = Field(alias="triggerEvent")
    payload: Booking = Field(default_factory=Booking)


@dataclass(frozen=True, slots=True)
class BookingUpdate:
    lead_id: str
    action: str
    """``booked``, ``rescheduled`` or ``cancelled``."""
    confirmation: str | None
    """A short message to send the lead, if any."""


class BookingService:
    def __init__(self, sessions: sessionmaker[Session], default_timezone: str = "UTC") -> None:
        self._sessions = sessions
        self._default_timezone = default_timezone

    def apply(self, webhook: CalcomWebhook) -> BookingUpdate | None:
        """Apply a Cal.com event. Returns None for events about unknown leads."""
        event = webhook.trigger_event.upper()
        if event not in BOOKED | CANCELLED:
            return None
        booking = webhook.payload
        with self._sessions.begin() as session:
            lead = self._find_lead(session, booking)
            if lead is None:
                logger.info("Ignored a %s webhook for a booking we can't match", event)
                return None

            if event in CANCELLED:
                if booking.uid and lead.booking_uid not in {None, booking.uid}:
                    return None  # An older booking; the current one still stands.
                lead.appointment_at = None
                lead.booking_uid = None
                lead.events.append(LeadEvent(type="lead.booking_cancelled", detail={}))
                if lead.status == LeadStatus.BOOKED:
                    lead.status = LeadStatus.QUALIFIED
                return BookingUpdate(lead.id, "cancelled", None)

            action = "rescheduled" if lead.appointment_at is not None else "booked"
            if event == "BOOKING_RESCHEDULED":
                action = "rescheduled"
            start = _utc(booking.start_time) if booking.start_time else None
            lead.appointment_at = start
            lead.booking_uid = booking.uid or None
            lead.next_follow_up_at = None  # Booked leads don't need chasing.
            lead.events.append(
                LeadEvent(
                    type=f"lead.{action}",
                    detail={
                        "booking_uid": booking.uid,
                        "start": start.isoformat() if start else None,
                    },
                )
            )
            if lead.status != LeadStatus.OPTED_OUT:
                if lead.status != LeadStatus.BOOKED:
                    lead.events.append(
                        LeadEvent(
                            type="lead.status_changed",
                            detail={"from": lead.status, "to": LeadStatus.BOOKED},
                        )
                    )
                lead.status = LeadStatus.BOOKED
            confirmation = None
            if start is not None:
                when = format_appointment(start, lead.timezone or self._default_timezone)
                confirmation = (
                    f"You're all set for {when}. See you then!"
                    if action == "booked"
                    else f"Got it, your assessment is now {when}."
                )
            return BookingUpdate(lead.id, action, confirmation)

    def _find_lead(self, session: Session, booking: Booking) -> Lead | None:
        lead_id = booking.metadata.get("lead_id")
        if isinstance(lead_id, str):
            lead = session.get(Lead, lead_id)
            if lead is not None:
                return lead
        for uid in (booking.reschedule_uid, booking.uid):
            if uid:
                lead = session.scalars(select(Lead).where(Lead.booking_uid == uid)).first()
                if lead is not None:
                    return lead
        for attendee in booking.attendees:
            if attendee.email:
                lead = session.scalars(
                    select(Lead)
                    .where(Lead.email == attendee.email.lower())
                    .order_by(Lead.created_at.desc())
                    .limit(1)
                ).first()
                if lead is not None:
                    return lead
        return None


def format_appointment(start: datetime, timezone: str) -> str:
    """E.g. "Friday, November 20 at 9:00 AM"."""
    local = _utc(start).astimezone(ZoneInfo(timezone))
    hour = local.hour % 12 or 12
    period = "AM" if local.hour < 12 else "PM"
    return f"{local:%A, %B} {local.day} at {hour}:{local.minute:02d} {period}"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
