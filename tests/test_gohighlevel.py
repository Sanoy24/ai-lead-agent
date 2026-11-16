import json
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from lead_agent.api import create_app
from lead_agent.config import Settings
from lead_agent.crm.gohighlevel import (
    API_VERSION,
    GoHighLevelClient,
    GoHighLevelError,
    GoHighLevelSync,
    contact_url,
    webhook_lead_fields,
)
from lead_agent.db import Lead, LeadEvent, LeadSource, LeadStatus
from lead_agent.leads import LeadIntake, LeadService
from lead_agent.responder import LeadResponder

TOKEN = "pit-test-token"
LOCATION = "loc-123"
SECRET = "ghl-webhook-secret"


class FakeGoHighLevel:
    """In-memory GoHighLevel: contacts deduplicated by email or phone, tags and notes."""

    def __init__(self) -> None:
        self.contacts: dict[str, dict[str, Any]] = {}
        self.tags: dict[str, set[str]] = {}
        self.notes: dict[str, list[str]] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.headers["Version"] == API_VERSION
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, request.url.path, body))
        if self.fail:
            return httpx.Response(503, json={"message": "Unavailable"})
        path = request.url.path
        if path == "/contacts/upsert":
            assert body["locationId"] == LOCATION
            match = next(
                (
                    cid
                    for cid, c in self.contacts.items()
                    if (body.get("email") and c.get("email") == body.get("email"))
                    or (body.get("phone") and c.get("phone") == body.get("phone"))
                ),
                None,
            )
            cid = match or f"c{len(self.contacts) + 1}"
            self.contacts.setdefault(cid, {}).update(body)
            return httpx.Response(200, json={"contact": {"id": cid}, "new": match is None})
        cid = path.split("/")[2]
        if path.endswith("/tags") and request.method == "POST":
            self.tags.setdefault(cid, set()).update(body["tags"])
        elif path.endswith("/tags") and request.method == "DELETE":
            self.tags.setdefault(cid, set()).difference_update(body["tags"])
        elif path.endswith("/notes"):
            self.notes.setdefault(cid, []).append(body["body"])
        return httpx.Response(200, json={})

    def client(self) -> GoHighLevelClient:
        return GoHighLevelClient(TOKEN, LOCATION, transport=httpx.MockTransport(self.handler))


def new_lead(service: LeadService, **overrides: Any) -> str:
    data: dict[str, Any] = {
        "name": "Dana Lopez",
        "email": "dana@example.com",
        "phone": "+13035550100",
        "message": "Leak in the attic <urgent>",
        "service_interest": "Roof repair",
        "consent": True,
    }
    data.update(overrides)
    return service.receive(LeadIntake.model_validate(data), LeadSource.FORM).lead_id


def load(sessions: sessionmaker[Session], lead_id: str) -> Lead:
    with sessions() as session:
        return session.get_one(Lead, lead_id)


# --- Sync ----------------------------------------------------------------------------


def test_new_lead_becomes_a_tagged_contact_with_a_plain_text_note(
    service: LeadService, sessions: sessionmaker[Session]
) -> None:
    fake = FakeGoHighLevel()
    lead_id = new_lead(service)

    assert GoHighLevelSync(sessions, fake.client()).sync_once() == 1

    contact_id = load(sessions, lead_id).crm_contact_id
    assert contact_id == "c1"
    contact = fake.contacts["c1"]
    assert (contact["firstName"], contact["lastName"], contact["email"], contact["phone"]) == (
        "Dana",
        "Lopez",
        "dana@example.com",
        "+13035550100",
    )
    assert "tags" not in contact  # The business's own tags are never overwritten.
    assert fake.tags["c1"] == {"ai-lead: new"}
    [note] = fake.notes["c1"]
    assert "<" not in note.replace("<urgent>", "")  # HTML is converted to plain text.
    assert "New enquiry via form" in note
    assert "Leak in the attic <urgent>" in note


