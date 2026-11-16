import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from lead_agent.api import create_app
from lead_agent.api.app import CALCOM_SIGNATURE_HEADER, SIGNATURE_HEADER, sign
from lead_agent.config import Settings
from lead_agent.db import Lead, LeadSource, LeadStatus
from lead_agent.leads import LeadIntake, LeadService
from lead_agent.responder import LeadResponder

SECRET = "whsec-test-secret"

ClientFactory = Callable[..., TestClient]

FORM: dict[str, Any] = {
    "name": "Sam Carter",
    "email": "sam.carter@example.com",
    "message": "Our roof leaks when it rains. Can you take a look?",
    "service_interest": "Roof repair",
    "consent": True,
}


class RecordingResponder:
    """Stands in for the responder: records which leads would get a reply."""

    def __init__(self) -> None:
        self.first_replies: list[str] = []
        self.replies: list[str] = []
        self.notifications: list[tuple[str, str]] = []

    def schedule_first_reply(self, lead_id: str) -> None:
        self.first_replies.append(lead_id)

    def schedule_reply(self, lead_id: str) -> None:
        self.replies.append(lead_id)

    def schedule_notify(self, lead_id: str, text: str) -> None:
        self.notifications.append((lead_id, text))

    def shutdown(self) -> None:
        pass


@pytest.fixture
def responder() -> RecordingResponder:
    return RecordingResponder()


@pytest.fixture
def make_client(service: LeadService, responder: RecordingResponder) -> ClientFactory:
    def factory(**overrides: object) -> TestClient:
        settings = Settings.model_validate({"webhook_secret": SecretStr(SECRET), **overrides})
        return TestClient(create_app(settings, service, cast(LeadResponder, responder)))

    return factory


def lead_count(sessions: sessionmaker[Session]) -> int:
    with sessions() as session:
        return session.scalar(select(func.count()).select_from(Lead)) or 0


# --- Website form --------------------------------------------------------------


def test_form_submission_creates_a_lead_and_schedules_a_reply(
    make_client: ClientFactory, sessions: sessionmaker[Session], responder: RecordingResponder
) -> None:
    with make_client() as client:
        response = client.post("/api/leads", json=FORM)
        client.post("/api/leads", json={**FORM, "message": "Any update?"})
    assert response.status_code == 202
    assert response.json() == {"status": "received"}
    assert lead_count(sessions) == 1
    assert len(responder.first_replies) == 1
    assert responder.replies == responder.first_replies


def test_form_requires_consent(make_client: ClientFactory) -> None:
    with make_client() as client:
        assert client.post("/api/leads", json={**FORM, "consent": False}).status_code == 422
        missing = {k: v for k, v in FORM.items() if k != "consent"}
        assert client.post("/api/leads", json=missing).status_code == 422


def test_spam_trap_submissions_are_dropped_silently(
    make_client: ClientFactory, sessions: sessionmaker[Session], responder: RecordingResponder
) -> None:
    with make_client() as client:
        response = client.post("/api/leads", json={**FORM, "website": "http://spam.example"})
    assert response.status_code == 202
    assert lead_count(sessions) == 0
    assert responder.first_replies == []


def test_form_is_rate_limited(make_client: ClientFactory) -> None:
    with make_client(form_rate_limit_per_minute=2) as client:
        codes = [
            client.post("/api/leads", json={**FORM, "email": f"p{i}@example.com"}).status_code
            for i in range(3)
        ]
    assert codes == [202, 202, 429]


def test_form_config_exposes_consent_text(make_client: ClientFactory) -> None:
    with make_client(business_name="Test Roofing") as client:
        body = client.get("/api/form-config").json()
    assert body["business_name"] == "Test Roofing"
    assert "opt out" in body["consent_text"]


# --- Signed webhook ------------------------------------------------------------


def post_webhook(
    client: TestClient, payload: dict[str, Any], signature: str | None = "valid"
) -> int:
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if signature == "valid":
        headers[SIGNATURE_HEADER] = sign(body, SECRET)
    elif signature is not None:
        headers[SIGNATURE_HEADER] = signature
    response = client.post("/api/webhooks/leads", content=body, headers=headers)
    return int(response.status_code)


def test_signed_webhook_creates_a_lead(
    make_client: ClientFactory, sessions: sessionmaker[Session]
) -> None:
    with make_client() as client:
        assert post_webhook(client, {**FORM, "source_detail": "facebook"}) == 202
    with sessions() as session:
        lead = session.scalars(select(Lead)).one()
        assert (lead.source, lead.source_detail) == ("webhook", "facebook")


@pytest.mark.parametrize("signature", [None, "sha256=deadbeef", "garbage"])
def test_unsigned_or_forged_webhooks_are_rejected(
    make_client: ClientFactory, sessions: sessionmaker[Session], signature: str | None
) -> None:
    with make_client() as client:
        assert post_webhook(client, FORM, signature) == 401
    assert lead_count(sessions) == 0


