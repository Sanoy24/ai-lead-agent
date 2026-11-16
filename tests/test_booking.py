from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from lead_agent.booking import BookingService, CalcomWebhook, format_appointment
from lead_agent.db import Lead, LeadSource, LeadStatus
from lead_agent.leads import LeadIntake, LeadService
from lead_agent.profile import load_profile


def new_lead(service: LeadService) -> str:
    intake = LeadIntake(name="Dana Lopez", email="dana@example.com", consent=True)
    return service.receive(intake, LeadSource.FORM).lead_id


def event(trigger: str, **payload: Any) -> CalcomWebhook:
    base: dict[str, Any] = {
        "uid": "bk_1",
        "startTime": "2026-11-20T16:00:00Z",
        "attendees": [{"email": "Dana@Example.com", "name": "Dana Lopez"}],
    }
    return CalcomWebhook.model_validate({"triggerEvent": trigger, "payload": {**base, **payload}})


def load(sessions: sessionmaker[Session], lead_id: str) -> Lead:
    with sessions() as session:
        lead = session.get_one(Lead, lead_id)
        _ = lead.events
        return lead


def test_booking_is_matched_by_lead_id_and_stops_follow_ups(
    service: LeadService, sessions: sessionmaker[Session]
) -> None:
    lead_id = new_lead(service)
    with sessions.begin() as session:
        session.get_one(Lead, lead_id).next_follow_up_at = datetime(2026, 11, 18, tzinfo=UTC)

    update = BookingService(sessions, "America/Denver").apply(
        event("BOOKING_CREATED", metadata={"lead_id": lead_id}, attendees=[])
    )

    assert update is not None
    assert (update.lead_id, update.action) == (lead_id, "booked")
    lead = load(sessions, lead_id)
    assert lead.status == LeadStatus.BOOKED
    assert lead.booking_uid == "bk_1"
    assert lead.appointment_at is not None
    assert lead.next_follow_up_at is None
    assert "lead.booked" in {e.type for e in lead.events}


def test_booking_falls_back_to_the_attendee_email(
    service: LeadService, sessions: sessionmaker[Session]
) -> None:
    lead_id = new_lead(service)
    update = BookingService(sessions).apply(event("BOOKING_CREATED"))
    assert update is not None
    assert update.lead_id == lead_id


def test_reschedule_and_cancel(service: LeadService, sessions: sessionmaker[Session]) -> None:
    lead_id = new_lead(service)
    bookings = BookingService(sessions, "America/Denver")
    bookings.apply(event("BOOKING_CREATED"))

    moved = bookings.apply(
        event(
            "BOOKING_RESCHEDULED",
            uid="bk_2",
            rescheduleUid="bk_1",
            attendees=[],
            startTime="2026-11-21T17:30:00Z",
        )
    )
    assert moved is not None
    assert moved.confirmation == "Got it, your assessment is now Saturday, November 21 at 10:30 AM."
    assert load(sessions, lead_id).booking_uid == "bk_2"

    assert bookings.apply(event("BOOKING_CANCELLED", uid="bk_1")) is None  # The old booking.
    cancelled = bookings.apply(event("BOOKING_CANCELLED", uid="bk_2"))
    assert cancelled is not None
    assert cancelled.confirmation is None
    lead = load(sessions, lead_id)
    assert (lead.status, lead.appointment_at, lead.booking_uid) == (
        LeadStatus.QUALIFIED,
        None,
        None,
    )


def test_unknown_bookings_and_other_events_are_ignored(sessions: sessionmaker[Session]) -> None:
    bookings = BookingService(sessions)
    assert bookings.apply(event("BOOKING_CREATED")) is None
    assert bookings.apply(event("MEETING_ENDED")) is None


def test_opted_out_leads_keep_their_status(
    service: LeadService, sessions: sessionmaker[Session]
) -> None:
    lead_id = new_lead(service)
    with sessions.begin() as session:
        session.get_one(Lead, lead_id).status = LeadStatus.OPTED_OUT
    BookingService(sessions).apply(event("BOOKING_CREATED"))
    assert load(sessions, lead_id).status == LeadStatus.OPTED_OUT


def test_appointment_is_shown_in_local_time() -> None:
    start = datetime(2026, 11, 20, 16, 0, tzinfo=UTC)
    assert format_appointment(start, "America/Denver") == "Friday, November 20 at 9:00 AM"
    assert format_appointment(start, "UTC") == "Friday, November 20 at 4:00 PM"


def test_booking_link_is_prefilled_for_the_lead() -> None:
    link = load_profile().booking_link("abc123", "Dana Lopez", "dana@example.com")
    assert link == (
        "https://cal.com/brightline-demo/assessment"
        "?name=Dana+Lopez&metadata%5Blead_id%5D=abc123&email=dana%40example.com"
    )
