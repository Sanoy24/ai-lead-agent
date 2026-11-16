"""Application settings, loaded from environment variables or a ``.env`` file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONSENT_TEXT = (
    "By submitting this form you agree that we may contact you by email or message "
    "about your enquiry. You can opt out at any time by replying STOP."
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Business
    business_name: str = "Brightline Solar & Roofing"
    business_profile: Path | None = Field(
        default=None,
        description="TOML file with the business, its questions and scoring. "
        "Defaults to the bundled demo business.",
    )
    consent_text: str = Field(
        default=DEFAULT_CONSENT_TEXT,
        description="Shown next to the form's consent checkbox and stored with each lead.",
    )

    # Language model (any OpenAI-compatible API; defaults to a local Ollama server)
    llm_base_url: str = "http://localhost:11434/v1"
    llm_model: str = "granite4.2:8b"
    llm_api_key: SecretStr | None = None
    llm_reasoning_effort: str | None = "none"
    llm_fallback_models: str = ""
    llm_timeout_seconds: float = 120.0

    # Intake
    webhook_secret: SecretStr | None = Field(
        default=None,
        description="Shared secret for signing webhook requests. Enables /api/webhooks/leads.",
    )
    duplicate_window_hours: int = Field(
        default=24,
        description="A repeat submission from the same email or phone within this window "
        "is added to the existing lead instead of creating a new one.",
    )
    form_rate_limit_per_minute: int = 5

    # App
    database_url: str = "sqlite:///data/local/leads.db"
    allowed_origins: list[str] = Field(
        default_factory=list, description="Websites allowed to embed the lead form."
    )


def fallback_models(settings: Settings) -> list[str]:
    return [m.strip() for m in settings.llm_fallback_models.split(",") if m.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
