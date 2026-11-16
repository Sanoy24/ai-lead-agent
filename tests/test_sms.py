import base64
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from lead_agent.agent import QualifyingAgent
from lead_agent.api import create_app
from lead_agent.api.app import TWILIO_SIGNATURE_HEADER
from lead_agent.channels import Recipient, SendError
from lead_agent.channels.sms import (
    OPT_OUT_FOOTER,
    SmsChannel,
    TwilioClient,
    TwilioError,
    phone_key,
    to_e164,
    twilio_signature,
    valid_twilio_signature,
)
from lead_agent.config import Settings
from lead_agent.db import Direction, Lead, LeadSource
from lead_agent.leads import SMS_CONSENT_TEXT, LeadIntake, LeadService
from lead_agent.llm import ModelResponse, ScriptedModel
from lead_agent.profile import load_profile
from lead_agent.responder import LeadResponder

SID = "ACtest-account"
TOKEN = "twilio-auth-token-123"
FROM = "+13035550199"


class FakeTwilio:
    def __init__(self, status: int = 201) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status >= 400:
            return httpx.Response(self.status, json={"code": 21608, "message": "Unverified"})
        return httpx.Response(self.status, json={"sid": "SM1"})

    def client(self) -> TwilioClient:
        return TwilioClient(SID, TOKEN, FROM, transport=httpx.MockTransport(self.handler))

    def sent(self) -> list[dict[str, str]]:
        return [dict(parse_qsl(r.content.decode())) for r in self.requests]


# --- Phone numbers -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+1 (303) 555-0100", "+13035550100"),
        ("(303) 555-0100", "+13035550100"),
        ("13035550100", "+13035550100"),
        ("0044 20 7946 0958", "+442079460958"),
        ("12", None),
    ],
)
def test_numbers_are_normalised_to_e164(raw: str, expected: str | None) -> None:
    assert to_e164(raw) == expected


def test_the_same_number_written_differently_matches() -> None:
    assert phone_key("+1 303-555-0100") == phone_key("(303) 555 0100")


# --- Twilio API and signatures ---------------------------------------------------------


def test_sms_is_sent_with_basic_auth_and_form_fields() -> None:
    fake = FakeTwilio()
    fake.client().send_sms("+13035550100", "Hi Dana!")

    [request] = fake.requests
    assert request.url.path == f"/2010-04-01/Accounts/{SID}/Messages.json"
    expected_auth = base64.b64encode(f"{SID}:{TOKEN}".encode()).decode()
    assert request.headers["Authorization"] == f"Basic {expected_auth}"
    assert fake.sent() == [{"To": "+13035550100", "From": FROM, "Body": "Hi Dana!"}]


def test_twilio_errors_do_not_leak_the_auth_token() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect {request.headers}")

    client = TwilioClient(SID, TOKEN, FROM, transport=httpx.MockTransport(fail))
    with pytest.raises(TwilioError) as error:
        client.send_sms("+13035550100", "hi")
    assert TOKEN not in str(error.value)


def test_signature_matches_twilio_algorithm_and_rejects_tampering() -> None:
    url = "https://leads.example.com/api/webhooks/sms"
    params = {"From": "+13035550100", "Body": "Hi", "To": FROM}
    signature = twilio_signature(url, params, TOKEN)
    assert valid_twilio_signature(url, params, signature, TOKEN)
    assert not valid_twilio_signature(url, {**params, "Body": "Hacked"}, signature, TOKEN)
    assert not valid_twilio_signature(url, params, None, TOKEN)


# --- Channel ---------------------------------------------------------------------


def recipient(**overrides: Any) -> Recipient:
    base: dict[str, Any] = {
        "lead_id": "1",
        "name": "Dana",
        "email": None,
        "telegram_chat_id": None,
        "phone": "(303) 555-0100",
    }
    base.update(overrides)
    return Recipient(**base)


def test_first_text_includes_the_opt_out_line_and_later_ones_do_not() -> None:
    fake = FakeTwilio()
    channel = SmsChannel(fake.client())
    channel.send(recipient(first_contact=True), "Hi Dana!")
    channel.send(recipient(), "Thanks!")

    assert [m["Body"] for m in fake.sent()] == [f"Hi Dana!\n\n{OPT_OUT_FOOTER}", "Thanks!"]
    assert fake.sent()[0]["To"] == "+13035550100"


def test_channel_needs_a_valid_phone_and_reports_failures() -> None:
    channel = SmsChannel(FakeTwilio(status=400).client())
    assert not channel.can_reach(recipient(phone=None))
    assert not channel.can_reach(recipient(phone="12"))
    with pytest.raises(SendError):
        channel.send(recipient(), "hi")


