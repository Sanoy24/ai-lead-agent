"""Email: send the agent's messages by SMTP and read lead replies by IMAP.

Works with any mail provider, including a free Gmail account with an app
password (SMTP ``smtp.gmail.com:587``, IMAP ``imap.gmail.com:993``). Replies
are matched to leads by a reference header in the thread, or by the sender's
address, and continue the same conversation.
"""

from __future__ import annotations

import contextlib
import html
import imaplib
import logging
import re
import smtplib
import ssl
import threading
from collections.abc import Callable
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from typing import Literal, Protocol

from lead_agent.channels.base import Recipient, SendError

logger = logging.getLogger(__name__)

Security = Literal["starttls", "ssl", "none"]

_LEAD_REF = re.compile(r"<lead-([0-9a-f]{32})@")
_QUOTE_START = re.compile(
    r"^(On .{5,200} wrote:|-{2,}\s*Original Message\s*-{2,}|From: .+|>.*)$",
    re.IGNORECASE,
)
_TAGS = re.compile(r"<[^>]+>")
_SIGNATURE = "\n\n--\n"


# --- Sending -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SmtpConfig:
    host: str
    port: int = 587
    username: str | None = None
    password: str | None = None
    security: Security = "starttls"
    timeout: float = 30.0


class SmtpSender:
    """Sends one message per connection: simple, and fine for lead volumes."""

    def __init__(self, config: SmtpConfig) -> None:
        self._config = config

    def send(self, message: EmailMessage) -> None:
        c = self._config
        try:
            smtp: smtplib.SMTP
            if c.security == "ssl":
                smtp = smtplib.SMTP_SSL(
                    c.host, c.port, timeout=c.timeout, context=ssl.create_default_context()
                )
            else:
                smtp = smtplib.SMTP(c.host, c.port, timeout=c.timeout)
            with smtp:
                if c.security == "starttls":
                    smtp.starttls(context=ssl.create_default_context())
                if c.username and c.password:
                    smtp.login(c.username, c.password)
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            raise SendError(f"SMTP error ({type(exc).__name__})") from None


class Sender(Protocol):
    def send(self, message: EmailMessage) -> None: ...


class EmailChannel:
    """Emails the agent's messages to leads, threaded per lead."""

    name = "email"

    def __init__(
        self,
        sender: Sender,
        *,
        from_address: str,
        from_name: str,
        business_name: str,
    ) -> None:
        self._sender = sender
        self._from_address = from_address
        self._from_name = from_name
        self._business_name = business_name
        self._domain = from_address.rpartition("@")[2] or "localhost"

    def can_reach(self, recipient: Recipient) -> bool:
        return recipient.email is not None

    def build(self, recipient: Recipient, text: str) -> EmailMessage:
        if recipient.email is None:
            raise SendError("The lead has no email address.")
        message = EmailMessage()
        message["From"] = formataddr((self._from_name, self._from_address))
        message["To"] = formataddr((recipient.name, recipient.email))
        message["Subject"] = f"Your enquiry with {self._business_name}"
        message["Message-ID"] = make_msgid(domain=self._domain)
        # A stable reference per lead keeps the conversation in one thread and
        # lets us match replies to the lead.
        thread = f"<lead-{recipient.lead_id}@{self._domain}>"
        message["References"] = thread
        message["In-Reply-To"] = thread
        message["List-Unsubscribe"] = f"<mailto:{self._from_address}?subject=unsubscribe>"
        message.set_content(
            f"{text}{_SIGNATURE}{self._from_name}\n"
            "Reply STOP if you'd rather not hear from us again."
        )
        return message

    def send(self, recipient: Recipient, text: str) -> None:
        self._sender.send(self.build(recipient, text))


# --- Receiving -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InboundEmail:
    sender: str
    subject: str
    text: str
    lead_id: str | None
    """The lead named in the thread's reference headers, if any."""