def test_webhook_signed_with_another_secret_is_rejected(make_client: ClientFactory) -> None:
    body = json.dumps(FORM).encode()
    with make_client() as client:
        response = client.post(
            "/api/webhooks/leads",
            content=body,
            headers={SIGNATURE_HEADER: sign(body, "wrong-secret")},
        )
    assert response.status_code == 401


def test_webhook_rejects_invalid_leads_and_large_bodies(make_client: ClientFactory) -> None:
    with make_client() as client:
        assert post_webhook(client, {**FORM, "consent": False}) == 422
        assert post_webhook(client, {**FORM, "message": "x" * 70_000}) == 413


def test_webhook_is_disabled_without_a_secret(make_client: ClientFactory) -> None:
    with make_client(webhook_secret=None) as client:
        assert post_webhook(client, FORM) == 404


# --- Cal.com bookings ------------------------------------------------------------

CAL_SECRET = "cal-test-secret"


def cal_event(lead_id: str, trigger: str = "BOOKING_CREATED") -> bytes:
    return json.dumps(
        {
            "triggerEvent": trigger,
            "payload": {
                "uid": "bk_1",
                "startTime": "2026-11-20T16:00:00Z",
                "attendees": [{"email": "nobody@example.com", "name": "Sam Carter"}],
                "metadata": {"lead_id": lead_id},
            },
        }
    ).encode()


def cal_signature(body: bytes, secret: str = CAL_SECRET) -> str:
    return sign(body, secret).removeprefix("sha256=")


def post_booking(client: TestClient, body: bytes, signature: str | None = None) -> int:
    headers = {CALCOM_SIGNATURE_HEADER: signature} if signature is not None else {}
    return int(client.post("/api/webhooks/calcom", content=body, headers=headers).status_code)


def test_signed_booking_marks_the_lead_booked_and_confirms(
    make_client: ClientFactory,
    service: LeadService,
    sessions: sessionmaker[Session],
    responder: RecordingResponder,
) -> None:
    lead_id = service.receive(LeadIntake.model_validate(FORM), LeadSource.FORM).lead_id
    body = cal_event(lead_id)
    with make_client(calcom_webhook_secret=SecretStr(CAL_SECRET)) as client:
        assert post_booking(client, body, cal_signature(body)) == 200
    with sessions() as session:
        assert session.get_one(Lead, lead_id).status == LeadStatus.BOOKED
    # 16:00 UTC is 9:00 AM in Denver, the demo business's time zone.
    assert responder.notifications == [
        (lead_id, "You're all set for Friday, November 20 at 9:00 AM. See you then!")
    ]


def test_booking_webhook_rejects_bad_signatures_and_is_off_without_a_secret(
    make_client: ClientFactory,
) -> None:
    body = cal_event("x")
    with make_client(calcom_webhook_secret=SecretStr(CAL_SECRET)) as client:
        assert post_booking(client, body, cal_signature(body, "wrong")) == 401
        assert post_booking(client, body) == 401
    with make_client() as client:
        assert post_booking(client, body) == 404


def test_cal_ping_and_unknown_bookings_are_accepted_quietly(
    make_client: ClientFactory, responder: RecordingResponder
) -> None:
    ping = json.dumps({"triggerEvent": "PING", "payload": {}}).encode()
    with make_client(calcom_webhook_secret=SecretStr(CAL_SECRET)) as client:
        for body in (ping, cal_event("unknown-lead")):
            assert post_booking(client, body, cal_signature(body)) == 200
    assert responder.notifications == []


# --- Frontend ----------------------------------------------------------------------


def test_built_frontend_is_served_with_client_side_routes(
    tmp_path: Path, service: LeadService, responder: RecordingResponder
) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<div id=root></div>")
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "favicon.svg").write_text("<svg/>")
    app = create_app(Settings(), service, cast(LeadResponder, responder), frontend_dir=tmp_path)
    with TestClient(app) as client:
        assert client.get("/").text == "<div id=root></div>"
        assert client.get("/admin/leads/abc").text == "<div id=root></div>"
        assert client.get("/assets/app.js").text == "console.log(1)"
        assert client.get("/favicon.svg").status_code == 200
        assert client.get("/api/nope").status_code == 404
        assert client.get("/../pyproject.toml").text == "<div id=root></div>"


def test_missing_frontend_build_explains_how_to_build(
    tmp_path: Path, service: LeadService, responder: RecordingResponder
) -> None:
    app = create_app(Settings(), service, cast(LeadResponder, responder), frontend_dir=tmp_path)
    with TestClient(app) as client:
        response = client.get("/admin")
    assert response.status_code == 404
    assert "npm --prefix frontend" in response.text
