"""FastAPI application: lead intake from the website form and from webhooks."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ValidationError

from lead_agent import __version__
from lead_agent.agent import QualifyingAgent
from lead_agent.api.ratelimit import RateLimiter
from lead_agent.channels import Channel
from lead_agent.channels.telegram import TelegramBot, TelegramChannel, TelegramClient
from lead_agent.config import Settings, fallback_models, get_settings
from lead_agent.db import LeadSource, make_engine, make_session_factory
from lead_agent.leads import IntakeResult, LeadIntake, LeadService
from lead_agent.llm import OpenAICompatibleModel
from lead_agent.profile import load_profile
from lead_agent.responder import LeadResponder

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Signature-256"
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


@dataclass(slots=True)
class Services:
    leads: LeadService
    responder: LeadResponder
    telegram: TelegramBot | None = None
    telegram_client: TelegramClient | None = None


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
    responder = LeadResponder(sessions, QualifyingAgent(model, profile), profile, channels)
    services = Services(leads, responder, telegram_client=telegram_client)
    if telegram_client is not None:
        services.telegram = TelegramBot(
            telegram_client,
            leads,
            responder,
            rate_limit_per_minute=settings.telegram_rate_limit_per_minute,
        )
    return services


def create_app(
    settings: Settings | None = None,
    service: LeadService | None = None,
    responder: LeadResponder | None = None,
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

        stop = threading.Event()
        poller = None
        if built is not None and built.telegram is not None:
            poller = threading.Thread(
                target=built.telegram.run_polling, args=(stop,), name="telegram", daemon=True
            )
            poller.start()
        try:
            yield
        finally:
            stop.set()
            if poller is not None:
                poller.join(timeout=30)
            app.state.responder.shutdown()
            if built is not None and built.telegram_client is not None:
                built.telegram_client.close()

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

    return app


def sign(body: bytes, secret: str) -> str:
    """The signature header value for a webhook body."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _valid_signature(body: bytes, header: str | None, secret: str) -> bool:
    if not header:
        return False
    return hmac.compare_digest(header.strip(), sign(body, secret))
