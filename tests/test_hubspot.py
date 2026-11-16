import json
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from lead_agent.crm.hubspot import HubSpotClient, HubSpotError, HubSpotSync
from lead_agent.db import Lead, LeadEvent, LeadSource, LeadStatus
from lead_agent.leads import LeadIntake, LeadService

TOKEN = "pat-test-token"


class FakeHubSpot:
    """A tiny in-memory HubSpot: contacts and notes."""

    def __init__(self, existing: dict[str, str] | None = None) -> None:
        self.contacts: dict[str, dict[str, str]] = {
            cid: {"email": email} for email, cid in (existing or {}).items()
        }
        self.notes: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        path = request.url.path
        self.calls.append((request.method, path))
        if self.fail:
            return httpx.Response(503, json={"message": "Service unavailable"})
        body = json.loads(request.content) if request.content else {}
        if path == "/crm/v3/objects/contacts/search":
            email = body["filterGroups"][0]["filters"][0]["value"]
            found = [{"id": cid} for cid, c in self.contacts.items() if c.get("email") == email]
            return httpx.Response(200, json={"results": found})
        if path == "/crm/v3/objects/contacts" and request.method == "POST":
            cid = str(1000 + len(self.contacts))
            self.contacts[cid] = dict(body["properties"])
            return httpx.Response(201, json={"id": cid})
        if path.startswith("/crm/v3/objects/contacts/") and request.method == "PATCH":
            self.contacts[path.rsplit("/", 1)[1]].update(body["properties"])
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[1]})
        if path == "/crm/v3/objects/notes":
            self.notes.append(body)
            return httpx.Response(201, json={"id": str(len(self.notes))})
        return httpx.Response(404, json={"message": "not found"})

    def client(self) -> HubSpotClient:
        return HubSpotClient(TOKEN, transport=httpx.MockTransport(self.handler))

    def note_bodies(self) -> list[str]:
        return [str(n["properties"]["hs_note_body"]) for n in self.notes]


def new_lead(service: LeadService, **overrides: Any) -> str:
    data: dict[str, Any] = {
        "name": "Dana Lopez",
        "email": "dana@example.com",
        "phone": "+13035550100",
        "message": "Solar for my house <please>",
        "service_interest": "Solar installation",
        "consent": True,
    }
    data.update(overrides)
    return service.receive(LeadIntake.model_validate(data), LeadSource.FORM).lead_id


def sync(sessions: sessionmaker[Session], fake: FakeHubSpot) -> HubSpotSync:
    return HubSpotSync(sessions, fake.client(), dashboard_url="https://leads.example.com/")


def load(sessions: sessionmaker[Session], lead_id: str) -> Lead:
    with sessions() as session:
        return session.get_one(Lead, lead_id)


def test_new_lead_becomes_a_contact_with_an_enquiry_note(
    service: LeadService, sessions: sessionmaker[Session]
) -> None:
    fake = FakeHubSpot()
    lead_id = new_lead(service)

    assert sync(sessions, fake).sync_once() == 1

    contact_id = load(sessions, lead_id).crm_contact_id
    assert contact_id is not None
    assert fake.contacts[contact_id] == {
        "firstname": "Dana",
        "lastname": "Lopez",
        "email": "dana@example.com",
        "phone": "+13035550100",
        "hs_lead_status": "NEW",
    }
    [note] = fake.notes
    assert note["associations"][0]["to"]["id"] == contact_id
    body = fake.note_bodies()[0]
    assert "New enquiry" in body
    assert "&lt;please&gt;" in body  # Lead text is escaped.
    assert f'href="https://leads.example.com/admin/leads/{lead_id}"' in body


def test_unchanged_leads_are_not_synced_again(
    service: LeadService, sessions: sessionmaker[Session]
) -> None:
    fake = FakeHubSpot()
    new_lead(service)
    hubspot = sync(sessions, fake)
    hubspot.sync_once()
    calls = len(fake.calls)

    assert hubspot.sync_once() == 0
    assert len(fake.calls) == calls


def test_changes_update_the_contact_and_add_notes_for_key_moments(
    service: LeadService, sessions: sessionmaker[Session]
) -> None:
    fake = FakeHubSpot()
    lead_id = new_lead(service)
    hubspot = sync(sessions, fake)
    hubspot.sync_once()

    with sessions.begin() as session:
        lead = session.get_one(Lead, lead_id)
        lead.status = LeadStatus.BOOKED
        lead.answers = {"zip_code": "80202"}
        lead.events.append(LeadEvent(type="lead.status_changed", detail={"to": "booked"}))
        lead.events.append(LeadEvent(type="answers.recorded", detail={"zip_code": "80202"}))
    assert hubspot.sync_once() == 1

    contact = fake.contacts[load(sessions, lead_id).crm_contact_id or ""]
    assert contact["hs_lead_status"] == "OPEN_DEAL"
    assert len(fake.notes) == 2  # Only the status change gets a note, not every answer.
    assert "zip code: 80202" in fake.note_bodies()[1]


def test_existing_contacts_are_matched_by_email_instead_of_duplicated(
    service: LeadService, sessions: sessionmaker[Session]
) -> None:
    fake = FakeHubSpot(existing={"dana@example.com": "555"})
    lead_id = new_lead(service)
    sync(sessions, fake).sync_once()

    assert load(sessions, lead_id).crm_contact_id == "555"
    assert len(fake.contacts) == 1
    assert fake.contacts["555"]["hs_lead_status"] == "NEW"


def test_telegram_leads_without_email_are_created_by_name(
    service: LeadService, sessions: sessionmaker[Session]
) -> None:
    fake = FakeHubSpot()
    lead_id = service.start_telegram_lead("42", "Sam")
    sync(sessions, fake).sync_once()

    contact_id = load(sessions, lead_id).crm_contact_id
    assert contact_id is not None
    assert fake.contacts[contact_id]["firstname"] == "Sam"
    assert ("POST", "/crm/v3/objects/contacts/search") not in fake.calls


def test_hubspot_outages_are_retried_on_the_next_pass(
    service: LeadService, sessions: sessionmaker[Session]
) -> None:
    fake = FakeHubSpot()
    lead_id = new_lead(service)
    hubspot = sync(sessions, fake)

    fake.fail = True
    assert hubspot.sync_once() == 0
    assert load(sessions, lead_id).crm_synced_at is None

    fake.fail = False
    assert hubspot.sync_once() == 1
    assert load(sessions, lead_id).crm_contact_id is not None


def test_errors_do_not_leak_the_token() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect, headers {request.headers}")

    client = HubSpotClient(TOKEN, transport=httpx.MockTransport(fail))
    with pytest.raises(HubSpotError) as error:
        client.find_contact("dana@example.com")
    assert TOKEN not in str(error.value)
