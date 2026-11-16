"""Telegram: a lead channel and a way to send replies.

Anyone who messages the bot becomes a lead, and the qualifying agent replies in
the chat. Uses long polling (``getUpdates``), so it works on a laptop or a
server with no public URL. Create a bot with @BotFather and set
``TELEGRAM_BOT_TOKEN``.
"""

from __future__ import annotations

import html
import logging
import re
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from lead_agent.api.ratelimit import RateLimiter
from lead_agent.channels.base import Recipient, SendError
from lead_agent.db import LeadStatus
from lead_agent.leads import LeadService
from lead_agent.responder import OPT_OUT_CONFIRMATION, LeadResponder

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4096
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)

NON_TEXT_REPLY = "Sorry, I can only read text messages for now. Please type your message."
RATE_LIMIT_REPLY = "You're sending messages very quickly. Please wait a moment and try again."
ERROR_REPLY = "Sorry, something went wrong on our side. Please try again in a minute."
WELCOME_BACK_REPLY = "Welcome back! Just reply here and we'll pick up where we left off."


class TelegramError(RuntimeError):
    """Raised when the Telegram Bot API returns an error."""


def to_telegram_html(text: str) -> str:
    """Escape text for Telegram's HTML mode and turn **bold** into <b>bold</b>."""
    return _BOLD.sub(r"<b>\1</b>", html.escape(text, quote=False))


def split_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split long messages on line breaks to fit Telegram's limit."""
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


class TelegramClient:
    """Minimal client for the Telegram Bot API."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.telegram.org",
        timeout: float = 40.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=f"{base_url}/bot{token}", timeout=timeout, transport=transport
        )

    def close(self) -> None:
        self._client.close()

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        try:
            response = self._client.post(f"/{method}", json=payload)
        except httpx.HTTPError as exc:
            # The exception text can include the URL, which contains the token.
            raise TelegramError(f"Could not reach Telegram ({type(exc).__name__})") from None
        try:
            body = response.json()
        except ValueError:
            raise TelegramError(f"Telegram returned HTTP {response.status_code}") from None
        if not body.get("ok"):
            raise TelegramError(f"Telegram error: {body.get('description', 'unknown')}")
        return body.get("result")

    def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result: list[dict[str, Any]] = self._call("getUpdates", payload)
        return result

    def send_message(self, chat_id: int | str, text: str) -> None:
        for chunk in split_message(text):
            self._call(
                "sendMessage",
                {"chat_id": chat_id, "text": to_telegram_html(chunk), "parse_mode": "HTML"},
            )

    def send_typing(self, chat_id: int | str) -> None:
        self._call("sendChatAction", {"chat_id": chat_id, "action": "typing"})


class TelegramChannel:
    """Sends the agent's messages to leads who reached us on Telegram."""

    name = "telegram"

    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    def can_reach(self, recipient: Recipient) -> bool:
        return recipient.telegram_chat_id is not None

    def send(self, recipient: Recipient, text: str) -> None:
        if recipient.telegram_chat_id is None:
            raise SendError("The lead has no Telegram chat.")
        try:
            self._client.send_message(recipient.telegram_chat_id, text)
        except TelegramError as exc:
            raise SendError(str(exc)) from None


class TelegramBot:
    """Turns Telegram chats into leads and passes their messages to the responder."""

    def __init__(
        self,
        client: TelegramClient,
        leads: LeadService,
        responder: LeadResponder,
        *,
        rate_limit_per_minute: int = 20,
        workers: int = 4,
        typing_interval: float = 4.0,
    ) -> None:
        self._client = client
        self._leads = leads
        self._responder = responder
        self._limiter = RateLimiter(rate_limit_per_minute)
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="telegram")
        self._chat_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
        self._typing_interval = typing_interval

    def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if chat.get("type") != "private" or "id" not in chat:
            return  # Only one-to-one chats; groups and channels are ignored.
        chat_id = chat["id"]
        chat_ref = str(chat_id)
        text = (message.get("text") or "").strip()
        sender = message.get("from") or {}
        name = " ".join(
            part for part in (sender.get("first_name"), sender.get("last_name")) if part
        )

        # Messages from one chat are handled in order, one at a time.
        with self._chat_locks[chat_ref]:
            try:
                self._handle(chat_id, chat_ref, name, text)
            except Exception:
                logger.exception("Failed to handle a Telegram message")
                self._safe_send(chat_id, ERROR_REPLY)

    def _handle(self, chat_id: int, chat_ref: str, name: str, text: str) -> None:
        if not text:
            self._client.send_message(chat_id, NON_TEXT_REPLY)
            return
        command = text.split(maxsplit=1)[0].split("@")[0].lower() if text.startswith("/") else ""
        lead = self._leads.telegram_lead(chat_ref)
        active = lead is not None and lead.status != LeadStatus.OPTED_OUT

        if command == "/stop":
            if lead is not None and active:
                self._responder.handle_inbound(lead.id, "STOP", "telegram")
            else:
                self._client.send_message(chat_id, OPT_OUT_CONFIRMATION)
            return
        if command in {"/start", "/help"}:
            if lead is not None and active:
                self._client.send_message(chat_id, WELCOME_BACK_REPLY)
                return
            # A new chat, or someone who opted out and chose to start again.
            lead_id = self._leads.start_telegram_lead(chat_ref, name)
            with self._typing(chat_id):
                self._responder.reply_to_new_lead(lead_id)
            return
        if not self._limiter.allow(chat_ref):
            self._client.send_message(chat_id, RATE_LIMIT_REPLY)
            return

        if lead is None:
            lead_id = self._leads.start_telegram_lead(chat_ref, name, text)
            with self._typing(chat_id):
                self._responder.reply_to_new_lead(lead_id)
            return
        if not active:
            # Opted out: keep the message for the record, but don't reply.
            self._responder.handle_inbound(lead.id, text, "telegram")
            return
        with self._typing(chat_id):
            self._responder.handle_inbound(lead.id, text, "telegram")

    def _typing(self, chat_id: int) -> _TypingIndicator:
        return _TypingIndicator(self._client, chat_id, self._typing_interval)

    def _safe_send(self, chat_id: int, text: str) -> None:
        try:
            self._client.send_message(chat_id, text)
        except TelegramError:
            logger.warning("Could not send an error reply on Telegram")

    def run_polling(self, stop: threading.Event, poll_timeout: int = 25) -> None:
        """Fetch updates until ``stop`` is set, handling each in a worker thread."""
        offset: int | None = None
        backoff = 1.0
        logger.info("Telegram bot started (long polling)")
        while not stop.is_set():
            try:
                updates = self._client.get_updates(offset, timeout=poll_timeout)
                backoff = 1.0
            except TelegramError as exc:
                logger.warning("Telegram polling failed: %s. Retrying in %.0fs", exc, backoff)
                stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            for update in updates:
                offset = int(update["update_id"]) + 1
                self._executor.submit(self.handle_update, update)
        self._executor.shutdown(wait=True)
        logger.info("Telegram bot stopped")


class _TypingIndicator:
    """Keeps Telegram's "typing…" status visible while the agent works."""

    def __init__(self, client: TelegramClient, chat_id: int, interval: float) -> None:
        self._client = client
        self._chat_id = chat_id
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> _TypingIndicator:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=1)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._client.send_typing(self._chat_id)
            except TelegramError:
                return
            self._stop.wait(self._interval)
