"""The business profile: who the agent works for, what it asks, and how leads are scored."""

from __future__ import annotations

import tomllib
from importlib import resources
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field, model_validator

from lead_agent.followups import FollowUpPolicy


class Question(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    ask: str
    type: Literal["choice", "boolean", "zip", "text"]
    choices: list[str] = Field(default_factory=list)
    required: bool = False
    points: int = 0
    """Points for any valid answer."""
    points_by_choice: dict[str, int] = Field(default_factory=dict)
    """Points per choice, used instead of ``points`` for choice questions."""
    disqualify_if: bool | str | None = None
    """An answer that makes the lead a poor fit (e.g. ``false`` for "owns the property")."""
    allowed_prefixes: list[str] = Field(default_factory=list)
    """For ZIP codes: prefixes inside the service area. Others make the lead a poor fit."""
    disqualify_reason: str = "This isn't a fit for the services we offer."

    @model_validator(mode="after")
    def _check(self) -> Question:
        if self.type == "choice" and not self.choices:
            raise ValueError(f"Question '{self.key}' is a choice question without choices.")
        unknown = set(self.points_by_choice) - set(self.choices)
        if unknown:
            raise ValueError(f"Question '{self.key}' scores unknown choices: {sorted(unknown)}")
        return self

    def json_schema(self) -> dict[str, Any]:
        """Schema for this answer in the agent's record_answers tool."""
        description = self.ask
        if self.type == "choice":
            return {"type": "string", "enum": self.choices, "description": description}
        if self.type == "boolean":
            return {"type": "boolean", "description": description}
        if self.type == "zip":
            return {"type": "string", "description": f"{description} Digits only."}
        return {"type": "string", "description": description}


class BusinessProfile(BaseModel):
    name: str
    agent_name: str = "Alex"
    description: str
    service_area: str
    timezone: str = "UTC"
    booking_url: str | None = None
    rules: list[str] = Field(default_factory=list)
    qualify_score: int = Field(default=70, ge=0, le=100)
    questions: list[Question]
    follow_ups: FollowUpPolicy = Field(default_factory=FollowUpPolicy)

    @model_validator(mode="after")
    def _unique_keys(self) -> BusinessProfile:
        keys = [q.key for q in self.questions]
        if len(keys) != len(set(keys)):
            raise ValueError("Question keys must be unique.")
        return self

    def booking_link(self, lead_id: str, name: str, email: str | None) -> str | None:
        """The booking URL, prefilled with the lead's details.

        Cal.com prefills ``name`` and ``email`` and passes ``metadata[...]`` back
        in its webhooks, which is how a booking is matched to the lead.
        """
        if not self.booking_url:
            return None
        parts = urlsplit(self.booking_url)
        query = [*parse_qsl(parts.query), ("name", name), ("metadata[lead_id]", lead_id)]
        if email:
            query.append(("email", email))
        return urlunsplit(parts._replace(query=urlencode(query)))

    def question(self, key: str) -> Question | None:
        return next((q for q in self.questions if q.key == key), None)


def load_profile(path: Path | None = None) -> BusinessProfile:
    """Load a profile from a TOML file, or the bundled demo business."""
    if path is None:
        raw = resources.files("lead_agent.demo").joinpath("business.toml").read_text("utf-8")
    else:
        raw = path.read_text("utf-8")
    return BusinessProfile.model_validate(tomllib.loads(raw))
