import random

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from lead_agent.db import Lead, LeadStatus, utcnow
from lead_agent.demo.seed import seed


def test_demo_seed_creates_a_realistic_pipeline(sessions: sessionmaker[Session]) -> None:
    with sessions.begin() as session:
        seed(session, 40, 30, random.Random(1))  # noqa: S311 - demo data

    with sessions() as session:
        leads = session.scalars(select(Lead)).all()
        assert len(leads) == 40
        assert {lead.status for lead in leads} >= {LeadStatus.BOOKED, LeadStatus.QUALIFYING}
        now = utcnow().replace(tzinfo=None)
        for lead in leads:
            assert lead.consent
            assert lead.messages
            assert all(m.created_at.replace(tzinfo=None) <= now for m in lead.messages)
