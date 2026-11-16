"""FastAPI application: lead intake from the website form and from webhooks."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ValidationError

from lead_agent import __version__
from lead_agent.api.ratelimit import RateLimiter
from lead_agent.config import Settings, get_settings
from lead_agent.db import LeadSource, make_engine, make_session_factory
from lead_agent.leads import LeadIntake, LeadService

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


def build_lead_service(settings: Settings) -> LeadService:
    sessions = make_session_factory(make_engine(settings.database_url))
    return LeadService(
        sessions,
        consent_text=settings.consent_text,
        duplicate_window=timedelta(hours=settings.duplicate_window_hours),
    )


def create_app(settings: Settings | None = None, service: LeadService | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.leads = service or build_lead_service(settings)
        yield

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
        leads(request).receive(intake, LeadSource.FORM)
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
        leads(request).receive(intake, LeadSource.WEBHOOK, payload.source_detail)
        return Accepted()

    return app


def sign(body: bytes, secret: str) -> str:
    """The signature header value for a webhook body."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _valid_signature(body: bytes, header: str | None, secret: str) -> bool:
    if not header:
        return False
    return hmac.compare_digest(header.strip(), sign(body, secret))