def test_stage_tag_follows_the_pipeline(
    service: LeadService, sessions: sessionmaker[Session]
) -> None:
    fake = FakeGoHighLevel()
    lead_id = new_lead(service)
    ghl = GoHighLevelSync(sessions, fake.client())
    ghl.sync_once()
    fake.tags["c1"].add("vip")  # A tag the business added itself.

    with sessions.begin() as session:
        lead = session.get_one(Lead, lead_id)
        lead.status = LeadStatus.BOOKED
        lead.events.append(LeadEvent(type="lead.status_changed", detail={"to": "booked"}))
    ghl.sync_once()

    assert fake.tags["c1"] == {"ai-lead: booked", "vip"}
    assert len(fake.notes["c1"]) == 2


def test_outages_are_retried_and_errors_hide_the_token(
    service: LeadService, sessions: sessionmaker[Session]
) -> None:
    fake = FakeGoHighLevel()
    new_lead(service)
    ghl = GoHighLevelSync(sessions, fake.client())
    fake.fail = True
    assert ghl.sync_once() == 0
    fake.fail = False
    assert ghl.sync_once() == 1

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"no route {request.headers}")

    client = GoHighLevelClient(TOKEN, LOCATION, transport=httpx.MockTransport(fail))
    with pytest.raises(GoHighLevelError) as error:
        client.add_note("c1", "hi")
    assert TOKEN not in str(error.value)


# --- Inbound webhook --------------------------------------------------------------------


def test_workflow_payloads_are_read_flexibly() -> None:
    fields = webhook_lead_fields(
        {
            "first_name": "Sam",
            "last_name": "Carter",
            "email": "sam@example.com",
            "contact_id": "abc",
            "customData": {"service": "Solar installation", "message": "Quote please"},
        }
    )
    assert fields == {
        "name": "Sam Carter",
        "email": "sam@example.com",
        "phone": None,
        "message": "Quote please",
        "service_interest": "Solar installation",
        "contact_id": "abc",
    }


class RecordingResponder:
    def __init__(self) -> None:
        self.first_replies: list[str] = []

    def schedule_first_reply(self, lead_id: str) -> None:
        self.first_replies.append(lead_id)

    def schedule_reply(self, lead_id: str) -> None:
        pass

    def shutdown(self) -> None:
        pass


def client_for(service: LeadService, responder: RecordingResponder, **settings: Any) -> TestClient:
    config = Settings.model_validate(settings)
    return TestClient(create_app(config, service, cast(LeadResponder, responder)))


def test_webhook_creates_a_lead_linked_to_the_ghl_contact(
    service: LeadService, sessions: sessionmaker[Session]
) -> None:
    responder = RecordingResponder()
    payload = {"full_name": "Sam Carter", "phone": "+13035550111", "contact_id": "ghl-42"}
    with client_for(service, responder, gohighlevel_webhook_secret=SecretStr(SECRET)) as client:
        response = client.post(
            "/api/webhooks/gohighlevel", json=payload, headers={"X-Webhook-Secret": SECRET}
        )
        by_query = client.post(
            f"/api/webhooks/gohighlevel?secret={SECRET}",
            json={**payload, "phone": "+13035550112", "contact_id": "ghl-43"},
        )

    assert (response.status_code, by_query.status_code) == (202, 202)
    lead = load(sessions, responder.first_replies[0])
    assert (lead.source, lead.source_detail, lead.crm_contact_id) == (
        LeadSource.WEBHOOK,
        "gohighlevel",
        "ghl-42",
    )
    assert lead.consent_text is not None
    assert "GoHighLevel" in lead.consent_text


def test_webhook_rejects_bad_secrets_and_bad_leads(service: LeadService) -> None:
    responder = RecordingResponder()
    with client_for(service, responder, gohighlevel_webhook_secret=SecretStr(SECRET)) as client:
        url = "/api/webhooks/gohighlevel"
        assert client.post(url, json={"email": "a@b.co"}).status_code == 401
        wrong = {"X-Webhook-Secret": "nope"}
        assert client.post(url, json={"email": "a@b.co"}, headers=wrong).status_code == 401
        no_contact = {"X-Webhook-Secret": SECRET}
        assert client.post(url, json={"full_name": "X"}, headers=no_contact).status_code == 422
    with client_for(service, responder) as client:
        assert client.post("/api/webhooks/gohighlevel", json={}).status_code == 404
    assert responder.first_replies == []


def test_contact_link_points_at_the_location() -> None:
    assert contact_url(LOCATION, "c1") == (
        "https://app.gohighlevel.com/v2/location/loc-123/contacts/detail/c1"
    )
