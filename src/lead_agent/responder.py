"""Runs the conversation with each lead: first reply, replies, opt-outs and routing.

Database work happens in short transactions before and after the model call,
never during it, so a slow model never blocks other leads.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from lead_agent.agent import AgentTurn, QualifyingAgent
from lead_agent.channels import Channel, LogChannel, Recipient, SendError
from lead_agent.db import (
    Direction,
    Lead,
    LeadEvent,
    LeadMessage,
    LeadSource,
    LeadStatus,
    utcnow,
)
from lead_agent.followups import FOLLOW_UP_STATUSES
from lead_agent.llm import LLMError, Message
from lead_agent.profile import BusinessProfile
from lead_agent.scoring import Outcome, assess

logger = logging.getLogger(__name__)

HISTORY_LIMIT = 30

OPT_OUT_WORDS = frozenset(
    {"stop", "stop all", "unsubscribe", "cancel", "end", "quit", "opt out", "optout", "remove me"}
)
OPT_OUT_CONFIRMATION = "You're unsubscribed and won't hear from us again. Take care!"
FALLBACK_REPLY = "Thanks for reaching out! A member of our team will get back to you shortly."
FALLBACK_OPENER = (
    "Hi {first_name}, it's {agent} from {business}. You got in touch with us {about}a while "
    "back. Is that still something you're thinking about?"
)
FALLBACK_FOLLOW_UP = "Just checking in on your enquiry. Reply here whenever suits you!"

_STATUS_FOR_OUTCOME = {
    Outcome.QUALIFYING: LeadStatus.QUALIFYING,
    Outcome.QUALIFIED: LeadStatus.QUALIFIED,
    Outcome.NURTURE: LeadStatus.NURTURING,
    Outcome.NOT_A_FIT: LeadStatus.LOST,
}
_FINAL_STATUSES = {LeadStatus.BOOKED, LeadStatus.LOST, LeadStatus.OPTED_OUT}


_LINK_TALK = re.compile(
    r"\b(booking link|link below|link above|added automatically|automatically added)\b", re.I
)
# Promises that the team will contact the lead to set up a time. The lead books
# with the link instead, so these sentences would contradict it.
_CONTACT_PROMISE = re.compile(
    r"\b(reach out|be in touch|get in touch|contact you|call you|give you a call"
    r"|follow up with you)\b",
    re.I,
)
_SCHEDULING = re.compile(r"\b(schedul\w*|set up|arrange|book\w*|confirm\w*|a time)\b", re.I)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_LINK_TALK_FALLBACK = "Thanks so much for sharing all that!"


def _talks_about_link(sentence: str) -> bool:
    if _LINK_TALK.search(sentence):
        return True
    return bool(_CONTACT_PROMISE.search(sentence) and _SCHEDULING.search(sentence))


def without_link_talk(text: str) -> str:
    """Drop sentences where the model talks about the booking link or promises a call.

    The link is added by code right after the model's message, and smaller
    models tend to announce it ("the booking link will be added automatically"),
    which reads oddly. Removing those sentences works the same for every model.
    """
    kept = [s for s in _SENTENCE_END.split(text.strip()) if s and not _talks_about_link(s)]
    return " ".join(kept) or _LINK_TALK_FALLBACK


class OptedOutError(RuntimeError):
    """The lead opted out, so nothing more may be sent to them."""


def is_opt_out(text: str) -> bool:
    normalized = re.sub(r"[^a-z ]", "", text.lower()).strip()
    return normalized in OPT_OUT_WORDS


@dataclass(slots=True)
class _Snapshot:
    recipient: Recipient
    name: str
    answers: dict[str, Any]
    history: list[Message]
    status: str


class LeadResponder:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        agent: QualifyingAgent,
        profile: BusinessProfile,
        channels: list[Channel] | None = None,
        *,
        workers: int = 4,
    ) -> None:
        self._sessions = sessions
        self._agent = agent
        self._profile = profile
        self._channels = [*(channels or []), LogChannel()]
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="responder")

    # --- Entry points ----------------------------------------------------------

    def schedule_first_reply(self, lead_id: str) -> Future[None]:
        """Reply to a new lead in the background, so intake returns immediately."""
        return self._executor.submit(self._safely, self.reply_to_new_lead, lead_id)

    def schedule_inbound(self, lead_id: str, text: str, channel: str) -> Future[None]:
        """Handle a message from a lead in the background (for webhooks that must answer fast)."""
        return self._executor.submit(self._safely, self.handle_inbound, lead_id, text, channel)

    def schedule_reply(self, lead_id: str) -> Future[None]:
        """Continue the conversation after an inbound message was saved."""
        return self._executor.submit(self._safely, self.continue_conversation, lead_id)

    def schedule_notify(self, lead_id: str, text: str) -> Future[None]:
        """Send a message that isn't part of the conversation, such as a booking confirmation."""
        return self._executor.submit(self._safely, self.notify, lead_id, text)

    def notify(self, lead_id: str, text: str) -> None:
        with self._sessions() as session:
            lead = session.get_one(Lead, lead_id)
            if lead.status == LeadStatus.OPTED_OUT or not lead.consent:
                return
            recipient = _recipient(lead)
        self._send(recipient, text, lead_id, record=True, author="system")

    def set_paused(self, lead_id: str, paused: bool, *, by: str = "team") -> None:
        """Take over a conversation (pause the AI) or hand it back."""
        with self._sessions.begin() as session:
            lead = session.get_one(Lead, lead_id)
            if lead.ai_paused == paused:
                return
            lead.ai_paused = paused
            if paused:
                lead.next_follow_up_at = None
            else:
                self._schedule_follow_up(lead, sent=lead.follow_ups_sent)
            lead.events.append(
                LeadEvent(type="lead.taken_over" if paused else "lead.resumed", detail={"by": by})
            )

    def send_from_team(self, lead_id: str, text: str) -> bool:
        """Send a message written by a person. Pauses the AI so it doesn't talk over them.

        Returns False if the message couldn't be delivered.
        """
        with self._sessions() as session:
            lead = session.get_one(Lead, lead_id)
            if lead.status == LeadStatus.OPTED_OUT:
                raise OptedOutError(lead_id)
            recipient = _recipient(lead)
        self.set_paused(lead_id, True)
        return self._send(recipient, text, lead_id, record=True, author="team")

    @property
    def profile(self) -> BusinessProfile:
        return self._profile

    @property
    def sessions(self) -> sessionmaker[Session]:
        return self._sessions

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True)

    def reply_to_new_lead(self, lead_id: str) -> None:
        self.continue_conversation(lead_id)

    def handle_inbound(self, lead_id: str, text: str, channel: str) -> None:
        """Save a message from a lead, handle opt-outs, then reply."""
        with self._sessions.begin() as session:
            lead = session.get_one(Lead, lead_id)
            lead.messages.append(
                LeadMessage(direction=Direction.INBOUND, channel=channel, content=text)
            )
            lead.last_inbound_at = utcnow()
            lead.next_follow_up_at = None  # They replied, so any pending follow-up is moot.
            lead.follow_ups_sent = 0
            if lead.status == LeadStatus.OPTED_OUT:
                return
            if is_opt_out(text):
                self._opt_out(session, lead)
                recipient = _recipient(lead)
                confirm = True
            else:
                confirm = False
        if confirm:
            self._send(recipient, OPT_OUT_CONFIRMATION, lead_id, record=True, author="system")
            return
        self.continue_conversation(lead_id)

    def continue_conversation(self, lead_id: str) -> None:
        # Phase 1: read what we need, in a short transaction.
        with self._sessions() as session:
            lead = session.get_one(Lead, lead_id)
            if lead.ai_paused or lead.status in _FINAL_STATUSES or not lead.consent:
                return
            snapshot = _Snapshot(
                recipient=_recipient(lead),
                name=lead.name,
                answers=dict(lead.answers),
                history=self._history(lead),
                status=lead.status,
            )

        # Phase 2: the model call, with no transaction open.
        try:
            turn = self._agent.reply(snapshot.name, snapshot.answers, snapshot.history)
        except LLMError:
            logger.exception("Agent failed for lead %s; sending a holding reply", lead_id)
            self._fail_safe(lead_id, snapshot)
            return

        # Phase 3: decide and save.
        assessment = assess(self._profile, turn.answers)
        text = self._final_text(snapshot.recipient, turn, assessment.outcome, assessment.reasons)
        with self._sessions.begin() as session:
            lead = session.get_one(Lead, lead_id)
            lead.answers = turn.answers
            if turn.recorded:
                lead.events.append(LeadEvent(type="answers.recorded", detail=turn.recorded))
            if lead.score != assessment.score or turn.recorded:
                lead.score = assessment.score
                lead.events.append(
                    LeadEvent(
                        type="lead.scored",
                        detail={"score": assessment.score, "reasons": assessment.reasons},
                    )
                )
            new_status = _STATUS_FOR_OUTCOME[assessment.outcome]
            if turn.handoff is not None:
                lead.ai_paused = True
                lead.events.append(
                    LeadEvent(
                        type="lead.handed_off",
                        detail={"reason": turn.handoff.reason, "summary": turn.handoff.summary},
                    )
                )
            if new_status != lead.status:
                lead.events.append(
                    LeadEvent(
                        type="lead.status_changed", detail={"from": lead.status, "to": new_status}
                    )
                )
                lead.status = new_status
            self._schedule_follow_up(lead, sent=0)

        self._send(snapshot.recipient, text, lead_id, record=True)

    def follow_up(self, lead_id: str) -> None:
        """Nudge a lead who hasn't replied. Called by the follow-up scheduler.

        Imported contacts waiting for their first message get an opener instead.
        """
        with self._sessions() as session:
            opener = session.get_one(Lead, lead_id).outreach_pending
        if opener:
            self.reach_out(lead_id)
            return
        with self._sessions() as session:
            lead = session.get_one(Lead, lead_id)
            if (
                lead.ai_paused
                or lead.status not in FOLLOW_UP_STATUSES
                or not lead.consent
                or lead.opted_out_at is not None
            ):
                return
            attempt = lead.follow_ups_sent + 1
            snapshot = _Snapshot(
                recipient=_recipient(lead),
                name=lead.name,
                answers=dict(lead.answers),
                history=self._history(lead),
                status=lead.status,
            )

        total = self._profile.follow_ups.max_follow_ups
        try:
            text = self._agent.follow_up(
                snapshot.name, snapshot.answers, snapshot.history, attempt, total
            )
        except LLMError:
            logger.warning(
                "Agent failed to write a follow-up for lead %s; using a template", lead_id
            )
            text = ""
        assessment = assess(self._profile, snapshot.answers)
        text = self._final_text(
            snapshot.recipient,
            AgentTurn(text=text or FALLBACK_FOLLOW_UP),
            assessment.outcome,
            assessment.reasons,
        )

        with self._sessions.begin() as session:
            lead = session.get_one(Lead, lead_id)
            if (
                lead.last_inbound_at
                and lead.last_outbound_at
                and (lead.last_inbound_at > lead.last_outbound_at)
            ):
                return  # They replied while we were writing.
            lead.follow_ups_sent = attempt
            lead.events.append(
                LeadEvent(type="follow_up.sent", detail={"attempt": attempt, "of": total})
            )
            self._schedule_follow_up(lead, sent=attempt)
            if lead.next_follow_up_at is None:
                lead.events.append(LeadEvent(type="follow_ups.finished", detail={}))

        self._send(snapshot.recipient, text, lead_id, record=True)

    def reach_out(self, lead_id: str) -> None:
        """Send the first message to an imported past lead or customer."""
        with self._sessions() as session:
            lead = session.get_one(Lead, lead_id)
            if (
                not lead.outreach_pending
                or lead.ai_paused
                or lead.status not in FOLLOW_UP_STATUSES
                or not lead.consent
                or lead.opted_out_at is not None
            ):
                return
            recipient = _recipient(lead)
            name, service = lead.name, lead.service_interest
            note = next(
                (
                    str(e.detail.get("note") or "") or None
                    for e in lead.events
                    if e.type == "lead.created"
                ),
                None,
            )

        try:
            text = self._agent.reach_out(name, service, note)
        except LLMError:
            logger.warning("Agent failed to write an opener for lead %s; using a template", lead_id)
            text = ""
        if not text:
            first_name = name.split()[0] if any(c.isalpha() for c in name) else "there"
            about = f"about {service.lower()} " if service else ""
            text = FALLBACK_OPENER.format(
                first_name=first_name,
                agent=self._profile.agent_name,
                business=self._profile.name,
                about=about,
            )

        with self._sessions.begin() as session:
            lead = session.get_one(Lead, lead_id)
            if not lead.outreach_pending or lead.last_inbound_at is not None:
                return  # Already sent, or they wrote to us first.
            lead.outreach_pending = False
            lead.events.append(LeadEvent(type="reactivation.sent", detail={}))
            if lead.status == LeadStatus.NEW:
                lead.events.append(
                    LeadEvent(
                        type="lead.status_changed",
                        detail={"from": lead.status, "to": LeadStatus.CONTACTED},
                    )
                )
                lead.status = LeadStatus.CONTACTED
            self._schedule_follow_up(lead, sent=0)

        self._send(recipient, text, lead_id, record=True)

    # --- Internals -------------------------------------------------------------

    def _final_text(
        self, recipient: Recipient, turn: AgentTurn, outcome: Outcome, reasons: list[str]
    ) -> str:
        first_name = (
            recipient.name.split()[0] if any(c.isalpha() for c in recipient.name) else "there"
        )
        if outcome is Outcome.NOT_A_FIT:
            reason = reasons[0] if reasons else "this isn't a fit for the services we offer"
            return (
                f"Thanks so much for reaching out, {first_name}. {reason} So unfortunately we're "
                "not the right fit for this one, but I wish you the best with your project!"
            )
        text = turn.text or FALLBACK_REPLY
        if outcome in {Outcome.QUALIFIED, Outcome.NURTURE} and turn.handoff is None:
            link = self._profile.booking_link(recipient.lead_id, recipient.name, recipient.email)
            if link:
                lead_in = (
                    "Book your free assessment"
                    if outcome is Outcome.QUALIFIED
                    else "Whenever you're ready, you can book a free assessment here"
                )
                text = f"{without_link_talk(text)}\n\n{lead_in}: {link}"
            elif outcome is Outcome.QUALIFIED:
                text = f"{text}\n\nA specialist will reach out shortly to schedule your assessment."
        return text

    def _schedule_follow_up(self, lead: Lead, *, sent: int) -> None:
        if lead.ai_paused or lead.status not in FOLLOW_UP_STATUSES:
            lead.next_follow_up_at = None
            return
        lead.next_follow_up_at = self._profile.follow_ups.schedule(
            utcnow(), sent, lead.timezone or self._profile.timezone
        )

    def _fail_safe(self, lead_id: str, snapshot: _Snapshot) -> None:
        with self._sessions.begin() as session:
            lead = session.get_one(Lead, lead_id)
            lead.ai_paused = True
            lead.events.append(
                LeadEvent(type="agent.failed", detail={"action": "holding reply, paused AI"})
            )
        if snapshot.status == LeadStatus.NEW:
            self._send(snapshot.recipient, FALLBACK_REPLY, lead_id, record=True)

    def _opt_out(self, session: Session, lead: Lead) -> None:
        lead.events.append(
            LeadEvent(type="lead.opted_out", detail={"previous_status": lead.status})
        )
        lead.status = LeadStatus.OPTED_OUT
        lead.opted_out_at = utcnow()
        lead.next_follow_up_at = None

    def _history(self, lead: Lead) -> list[Message]:
        history: list[Message] = []
        for message in lead.messages[-HISTORY_LIMIT:]:
            if message.direction == Direction.INBOUND:
                history.append(Message("user", message.content))
            else:
                history.append(Message("assistant", message.content))
        if not history or history[0].role != "user":
            if lead.source == LeadSource.REACTIVATION:
                intro = f"{lead.name}, a past enquiry or customer, was re-contacted by us"
            else:
                intro = f"New enquiry from {lead.name}"
            if lead.service_interest:
                intro += f" about {lead.service_interest}"
            history.insert(0, Message("user", f"[{intro}.]"))
        return history

    def _send(
        self,
        recipient: Recipient,
        text: str,
        lead_id: str,
        *,
        record: bool,
        author: str = "agent",
    ) -> bool:
        reachable = [c for c in self._channels if c.can_reach(recipient)]
        channel = next(
            (c for c in reachable if c.name == recipient.preferred_channel), reachable[0]
        )
        delivered = True
        try:
            channel.send(recipient, text)
        except SendError as exc:
            delivered = False
            logger.warning("Could not send to lead %s via %s: %s", lead_id, channel.name, exc)
        if not record:
            return delivered
        with self._sessions.begin() as session:
            lead = session.get_one(Lead, lead_id)
            lead.messages.append(
                LeadMessage(
                    direction=Direction.OUTBOUND,
                    channel=channel.name,
                    content=text,
                    author=author,
                )
            )
            now = utcnow()
            if delivered:
                lead.last_outbound_at = now
                if lead.first_response_at is None:
                    lead.first_response_at = now
                    lead.events.append(LeadEvent(type="lead.first_response", detail={}))
            else:
                lead.events.append(
                    LeadEvent(type="message.failed", detail={"channel": channel.name})
                )
        return delivered

    @staticmethod
    def _safely(fn: Any, *args: Any) -> None:
        try:
            fn(*args)
        except Exception:
            logger.exception("Responder task failed")


_REPLY_CHANNELS = {"email", "telegram", "sms"}


def _recipient(lead: Lead) -> Recipient:
    last_inbound = next(
        (m.channel for m in reversed(lead.messages) if m.direction == Direction.INBOUND), None
    )
    return Recipient(
        lead_id=lead.id,
        name=lead.name,
        email=lead.email,
        telegram_chat_id=lead.telegram_chat_id,
        phone=lead.phone,
        preferred_channel=last_inbound if last_inbound in _REPLY_CHANNELS else None,
        first_contact=lead.last_outbound_at is None,
    )
