import json
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from lead_agent.api import create_app
from lead_agent.api.app import SIGNATURE_HEADER, sign
from lead_agent.config import Settings
from lead_agent.db import Lead
from lead_agent.leads import LeadService

SECRET = "whsec-test-secret"

ClientFactory = Callable[..., TestClient]

FORM: dict[str, Any] = {
    "name": "Sam Carter",
    "email": "sam.carter@example.com",
    "message": "Our roof leaks when it rains. Can you take a look?",
    "service_interest": "Roof repair",
    "consent": True,
}


@pytest.fixture
def make_client(service: LeadService) -> ClientFactory:
    def factory(**overrides: object) -> TestClient:
        settings = Settings.model_validate({"webhook_secret": SecretStr(SECRET), **overrides})
        return TestClient(create_app(settings, service))

    return factory


def lead_count(sessions: sessionmaker[Session]) -> int:
    with sessions() as session:
        return session.scalar(select(func.count()).select_from(Lead)) or 0


# --- Website form --------------------------------------------------------------


def test_form_submission_creates_a_lead(
    make_client: ClientFactory, sessions: sessionmaker[Session]
) -> None:
    with make_client() as client:
        response = client.post("/api/leads", json=FORM)
    assert response.status_code == 202
    assert response.json() == {"status": "received"}
    assert lead_count(sessions) == 1


def test_form_requires_consent(make_client: ClientFactory) -> None:
    with make_client() as client:
        assert client.post("/api/leads", json={**FORM, "consent": False}).status_code == 422
        missing = {k: v for k, v in FORM.items() if k != "consent"}
        assert client.post("/api/leads", json=missing).status_code == 422


def test_spam_trap_submissions_are_dropped_silently(
    make_client: ClientFactory, sessions: sessionmaker[Session]
) -> None:
    with make_client() as client:
        response = client.post("/api/leads", json={**FORM, "website": "http://spam.example"})
    assert response.status_code == 202
    assert lead_count(sessions) == 0


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
