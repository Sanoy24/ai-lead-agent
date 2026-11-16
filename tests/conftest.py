from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from lead_agent.config import Settings
from lead_agent.db import make_engine, make_session_factory
from lead_agent.leads import LeadService

CONSENT = "You may contact me about my enquiry."


@pytest.fixture(autouse=True)
def isolate_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's .env file and environment variables out of the tests."""
    monkeypatch.chdir(tmp_path)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def sessions() -> sessionmaker[Session]:
    return make_session_factory(make_engine("sqlite://"))


@pytest.fixture
def service(sessions: sessionmaker[Session]) -> LeadService:
    return LeadService(sessions, consent_text=CONSENT)
