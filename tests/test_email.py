import smtplib
from email.message import EmailMessage
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from lead_agent.agent import QualifyingAgent
from lead_agent.channels import Recipient, SendError
from lead_agent.channels.email import (
    EmailChannel,
    EmailInbox,
    SmtpConfig,
    SmtpSender,
    parse_email,
    strip_quoted_reply,
)
from lead_agent.db import Direction, Lead, LeadSource, LeadStatus
from lead_agent.leads import LeadIntake, LeadService
from lead_agent.llm import ModelResponse, ScriptedModel
from lead_agent.profile import load_profile
from lead_agent.responder import OPT_OUT_CONFIRMATION, LeadResponder

LEAD_ID = "0123456789abcdef0123456789abcdef"
DANA = Recipient(LEAD_ID, "Dana Lopez", "dana@example.com", None)


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


class FakeMailbox:
    def __init__(self, messages: list[bytes]) -> None:
        self.unseen = {str(i): raw for i, raw in enumerate(messages)}
        self.seen: list[str] = []

    def fetch_unseen(self) -> list[tuple[str, bytes]]:
        return [(uid, raw) for uid, raw in self.unseen.items() if uid not in self.seen]

    def mark_seen(self, uid: str) -> None:
        self.seen.append(uid)


def channel(sender: RecordingSender | None = None) -> EmailChannel:
    return EmailChannel(
        sender or RecordingSender(),
        from_address="hello@brightline.example",
        from_name="Alex at Brightline",
        business_name="Brightline Solar & Roofing",
    )


def reply(
    body: str,
    *,
    sender: str = "Dana Lopez <Dana@Example.com>",
    subject: str = "Re: Your enquiry with Brightline Solar & Roofing",
    references: str | None = f"<lead-{LEAD_ID}@brightline.example>",
    headers: dict[str, str] | None = None,
    html: bool = False,
) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "hello@brightline.example"
    message["Subject"] = subject
    if references:
        message["In-Reply-To"] = references
    for key, value in (headers or {}).items():
        message[key] = value
    message.set_content(body, subtype="html" if html else "plain")
    return message.as_bytes()


# --- Sending ---------------------------------------------------------------------


def test_messages_are_threaded_per_lead_and_offer_an_opt_out() -> None:
    message = channel().build(DANA, "Hi Dana! Do you own the home?")

    assert message["To"] == "Dana Lopez <dana@example.com>"
    assert message["From"] == "Alex at Brightline <hello@brightline.example>"
    assert message["Subject"] == "Your enquiry with Brightline Solar & Roofing"
    assert message["References"] == f"<lead-{LEAD_ID}@brightline.example>"
    assert message["List-Unsubscribe"] == "<mailto:hello@brightline.example?subject=unsubscribe>"
    body = message.get_content()
    assert body.startswith("Hi Dana! Do you own the home?")
    assert "Reply STOP" in body


def test_channel_reaches_only_leads_with_an_email() -> None:
    assert channel().can_reach(DANA)
    assert not channel().can_reach(Recipient("x", "Sam", None, "42"))


def test_smtp_failures_become_send_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise smtplib.SMTPConnectError(421, "try later")

    monkeypatch.setattr(smtplib, "SMTP", refuse)
    sender = SmtpSender(SmtpConfig("smtp.example.com", password="secret-password"))
    with pytest.raises(SendError) as error:
        sender.send(channel().build(DANA, "hi"))
    assert "secret-password" not in str(error.value)


# --- Reading replies ---------------------------------------------------------------


def test_a_reply_is_parsed_without_the_quoted_history() -> None:
    inbound = parse_email(
        reply("Yes, I own it.\n\nOn Mon, 16 Nov 2026 at 09:00, Alex wrote:\n> Do you own the home?")
    )
    assert inbound is not None
    assert (inbound.sender, inbound.text, inbound.lead_id) == (
        "dana@example.com",
        "Yes, I own it.",
        LEAD_ID,
    )


