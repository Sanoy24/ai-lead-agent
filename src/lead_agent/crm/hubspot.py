"""HubSpot sync: every lead becomes a contact, with key moments added as notes.

Works with the free HubSpot CRM. Create a private app (Settings > Integrations >
Private apps) with the ``crm.objects.contacts.read`` and
``crm.objects.contacts.write`` scopes, and set ``HUBSPOT_ACCESS_TOKEN``.

Sync runs in the background and never blocks a reply to a lead. Changed leads
are picked up on the next pass, so a HubSpot outage only delays the sync.
"""

from __future__ import annotations

import html
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, selectinload, sessionmaker

from lead_agent.db import Lead, LeadEvent, LeadStatus, utcnow

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

NOTE_EVENTS = {
    "lead.created",
    "lead.status_changed",
    "lead.booked",
    "lead.rescheduled",
    "lead.booking_cancelled",
    "lead.handed_off",
    "lead.opted_out",
}


class HubSpotError(RuntimeError):
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


@dataclass(frozen=True, slots=True)
class _Snapshot:
    id: str
    contact_id: str | None
    email: str | None
    properties: dict[str, str]
    notes: list[tuple[int, str, datetime]]
    last_event_id: int


class HubSpotSync:
    """Pushes new and changed leads to HubSpot. Run ``run_polling`` in a thread."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        client: HubSpotClient,
        *,
        dashboard_url: str | None = None,
        batch_size: int = 20,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sessions = sessions
        self._client = client
        self._dashboard_url = dashboard_url.rstrip("/") if dashboard_url else None
        self._batch_size = batch_size
        self._clock = clock

    def sync_once(self) -> int:
        """Sync leads that are new or changed since their last sync. Returns how many."""
        started = self._clock()
        with self._sessions() as session:
            leads = session.scalars(
                select(Lead)
                .where(or_(Lead.crm_synced_at.is_(None), Lead.updated_at > Lead.crm_synced_at))
                .options(selectinload(Lead.events), selectinload(Lead.messages))
                .order_by(Lead.updated_at)
                .limit(self._batch_size)
            ).all()
            snapshots = [self._snapshot(lead) for lead in leads]

        synced = 0
        for snapshot in snapshots:
            try:
                contact_id = self._push(snapshot)
            except HubSpotError as exc:
                logger.warning("HubSpot sync failed for lead %s: %s", snapshot.id, exc)
                continue
            with self._sessions.begin() as session:
                session.execute(
                    update(Lead)
                    .where(Lead.id == snapshot.id)
                    .values(
                        crm_contact_id=contact_id,
                        crm_synced_at=started,
                        crm_synced_event_id=snapshot.last_event_id,
                        # Leave updated_at alone, or every sync would look like a change.
                        updated_at=Lead.updated_at,
                    )
                )
            synced += 1
        return synced

    def run_polling(self, stop: threading.Event, interval: float = 30.0) -> None:
        logger.info("HubSpot sync started")
        while not stop.is_set():
            try:
                self.sync_once()
            except Exception:
                logger.exception("HubSpot sync run failed")
            stop.wait(interval)
        logger.info("HubSpot sync stopped")

    def _push(self, snapshot: _Snapshot) -> str:
        contact_id = snapshot.contact_id
        if contact_id is None and snapshot.email:
            contact_id = self._client.find_contact(snapshot.email)
        if contact_id is None:
            contact_id = self._client.create_contact(snapshot.properties)
        else:
            self._client.update_contact(contact_id, snapshot.properties)
        for _, body, at in snapshot.notes:
            self._client.add_note(contact_id, body, at)
        return contact_id

    def _snapshot(self, lead: Lead) -> _Snapshot:
        first, _, last = lead.name.partition(" ")
        properties = {
            "firstname": first,
            "lastname": last,
            "hs_lead_status": LEAD_STATUS.get(LeadStatus(lead.status), "OPEN"),
        }
        if lead.email:
            properties["email"] = lead.email
        if lead.phone:
            properties["phone"] = lead.phone
        new_events = [e for e in lead.events if e.id > (lead.crm_synced_event_id or 0)]
        notes = [
            (event.id, self._note(lead, event), event.created_at)
            for event in new_events
            if event.type in NOTE_EVENTS
        ]
        last_event_id = max((e.id for e in lead.events), default=lead.crm_synced_event_id or 0)
        return _Snapshot(lead.id, lead.crm_contact_id, lead.email, properties, notes, last_event_id)

    def _note(self, lead: Lead, event: LeadEvent) -> str:
        d = event.detail
        if event.type == "lead.created":
            first = next((m.content for m in lead.messages), "")
            lines = [f"<b>New enquiry</b> via {html.escape(str(d.get('source', lead.source)))}"]
            if lead.service_interest:
                lines.append(f"Interested in: {html.escape(lead.service_interest)}")
            if first:
                lines.append(f"Message: {html.escape(first[:500])}")
        elif event.type == "lead.status_changed":
            lines = [f"<b>Status:</b> {html.escape(str(d.get('to', lead.status)))}"]
            if lead.answers:
                lines += [
                    f"{html.escape(k.replace('_', ' '))}: {html.escape(str(v))}"
                    for k, v in lead.answers.items()
                ]
            if lead.score is not None:
                lines.append(f"Score: {lead.score}")
        elif event.type == "lead.handed_off":
            lines = [
                "<b>The AI agent asked for a person</b>",
                html.escape(str(d.get("summary") or d.get("reason") or "")),
            ]
        elif event.type == "lead.booked":
            when = lead.appointment_at.isoformat() if lead.appointment_at else "a time"
            lines = [f"<b>Booked a free assessment</b> for {html.escape(when)} (UTC)"]
        elif event.type == "lead.opted_out":
            lines = ["<b>Opted out.</b> Do not contact."]
        else:
            lines = [html.escape(event.type.replace("lead.", "").replace("_", " ").capitalize())]
        if self._dashboard_url:
            lines.append(
                f'<a href="{self._dashboard_url}/admin/leads/{lead.id}">Open in ai-lead-agent</a>'
            )
        return "<br>".join(line for line in lines if line)
