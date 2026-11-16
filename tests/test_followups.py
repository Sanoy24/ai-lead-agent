from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from lead_agent.agent import QualifyingAgent
from lead_agent.channels import Recipient
from lead_agent.db import Lead, LeadSource, LeadStatus
from lead_agent.followups import FollowUpPolicy, FollowUpScheduler
from lead_agent.leads import LeadIntake, LeadService
from lead_agent.llm import LLMError, Message, ModelResponse, ScriptedModel, ToolCall, ToolSpec
from lead_agent.profile import BusinessProfile, load_profile
from lead_agent.responder import FALLBACK_FOLLOW_UP, LeadResponder

DENVER = "America/Denver"
POLICY = FollowUpPolicy()


class FakeChannel:
    name = "fake"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def can_reach(self, recipient: Recipient) -> bool:
        return True

    def send(self, recipient: Recipient, text: str) -> None:
        self.sent.append(text)


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# --- Timing --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("local", "quiet"),
    [(time(8, 59), True), (time(9, 0), False), (time(19, 59), False), (time(20, 0), True)],
)
def test_quiet_hours_span_midnight(local: time, quiet: bool) -> None:
    assert POLICY.is_quiet(local) is quiet


def test_follow_ups_in_quiet_hours_move_to_the_morning() -> None:
    # 22:00 in Denver (05:00 UTC next day) moves to 09:00 Denver (16:00 UTC).
    assert POLICY.next_allowed(utc(2026, 11, 17, 5, 0), DENVER) == utc(2026, 11, 17, 16, 0)
    # 07:00 Denver moves to 09:00 the same day.
    assert POLICY.next_allowed(utc(2026, 11, 17, 14, 0), DENVER) == utc(2026, 11, 17, 16, 0)
    # Midday is fine as is.
    assert POLICY.next_allowed(utc(2026, 11, 17, 19, 0), DENVER) == utc(2026, 11, 17, 19, 0)


def test_schedule_uses_each_delay_then_stops() -> None:
    noon = utc(2026, 11, 17, 19, 0)  # 12:00 in Denver
    assert POLICY.schedule(noon, 0, DENVER) == noon + timedelta(days=1)
    assert POLICY.schedule(noon, 2, DENVER) == noon + timedelta(days=7)
    assert POLICY.schedule(noon, 3, DENVER) is None


def test_profile_can_turn_follow_ups_off() -> None:
    policy = FollowUpPolicy(delays_hours=[])
    assert policy.schedule(utc(2026, 11, 17, 19, 0), 0, DENVER) is None


# --- Responder and scheduler --------------------------------------------------------


@pytest.fixture(scope="module")
def profile() -> BusinessProfile:
    return load_profile()


def new_lead(service: LeadService) -> str:
    intake = LeadIntake(
        name="Dana Lopez", email="dana@example.com", message="Solar for my house", consent=True
    )
    return service.receive(intake, LeadSource.FORM).lead_id


def load(sessions: sessionmaker[Session], lead_id: str) -> Lead:
    with sessions() as session:
        lead = session.get_one(Lead, lead_id)
        _ = lead.messages, lead.events
        return lead


def make(
    sessions: sessionmaker[Session], profile: BusinessProfile, responses: list[ModelResponse]
) -> tuple[LeadResponder, ScriptedModel, FakeChannel]:
    model = ScriptedModel(responses)
    channel = FakeChannel()
    return (
        LeadResponder(sessions, QualifyingAgent(model, profile), profile, [channel]),
        model,
        channel,
    )


def make_due(sessions: sessionmaker[Session], lead_id: str) -> None:
    with sessions.begin() as session:
        session.get_one(Lead, lead_id).next_follow_up_at = utc(2026, 11, 17, 18, 0)


def scheduler(
    sessions: sessionmaker[Session], responder: LeadResponder, now: datetime
) -> FollowUpScheduler:
    return FollowUpScheduler(sessions, responder.follow_up, POLICY, DENVER, clock=lambda: now)


MIDDAY = utc(2026, 11, 17, 19, 0)  # 12:00 in Denver


def test_a_reply_from_the_agent_schedules_a_follow_up(
    service: LeadService, sessions: sessionmaker[Session], profile: BusinessProfile
) -> None:
    lead_id = new_lead(service)
    responder, _, _ = make(sessions, profile, [ModelResponse("Hi Dana! Do you own it?")])
    responder.reply_to_new_lead(lead_id)
    assert load(sessions, lead_id).next_follow_up_at is not None


def test_due_follow_up_is_sent_once_and_the_next_one_scheduled(
    service: LeadService, sessions: sessionmaker[Session], profile: BusinessProfile
) -> None:
    lead_id = new_lead(service)
    responder, model, channel = make(
        sessions,
        profile,
        [ModelResponse("Hi Dana! Do you own it?"), ModelResponse("Just checking in, Dana!")],
    )
    responder.reply_to_new_lead(lead_id)
    make_due(sessions, lead_id)

    assert scheduler(sessions, responder, MIDDAY).run_once() == 1
    assert scheduler(sessions, responder, MIDDAY).run_once() == 0  # Not sent twice.

    assert channel.sent[-1] == "Just checking in, Dana!"
    nudge = model.requests[1][0][-1]
    assert nudge.role == "user"
    assert "follow-up 1 of 3" in nudge.content
    assert model.requests[1][1] == []  # No tools while following up.
    lead = load(sessions, lead_id)
    assert lead.follow_ups_sent == 1
    assert lead.next_follow_up_at is not None
    assert "follow_up.sent" in {e.type for e in lead.events}


