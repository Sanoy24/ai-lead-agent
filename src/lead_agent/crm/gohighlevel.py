"""GoHighLevel: sync leads to contacts, and take new leads in from GoHighLevel.

Outbound: every lead becomes a contact in your GoHighLevel location (matched
by email or phone through GoHighLevel's upsert), tagged with its pipeline
stage (for example ``ai-lead: qualified``), with key moments added as notes.
Create a Private Integration token (Settings > Private Integrations) with the
contacts read/write scopes and set ``GOHIGHLEVEL_API_TOKEN`` and
``GOHIGHLEVEL_LOCATION_ID``.

Inbound: a GoHighLevel workflow can send new contacts to
``/api/webhooks/gohighlevel`` with a "Webhook" action, so the agent replies
within a minute to leads captured in GoHighLevel forms and funnels.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session, sessionmaker

from lead_agent.crm.sync import CrmContact, CrmError, CrmSync
from lead_agent.db import LeadStatus, utcnow

API_VERSION = "2021-07-28"
TAG_PREFIX = "ai-lead: "
STATUS_TAGS = {status: f"{TAG_PREFIX}{status.value.replace('_', ' ')}" for status in LeadStatus}
"""One tag per pipeline stage, so GoHighLevel workflows can react to it."""


class GoHighLevelError(CrmError):
    """Raised when the GoHighLevel API can't be reached or returns an error."""


class GoHighLevelClient:
    """Minimal client for the GoHighLevel (LeadConnector) v2 API."""

    def __init__(
        self,
        token: str,
        location_id: str,
        *,
        base_url: str = "https://services.leadconnectorhq.com",
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.location_id = location_id
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Version": API_VERSION,
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    def _call(self, method: str, path: str, body: dict[str, Any]) -> Any:
        try:
            response = self._client.request(method, path, json=body)
        except httpx.HTTPError as exc:
            raise GoHighLevelError(f"Could not reach GoHighLevel ({type(exc).__name__})") from None
        if response.status_code >= 400:
            try:
                message = str(response.json().get("message", ""))
            except ValueError:
                message = ""
            raise GoHighLevelError(f"GoHighLevel returned {response.status_code}: {message[:200]}")
        return response.json() if response.content else None

    def upsert_contact(self, fields: dict[str, Any]) -> str:
        """Create the contact, or update the one with the same email or phone."""
        result = self._call("POST", "/contacts/upsert", {"locationId": self.location_id, **fields})
        return str(result["contact"]["id"])

    def add_tags(self, contact_id: str, tags: list[str]) -> None:
        self._call("POST", f"/contacts/{contact_id}/tags", {"tags": tags})

    def remove_tags(self, contact_id: str, tags: list[str]) -> None:
        self._call("DELETE", f"/contacts/{contact_id}/tags", {"tags": tags})

    def add_note(self, contact_id: str, body: str) -> None:
        self._call("POST", f"/contacts/{contact_id}/notes", {"body": body})


class GoHighLevelConnector:
    """Upserts GoHighLevel contacts, keeps their stage tag current and adds notes."""

    name = "GoHighLevel"

    def __init__(self, client: GoHighLevelClient) -> None:
        self._client = client

    def push(self, contact: CrmContact) -> str:
        fields: dict[str, Any] = {
            "firstName": contact.first_name,
            "lastName": contact.last_name,
            "source": "ai-lead-agent",
        }
        if contact.email:
            fields["email"] = contact.email
        if contact.phone:
            fields["phone"] = contact.phone
        # Upsert never sends tags, so the business's own tags are left alone.
        contact_id = self._client.upsert_contact(fields)

        current = STATUS_TAGS[contact.status]
        stale = [tag for status, tag in STATUS_TAGS.items() if status != contact.status]
        self._client.remove_tags(contact_id, stale)
        self._client.add_tags(contact_id, [current])
        for note in contact.notes:
            self._client.add_note(contact_id, note.text)
        return contact_id


class GoHighLevelSync(CrmSync):
    """CRM sync with GoHighLevel as the connector."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        client: GoHighLevelClient,
        *,
        dashboard_url: str | None = None,
        batch_size: int = 20,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        super().__init__(
            sessions,
            GoHighLevelConnector(client),
            dashboard_url=dashboard_url,
            batch_size=batch_size,
            clock=clock,
        )


def contact_url(location_id: str, contact_id: str) -> str:
    """The contact's page in the GoHighLevel app."""
    return f"https://app.gohighlevel.com/v2/location/{location_id}/contacts/detail/{contact_id}"


def webhook_lead_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull the lead's details out of a GoHighLevel workflow webhook.

    Workflow webhooks send the contact's standard fields at the top level
    (``first_name``, ``email``, ``phone``, ``contact_id`` and so on); custom
    data set on the webhook action may arrive under ``customData``.
    """
    raw_custom = payload.get("customData")
    custom: dict[str, Any] = raw_custom if isinstance(raw_custom, dict) else {}

    def text(*keys: str) -> str | None:
        for source in (payload, custom):
            for key in keys:
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    name = text("full_name", "name") or " ".join(
        part for part in (text("first_name", "firstName"), text("last_name", "lastName")) if part
    )
    return {
        "name": name or "GoHighLevel lead",
        "email": text("email"),
        "phone": text("phone"),
        "message": text("message", "notes") or "",
        "service_interest": text("service", "service_interest"),
        "contact_id": text("contact_id", "contactId", "id"),
    }
