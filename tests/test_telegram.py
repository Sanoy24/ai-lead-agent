import json
import threading
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from lead_agent.agent import QualifyingAgent
from lead_agent.channels import Recipient, SendError
from lead_agent.channels.telegram import (
    NON_TEXT_REPLY,
    RATE_LIMIT_REPLY,
    WELCOME_BACK_REPLY,
    TelegramBot,
    TelegramChannel,
    TelegramClient,
    TelegramError,
    split_message,
    to_telegram_html,
)
from lead_agent.db import Direction, Lead, LeadSource, LeadStatus
from lead_agent.leads import TELEGRAM_CONSENT_TEXT, LeadService
from lead_agent.llm import ModelResponse, ScriptedModel, ToolCall
from lead_agent.profile import load_profile
from lead_agent.responder import OPT_OUT_CONFIRMATION, LeadResponder

TOKEN = "123:test-token"
CHAT = 42


class FakeTelegram:
    """Records Bot API calls and serves queued getUpdates batches."""

    def __init__(self, updates: list[list[dict[str, Any]]] | None = None, fail: bool = False):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._updates = list(updates or [])
        self.fail = fail

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith(f"/bot{TOKEN}/")
        method = request.url.path.rsplit("/", 1)[1]
        payload = json.loads(request.content)
        self.calls.append((method, payload))
        if method == "getUpdates":
            batch = self._updates.pop(0) if self._updates else []
            return httpx.Response(200, json={"ok": True, "result": batch})
        if self.fail:
            return httpx.Response(403, json={"ok": False, "description": "bot was blocked"})
        return httpx.Response(200, json={"ok": True, "result": True})

    def client(self) -> TelegramClient:
        return TelegramClient(TOKEN, transport=httpx.MockTransport(self.handler))

    def sent_texts(self) -> list[str]:
        return [p["text"] for m, p in self.calls if m == "sendMessage"]


def message(
    text: str | None, chat_id: int = CHAT, chat_type: str = "private", update_id: int = 1
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": chat_id, "first_name": "Dana", "last_name": "Lopez"},
    }
    if text is not None:
        msg["text"] = text
    return {"update_id": update_id, "message": msg}


def say(text: str) -> ModelResponse:
    return ModelResponse(text)


def record(**answers: object) -> ModelResponse:
    return ModelResponse(
        "", tool_calls=(ToolCall(id="r", name="record_answers", arguments=answers),)
    )


class Harness:
    def __init__(
        self,
        service: LeadService,
        sessions: sessionmaker[Session],
        responses: list[ModelResponse],
        **bot_options: Any,
    ) -> None:
        self.fake = FakeTelegram()
        client = self.fake.client()
        profile = load_profile()
        self.model = ScriptedModel(responses)
        self.responder = LeadResponder(
            sessions, QualifyingAgent(self.model, profile), profile, [TelegramChannel(client)]
        )
        self.bot = TelegramBot(client, service, self.responder, typing_interval=60, **bot_options)
        self.service = service
        self.sessions = sessions

    def send(self, text: str | None, **kwargs: Any) -> None:
        self.bot.handle_update(message(text, **kwargs))

    def lead(self) -> Lead:
        ref = self.service.telegram_lead(str(CHAT))
        assert ref is not None
        with self.sessions() as session:
            lead = session.get_one(Lead, ref.id)
            _ = lead.messages, lead.events
            return lead


@pytest.fixture
def harness_factory(service: LeadService, sessions: sessionmaker[Session]) -> Any:
    def make(responses: list[ModelResponse], **bot_options: Any) -> Harness:
        return Harness(service, sessions, responses, **bot_options)

    return make


# --- Formatting and client ----------------------------------------------------


def test_html_is_escaped_and_bold_is_converted() -> None:
    assert to_telegram_html("Save **20%** <b>x</b> & more") == (
        "Save <b>20%</b> &lt;b&gt;x&lt;/b&gt; &amp; more"
    )


def test_long_messages_are_split() -> None:
    assert split_message("x" * 10, limit=4) == ["xxxx", "xxxx", "xx"]
    chunks = split_message("\n".join(["line " * 20] * 10), limit=250)
    assert all(len(c) <= 250 for c in chunks)


def test_api_errors_do_not_leak_the_token() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    client = TelegramClient(TOKEN, transport=httpx.MockTransport(fail))
    with pytest.raises(TelegramError) as error:
        client.send_message(1, "hi")
    assert TOKEN not in str(error.value)


def test_channel_reaches_only_telegram_leads_and_reports_failures() -> None:
    fake = FakeTelegram(fail=True)
    channel = TelegramChannel(fake.client())
    telegram_lead = Recipient("1", "Dana", None, "42")
    assert channel.can_reach(telegram_lead)
    assert not channel.can_reach(Recipient("2", "Sam", "sam@example.com", None))
    with pytest.raises(SendError):
        channel.send(telegram_lead, "hi")


