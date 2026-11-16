from pathlib import Path
from typing import Any

import pytest

from lead_agent.profile import BusinessProfile, load_profile
from lead_agent.scoring import Outcome, assess, clean_answer


@pytest.fixture(scope="module")
def profile() -> BusinessProfile:
    return load_profile()


GOOD: dict[str, Any] = {
    "service": "solar installation",
    "homeowner": True,
    "zip_code": "80202",
    "timeline": "1 to 3 months",
}


def test_demo_profile_loads(profile: BusinessProfile) -> None:
    assert profile.name == "Brightline Solar & Roofing"
    assert [q.key for q in profile.questions] == ["service", "homeowner", "zip_code", "timeline"]


def test_complete_strong_lead_is_qualified(profile: BusinessProfile) -> None:
    result = assess(profile, GOOD)
    assert result.outcome is Outcome.QUALIFIED
    assert result.score == 90
    assert result.missing == []


def test_missing_answers_keep_qualifying_in_question_order(profile: BusinessProfile) -> None:
    result = assess(profile, {"service": "roof repair"})
    assert result.outcome is Outcome.QUALIFYING
    assert [q.key for q in result.missing] == ["homeowner", "zip_code", "timeline"]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"homeowner": False}, "property owners"),
        ({"zip_code": "10001"}, "service area"),
    ],
)
def test_disqualifiers_end_qualification(
    profile: BusinessProfile, change: dict[str, Any], reason: str
) -> None:
    result = assess(profile, {**GOOD, **change})
    assert result.outcome is Outcome.NOT_A_FIT
    assert reason in result.reasons[0]


def test_disqualifier_applies_even_before_other_answers(profile: BusinessProfile) -> None:
    assert assess(profile, {"homeowner": False}).outcome is Outcome.NOT_A_FIT


def test_complete_but_weak_lead_is_nurtured(profile: BusinessProfile) -> None:
    result = assess(profile, {**GOOD, "timeline": "just researching"})
    assert result.outcome is Outcome.NURTURE
    assert result.score == 60


@pytest.mark.parametrize(
    ("key", "raw", "expected"),
    [
        ("service", "Solar Installation", "solar installation"),
        ("service", "pool cleaning", None),
        ("homeowner", True, True),
        ("homeowner", "yes", None),
        ("zip_code", " 80202-1234 ", "80202"),
        ("zip_code", "802", None),
    ],
)
def test_answers_are_normalized_or_rejected(
    profile: BusinessProfile, key: str, raw: Any, expected: Any
) -> None:
    question = profile.question(key)
    assert question is not None
    assert clean_answer(question, raw) == expected


def test_invalid_profiles_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text(
        'name = "X"\ndescription = "d"\nservice_area = "a"\n'
        '[[questions]]\nkey = "service"\nask = "?"\ntype = "choice"\n'
    )
    with pytest.raises(ValueError, match="without choices"):
        load_profile(path)
