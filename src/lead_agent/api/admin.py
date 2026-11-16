"""Admin JSON API used by the dashboard.

Disabled (404) unless ``ADMIN_PASSWORD`` is set. Login exchanges the password
for an HttpOnly, SameSite=Strict session cookie.
"""

import secrets
import statistics
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from lead_agent.api.auth import COOKIE_NAME, SESSION_SECONDS, SessionSigner, password_matches
from lead_agent.api.ratelimit import RateLimiter
from lead_agent.campaigns import (
    MAX_CONTACTS,
    CampaignStats,
    ImportResult,
    ReactivationService,
)
from lead_agent.config import Settings
from lead_agent.db import Direction, Lead, LeadEvent, LeadMessage, LeadStatus, utcnow
from lead_agent.responder import LeadResponder, OptedOutError

SPEED_TARGET_SECONDS = 60
"""A lead counts as answered "fast" when the first reply took at most this long."""


class LoginRequest(BaseModel):
    password: str = Field(max_length=200)


class LeadSummary(BaseModel):
    id: str
    name: str
    email: str | None
    phone: str | None
    source: str
    source_detail: str | None
    service_interest: str | None
    status: str
    score: int | None
    ai_paused: bool
    created_at: datetime
    first_response_seconds: float | None
    last_activity_at: datetime
    last_message: str | None
    appointment_at: datetime | None
    next_follow_up_at: datetime | None


class LeadPage(BaseModel):
    total: int
    leads: list[LeadSummary]


class MessageOut(BaseModel):
    id: int
    direction: str
    author: str
    channel: str
    content: str
    created_at: datetime


class EventOut(BaseModel):
    id: int
    type: str
    detail: dict[str, Any]
    created_at: datetime


class LeadDetail(LeadSummary):
    crm_url: str | None
    """The lead's contact in HubSpot, once synced."""
    telegram_chat_id: str | None
    timezone: str | None
    answers: dict[str, Any]
    consent_text: str | None
    follow_ups_sent: int
    opted_out_at: datetime | None
    messages: list[MessageOut]
    events: list[EventOut]


class DailyCount(BaseModel):
    date: str
    leads: int
    booked: int


class Metrics(BaseModel):
    days: int
    total_leads: int
    by_status: dict[str, int]
    by_source: dict[str, int]
    booked: int
    booking_rate: float | None
    """Booked leads as a share of all leads in the period."""
    responded: int
    median_first_response_seconds: float | None
    p90_first_response_seconds: float | None
    responded_within_target: float | None
    """Share of responded leads answered within ``target_seconds``."""
    target_seconds: int
    needs_attention: int
    """Leads a person has taken over that aren't closed yet."""
    daily: list[DailyCount]


