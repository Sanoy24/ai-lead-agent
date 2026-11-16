from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from lead_agent.agent import QualifyingAgent
from lead_agent.channels import Recipient, SendError
from lead_agent.db import Direction, Lead, LeadSource, LeadStatus
from lead_agent.leads import LeadIntake, LeadService
from lead_agent.llm import LLMError, Message, ModelResponse, ScriptedModel, ToolCall, ToolSpec
from lead_agent.profile import BusinessProfile, load_profile
from lead_agent.responder import (
    FALLBACK_REPLY,
    OPT_OUT_CONFIRMATION,
    LeadResponder,
    is_opt_out,
)


class FakeChannel:
    name = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    def can_reach(self, recipient: Recipient) -> bool:
        return recipient.email is not None

    def send(self, recipient: Recipient, text: str) -> None:
        if self.fail:
            raise SendError("mailbox unavailable")
        self.sent.append((recipient.email or "", text))


@pytest.fixture(scope="module")
def profile() -> BusinessProfile:
    return load_profile()


def record(**answers: object) -> ModelResponse:
    return ModelResponse(
        "", tool_calls=(ToolCall(id="r", name="record_answers", arguments=answers),)
    )


def say(text: str) -> ModelResponse:
    return ModelResponse(text)


def new_lead(service: LeadService, **overrides: Any) -> str:
    data: dict[str, Any] = {
        "name": "Dana Lopez",
        "email": "dana@example.com",
        "message": "Looking into solar for my house.",
        "service_interest": "Solar installation",
        "consent": True,
    }
    data.update(overrides)
    return service.receive(LeadIntake.model_validate(data), LeadSource.FORM).lead_id


def make_responder(
    sessions: sessionmaker[Session],
    profile: BusinessProfile,
    responses: list[ModelResponse],
    channel: FakeChannel | None = None,
) -> tuple[LeadResponder, ScriptedModel, FakeChannel]:
    model = ScriptedModel(responses)
    channel = channel or FakeChannel()
    return (
        LeadResponder(sessions, QualifyingAgent(model, profile), profile, [channel]),
        model,
        channel,
    )


def load(sessions: sessionmaker[Session], lead_id: str) -> Lead:
    with sessions() as session:
        lead = session.get_one(Lead, lead_id)
        _ = lead.messages, lead.events
        return lead


def test_first_reply_is_sent_saved_and_timed(
    service: LeadService, sessions: sessionmaker[Session], profile: BusinessProfile
) -> None:
    lead_id = new_lead(service)
    responder, model, channel = make_responder(
        sessions,
        profile,
        [record(service="solar installation"), say("Hi Dana! Do you own the property?")],
    )

    responder.reply_to_new_lead(lead_id)

    assert channel.sent == [("dana@example.com", "Hi Dana! Do you own the property?")]
    lead = load(sessions, lead_id)
    assert lead.status == LeadStatus.QUALIFYING
    assert lead.answers == {"service": "solar installation"}
    assert lead.first_response_at is not None
    assert lead.messages[-1].direction == Direction.OUTBOUND
    assert {e.type for e in lead.events} >= {
        "answers.recorded",
        "lead.scored",
        "lead.first_response",
    }
    history = model.requests[0][0][1:]
    assert history[0].content == "Looking into solar for my house."


def test_qualified_lead_gets_the_booking_link(
    service: LeadService, sessions: sessionmaker[Session], profile: BusinessProfile
) -> None:
    lead_id = new_lead(service)
    responder, _, channel = make_responder(
        sessions,
        profile,
        [
            record(
                service="solar installation",
                homeowner=True,
                zip_code="80202",
                timeline="1 to 3 months",
            ),
            say("Perfect, thanks Dana!"),
        ],
    )
    responder.reply_to_new_lead(lead_id)

    text = channel.sent[0][1]
    assert text.startswith("Perfect, thanks Dana!")
    assert "Book your free assessment: https://cal.com/" in text
    assert f"metadata%5Blead_id%5D={lead_id}" in text
    assert "email=dana%40example.com" in text
    lead = load(sessions, lead_id)
    assert (lead.status, lead.score) == (LeadStatus.QUALIFIED, 90)


