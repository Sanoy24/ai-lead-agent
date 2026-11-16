"""HubSpot sync: every lead becomes a contact, with key moments added as notes.

Works with the free HubSpot CRM. Create a private app (Settings > Integrations >
Private apps) with the ``crm.objects.contacts.read`` and
``crm.objects.contacts.write`` scopes, and set ``HUBSPOT_ACCESS_TOKEN``.

The sync engine itself lives in ``lead_agent.crm.sync``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session, sessionmaker

from lead_agent.crm.sync import CrmContact, CrmError, CrmSync
from lead_agent.db import LeadStatus, utcnow

logger = logging.getLogger(__name__)

NOTE_TO_CONTACT = 202
"""HubSpot's association type id for note → contact."""

LEAD_STATUS = {
    LeadStatus.NEW: "NEW",
    LeadStatus.CONTACTED: "ATTEMPTED_TO_CONTACT",
    LeadStatus.QUALIFYING: "IN_PROGRESS",
    LeadStatus.QUALIFIED: "CONNECTED",
    LeadStatus.BOOKED: "OPEN_DEAL",
    LeadStatus.NURTURING: "BAD_TIMING",
    LeadStatus.LOST: "UNQUALIFIED",
    LeadStatus.OPTED_OUT: "UNQUALIFIED",
}
"""Our pipeline mapped onto HubSpot's default "Lead status" property."""


class HubSpotError(CrmError):
    """Raised when the HubSpot API returns an error."""


class HubSpotClient:
    """Minimal client for the HubSpot CRM v3 API."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.hubapi.com",
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {token}"},
        )

    def close(self) -> None:
        self._client.close()

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            response = self._client.request(method, path, json=body)
        except httpx.HTTPError as exc:
            raise HubSpotError(f"Could not reach HubSpot ({type(exc).__name__})") from None
        if response.status_code >= 400:
            try:
                message = str(response.json().get("message", ""))
            except ValueError:
                message = ""
            raise HubSpotError(f"HubSpot returned {response.status_code}: {message[:200]}")
        return response.json() if response.content else None

    def find_contact(self, email: str) -> str | None:
        body = {
            "filterGroups": [
                {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
            ],
            "properties": ["email"],
            "limit": 1,
        }
        results = self._call("POST", "/crm/v3/objects/contacts/search", body).get("results", [])
        return str(results[0]["id"]) if results else None

    def create_contact(self, properties: dict[str, str]) -> str:
        created = self._call("POST", "/crm/v3/objects/contacts", {"properties": properties})
        return str(created["id"])

    def update_contact(self, contact_id: str, properties: dict[str, str]) -> None:
        self._call("PATCH", f"/crm/v3/objects/contacts/{contact_id}", {"properties": properties})

    def add_note(self, contact_id: str, body: str, at: datetime) -> None:
        self._call(
            "POST",
            "/crm/v3/objects/notes",
            {
                "properties": {"hs_timestamp": at.isoformat(), "hs_note_body": body},
                "associations": [
                    {
                        "to": {"id": contact_id},
                        "types": [
                            {
                                "associationCategory": "HUBSPOT_DEFINED",
                                "associationTypeId": NOTE_TO_CONTACT,
                            }
                        ],
                    }
                ],
            },
        )


class HubSpotConnector:
    """Creates or updates HubSpot contacts and adds notes to them."""

    name = "HubSpot"

    def __init__(self, client: HubSpotClient) -> None:
        self._client = client

    def push(self, contact: CrmContact) -> str:
        properties = {
            "firstname": contact.first_name,
            "lastname": contact.last_name,
            "hs_lead_status": LEAD_STATUS.get(contact.status, "OPEN"),
        }
        if contact.email:
            properties["email"] = contact.email
        if contact.phone:
            properties["phone"] = contact.phone
        contact_id = contact.contact_id
        if contact_id is None and contact.email:
            contact_id = self._client.find_contact(contact.email)
        if contact_id is None:
            contact_id = self._client.create_contact(properties)
        else:
            self._client.update_contact(contact_id, properties)
        for note in contact.notes:
            self._client.add_note(contact_id, note.html, note.created_at)
        return contact_id


class HubSpotSync(CrmSync):
    """CRM sync with HubSpot as the connector."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        client: HubSpotClient,
        *,
        dashboard_url: str | None = None,
        batch_size: int = 20,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        super().__init__(
            sessions,
            HubSpotConnector(client),
            dashboard_url=dashboard_url,
            batch_size=batch_size,
            clock=clock,
        )
