from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from lead_agent.db import Lead, LeadSource, LeadStatus, make_engine, make_session_factory, utcnow
from lead_agent.leads import LeadIntake, LeadService
from tests.conftest import CONSENT


def intake(**overrides: Any) -> LeadIntake:
    data: dict[str, Any] = {
        "name": "Dana Lopez",
        "email": "Dana.Lopez@example.com",
        "message": "Interested in solar panels for a 3-bedroom house.",
        "service_interest": "Solar installation",
        "consent": True,
    }
    data.update(overrides)
    return LeadIntake.model_validate(data)


def test_new_lead_is_stored_with_consent_and_message(
    service: LeadService, sessions: sessionmaker[Session]
) -> None:
    result = service.receive(intake(), LeadSource.FORM)

    assert result.created is True
    with sessions() as session:
        lead = session.get_one(Lead, result.lead_id)
        assert lead.email == "dana.lopez@example.com"
        assert lead.status == LeadStatus.NEW
        assert lead.consent is True
        assert lead.consent_text == CONSENT
        assert [m.content for m in lead.messages] == [
            "Interested in solar panels for a 3-bedroom house."
        ]
        assert [e.type for e in lead.events] == ["lead.created"]


def test_repeat_submission_is_added_to_the_recent_lead(
    service: LeadService, sessions: sessionmaker[Session]
) -> None:
    first = service.receive(intake(), LeadSource.FORM)
    second = service.receive(
        intake(email="dana.lopez@EXAMPLE.com", message="Also need a roof check."), LeadSource.FORM
    )

    assert second.created is False
    assert second.lead_id == first.lead_id
    with sessions() as session:
        lead = session.get_one(Lead, first.lead_id)
        assert len(lead.messages) == 2
        assert [e.type for e in lead.events] == ["lead.created", "lead.resubmitted"]


def test_duplicate_detection_also_matches_phone(service: LeadService) -> None:
    first = service.receive(intake(email=None, phone="+1 (555) 010-2030"), LeadSource.FORM)
    second = service.receive(intake(email=None, phone="+15550102030"), LeadSource.WEBHOOK)
    assert second.lead_id == first.lead_id


def test_us_numbers_written_with_parentheses_are_accepted(service: LeadService) -> None:
    result = service.receive(intake(email=None, phone="(303) 555-0100"), LeadSource.FORM)
    assert result.created


def test_old_or_opted_out_leads_are_not_reused(
    service: LeadService, sessions: sessionmaker[Session]
) -> None:
    first = service.receive(intake(), LeadSource.FORM)
    with sessions.begin() as session:
        lead = session.get_one(Lead, first.lead_id)
        lead.created_at = utcnow() - timedelta(days=2)

    assert service.receive(intake(), LeadSource.FORM).created is True

    with sessions.begin() as session:
        for lead in session.scalars(select(Lead)):
            lead.status = LeadStatus.OPTED_OUT

    assert service.receive(intake(), LeadSource.FORM).created is True


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"consent": False}, "agree to be contacted"),
        ({"email": None, "phone": None}, "email address or a phone number"),
        ({"email": "not-an-email"}, "email"),
        ({"phone": "call me maybe"}, "valid phone"),
        ({"name": ""}, "at least 1 character"),
    ],
)
def test_invalid_intake_is_rejected(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        intake(**overrides)


def test_unknown_timezone_is_ignored() -> None:
    assert intake(timezone="Mars/Olympus").timezone is None
    assert intake(timezone="America/Denver").timezone == "America/Denver"


def test_columns_added_in_later_versions_are_added_to_old_databases(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = make_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE leads (id VARCHAR(32) PRIMARY KEY, created_at DATETIME, "
                "updated_at DATETIME, source VARCHAR(16), name VARCHAR(120))"
            )
        )
        connection.execute(text("INSERT INTO leads (id, source, name) VALUES ('a', 'form', 'Old')"))

    sessions = make_session_factory(engine)

    with sessions() as session:
        lead = session.get_one(Lead, "a")
        assert lead.crm_synced_event_id == 0
        assert lead.booking_uid is None
        assert lead.follow_ups_sent == 0