def test_last_follow_up_says_so_and_ends_the_sequence(
    service: LeadService, sessions: sessionmaker[Session], profile: BusinessProfile
) -> None:
    lead_id = new_lead(service)
    responder, model, _ = make(sessions, profile, [ModelResponse("Hi!"), ModelResponse("Bye!")])
    responder.reply_to_new_lead(lead_id)
    with sessions.begin() as session:
        session.get_one(Lead, lead_id).follow_ups_sent = 2
    make_due(sessions, lead_id)

    scheduler(sessions, responder, MIDDAY).run_once()

    assert "This is the last follow-up" in model.requests[1][0][-1].content
    lead = load(sessions, lead_id)
    assert lead.next_follow_up_at is None
    assert "follow_ups.finished" in {e.type for e in lead.events}


def test_a_reply_from_the_lead_restarts_the_sequence(
    service: LeadService, sessions: sessionmaker[Session], profile: BusinessProfile
) -> None:
    lead_id = new_lead(service)
    responder, _, _ = make(
        sessions,
        profile,
        [
            ModelResponse("Hi!"),
            ModelResponse("Checking in!"),
            ModelResponse("", tool_calls=(ToolCall(id="r", name="record_answers", arguments={}),)),
            ModelResponse("Thanks!"),
        ],
    )
    responder.reply_to_new_lead(lead_id)
    make_due(sessions, lead_id)
    scheduler(sessions, responder, MIDDAY).run_once()
    responder.handle_inbound(lead_id, "Sorry, busy week. Yes I own it.", "email")

    lead = load(sessions, lead_id)
    assert lead.follow_ups_sent == 0
    assert lead.next_follow_up_at is not None


def test_qualified_leads_get_the_booking_link_in_follow_ups(
    service: LeadService, sessions: sessionmaker[Session], profile: BusinessProfile
) -> None:
    lead_id = new_lead(service)
    with sessions.begin() as session:
        lead = session.get_one(Lead, lead_id)
        lead.status = LeadStatus.QUALIFIED
        lead.answers = {
            "service": "solar installation",
            "homeowner": True,
            "zip_code": "80202",
            "timeline": "as soon as possible",
        }
    responder, _, channel = make(sessions, profile, [ModelResponse("Still keen to chat?")])
    make_due(sessions, lead_id)
    scheduler(sessions, responder, MIDDAY).run_once()

    assert channel.sent[0].startswith("Still keen to chat?")
    assert "https://cal.com/" in channel.sent[0]


@pytest.mark.parametrize(
    "change",
    [
        {"status": LeadStatus.BOOKED},
        {"status": LeadStatus.OPTED_OUT},
        {"status": LeadStatus.LOST},
        {"ai_paused": True},
    ],
)
def test_no_follow_ups_for_booked_opted_out_lost_or_taken_over_leads(
    service: LeadService,
    sessions: sessionmaker[Session],
    profile: BusinessProfile,
    change: dict[str, object],
) -> None:
    lead_id = new_lead(service)
    with sessions.begin() as session:
        lead = session.get_one(Lead, lead_id)
        for key, value in change.items():
            setattr(lead, key, value)
    responder, _, channel = make(sessions, profile, [])
    make_due(sessions, lead_id)

    assert scheduler(sessions, responder, MIDDAY).run_once() == 0
    assert channel.sent == []
    assert load(sessions, lead_id).next_follow_up_at is None


def test_due_follow_ups_wait_for_the_end_of_quiet_hours(
    service: LeadService, sessions: sessionmaker[Session], profile: BusinessProfile
) -> None:
    lead_id = new_lead(service)
    responder, _, channel = make(sessions, profile, [])
    make_due(sessions, lead_id)

    night = utc(2026, 11, 18, 6, 0)  # 23:00 in Denver
    assert scheduler(sessions, responder, night).run_once() == 0
    assert channel.sent == []
    with sessions() as session:
        moved = session.get_one(Lead, lead_id).next_follow_up_at
    assert moved is not None
    assert moved.replace(tzinfo=UTC) == utc(2026, 11, 18, 16, 0)  # 09:00 in Denver


def test_model_failure_falls_back_to_a_template(
    service: LeadService, sessions: sessionmaker[Session], profile: BusinessProfile
) -> None:
    class Broken:
        def complete(self, messages: list[Message], tools: list[ToolSpec]) -> ModelResponse:
            raise LLMError("provider down")

    lead_id = new_lead(service)
    channel = FakeChannel()
    responder = LeadResponder(sessions, QualifyingAgent(Broken(), profile), profile, [channel])
    make_due(sessions, lead_id)
    scheduler(sessions, responder, MIDDAY).run_once()

    assert channel.sent == [FALLBACK_FOLLOW_UP]
