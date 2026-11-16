from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from lead_agent.agent import QualifyingAgent
from lead_agent.api import create_app
from lead_agent.campaigns import ReactivationService
from lead_agent.channels import Recipient
from lead_agent.config import Settings
from lead_agent.db import Lead, LeadSource, LeadStatus
from lead_agent.followups import FollowUpPolicy, FollowUpScheduler
from lead_agent.leads import LeadIntake, LeadService
from lead_agent.llm import LLMError, Message, ModelResponse, ScriptedModel, ToolSpec
from lead_agent.profile import load_profile
from lead_agent.responder import LeadResponder

MIDDAY = datetime(2026, 11, 17, 19, 0, tzinfo=UTC)  # 12:00 in Denver
NIGHT = datetime(2026, 11, 18, 6, 0, tzinfo=UTC)  # 23:00 in Denver
PASSWORD = "admin-test-password"


class Capture:
    name = "capture"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def can_reach(self, recipient: Recipient) -> bool:
        return True

    def send(self, recipient: Recipient, text: str) -> None:
        self.sent.append(text)


def contact(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": "Maria Gomez",
        "email": "maria@example.com",
        "service_interest": "Solar installation",
        "note": "Asked for a quote in spring",
    }
    row.update(overrides)
    return row


def leads_of(sessions: sessionmaker[Session]) -> list[Lead]:
    with sessions() as session:
        leads = session.scalars(
            select(Lead).where(Lead.source == LeadSource.REACTIVATION).order_by(Lead.name)
        ).all()
        for lead in leads:
            _ = lead.messages, lead.events
        return list(leads)


def make_scheduler(
    sessions: sessionmaker[Session], responses: list[ModelResponse] | None = None, model: Any = None
) -> tuple[Callable[[datetime], FollowUpScheduler], Capture, tuple[Any, LeadResponder]]:
    profile = load_profile()
    model = model or ScriptedModel(responses or [])
    channel = Capture()
    responder = LeadResponder(sessions, QualifyingAgent(model, profile), profile, [channel])

    def scheduler(now: datetime) -> FollowUpScheduler:
        return FollowUpScheduler(
            sessions, responder.follow_up, FollowUpPolicy(), profile.timezone, clock=lambda: now
        )

    return scheduler, channel, (model, responder)


# --- Import safeguards -------------------------------------------------------------------


def test_import_queues_valid_contacts_and_explains_every_skip(
    service: LeadService, sessions: sessionmaker[Session]
) -> None:
    opted_out = service.receive(
        LeadIntake(name="Old", email="gone@example.com", consent=True), LeadSource.FORM
    ).lead_id
    service.receive(LeadIntake(name="Busy", phone="+1 303 555 0177", consent=True), LeadSource.FORM)
    with sessions.begin() as session:
        lead = session.get_one(Lead, opted_out)
        lead.status = LeadStatus.OPTED_OUT
        lead.opted_out_at = MIDDAY

    result = ReactivationService(sessions).import_contacts(
        "Spring quotes",
        [
            contact(),
            contact(name="Tom Reed", email=None, phone="(303) 555-0101"),
            contact(name="No Contact", email=None),
            contact(name="Bad Phone", email=None, phone="12"),
            contact(name="Maria Again", email="MARIA@example.com"),
            contact(name="Unsubscribed", email="gone@example.com"),
            contact(name="Active", email=None, phone="303-555-0177"),
        ],
        "Past quote requests.",
    )

    assert result.queued == 2
    assert [(s.row, s.reason) for s in result.skipped] == [
        (3, "needs an email address or a phone number"),
        (4, "phone: not a valid phone number"),
        (5, "duplicate in this list"),
        (6, "opted out earlier"),
        (7, "already in a recent conversation"),
    ]
    maria, tom = leads_of(sessions)
    assert (maria.status, maria.outreach_pending, maria.source_detail) == (
        LeadStatus.NEW,
        True,
        "Spring quotes",
    )
    assert maria.consent_text == 'Reactivation campaign "Spring quotes": Past quote requests.'
    assert tom.phone == "(303) 555-0101"


def test_old_conversations_can_be_reactivated(
    service: LeadService, sessions: sessionmaker[Session]
) -> None:
    old = service.receive(
        LeadIntake(name="Old Lead", email="old@example.com", consent=True), LeadSource.FORM
    ).lead_id
    with sessions.begin() as session:
        session.execute(
            update(Lead)
            .where(Lead.id == old)
            .values(status=LeadStatus.NURTURING, updated_at=MIDDAY - timedelta(days=90))
        )
    result = ReactivationService(sessions).import_contacts(
        "Q4", [contact(email="old@example.com")], "Past customers."
    )
    assert result.queued == 1


# --- Sending the opener --------------------------------------------------------------------