def test_html_replies_are_read_as_text() -> None:
    inbound = parse_email(reply("<p>Yes &amp; soon</p><br>Thanks", html=True))
    assert inbound is not None
    assert inbound.text == "Yes & soon\n\nThanks"


@pytest.mark.parametrize(
    "headers",
    [{"Auto-Submitted": "auto-replied"}, {"Precedence": "bulk"}, {"X-Autoreply": "yes"}],
)
def test_automatic_replies_are_ignored(headers: dict[str, str]) -> None:
    assert parse_email(reply("I'm out of the office.", headers=headers)) is None


def test_an_empty_unsubscribe_mail_counts_as_an_opt_out() -> None:
    inbound = parse_email(reply("", subject="unsubscribe", references=None))
    assert inbound is not None
    assert inbound.text == "unsubscribe"


def test_signature_and_outlook_style_quotes_are_stripped() -> None:
    assert strip_quoted_reply("Sounds good\n--\nDana") == "Sounds good"
    assert strip_quoted_reply("ok\r\n-----Original Message-----\r\nFrom: x") == "ok"


# --- End to end ----------------------------------------------------------------


def make_responder(
    sessions: sessionmaker[Session], responses: list[ModelResponse], sender: RecordingSender
) -> LeadResponder:
    profile = load_profile()
    return LeadResponder(
        sessions,
        QualifyingAgent(ScriptedModel(responses), profile),
        profile,
        [channel(sender)],
    )


def new_lead(service: LeadService) -> str:
    intake = LeadIntake(
        name="Dana Lopez", email="dana@example.com", message="Solar for my house", consent=True
    )
    return service.receive(intake, LeadSource.FORM).lead_id


def inbox(mailbox: FakeMailbox, service: LeadService, responder: LeadResponder) -> EmailInbox:
    return EmailInbox(
        mailbox,
        service.email_lead,
        lambda lead_id, text: responder.handle_inbound(lead_id, text, "email"),
    )


def test_replies_continue_the_conversation(
    service: LeadService, sessions: sessionmaker[Session]
) -> None:
    sender = RecordingSender()
    responder = make_responder(
        sessions, [ModelResponse("Hi Dana!"), ModelResponse("Great, thanks!")], sender
    )
    lead_id = new_lead(service)
    responder.reply_to_new_lead(lead_id)

    raw = reply("Yes, I own it.", references=f"<lead-{lead_id}@brightline.example>")
    mailbox = FakeMailbox([raw, reply("Unrelated newsletter", sender="news@shop.example")])
    assert inbox(mailbox, service, responder).poll_once() == 1

    assert mailbox.seen == ["0"]  # The unrelated mail stays unread for a person.
    assert [m.get_content().split("\n")[0] for m in sender.sent] == ["Hi Dana!", "Great, thanks!"]
    with sessions() as session:
        lead = session.get_one(Lead, lead_id)
        assert [(m.direction, m.channel) for m in lead.messages][-2:] == [
            (Direction.INBOUND, "email"),
            (Direction.OUTBOUND, "email"),
        ]


def test_stop_by_email_opts_out(service: LeadService, sessions: sessionmaker[Session]) -> None:
    sender = RecordingSender()
    responder = make_responder(sessions, [], sender)
    lead_id = new_lead(service)

    inbox(FakeMailbox([reply("STOP", references=None)]), service, responder).poll_once()

    assert sender.sent[0].get_content().startswith(OPT_OUT_CONFIRMATION)
    with sessions() as session:
        assert session.get_one(Lead, lead_id).status == LeadStatus.OPTED_OUT


def test_thread_reference_must_match_the_sender(service: LeadService) -> None:
    lead_id = new_lead(service)
    assert service.email_lead("DANA@example.com", None) == lead_id
    assert service.email_lead("mallory@example.com", lead_id) is None
