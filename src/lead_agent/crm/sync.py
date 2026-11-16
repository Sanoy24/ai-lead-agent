"""CRM sync engine: pushes new and changed leads to a CRM through a connector.

Each connector (HubSpot, GoHighLevel) knows its own API; this module decides
which leads to sync, builds the contact snapshot and notes, and records what
was synced. Sync runs in the background and never blocks a reply to a lead.
Changed leads are picked up on the next pass, so an outage only delays it.
"""

from __future__ import annotations

import html
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, selectinload, sessionmaker

from lead_agent.db import Lead, LeadEvent, LeadStatus, utcnow

logger = logging.getLogger(__name__)

NOTE_EVENTS = {
    "lead.created",
    "lead.status_changed",
    "lead.booked",
    "lead.rescheduled",
    "lead.booking_cancelled",
    "lead.handed_off",
    "lead.opted_out",
}
"""Events worth a note in the CRM. Answers and scores appear in status notes."""

_TAGS = re.compile(r"<[^>]+>")


class CrmError(RuntimeError):
    """Raised by a connector when the CRM can't be reached or rejects a request."""


@dataclass(frozen=True, slots=True)
class Note:
    event_id: int
    html: str
    created_at: datetime

    @property
    def text(self) -> str:
        """The note as plain text, for CRMs that don't render HTML."""
        with_breaks = self.html.replace("<br>", "\n")
        return html.unescape(_TAGS.sub("", with_breaks))


@dataclass(frozen=True, slots=True)
class CrmContact:
    lead_id: str
    contact_id: str | None
    """The CRM's id for this lead, once known."""
    first_name: str
    last_name: str
    email: str | None
    phone: str | None
    status: LeadStatus
    notes: list[Note] = field(default_factory=list)
    last_event_id: int = 0


class CrmConnector(Protocol):
    name: str

    def push(self, contact: CrmContact) -> str:
        """Create or update the contact, add its notes, and return the CRM's contact id."""
        ...


class CrmSync:
    """Pushes new and changed leads through a connector. Run ``run_polling`` in a thread."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        connector: CrmConnector,
        *,
        dashboard_url: str | None = None,
        batch_size: int = 20,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sessions = sessions
        self._connector = connector
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
            contacts = [self._snapshot(lead) for lead in leads]

        synced = 0
        for contact in contacts:
            try:
                contact_id = self._connector.push(contact)
            except CrmError as exc:
                logger.warning(
                    "%s sync failed for lead %s: %s", self._connector.name, contact.lead_id, exc
                )
                continue
            with self._sessions.begin() as session:
                session.execute(
                    update(Lead)
                    .where(Lead.id == contact.lead_id)
                    .values(
                        crm_contact_id=contact_id,
                        crm_synced_at=started,
                        crm_synced_event_id=contact.last_event_id,
                        # Leave updated_at alone, or every sync would look like a change.
                        updated_at=Lead.updated_at,
                    )
                )
            synced += 1
        return synced

    def run_polling(self, stop: threading.Event, interval: float = 30.0) -> None:
        logger.info("%s sync started", self._connector.name)
        while not stop.is_set():
            try:
                self.sync_once()
            except Exception:
                logger.exception("%s sync run failed", self._connector.name)
            stop.wait(interval)
        logger.info("%s sync stopped", self._connector.name)

    def _snapshot(self, lead: Lead) -> CrmContact:
        first, _, last = lead.name.partition(" ")
        new_events = [e for e in lead.events if e.id > (lead.crm_synced_event_id or 0)]
        notes = [
            Note(event.id, self._note(lead, event), event.created_at)
            for event in new_events
            if event.type in NOTE_EVENTS
        ]
        return CrmContact(
            lead_id=lead.id,
            contact_id=lead.crm_contact_id,
            first_name=first,
            last_name=last,
            email=lead.email,
            phone=lead.phone,
            status=LeadStatus(lead.status),
            notes=notes,
            last_event_id=max((e.id for e in lead.events), default=lead.crm_synced_event_id or 0),
        )

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
