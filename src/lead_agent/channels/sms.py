"""SMS through Twilio: text leads, and receive their texts by webhook.

Create a Twilio account (the free trial works for testing: it can text
numbers you've verified), buy or use the trial number, and set
``TWILIO_ACCOUNT_SID``, ``TWILIO_AUTH_TOKEN`` and ``TWILIO_FROM_NUMBER``.
Point the number's "A message comes in" webhook at ``/api/webhooks/sms``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from collections.abc import Mapping

import httpx

from lead_agent.channels.base import Recipient, SendError

MAX_SMS_LENGTH = 1500
"""Twilio joins long messages up to 1,600 characters; stay safely below that."""
OPT_OUT_FOOTER = "Reply STOP to opt out."

_NON_DIGITS = re.compile(r"\D")


class TwilioError(RuntimeError):
    """Raised when Twilio can't be reached or rejects a request."""


def to_e164(phone: str, default_country_code: str = "+1") -> str | None:
    """Normalise a phone number to E.164 (``+13035550100``).

    Numbers without a country code get ``default_country_code``. Returns None
    when there aren't enough digits to be a phone number.
    """
    raw = phone.strip()
    digits = _NON_DIGITS.sub("", raw)
    if raw.startswith("+"):
        number = digits
    elif raw.startswith("00"):
        number = digits[2:]
    else:
        country = _NON_DIGITS.sub("", default_country_code)
        national = digits.lstrip("0")
        number = (
            national
            if national.startswith(country) and len(national) > 10
            else (country + national)
        )
    return f"+{number}" if 8 <= len(number) <= 15 else None


def phone_key(phone: str) -> str:
    """A comparison key that matches the same number written different ways."""
    return _NON_DIGITS.sub("", phone)[-9:]


def twilio_signature(url: str, params: Mapping[str, str], auth_token: str) -> str:
    """The ``X-Twilio-Signature`` Twilio sends for a webhook request."""
    payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def valid_twilio_signature(
    url: str, params: Mapping[str, str], signature: str | None, auth_token: str
) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(signature.strip(), twilio_signature(url, params, auth_token))


class TwilioClient:
    """Minimal client for sending SMS with the Twilio REST API."""

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        *,
        base_url: str = "https://api.twilio.com",
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._account_sid = account_sid
        self._from = from_number
        self._client = httpx.Client(
            base_url=base_url,
            auth=(account_sid, auth_token),
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def send_sms(self, to: str, body: str) -> None:
        try:
            response = self._client.post(
                f"/2010-04-01/Accounts/{self._account_sid}/Messages.json",
                data={"To": to, "From": self._from, "Body": body},
            )
        except httpx.HTTPError as exc:
            raise TwilioError(f"Could not reach Twilio ({type(exc).__name__})") from None
        if response.status_code >= 400:
            try:
                detail = response.json()
                message = f"{detail.get('code', '')} {detail.get('message', '')}".strip()
            except ValueError:
                message = ""
            raise TwilioError(f"Twilio returned {response.status_code}: {message[:200]}")


class SmsChannel:
    """Texts the agent's messages to leads with a phone number."""

    name = "sms"

    def __init__(self, client: TwilioClient, *, default_country_code: str = "+1") -> None:
        self._client = client
        self._country_code = default_country_code

    def can_reach(self, recipient: Recipient) -> bool:
        return bool(recipient.phone and to_e164(recipient.phone, self._country_code))

    def send(self, recipient: Recipient, text: str) -> None:
        number = to_e164(recipient.phone or "", self._country_code)
        if number is None:
            raise SendError("The lead has no valid phone number.")
        body = text
        if recipient.first_contact:
            body = f"{body}\n\n{OPT_OUT_FOOTER}"
        if len(body) > MAX_SMS_LENGTH:
            body = body[: MAX_SMS_LENGTH - 1].rstrip() + "…"
        try:
            self._client.send_sms(number, body)
        except TwilioError as exc:
            raise SendError(str(exc)) from None
