"""FastAPI application: lead intake from the website form and from webhooks."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr, ValidationError

from lead_agent import __version__
from lead_agent.agent import QualifyingAgent
from lead_agent.api.admin import build_admin_router
from lead_agent.api.ratelimit import RateLimiter
from lead_agent.booking import BookingService, CalcomWebhook
from lead_agent.channels import Channel
from lead_agent.channels.email import (
    EmailChannel,
    EmailInbox,
    ImapConfig,
    ImapMailbox,
    SmtpConfig,
    SmtpSender,
)
from lead_agent.channels.sms import SmsChannel, TwilioClient, to_e164, valid_twilio_signature
from lead_agent.channels.telegram import TelegramBot, TelegramChannel, TelegramClient
from lead_agent.config import Settings, fallback_models, get_settings
from lead_agent.crm.hubspot import HubSpotClient, HubSpotSync
from lead_agent.db import LeadSource, make_engine, make_session_factory
from lead_agent.followups import FollowUpScheduler
from lead_agent.leads import IntakeResult, LeadIntake, LeadService
from lead_agent.llm import OpenAICompatibleModel
from lead_agent.profile import load_profile
from lead_agent.responder import LeadResponder

logger = logging.getLogger(__name__)

DEFAULT_FRONTEND_DIR = Path(str(resources.files("lead_agent").joinpath("web", "dist")))
SIGNATURE_HEADER = "X-Signature-256"
CALCOM_SIGNATURE_HEADER = "X-Cal-Signature-256"
TWILIO_SIGNATURE_HEADER = "X-Twilio-Signature"
MAX_WEBHOOK_BYTES = 64 * 1024


class FormSubmission(LeadIntake):
    website: str | None = Field(
        default=None,
        description="Spam trap: hidden from people, so only bots fill it in. Leave empty.",
    )


class WebhookLead(LeadIntake):
    source_detail: str | None = Field(
        default=None, max_length=64, description='Where the lead came from, e.g. "facebook".'
    )


class Accepted(BaseModel):
    status: str = "received"


Worker = Callable[[threading.Event], None]
"""A background loop that runs until the event is set."""


@dataclass(slots=True)
class Services:
    leads: LeadService
    responder: LeadResponder
    workers: dict[str, Worker] = field(default_factory=dict)
    cleanup: list[Callable[[], None]] = field(default_factory=list)


def build_services(settings: Settings) -> Services:
    sessions = make_session_factory(make_engine(settings.database_url))
    leads = LeadService(
        sessions,
        consent_text=settings.consent_text,
        duplicate_window=timedelta(hours=settings.duplicate_window_hours),
    )
    profile = load_profile(settings.business_profile)
    model = OpenAICompatibleModel(
        settings.llm_model,
        settings.llm_base_url,
        settings.llm_api_key.get_secret_value() if settings.llm_api_key else None,
        reasoning_effort=settings.llm_reasoning_effort or None,
        fallback_models=fallback_models(settings),
        timeout=settings.llm_timeout_seconds,
    )

    channels: list[Channel] = []
    telegram_client = None
    if settings.telegram_bot_token is not None:
        telegram_client = TelegramClient(settings.telegram_bot_token.get_secret_value())
        channels.append(TelegramChannel(telegram_client))
    twilio_client = None
    if settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_from_number:
        twilio_client = TwilioClient(
            settings.twilio_account_sid,
            settings.twilio_auth_token.get_secret_value(),
            settings.twilio_from_number,
        )
        channels.append(
            SmsChannel(twilio_client, default_country_code=settings.sms_default_country_code)
        )
    if settings.smtp_host and settings.email_from:
        channels.append(
            EmailChannel(
                SmtpSender(
                    SmtpConfig(
                        settings.smtp_host,
                        settings.smtp_port,
                        settings.smtp_username,
                        _secret(settings.smtp_password),
                        settings.smtp_security,
                    )
                ),
                from_address=settings.email_from,
                from_name=f"{profile.agent_name} at {profile.name}",
                business_name=profile.name,
            )
        )

    responder = LeadResponder(sessions, QualifyingAgent(model, profile), profile, channels)
    services = Services(leads, responder)
    if twilio_client is not None:
        services.cleanup.append(twilio_client.close)

    if profile.follow_ups.delays_hours:
        scheduler = FollowUpScheduler(
            sessions, responder.follow_up, profile.follow_ups, profile.timezone
        )
        services.workers["follow-ups"] = scheduler.run_polling

    if telegram_client is not None:
        bot = TelegramBot(
            telegram_client,
            leads,
            responder,
            rate_limit_per_minute=settings.telegram_rate_limit_per_minute,
        )
        services.workers["telegram"] = bot.run_polling
        services.cleanup.append(telegram_client.close)

    if settings.hubspot_access_token is not None:
        hubspot = HubSpotClient(settings.hubspot_access_token.get_secret_value())
        crm = HubSpotSync(sessions, hubspot, dashboard_url=settings.public_url)
        crm_interval = settings.hubspot_sync_seconds
        services.workers["hubspot"] = lambda stop: crm.run_polling(stop, crm_interval)
        services.cleanup.append(hubspot.close)

    if settings.imap_host:
        mailbox = ImapMailbox(
            ImapConfig(
                settings.imap_host,
                settings.imap_port,
                settings.imap_username or settings.smtp_username or "",
                _secret(settings.imap_password) or _secret(settings.smtp_password) or "",
                settings.imap_folder,
            )
        )
        inbox = EmailInbox(
            mailbox,
            leads.email_lead,
            lambda lead_id, text: responder.handle_inbound(lead_id, text, "email"),
        )
        interval = settings.imap_poll_seconds
        services.workers["email"] = lambda stop: inbox.run_polling(stop, interval)
        services.cleanup.append(mailbox.close)
    return services


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None


def create_app(
    settings: Settings | None = None,
    service: LeadService | None = None,
    responder: LeadResponder | None = None,
    bookings: BookingService | None = None,
    frontend_dir: Path = DEFAULT_FRONTEND_DIR,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        built: Services | None = None
        if service is not None and responder is not None:
            app.state.leads, app.state.responder = service, responder
        else:
            built = build_services(settings)
            app.state.leads = service or built.leads
            app.state.responder = responder or built.responder
        app.state.bookings = bookings or BookingService(
            app.state.leads.sessions, load_profile(settings.business_profile).timezone
        )

        stop = threading.Event()
        threads = [
            threading.Thread(target=worker, args=(stop,), name=name, daemon=True)
            for name, worker in (built.workers.items() if built else [])
        ]
        for thread in threads:
            thread.start()
        try:
            yield
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=30)
            app.state.responder.shutdown()
            for close in built.cleanup if built else []:
                close()

    app = FastAPI(
        title="ai-lead-agent",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    if settings.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.allowed_origins,
            allow_methods=["POST"],
            allow_headers=["Content-Type"],
        )

    form_limiter = RateLimiter(settings.form_rate_limit_per_minute)
    app.include_router(build_admin_router(settings))

    def leads(request: Request) -> LeadService:
        svc: LeadService = request.app.state.leads
        return svc

    def respond(request: Request, result: IntakeResult) -> None:
        responder: LeadResponder = request.app.state.responder
        if result.created:
            responder.schedule_first_reply(result.lead_id)
        else:
            responder.schedule_reply(result.lead_id)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/form-config", tags=["intake"])
    def form_config() -> dict[str, str]:
        """Text the embeddable form shows, such as the consent wording."""
        return {"business_name": settings.business_name, "consent_text": settings.consent_text}

    @app.post("/api/leads", status_code=202, tags=["intake"])
    def submit_form(body: FormSubmission, request: Request) -> Accepted:
        """Receive a lead from the website form."""
        client = request.client.host if request.client else "unknown"
        if not form_limiter.allow(client):
            raise HTTPException(429, "Too many submissions. Please wait a minute and try again.")
        if body.website:
            # Looks like a bot. Answer normally so it learns nothing, but drop it.
            logger.info("Dropped a form submission that filled the spam trap")
            return Accepted()
        intake = LeadIntake.model_validate(body.model_dump(exclude={"website"}))
        respond(request, leads(request).receive(intake, LeadSource.FORM))
        return Accepted()

    @app.post("/api/webhooks/leads", status_code=202, tags=["intake"])
    async def receive_webhook(request: Request) -> Accepted:
        """Receive a lead from another system (ads, form builders, Zapier and so on).

        The request body must be signed: send ``X-Signature-256: sha256=<hex>``, the
        HMAC-SHA256 of the raw body using ``WEBHOOK_SECRET``.
        """
        if settings.webhook_secret is None:
            raise HTTPException(404)
        body = await request.body()
        if len(body) > MAX_WEBHOOK_BYTES:
            raise HTTPException(413, "Payload too large.")
        if not _valid_signature(
            body, request.headers.get(SIGNATURE_HEADER), settings.webhook_secret.get_secret_value()
        ):
            raise HTTPException(401, "Invalid or missing signature.")
        try:
            payload = WebhookLead.model_validate(json.loads(body))
        except (ValueError, ValidationError) as exc:
            raise HTTPException(422, f"Invalid lead: {exc}") from None
        intake = LeadIntake.model_validate(payload.model_dump(exclude={"source_detail"}))
        result = leads(request).receive(intake, LeadSource.WEBHOOK, payload.source_detail)
        respond(request, result)
        return Accepted()

    sms_limiter = RateLimiter(settings.sms_rate_limit_per_minute)

    @app.post("/api/webhooks/sms", tags=["intake"], include_in_schema=True)
    async def receive_sms(request: Request) -> Response:
        """Texts from leads, sent by Twilio. Checked with ``X-Twilio-Signature``.

        A text from an unknown number starts a new lead; a text from a known
        lead continues their conversation. Replies are sent in the background.
        """
        if settings.twilio_auth_token is None:
            raise HTTPException(404)
        body = await request.body()
        if len(body) > MAX_WEBHOOK_BYTES:
            raise HTTPException(413, "Payload too large.")
        params = dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))
        url = (
            f"{settings.public_url.rstrip('/')}{request.url.path}"
            if settings.public_url
            else str(request.url)
        )
        if not valid_twilio_signature(
            url,
            params,
            request.headers.get(TWILIO_SIGNATURE_HEADER),
            settings.twilio_auth_token.get_secret_value(),
        ):
            raise HTTPException(403, "Invalid Twilio signature.")

        sender = to_e164(params.get("From", ""), settings.sms_default_country_code)
        text = params.get("Body", "").strip()[:2000]
        if sender and text and sms_limiter.allow(sender):
            leads_service: LeadService = request.app.state.leads
            responder_service: LeadResponder = request.app.state.responder
            lead = leads_service.sms_lead(sender)
            if lead is None:
                responder_service.schedule_first_reply(leads_service.start_sms_lead(sender, text))
            else:
                responder_service.schedule_inbound(lead.id, text, "sms")
        # An empty TwiML response: replies are sent separately through the API.
        return Response("<Response></Response>", media_type="text/xml")

    @app.post("/api/webhooks/calcom", tags=["booking"])
    async def receive_booking(request: Request) -> Accepted:
        """Booking events from Cal.com (created, rescheduled, cancelled).

        In Cal.com, add a webhook pointing here with the same secret as
        ``CALCOM_WEBHOOK_SECRET``. Cal.com signs the body in ``X-Cal-Signature-256``.
        """
        if settings.calcom_webhook_secret is None:
            raise HTTPException(404)
        body = await request.body()
        if len(body) > MAX_WEBHOOK_BYTES:
            raise HTTPException(413, "Payload too large.")
        secret = settings.calcom_webhook_secret.get_secret_value()
        signature = request.headers.get(CALCOM_SIGNATURE_HEADER, "")
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature.strip().removeprefix("sha256="), expected):
            raise HTTPException(401, "Invalid or missing signature.")
        try:
            webhook = CalcomWebhook.model_validate(json.loads(body))
        except (ValueError, ValidationError) as exc:
            raise HTTPException(422, f"Invalid webhook: {exc}") from None
        bookings: BookingService = request.app.state.bookings
        update = bookings.apply(webhook)
        if update is not None and update.confirmation:
            responder: LeadResponder = request.app.state.responder
            responder.schedule_notify(update.lead_id, update.confirmation)
        return Accepted()

    _mount_frontend(app, frontend_dir)
    return app


def _mount_frontend(app: FastAPI, frontend_dir: Path) -> None:
    """Serve the built React app (landing page and dashboard), with client-side routing."""
    index = frontend_dir / "index.html"
    if (frontend_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=frontend_dir / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> Response:
        if path.startswith("api/"):
            raise HTTPException(404)
        candidate = (frontend_dir / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(frontend_dir.resolve()):
            return FileResponse(candidate)
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return PlainTextResponse(
            "Frontend not built. Run: npm --prefix frontend ci && npm --prefix frontend run build",
            404,
        )


def sign(body: bytes, secret: str) -> str:
    """The signature header value for a webhook body."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _valid_signature(body: bytes, header: str | None, secret: str) -> bool:
    if not header:
        return False
    return hmac.compare_digest(header.strip(), sign(body, secret))
