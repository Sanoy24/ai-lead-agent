from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from lead_agent.agent import QualifyingAgent
from lead_agent.api import create_app
from lead_agent.channels import Recipient
from lead_agent.config import Settings
from lead_agent.db import Lead, LeadSource, LeadStatus
from lead_agent.leads import LeadIntake, LeadService
from lead_agent.llm import ModelResponse, ScriptedModel
from lead_agent.profile import load_profile
from lead_agent.responder import LeadResponder

PASSWORD = "correct horse battery staple"


class FakeChannel:
    name = "fake"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def can_reach(self, recipient: Recipient) -> bool:
        return recipient.email is not None

    def send(self, recipient: Recipient, text: str) -> None:
        self.sent.append(text)


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def responder(sessions: sessionmaker[Session], channel: FakeChannel) -> LeadResponder:
    profile = load_profile()
    model = ScriptedModel([ModelResponse("Hi! Do you own the home?")] * 5)
    return LeadResponder(sessions, QualifyingAgent(model, profile), profile, [channel])


@pytest.fixture
def client(service: LeadService, responder: LeadResponder) -> Iterator[TestClient]:
    settings = Settings(admin_password=SecretStr(PASSWORD))
    with TestClient(create_app(settings, service, responder)) as test_client:
        yield test_client


@pytest.fixture
def signed_in(client: TestClient) -> TestClient:
    assert client.post("/api/admin/login", json={"password": PASSWORD}).status_code == 204
    return client


def new_lead(
    service: LeadService, name: str = "Dana Lopez", email: str = "dana@example.com"
) -> str:
    intake = LeadIntake(name=name, email=email, message="Solar for my house", consent=True)
    return service.receive(intake, LeadSource.FORM).lead_id


# --- Auth ------------------------------------------------------------------------


def test_admin_api_is_off_without_a_password(
    service: LeadService, responder: LeadResponder
) -> None:
    with TestClient(create_app(Settings(), service, responder)) as client:
        assert client.get("/api/admin/session").status_code == 404
        assert client.post("/api/admin/login", json={"password": "x"}).status_code == 404


def test_login_sets_a_strict_http_only_cookie(client: TestClient) -> None:
    assert client.get("/api/admin/leads").status_code == 401
    assert client.post("/api/admin/login", json={"password": "wrong"}).status_code == 401

    response = client.post("/api/admin/login", json={"password": PASSWORD})
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "path=/api/admin" in cookie
    assert client.get("/api/admin/session").status_code == 204

    client.post("/api/admin/logout")
    assert client.get("/api/admin/session").status_code == 401


def test_login_is_rate_limited(client: TestClient) -> None:
    codes = [client.post("/api/admin/login", json={"password": "x"}).status_code for _ in range(6)]
    assert codes[-1] == 429


def test_forged_cookies_are_rejected(client: TestClient) -> None:
    client.cookies.set("lead_admin", "9999999999.abc.def", path="/api/admin")
    assert client.get("/api/admin/session").status_code == 401


# --- Leads -----------------------------------------------------------------------


def test_leads_are_listed_filtered_and_searched(
    signed_in: TestClient, service: LeadService, responder: LeadResponder
) -> None:
    dana = new_lead(service)
    new_lead(service, "Sam Carter", "sam@example.com")
    responder.reply_to_new_lead(dana)

    page = signed_in.get("/api/admin/leads").json()
    assert page["total"] == 2
    assert page["leads"][0]["id"] == dana  # Most recent activity first.
    assert page["leads"][0]["last_message"] == "Hi! Do you own the home?"
    assert page["leads"][0]["first_response_seconds"] is not None

    assert signed_in.get("/api/admin/leads", params={"q": "SAM"}).json()["total"] == 1
    qualifying = signed_in.get("/api/admin/leads", params={"status": "qualifying"}).json()
    assert [lead["id"] for lead in qualifying["leads"]] == [dana]


def test_lead_detail_shows_the_conversation_and_audit_trail(
    signed_in: TestClient, service: LeadService, responder: LeadResponder
) -> None:
    lead_id = new_lead(service)
    responder.reply_to_new_lead(lead_id)

    detail = signed_in.get(f"/api/admin/leads/{lead_id}").json()
    assert [(m["author"], m["content"]) for m in detail["messages"]] == [
        ("lead", "Solar for my house"),
        ("agent", "Hi! Do you own the home?"),
    ]
    assert "lead.created" in [e["type"] for e in detail["events"]]
    assert detail["consent_text"]
    assert signed_in.get("/api/admin/leads/nope").status_code == 404


