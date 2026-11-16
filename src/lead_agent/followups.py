"""Follow-ups: a friendly nudge when a lead goes quiet, never during quiet hours.

After each message from the agent, the next follow-up is scheduled from the
business profile (by default 1, 3 and 7 days later). Any reply from the lead,
a booking, an opt-out or a person taking over cancels it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from lead_agent.db import Lead, LeadStatus, utcnow

logger = logging.getLogger(__name__)

FOLLOW_UP_STATUSES = frozenset(
    {
        LeadStatus.NEW,
        LeadStatus.CONTACTED,
        LeadStatus.QUALIFYING,
        LeadStatus.QUALIFIED,
        LeadStatus.NURTURING,
    }
)


class FollowUpPolicy(BaseModel):
    delays_hours: list[float] = Field(default_factory=lambda: [24.0, 72.0, 168.0])
    """Hours after the agent's last message for each follow-up. Empty turns them off."""
    quiet_start: time = time(20, 0)
    quiet_end: time = time(9, 0)
    """No follow-ups between these local times, in the lead's time zone."""

    @property
    def max_follow_ups(self) -> int:
        return len(self.delays_hours)

    def is_quiet(self, local: time) -> bool:
        start, end = self.quiet_start, self.quiet_end
        if start == end:
            return False
        if start < end:
            return start <= local < end
        return local >= start or local < end  # Quiet hours span midnight.

    def next_allowed(self, moment: datetime, timezone: str) -> datetime:
        """The first time at or after ``moment`` that's outside quiet hours."""
        zone = ZoneInfo(timezone)
        local = moment.astimezone(zone)
        if not self.is_quiet(local.time().replace(tzinfo=None)):
            return moment
        end = datetime.combine(local.date(), self.quiet_end, tzinfo=zone)
        if end <= local:
            end = datetime.combine(local.date() + timedelta(days=1), self.quiet_end, tzinfo=zone)
        return end.astimezone(UTC)

    def schedule(self, after: datetime, sent: int, timezone: str) -> datetime | None:
        """When to send follow-up number ``sent + 1``, or None when they're used up."""
        if sent >= self.max_follow_ups:
            return None
        return self.next_allowed(after + timedelta(hours=self.delays_hours[sent]), timezone)


class FollowUpScheduler:
    """Sends follow-ups that are due. Run ``run_polling`` in a background thread."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        send_follow_up: Callable[[str], None],
        policy: FollowUpPolicy,
        default_timezone: str,
        *,
        clock: Callable[[], datetime] = utcnow,
        batch_size: int = 20,
    ) -> None:
        self._sessions = sessions
        self._send_follow_up = send_follow_up
        self._policy = policy
        self._default_timezone = default_timezone
        self._clock = clock
        self._batch_size = batch_size

    def run_once(self) -> int:
        """Send due follow-ups. Returns how many were sent."""
        now = self._clock()
        due: list[str] = []
        with self._sessions.begin() as session:
            leads = session.scalars(
                select(Lead)
                .where(
                    Lead.next_follow_up_at.is_not(None),
                    Lead.next_follow_up_at <= now,
                )
                .order_by(Lead.next_follow_up_at)
                .limit(self._batch_size)
            ).all()
            for lead in leads:
                if (
                    lead.status not in FOLLOW_UP_STATUSES
                    or lead.ai_paused
                    or not lead.consent
                    or lead.opted_out_at is not None
                ):
                    lead.next_follow_up_at = None
                    continue
                # A backlog (e.g. the server was off overnight) must still respect quiet hours.
                allowed = self._policy.next_allowed(now, lead.timezone or self._default_timezone)
                if allowed > now:
                    lead.next_follow_up_at = allowed
                    continue
                lead.next_follow_up_at = None  # Claimed: never sent twice.
                due.append(lead.id)

        sent = 0
        for lead_id in due:
            try:
                self._send_follow_up(lead_id)
                sent += 1
            except Exception:
                logger.exception("Follow-up for lead %s failed", lead_id)
        return sent

    def run_polling(self, stop: threading.Event, interval: float = 60.0) -> None:
        logger.info("Follow-up scheduler started")
        while not stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("Follow-up run failed")
            stop.wait(interval)
        logger.info("Follow-up scheduler stopped")