class TeamMessage(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class CampaignUpload(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    consent_confirmed: bool = Field(
        description="The business confirms everyone on the list previously contacted it or "
        "bought from it and may be contacted again."
    )
    consent_basis: str = Field(default="Past enquiry or customer of the business.", max_length=300)
    contacts: list[dict[str, Any]] = Field(max_length=MAX_CONTACTS)


class StatusChange(BaseModel):
    status: Literal["qualified", "nurturing", "booked", "lost"]


_CLOSED = {LeadStatus.BOOKED, LeadStatus.LOST, LeadStatus.OPTED_OUT}


def build_admin_router(settings: Settings) -> APIRouter:
    secret = (
        settings.session_secret.get_secret_value().encode()
        if settings.session_secret
        else secrets.token_bytes(32)
    )
    signer = SessionSigner(secret)
    login_limiter = RateLimiter(5)

    def admin_enabled() -> str:
        if settings.admin_password is None:
            raise HTTPException(404)
        return settings.admin_password.get_secret_value()

    def require_session(
        _: Annotated[str, Depends(admin_enabled)],
        lead_admin: Annotated[str | None, Cookie()] = None,
    ) -> None:
        if not signer.verify(lead_admin):
            raise HTTPException(401, "Please sign in.")

    def sessions(request: Request) -> sessionmaker[Session]:
        factory: sessionmaker[Session] = request.app.state.leads.sessions
        return factory

    def responder(request: Request) -> LeadResponder:
        found: LeadResponder = request.app.state.responder
        return found

    public = APIRouter(prefix="/api/admin", tags=["admin"])
    private = APIRouter(
        prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_session)]
    )

    @public.post("/login", status_code=204)
    def login(
        body: LoginRequest,
        request: Request,
        response: Response,
        expected: Annotated[str, Depends(admin_enabled)],
    ) -> None:
        client = request.client.host if request.client else "unknown"
        if not login_limiter.allow(client):
            raise HTTPException(429, "Too many attempts. Wait a minute and try again.")
        if not password_matches(body.password, expected):
            raise HTTPException(401, "Incorrect password.")
        response.set_cookie(
            COOKIE_NAME,
            signer.issue(),
            max_age=SESSION_SECONDS,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/api/admin",
        )

    @public.post("/logout", status_code=204)
    def logout(response: Response) -> None:
        response.delete_cookie(COOKIE_NAME, path="/api/admin")

    @private.get("/session", status_code=204)
    def session_check() -> None:
        """Returns 204 when signed in, 401 otherwise."""

    @private.get("/metrics")
    def metrics(request: Request, days: Annotated[int, Query(ge=1, le=365)] = 30) -> Metrics:
        """Pipeline and speed-to-lead numbers for the last ``days`` days."""
        since = utcnow() - timedelta(days=days)
        with sessions(request)() as db:
            leads = db.scalars(select(Lead).where(Lead.created_at >= since)).all()
            needs_attention = (
                db.scalar(
                    select(func.count(Lead.id)).where(
                        Lead.ai_paused.is_(True), Lead.status.not_in(_CLOSED)
                    )
                )
                or 0
            )
        response_times = sorted(
            t for t in (_first_response_seconds(lead) for lead in leads) if t is not None
        )
        booked = sum(1 for lead in leads if lead.status == LeadStatus.BOOKED)

        today = utcnow().date()
        daily = {
            (today - timedelta(days=i)).isoformat(): [0, 0]
            for i in range(min(days, 90) - 1, -1, -1)
        }
        for lead in leads:
            key = _aware(lead.created_at).date().isoformat()
            if key in daily:
                daily[key][0] += 1
                daily[key][1] += lead.status == LeadStatus.BOOKED

        return Metrics(
            days=days,
            total_leads=len(leads),
            by_status=dict(Counter(lead.status for lead in leads)),
            by_source=dict(Counter(lead.source for lead in leads)),
            booked=booked,
            booking_rate=booked / len(leads) if leads else None,
            responded=len(response_times),
            median_first_response_seconds=(
                statistics.median(response_times) if response_times else None
            ),
            p90_first_response_seconds=_percentile(response_times, 0.9),
            responded_within_target=(
                sum(1 for t in response_times if t <= SPEED_TARGET_SECONDS) / len(response_times)
                if response_times
                else None
            ),
            target_seconds=SPEED_TARGET_SECONDS,
            needs_attention=needs_attention,
            daily=[DailyCount(date=d, leads=n, booked=b) for d, (n, b) in daily.items()],
        )

    @private.get("/leads")
    def list_leads(
        request: Request,
        status: Annotated[str | None, Query(max_length=16)] = None,
        q: Annotated[str | None, Query(max_length=100)] = None,
        attention: bool = False,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> LeadPage:
        """Leads, most recent activity first. Filter by status, a search term, or
        ``attention`` for conversations a person has taken over."""
        conditions: list[Any] = []
        if status:
            conditions.append(Lead.status == status)
        if attention:
            conditions += [Lead.ai_paused.is_(True), Lead.status.not_in(_CLOSED)]
        if q:
            like = f"%{q.strip().lower()}%"
            conditions.append(
                or_(
                    func.lower(Lead.name).like(like),
                    func.lower(Lead.email).like(like),
                    Lead.phone.like(like),
                )
            )
        with sessions(request)() as db:
            total = db.scalar(select(func.count(Lead.id)).where(*conditions)) or 0
            leads = db.scalars(
                select(Lead)
                .where(*conditions)
                .options(selectinload(Lead.messages))
                .order_by(Lead.updated_at.desc())
                .limit(limit)
                .offset(offset)
            ).all()
            return LeadPage(total=total, leads=[_summary(lead) for lead in leads])

    @private.get("/leads/{lead_id}")
    def lead_detail(lead_id: str, request: Request) -> LeadDetail:
        with sessions(request)() as db:
            return _detail(_get(db, lead_id), settings.hubspot_portal_id)

    @private.post("/leads/{lead_id}/takeover")
    def take_over(lead_id: str, request: Request) -> LeadDetail:
        """Pause the AI so a person can handle the conversation."""
        return _toggle(request, lead_id, paused=True)

    @private.post("/leads/{lead_id}/resume")
    def resume(lead_id: str, request: Request) -> LeadDetail:
        """Hand the conversation back to the AI."""
        return _toggle(request, lead_id, paused=False)

    @private.post("/leads/{lead_id}/messages")
    def send_message(lead_id: str, body: TeamMessage, request: Request) -> LeadDetail:
        """Send a message as a person on the team. This pauses the AI."""
        with sessions(request)() as db:
            _get(db, lead_id)
        try:
            delivered = responder(request).send_from_team(lead_id, body.text.strip())
        except OptedOutError:
            raise HTTPException(
                409, "This lead opted out, so no more messages can be sent."
            ) from None
        if not delivered:
            raise HTTPException(502, "The message was saved but couldn't be delivered.")
        with sessions(request)() as db:
            return _detail(_get(db, lead_id), settings.hubspot_portal_id)

    @private.post("/leads/{lead_id}/status")
    def change_status(lead_id: str, body: StatusChange, request: Request) -> LeadDetail:
        """Correct a lead's status by hand, e.g. mark it lost or booked."""
        with sessions(request).begin() as db:
            lead = _get(db, lead_id)
            if lead.status == LeadStatus.OPTED_OUT:
                raise HTTPException(409, "Opted-out leads can't change status.")
            if lead.status != body.status:
                lead.events.append(
                    LeadEvent(
                        type="lead.status_changed",
                        detail={"from": lead.status, "to": body.status, "by": "team"},
                    )
                )
                lead.status = body.status
                if body.status in _CLOSED:
                    lead.next_follow_up_at = None
        with sessions(request)() as db:
            return _detail(_get(db, lead_id), settings.hubspot_portal_id)

    @private.get("/campaigns")
    def campaigns(request: Request) -> list[CampaignStats]:
        """Reactivation campaigns and how each is doing."""
        return ReactivationService(sessions(request)).stats()

    @private.post("/campaigns", status_code=201)
    def start_campaign(body: CampaignUpload, request: Request) -> ImportResult:
        """Import past leads or customers to re-contact. The agent sends each an opener
        through the follow-up scheduler, outside quiet hours."""
        if not body.consent_confirmed:
            raise HTTPException(
                400, "Confirm that everyone on the list previously contacted or bought from you."
            )
        return ReactivationService(sessions(request)).import_contacts(
            body.name, body.contacts, body.consent_basis.strip()
        )

    def _toggle(request: Request, lead_id: str, *, paused: bool) -> LeadDetail:
        with sessions(request)() as db:
            _get(db, lead_id)
        responder(request).set_paused(lead_id, paused)
        with sessions(request)() as db:
            return _detail(_get(db, lead_id), settings.hubspot_portal_id)

    router = APIRouter()
    router.include_router(public)
    router.include_router(private)
    return router


def _get(db: Session, lead_id: str) -> Lead:
    lead = db.get(Lead, lead_id, options=[selectinload(Lead.messages), selectinload(Lead.events)])
    if lead is None:
        raise HTTPException(404, "Lead not found.")
    return lead


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _maybe(value: datetime | None) -> datetime | None:
    return _aware(value) if value is not None else None


def _first_response_seconds(lead: Lead) -> float | None:
    if lead.first_response_at is None:
        return None
    return max(0.0, (_aware(lead.first_response_at) - _aware(lead.created_at)).total_seconds())


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    index = min(len(values) - 1, round(fraction * (len(values) - 1)))
    return values[index]


def _summary(lead: Lead) -> LeadSummary:
    times = [t for t in (lead.created_at, lead.last_inbound_at, lead.last_outbound_at) if t]
    last = lead.messages[-1].content if lead.messages else None
    return LeadSummary(
        id=lead.id,
        name=lead.name,
        email=lead.email,
        phone=lead.phone,
        source=lead.source,
        source_detail=lead.source_detail,
        service_interest=lead.service_interest,
        status=lead.status,
        score=lead.score,
        ai_paused=lead.ai_paused,
        created_at=_aware(lead.created_at),
        first_response_seconds=_first_response_seconds(lead),
        last_activity_at=max(_aware(t) for t in times),
        last_message=last[:160] if last else None,
        appointment_at=_maybe(lead.appointment_at),
        next_follow_up_at=_maybe(lead.next_follow_up_at),
    )


def _detail(lead: Lead, portal_id: str | None = None) -> LeadDetail:
    crm_url = (
        f"https://app.hubspot.com/contacts/{portal_id}/record/0-1/{lead.crm_contact_id}"
        if portal_id and lead.crm_contact_id
        else None
    )
    return LeadDetail(
        **_summary(lead).model_dump(),
        crm_url=crm_url,
        telegram_chat_id=lead.telegram_chat_id,
        timezone=lead.timezone,
        answers=lead.answers,
        consent_text=lead.consent_text,
        follow_ups_sent=lead.follow_ups_sent,
        opted_out_at=_maybe(lead.opted_out_at),
        messages=[_message(m) for m in lead.messages],
        events=[
            EventOut(id=e.id, type=e.type, detail=e.detail, created_at=_aware(e.created_at))
            for e in lead.events
        ],
    )


def _message(message: LeadMessage) -> MessageOut:
    author = message.author or ("lead" if message.direction == Direction.INBOUND else "agent")
    return MessageOut(
        id=message.id,
        direction=message.direction,
        author=author,
        channel=message.channel,
        content=message.content,
        created_at=_aware(message.created_at),
    )
