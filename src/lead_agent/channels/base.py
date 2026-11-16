"""How messages reach a lead."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Recipient:
    lead_id: str
    name: str
    email: str | None
    telegram_chat_id: str | None
    phone: str | None = None
    preferred_channel: str | None = None
    """The channel the lead last wrote from, so replies go back the same way."""
    first_contact: bool = False
    """True when nothing has been sent to the lead yet."""


class Channel(Protocol):
    name: str

    def can_reach(self, recipient: Recipient) -> bool: ...

    def send(self, recipient: Recipient, text: str) -> None: ...


class LogChannel:
    """Writes messages to the log instead of sending them.

    Used when no real channel can reach the lead, e.g. in development before
    email is configured. Messages are still saved, so they show up in the
    dashboard.
    """

    name = "log"

    def can_reach(self, recipient: Recipient) -> bool:
        return True

    def send(self, recipient: Recipient, text: str) -> None:
        logger.info("[log channel] to lead %s: %s", recipient.lead_id, text)


class SendError(RuntimeError):
    """Raised by a channel when a message could not be delivered."""