def test_take_over_reply_as_a_person_and_hand_back(
    signed_in: TestClient,
    service: LeadService,
    responder: LeadResponder,
    sessions: sessionmaker[Session],
    channel: FakeChannel,
) -> None:
    lead_id = new_lead(service)
    responder.reply_to_new_lead(lead_id)

    sent = signed_in.post(
        f"/api/admin/leads/{lead_id}/messages", json={"text": "Hi Dana, Maria here."}
    ).json()
    assert sent["ai_paused"] is True
    assert sent["next_follow_up_at"] is None
    assert (sent["messages"][-1]["author"], channel.sent[-1]) == ("team", "Hi Dana, Maria here.")

    # While a person has the conversation, the AI stays quiet.
    responder.handle_inbound(lead_id, "Great, thanks Maria", "email")
    assert channel.sent[-1] == "Hi Dana, Maria here."
    attention = signed_in.get("/api/admin/leads", params={"attention": True}).json()
    assert attention["total"] == 1

    resumed = signed_in.post(f"/api/admin/leads/{lead_id}/resume").json()
    assert resumed["ai_paused"] is False
    assert resumed["next_follow_up_at"] is not None
    types = [e["type"] for e in resumed["events"]]
    assert "lead.taken_over" in types
    assert "lead.resumed" in types


def test_opted_out_leads_cannot_be_messaged(
    signed_in: TestClient, service: LeadService, responder: LeadResponder
) -> None:
    lead_id = new_lead(service)
    responder.handle_inbound(lead_id, "STOP", "email")
    response = signed_in.post(f"/api/admin/leads/{lead_id}/messages", json={"text": "Hello?"})
    assert response.status_code == 409
    assert (
        signed_in.post(
            f"/api/admin/leads/{lead_id}/status", json={"status": "qualified"}
        ).status_code
        == 409
    )


def test_status_can_be_corrected_by_hand(
    signed_in: TestClient, service: LeadService, sessions: sessionmaker[Session]
) -> None:
    lead_id = new_lead(service)
    detail = signed_in.post(f"/api/admin/leads/{lead_id}/status", json={"status": "lost"}).json()
    assert detail["status"] == "lost"
    assert detail["events"][-1]["detail"]["by"] == "team"
    bad = signed_in.post(f"/api/admin/leads/{lead_id}/status", json={"status": "opted_out"})
    assert bad.status_code == 422


# --- Metrics ---------------------------------------------------------------------


def test_metrics_report_pipeline_and_speed_to_lead(
    signed_in: TestClient,
    service: LeadService,
    responder: LeadResponder,
    sessions: sessionmaker[Session],
) -> None:
    fast, slow, booked = (
        new_lead(service, "A", "a@example.com"),
        new_lead(service, "B", "b@example.com"),
        new_lead(service, "C", "c@example.com"),
    )
    with sessions.begin() as session:
        for lead_id, seconds in ((fast, 20), (slow, 600), (booked, 40)):
            lead = session.get_one(Lead, lead_id)
            lead.first_response_at = lead.created_at + timedelta(seconds=seconds)
        session.get_one(Lead, booked).status = LeadStatus.BOOKED

    metrics = signed_in.get("/api/admin/metrics", params={"days": 7}).json()
    assert metrics["total_leads"] == 3
    assert metrics["booked"] == 1
    assert metrics["booking_rate"] == pytest.approx(1 / 3)
    assert metrics["median_first_response_seconds"] == pytest.approx(40)
    assert metrics["responded_within_target"] == pytest.approx(2 / 3)
    assert metrics["by_source"] == {"form": 3}
    assert len(metrics["daily"]) == 7
    assert metrics["daily"][-1]["leads"] == 3


def test_metrics_are_empty_without_leads(signed_in: TestClient) -> None:
    metrics = signed_in.get("/api/admin/metrics").json()
    assert metrics["total_leads"] == 0
    assert metrics["median_first_response_seconds"] is None