def parse_email(raw: bytes) -> InboundEmail | None:
    """Read a reply from a lead. Returns None for auto-replies and empty mail."""
    message = message_from_bytes(raw, policy=policy.default)
    if _is_automatic(message):
        return None
    sender = parseaddr(str(message.get("From", "")))[1].lower()
    if not sender:
        return None
    subject = str(message.get("Subject", "")).strip()

    body = message.get_body(preferencelist=("plain", "html"))
    content = ""
    if isinstance(body, EmailMessage):
        content = body.get_content()
        if body.get_content_type() == "text/html":
            content = html.unescape(_TAGS.sub("", re.sub(r"<br\s*/?>|</p>", "\n", content)))
    text = strip_quoted_reply(content)
    if not text and "unsubscribe" in subject.lower():
        text = "unsubscribe"  # A List-Unsubscribe click sends an empty mail.
    if not text:
        return None

    references = f"{message.get('In-Reply-To', '')} {message.get('References', '')}"
    match = _LEAD_REF.search(references)
    return InboundEmail(sender, subject, text, match.group(1) if match else None)


def strip_quoted_reply(text: str) -> str:
    """Keep only the new part of a reply: drop the quoted history and our signature."""
    kept: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if _QUOTE_START.match(stripped) or stripped == "--":
            break
        kept.append(line)
    return "\n".join(kept).strip()[:2000]


def _is_automatic(message: EmailMessage) -> bool:
    auto = str(message.get("Auto-Submitted", "no")).lower()
    precedence = str(message.get("Precedence", "")).lower()
    return (
        auto != "no"
        or precedence in {"bulk", "junk", "list", "auto_reply"}
        or "X-Autoreply" in message
        or "X-Autorespond" in message
    )


class Mailbox(Protocol):
    def fetch_unseen(self) -> list[tuple[str, bytes]]:
        """Unread messages as (uid, raw bytes), without marking them read."""
        ...

    def mark_seen(self, uid: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ImapConfig:
    host: str
    port: int = 993
    username: str = ""
    password: str = ""
    folder: str = "INBOX"


class ImapMailbox:
    """Reads unread mail over IMAP (SSL), connecting fresh for each poll."""

    def __init__(self, config: ImapConfig) -> None:
        self._config = config
        self._imap: imaplib.IMAP4_SSL | None = None

    def _connect(self) -> imaplib.IMAP4_SSL:
        c = self._config
        imap = imaplib.IMAP4_SSL(c.host, c.port, ssl_context=ssl.create_default_context())
        imap.login(c.username, c.password)
        imap.select(c.folder)
        return imap

    def fetch_unseen(self) -> list[tuple[str, bytes]]:
        self.close()
        self._imap = self._connect()
        _, data = self._imap.uid("search", "UNSEEN")
        uids = data[0].decode().split() if data and data[0] else []
        messages: list[tuple[str, bytes]] = []
        for uid in uids[:50]:
            _, parts = self._imap.uid("fetch", uid, "(BODY.PEEK[])")
            for part in parts:
                if isinstance(part, tuple):
                    messages.append((uid, bytes(part[1])))
        return messages

    def mark_seen(self, uid: str) -> None:
        if self._imap is not None:
            self._imap.uid("store", uid, "+FLAGS", "(\\Seen)")

    def close(self) -> None:
        if self._imap is not None:
            with contextlib.suppress(imaplib.IMAP4.error, OSError):
                self._imap.logout()
            self._imap = None


class EmailInbox:
    """Polls the mailbox and passes lead replies to ``handle``.

    ``find_lead(sender, lead_id_hint)`` returns the lead's id, or None for
    mail that isn't from a known lead (which is left unread for a person).
    """

    def __init__(
        self,
        mailbox: Mailbox,
        find_lead: Callable[[str, str | None], str | None],
        handle: Callable[[str, str], None],
    ) -> None:
        self._mailbox = mailbox
        self._find_lead = find_lead
        self._handle = handle

    def poll_once(self) -> int:
        """Process unread mail. Returns how many lead replies were handled."""
        handled = 0
        for uid, raw in self._mailbox.fetch_unseen():
            try:
                inbound = parse_email(raw)
                lead_id = self._find_lead(inbound.sender, inbound.lead_id) if inbound else None
                if inbound is None or lead_id is None:
                    continue  # Not a lead reply: leave it for a person to read.
                self._mailbox.mark_seen(uid)
                self._handle(lead_id, inbound.text)
                handled += 1
            except Exception:
                logger.exception("Failed to process an email")
        return handled

    def run_polling(self, stop: threading.Event, interval: float = 60.0) -> None:
        logger.info("Email inbox polling started")
        while not stop.is_set():
            try:
                self.poll_once()
            except (imaplib.IMAP4.error, OSError) as exc:
                logger.warning("Email polling failed (%s); retrying later", type(exc).__name__)
            stop.wait(interval)
        logger.info("Email inbox polling stopped")
