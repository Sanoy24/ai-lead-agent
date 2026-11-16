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
from lead_agent.db import Direction, Lead, LeadEvent, LeadMessage, LeadStatus, utcnow
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

_STATUS_FOR_OUTCOME = {
    Outcome.QUALIFYING: LeadStatus.QUALIFYING,
    Outcome.QUALIFIED: LeadStatus.QUALIFIED,
    Outcome.NURTURE: LeadStatus.NURTURING,
    Outcome.NOT_A_FIT: LeadStatus.LOST,
}
_FINAL_STATUSES = {LeadStatus.BOOKED, LeadStatus.LOST, LeadStatus.OPTED_OUT}


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
        self._send(recipient, text, lead_id, record=True)

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
            if lead.status == LeadStatus.OPTED_OUT:
                return
            if is_opt_out(text):
                self._opt_out(session, lead)
                recipient = _recipient(lead)
                confirm = True
            else:
                confirm = False
        if confirm:
            self._send(recipient, OPT_OUT_CONFIRMATION, lead_id, record=True)
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

        self._send(snapshot.recipient, text, lead_id, record=True)

    # --- Internals -------------------------------------------------------------

    def _final_text(
        self, recipient: Recipient, turn: AgentTurn, outcome: Outcome, reasons: list[str]
    ) -> str:
        first_name = recipient.name.split()[0] if recipient.name.strip() else "there"
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
                    "You can pick a time for your free assessment here"
                    if outcome is Outcome.QUALIFIED
                    else "Whenever you're ready, you can book a free assessment here"
                )
                text = f"{text}\n\n{lead_in}: {link}"
            elif outcome is Outcome.QUALIFIED:
                text = f"{text}\n\nA specialist will reach out shortly to schedule your assessment."
        return text

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
            intro = f"New enquiry from {lead.name}"
            if lead.service_interest:
                intro += f" about {lead.service_interest}"
            history.insert(0, Message("user", f"[{intro}.]"))
        return history

    def _send(self, recipient: Recipient, text: str, lead_id: str, *, record: bool) -> None:
        channel = next(c for c in self._channels if c.can_reach(recipient))
        delivered = True
        try:
            channel.send(recipient, text)
        except SendError as exc:
            delivered = False
            logger.warning("Could not send to lead %s via %s: %s", lead_id, channel.name, exc)
        if not record:
            return
        with self._sessions.begin() as session:
            lead = session.get_one(Lead, lead_id)
            lead.messages.append(
                LeadMessage(direction=Direction.OUTBOUND, channel=channel.name, content=text)
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

    @staticmethod
    def _safely(fn: Any, *args: Any) -> None:
        try:
            fn(*args)
        except Exception:
            logger.exception("Responder task failed")


def _recipient(lead: Lead) -> Recipient:
    return Recipient(
        lead_id=lead.id,
        name=lead.name,
        email=lead.email,
        telegram_chat_id=lead.telegram_chat_id,
    )
