"""Deterministic lead scoring and routing.

The language model only extracts answers. Whether a lead is qualified, a poor
fit or still being qualified is decided here, by rules from the business
profile, so the outcome is predictable, explainable and testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from lead_agent.profile import BusinessProfile, Question


class Outcome(StrEnum):
    QUALIFYING = "qualifying"
    """Still missing required answers."""
    QUALIFIED = "qualified"
    NURTURE = "nurture"
    """All answers in, but the score is below the bar (e.g. just researching)."""
    NOT_A_FIT = "not_a_fit"


@dataclass(frozen=True, slots=True)
class Assessment:
    outcome: Outcome
    score: int
    missing: list[Question] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    """Why the lead scored as it did, for the dashboard and audit trail."""


def clean_answer(question: Question, value: Any) -> Any:
    """Return a normalized answer, or None if it doesn't fit the question."""
    if question.type == "boolean":
        return value if isinstance(value, bool) else None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if question.type == "choice":
        lowered = text.lower()
        return next((c for c in question.choices if c.lower() == lowered), None)
    if question.type == "zip":
        digits = re.sub(r"\D", "", text)
        return digits[:5] if len(digits) >= 5 else None
    return text[:200]


def assess(profile: BusinessProfile, answers: dict[str, Any]) -> Assessment:
    score = 0
    reasons: list[str] = []
    missing: list[Question] = []

    for question in profile.questions:
        value = answers.get(question.key)
        if value is None:
            if question.required:
                missing.append(question)
            continue

        if question.disqualify_if is not None and value == question.disqualify_if:
            return Assessment(Outcome.NOT_A_FIT, 0, reasons=[question.disqualify_reason])
        if (
            question.type == "zip"
            and question.allowed_prefixes
            and not str(value).startswith(tuple(question.allowed_prefixes))
        ):
            return Assessment(Outcome.NOT_A_FIT, 0, reasons=[question.disqualify_reason])

        points = (
            question.points_by_choice.get(str(value), 0)
            if question.points_by_choice
            else question.points
        )
        score += points
        reasons.append(f"{question.key}={value} (+{points})")

    score = min(score, 100)
    if missing:
        return Assessment(Outcome.QUALIFYING, score, missing=missing, reasons=reasons)
    if score >= profile.qualify_score:
        return Assessment(Outcome.QUALIFIED, score, reasons=reasons)
    return Assessment(Outcome.NURTURE, score, reasons=reasons)