def test_scheduler_sends_one_opener_then_schedules_follow_ups(
    sessions: sessionmaker[Session],
) -> None:
    ReactivationService(sessions).import_contacts("Q4", [contact()], "Past quote requests.")
    scheduler, channel, (model, _) = make_scheduler(
        sessions,
        [ModelResponse("Hi Maria, it's Alex from Brightline. Still thinking about solar?")],
    )

    assert scheduler(MIDDAY).run_once() == 1
    assert scheduler(MIDDAY).run_once() == 0  # Never sent twice.

    assert channel.sent == ["Hi Maria, it's Alex from Brightline. Still thinking about solar?"]
    messages, tools = model.requests[0]
    assert tools == []
    assert "Solar installation" in messages[-1].content
    assert "Asked for a quote in spring" in messages[-1].content
    [lead] = leads_of(sessions)
    assert (lead.status, lead.outreach_pending) == (LeadStatus.CONTACTED, False)
    assert lead.next_follow_up_at is not None
    assert "reactivation.sent" in {e.type for e in lead.events}


def test_openers_wait_for_the_end_of_quiet_hours(sessions: sessionmaker[Session]) -> None:
    ReactivationService(sessions).import_contacts("Q4", [contact()], "Past customers.")
    scheduler, channel, _ = make_scheduler(sessions, [ModelResponse("Hi!")])
    assert scheduler(NIGHT).run_once() == 0
    assert channel.sent == []


def test_a_template_is_used_if_the_model_fails(sessions: sessionmaker[Session]) -> None:
    class Broken:
        def complete(self, messages: list[Message], tools: list[ToolSpec]) -> ModelResponse:
            raise LLMError("down")

    ReactivationService(sessions).import_contacts("Q4", [contact()], "Past customers.")
    scheduler, channel, _ = make_scheduler(sessions, model=Broken())
    scheduler(MIDDAY).run_once()
    assert channel.sent == [
        "Hi Maria, it's Alex from Brightline Solar & Roofing. You got in touch with us about "
        "solar installation a while back. Is that still something you're thinking about?"
    ]


def test_a_reply_continues_as_a_normal_conversation(sessions: sessionmaker[Session]) -> None:
    ReactivationService(sessions).import_contacts("Q4", [contact()], "Past customers.")
    scheduler, channel, (model, responder) = make_scheduler(
        sessions, [ModelResponse("Hi Maria! Still keen on solar?"), ModelResponse("Great!")]
    )
    scheduler(MIDDAY).run_once()
    [lead] = leads_of(sessions)
    responder.handle_inbound(lead.id, "Yes, actually!", "email")

    history = [m.content for m in model.requests[1][0][1:]]
    assert history[0].startswith("[Maria Gomez, a past enquiry or customer, was re-contacted")
    assert history[1:] == ["Hi Maria! Still keen on solar?", "Yes, actually!"]
    assert channel.sent[-1] == "Great!"


# --- Stats and API ---------------------------------------------------------------------------


def test_stats_per_campaign(sessions: sessionmaker[Session]) -> None:
    campaigns = ReactivationService(sessions)
    campaigns.import_contacts("A", [contact(), contact(name="B", email="b@example.com")], "x")
    campaigns.import_contacts("B", [contact(name="C", email="c@example.com")], "x")
    with sessions.begin() as session:
        lead = session.scalars(select(Lead).where(Lead.email == "b@example.com")).one()
        lead.outreach_pending = False
        lead.status = LeadStatus.BOOKED
        lead.last_inbound_at = MIDDAY

    stats = {s.campaign: s for s in campaigns.stats()}
    assert (stats["A"].total, stats["A"].waiting, stats["A"].replied, stats["A"].booked) == (
        2,
        1,
        1,
        1,
    )
    assert stats["B"].total == 1


@pytest.fixture
def admin(service: LeadService, sessions: sessionmaker[Session]) -> Any:
    profile = load_profile()
    responder = LeadResponder(
        sessions, QualifyingAgent(ScriptedModel([]), profile), profile, [Capture()]
    )
    app = create_app(Settings(admin_password=SecretStr(PASSWORD)), service, responder)
    with TestClient(app) as client:
        client.post("/api/admin/login", json={"password": PASSWORD})
        yield client


def test_campaign_api_requires_confirmed_consent(admin: TestClient) -> None:
    body = {"name": "Q4", "consent_confirmed": False, "contacts": [contact()]}
    assert admin.post("/api/admin/campaigns", json=body).status_code == 400

    body["consent_confirmed"] = True
    created = admin.post("/api/admin/campaigns", json=body)
    assert created.status_code == 201
    assert created.json()["queued"] == 1
    [stats] = admin.get("/api/admin/campaigns").json()
    assert (stats["campaign"], stats["waiting"]) == ("Q4", 1)