def test_long_messages_are_trimmed_to_fit() -> None:
    fake = FakeTwilio()
    SmsChannel(fake.client()).send(recipient(), "x" * 2000)
    assert len(fake.sent()[0]["Body"]) == 1500


# --- Replying on the channel the lead used ---------------------------------------------------


class Capture:
    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[str] = []

    def can_reach(self, recipient: Recipient) -> bool:
        return True

    def send(self, recipient: Recipient, text: str) -> None:
        self.sent.append(text)


def test_replies_go_back_on_the_channel_the_lead_last_used(
    service: LeadService, sessions: sessionmaker[Session]
) -> None:
    intake = LeadIntake(
        name="Dana", email="dana@example.com", phone="303-555-0100", message="Hi", consent=True
    )
    lead_id = service.receive(intake, LeadSource.FORM).lead_id
    email, sms = Capture("email"), Capture("sms")
    profile = load_profile()
    model = ScriptedModel([ModelResponse("Hi Dana!"), ModelResponse("Thanks!")])
    responder = LeadResponder(
        sessions, QualifyingAgent(model, profile), profile, cast(Any, [email, sms])
    )

    responder.reply_to_new_lead(lead_id)  # Came in by form: first channel that can reach.
    responder.handle_inbound(lead_id, "Yes I own it", "sms")

    assert email.sent == ["Hi Dana!"]
    assert sms.sent == ["Thanks!"]


# --- Webhook ----------------------------------------------------------------------


class RecordingResponder:
    def __init__(self) -> None:
        self.first_replies: list[str] = []
        self.inbound: list[tuple[str, str, str]] = []

    def schedule_first_reply(self, lead_id: str) -> None:
        self.first_replies.append(lead_id)

    def schedule_inbound(self, lead_id: str, text: str, channel: str) -> None:
        self.inbound.append((lead_id, text, channel))

    def shutdown(self) -> None:
        pass


@pytest.fixture
def responder() -> RecordingResponder:
    return RecordingResponder()


def client_for(service: LeadService, responder: RecordingResponder, **settings: Any) -> TestClient:
    config = Settings.model_validate({"twilio_auth_token": SecretStr(TOKEN), **settings})
    return TestClient(create_app(config, service, cast(LeadResponder, responder)))


def post_sms(client: TestClient, params: dict[str, str], url: str | None = None) -> Any:
    target = url or "http://testserver/api/webhooks/sms"
    signature = twilio_signature(target, params, TOKEN)
    return client.post(
        "/api/webhooks/sms",
        content=urlencode(params),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            TWILIO_SIGNATURE_HEADER: signature,
        },
    )


def test_a_text_from_a_new_number_starts_a_lead(
    service: LeadService, sessions: sessionmaker[Session], responder: RecordingResponder
) -> None:
    with client_for(service, responder) as client:
        response = post_sms(client, {"From": "+13035550142", "To": FROM, "Body": "Roof leak!"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/xml")
    [lead_id] = responder.first_replies
    with sessions() as session:
        lead = session.get_one(Lead, lead_id)
        assert (lead.source, lead.phone, lead.consent_text) == (
            LeadSource.SMS,
            "+13035550142",
            SMS_CONSENT_TEXT,
        )
        assert [(m.direction, m.channel, m.content) for m in lead.messages] == [
            (Direction.INBOUND, "sms", "Roof leak!")
        ]


def test_a_text_from_a_known_lead_continues_their_conversation(
    service: LeadService, responder: RecordingResponder
) -> None:
    intake = LeadIntake(name="Sam", phone="(303) 555-0100", consent=True)
    lead_id = service.receive(intake, LeadSource.FORM).lead_id
    with client_for(service, responder) as client:
        post_sms(client, {"From": "+13035550100", "To": FROM, "Body": "Yes, 80202"})

    assert responder.inbound == [(lead_id, "Yes, 80202", "sms")]
    assert responder.first_replies == []


def test_unsigned_texts_are_rejected_and_the_webhook_is_off_without_twilio(
    service: LeadService, responder: RecordingResponder
) -> None:
    with client_for(service, responder) as client:
        forged = client.post(
            "/api/webhooks/sms",
            content="From=%2B13035550100&Body=hi",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert forged.status_code == 403
    with TestClient(create_app(Settings(), service, cast(LeadResponder, responder))) as client:
        assert client.post("/api/webhooks/sms", content="").status_code == 404
    assert responder.first_replies == []


def test_signature_uses_the_public_url_behind_a_tunnel(
    service: LeadService, responder: RecordingResponder
) -> None:
    public = "https://leads.example.com"
    with client_for(service, responder, public_url=public) as client:
        params = {"From": "+13035550142", "To": FROM, "Body": "Hi"}
        assert post_sms(client, params, f"{public}/api/webhooks/sms").status_code == 200
        assert post_sms(client, params).status_code == 403