def test_not_a_fit_gets_a_polite_close_instead_of_the_model_text(
    service: LeadService, sessions: sessionmaker[Session], profile: BusinessProfile
) -> None:
    lead_id = new_lead(service)
    responder, _, channel = make_responder(
        sessions, profile, [record(homeowner=False), say("Great, what's your ZIP code?")]
    )
    responder.reply_to_new_lead(lead_id)

    text = channel.sent[0][1]
    assert "What's your ZIP" not in text
    assert "only work with property owners" in text
    assert load(sessions, lead_id).status == LeadStatus.LOST


def test_opt_out_is_honoured_without_calling_the_model(
    service: LeadService, sessions: sessionmaker[Session], profile: BusinessProfile
) -> None:
    lead_id = new_lead(service)
    responder, model, channel = make_responder(sessions, profile, [])

    responder.handle_inbound(lead_id, "STOP", "email")
    responder.handle_inbound(lead_id, "hello again?", "email")
    responder.continue_conversation(lead_id)

    assert channel.sent == [("dana@example.com", OPT_OUT_CONFIRMATION)]
    assert model.requests == []
    lead = load(sessions, lead_id)
    assert lead.status == LeadStatus.OPTED_OUT
    assert lead.opted_out_at is not None


@pytest.mark.parametrize(
    ("text", "expected"),
    [("STOP", True), ("Unsubscribe.", True), ("stop!", True), ("please don't stop", False)],
)
def test_opt_out_detection(text: str, expected: bool) -> None:
    assert is_opt_out(text) is expected


def test_handoff_pauses_the_ai(
    service: LeadService, sessions: sessionmaker[Session], profile: BusinessProfile
) -> None:
    lead_id = new_lead(service)
    handoff = ModelResponse(
        "",
        tool_calls=(
            ToolCall(
                id="h",
                name="hand_off_to_human",
                arguments={"reason": "Asked for a person", "summary": "Wants a call today."},
            ),
        ),
    )
    responder, model, channel = make_responder(
        sessions, profile, [handoff, say("Sure, a teammate will call you today.")]
    )
    responder.reply_to_new_lead(lead_id)
    responder.handle_inbound(lead_id, "thanks", "email")

    assert len(channel.sent) == 1
    assert len(model.requests) == 2
    lead = load(sessions, lead_id)
    assert lead.ai_paused is True
    assert "lead.handed_off" in {e.type for e in lead.events}


def test_model_failure_still_answers_quickly_and_pauses_ai(
    service: LeadService, sessions: sessionmaker[Session], profile: BusinessProfile
) -> None:
    class Broken:
        def complete(self, messages: list[Message], tools: list[ToolSpec]) -> ModelResponse:
            raise LLMError("provider down")

    lead_id = new_lead(service)
    channel = FakeChannel()
    responder = LeadResponder(sessions, QualifyingAgent(Broken(), profile), profile, [channel])
    responder.reply_to_new_lead(lead_id)

    assert channel.sent == [("dana@example.com", FALLBACK_REPLY)]
    lead = load(sessions, lead_id)
    assert lead.ai_paused is True
    assert "agent.failed" in {e.type for e in lead.events}


def test_failed_delivery_is_recorded_but_not_counted_as_a_response(
    service: LeadService, sessions: sessionmaker[Session], profile: BusinessProfile
) -> None:
    lead_id = new_lead(service)
    responder, _, _ = make_responder(
        sessions, profile, [say("Hi Dana!")], channel=FakeChannel(fail=True)
    )
    responder.reply_to_new_lead(lead_id)

    lead = load(sessions, lead_id)
    assert lead.first_response_at is None
    assert "message.failed" in {e.type for e in lead.events}


def test_leads_without_a_reachable_channel_fall_back_to_the_log(
    service: LeadService, sessions: sessionmaker[Session], profile: BusinessProfile
) -> None:
    lead_id = new_lead(service, email=None, phone="+13035550100")
    responder, _, channel = make_responder(sessions, profile, [say("Hi Dana!")])
    responder.reply_to_new_lead(lead_id)

    assert channel.sent == []
    assert load(sessions, lead_id).messages[-1].channel == "log"


def test_background_scheduling_runs_the_reply(
    service: LeadService, sessions: sessionmaker[Session], profile: BusinessProfile
) -> None:
    lead_id = new_lead(service)
    responder, _, channel = make_responder(sessions, profile, [say("Hi Dana!")])
    responder.schedule_first_reply(lead_id).result(timeout=5)
    responder.shutdown()
    assert channel.sent == [("dana@example.com", "Hi Dana!")]