# --- Leads from Telegram -------------------------------------------------------


def test_first_message_creates_a_lead_and_the_agent_replies(harness_factory: Any) -> None:
    h = harness_factory([record(service="solar installation"), say("Hi Dana! Do you own it?")])

    h.send("Hi, I'm thinking about solar panels for my house")

    assert h.fake.sent_texts() == ["Hi Dana! Do you own it?"]
    assert ("sendChatAction", {"chat_id": CHAT, "action": "typing"}) in h.fake.calls
    lead = h.lead()
    assert (lead.source, lead.name, lead.telegram_chat_id) == (
        LeadSource.TELEGRAM,
        "Dana Lopez",
        "42",
    )
    assert (lead.consent, lead.consent_text) == (True, TELEGRAM_CONSENT_TEXT)
    assert lead.answers == {"service": "solar installation"}
    assert lead.first_response_at is not None
    assert [(m.direction, m.channel) for m in lead.messages] == [
        (Direction.INBOUND, "telegram"),
        (Direction.OUTBOUND, "telegram"),
    ]


def test_start_creates_a_lead_and_the_agent_opens(harness_factory: Any) -> None:
    h = harness_factory([say("Hi Dana, I'm Alex from Brightline. How can I help?")])
    h.send("/start")

    assert h.fake.sent_texts() == ["Hi Dana, I'm Alex from Brightline. How can I help?"]
    assert h.lead().first_response_at is not None


def test_later_messages_continue_the_same_lead(harness_factory: Any) -> None:
    h = harness_factory([say("Hi! Do you own the home?"), record(homeowner=True), say("Great!")])
    h.send("Solar please")
    h.send("Yes, I own it")

    lead = h.lead()
    assert lead.answers == {"homeowner": True}
    assert len(lead.messages) == 4
    history = h.model.requests[1][0][1:]
    assert [m.content for m in history] == [
        "Solar please",
        "Hi! Do you own the home?",
        "Yes, I own it",
    ]


def test_start_in_an_active_chat_does_not_create_a_second_lead(harness_factory: Any) -> None:
    h = harness_factory([say("Hi!")])
    h.send("hello")
    first = h.lead().id
    h.send("/start")

    assert h.fake.sent_texts()[-1] == WELCOME_BACK_REPLY
    assert h.lead().id == first


def test_stop_opts_out_and_the_bot_goes_quiet(harness_factory: Any) -> None:
    h = harness_factory([say("Hi!")])
    h.send("hello")
    h.send("/stop")
    h.send("are you there?")

    assert h.fake.sent_texts() == ["Hi!", OPT_OUT_CONFIRMATION]
    lead = h.lead()
    assert lead.status == LeadStatus.OPTED_OUT
    assert lead.messages[-1].content == "are you there?"
    assert len(h.model.requests) == 1


def test_start_after_opting_out_begins_a_new_lead(harness_factory: Any) -> None:
    h = harness_factory([say("Hi!"), say("Welcome back, how can I help?")])
    h.send("hello")
    old = h.lead().id
    h.send("/stop")
    h.send("/start")

    lead = h.lead()
    assert lead.id != old
    assert lead.status != LeadStatus.OPTED_OUT
    assert h.fake.sent_texts()[-1] == "Welcome back, how can I help?"


def test_non_text_messages_get_a_polite_reply(harness_factory: Any) -> None:
    h = harness_factory([])
    h.send(None)
    assert h.fake.sent_texts() == [NON_TEXT_REPLY]


def test_group_chats_are_ignored(harness_factory: Any) -> None:
    h = harness_factory([])
    h.send("hi", chat_type="group")
    assert h.fake.calls == []


def test_each_chat_is_rate_limited(harness_factory: Any) -> None:
    h = harness_factory([say("Hi!")], rate_limit_per_minute=1)
    h.send("one")
    h.send("two")
    assert h.fake.sent_texts() == ["Hi!", RATE_LIMIT_REPLY]


def test_polling_tracks_the_offset_and_stops(harness_factory: Any) -> None:
    h = harness_factory([say("Hi!")])
    h.fake._updates = [[message("hello", update_id=7)], []]
    stop = threading.Event()
    original = h.fake.handler

    def stop_after_second_poll(request: httpx.Request) -> httpx.Response:
        response: httpx.Response = original(request)
        if sum(1 for m, _ in h.fake.calls if m == "getUpdates") >= 2:
            stop.set()
        return response

    h.bot._client = TelegramClient(TOKEN, transport=httpx.MockTransport(stop_after_second_poll))
    h.bot.run_polling(stop, poll_timeout=0)

    polls = [p for m, p in h.fake.calls if m == "getUpdates"]
    assert "offset" not in polls[0]
    assert polls[1]["offset"] == 8
    assert h.fake.sent_texts() == ["Hi!"]
