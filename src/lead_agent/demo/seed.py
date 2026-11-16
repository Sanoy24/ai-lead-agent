"""Fill the database with realistic demo leads: ``python -m lead_agent.demo.seed``.

Creates a few weeks of fictional leads for the demo business, at every stage of
the pipeline, so the dashboard has something to show. Refuses to run on a
database that already has leads unless ``--force`` is given.
"""

from __future__ import annotations

import argparse
import random
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lead_agent.config import get_settings
from lead_agent.db import (
    Direction,
    Lead,
    LeadEvent,
    LeadMessage,
    LeadSource,
    LeadStatus,
    make_engine,
    make_session_factory,
    utcnow,
)
from lead_agent.profile import load_profile

CONSENT = (
    "By submitting this form you agree that we may contact you by email or message "
    "about your enquiry. You can opt out at any time by replying STOP."
)

FIRST = [
    "Dana",
    "Sam",
    "Priya",
    "Marcus",
    "Elena",
    "Tom",
    "Aisha",
    "Luis",
    "Grace",
    "Ben",
    "Sofia",
    "Omar",
    "Hannah",
    "Jake",
    "Mei",
    "Carlos",
    "Nina",
    "Ryan",
    "Zoe",
    "Ethan",
]
LAST = [
    "Lopez",
    "Carter",
    "Patel",
    "Reed",
    "Novak",
    "Brooks",
    "Khan",
    "Garcia",
    "Kim",
    "Shaw",
    "Rossi",
    "Haddad",
    "Meyer",
    "Foster",
    "Chen",
    "Ruiz",
    "Berg",
    "Walsh",
    "Ortiz",
    "Lane",
]

SCRIPTS: list[dict[str, Any]] = [
    {
        "status": LeadStatus.BOOKED,
        "service": "solar installation",
        "answers": {
            "service": "solar installation",
            "homeowner": True,
            "zip_code": "80220",
            "timeline": "1 to 3 months",
        },
        "score": 90,
        "chat": [
            ("lead", "Hi, thinking about solar panels for our house. Electric bills are crazy."),
            (
                "agent",
                "Hi {first}, thanks for reaching out! Solar is a great way to take the "
                "edge off those bills. Do you own the home?",
            ),
            ("lead", "Yes we do, bought it 3 years ago"),
            ("agent", "Great. What's the ZIP code of the property?"),
            ("lead", "80220"),
            ("agent", "Thanks! And when are you hoping to get this done?"),
            ("lead", "Next couple of months ideally"),
            (
                "agent",
                "Perfect, that works well for us. A specialist will walk you through "
                "panels, savings and options at your free assessment.\n\nYou can pick a "
                "time for your free assessment here: {link}",
            ),
        ],
    },
    {
        "status": LeadStatus.QUALIFIED,
        "service": "roof replacement",
        "answers": {
            "service": "roof replacement",
            "homeowner": True,
            "zip_code": "80012",
            "timeline": "as soon as possible",
        },
        "score": 100,
        "chat": [
            ("lead", "Our roof is 25 years old and the hail last week finished it off."),
            (
                "agent",
                "Sorry to hear about the hail damage, {first}. We can definitely help "
                "with a replacement. Do you own the property?",
            ),
            ("lead", "Yes"),
            ("agent", "Thanks. What's the ZIP code there?"),
            ("lead", "80012. How much does a new roof cost roughly?"),
            (
                "agent",
                "It depends on the size, pitch and materials, so a specialist will give "
                "you an exact quote at the free assessment. When would you like the work "
                "done?",
            ),
            ("lead", "ASAP honestly"),
            (
                "agent",
                "Understood, we'll prioritise it.\n\nYou can pick a time for your free "
                "assessment here: {link}",
            ),
        ],
    },
    {
        "status": LeadStatus.QUALIFYING,
        "service": "roof repair",
        "answers": {"service": "roof repair", "homeowner": True},
        "score": 40,
        "chat": [
            ("lead", "Few shingles came off in the wind, there's a small leak in the attic."),
            (
                "agent",
                "Thanks for letting us know, {first}. A small leak is worth fixing "
                "before it spreads. Do you own the home?",
            ),
            ("lead", "I do"),
            ("agent", "Great. What's the ZIP code of the property?"),
        ],
    },
    {
        "status": LeadStatus.NURTURING,
        "service": "solar installation",
        "answers": {
            "service": "solar installation",
            "homeowner": True,
            "zip_code": "80302",
            "timeline": "just researching",
        },
        "score": 60,
        "chat": [
            ("lead", "Just starting to look into solar, not in a rush."),
            ("agent", "No rush at all, {first}. Happy to help you research. Do you own the home?"),
            ("lead", "yes, in Boulder, 80302"),
            ("agent", "Thanks! When are you thinking of doing this?"),
            ("lead", "Just researching for now"),
            (
                "agent",
                "That makes sense. Our free assessment is a no-pressure way to see what "
                "your roof could produce.\n\nWhenever you're ready, you can book a free "
                "assessment here: {link}",
            ),
        ],
    },
    {
        "status": LeadStatus.LOST,
        "service": "solar installation",
        "answers": {"service": "solar installation", "homeowner": False},
        "score": 20,
        "chat": [
            ("lead", "Can I get solar on the apartment I rent?"),
            (
                "agent",
                "Thanks so much for reaching out, {first}. We can only work with property "
                "owners. So unfortunately we're not the right fit for this one, but I "
                "wish you the best with your project!",
            ),
        ],
    },
    {
        "status": LeadStatus.QUALIFYING,
        "service": "roof repair",
        "answers": {"service": "roof repair"},
        "score": 20,
        "paused": True,
        "chat": [
            ("lead", "Water is coming through the ceiling right now, please call me!"),
            (
                "agent",
                "I'm so sorry, {first}, that sounds stressful. I've asked a member of our "
                "team to call you right away.",
            ),
            ("team", "Hi {first}, this is Maria from Brightline. I'm calling you in 5 minutes."),
        ],
    },
    {
        "status": LeadStatus.OPTED_OUT,
        "service": "roof replacement",
        "answers": {},
        "score": None,
        "chat": [
            ("lead", "Looking for a quote on a new roof"),
            ("agent", "Hi {first}, thanks for reaching out! Do you own the property?"),
            ("lead", "STOP"),
            ("system", "You're unsubscribed and won't hear from us again. Take care!"),
        ],
    },
]

WEIGHTS = [3, 2, 3, 2, 2, 1, 1]


def seed(session: Session, count: int, days: int, rng: random.Random) -> int:
    profile = load_profile(get_settings().business_profile)
    now = utcnow()
    for i in range(count):
        script = rng.choices(SCRIPTS, WEIGHTS)[0]
        first, last = rng.choice(FIRST), rng.choice(LAST)
        created = now - timedelta(days=rng.uniform(0, days), minutes=rng.uniform(0, 600))
        if i < 3:  # A few fresh ones for the top of the list.
            created = now - timedelta(minutes=rng.uniform(5, 90))
        reply_seconds = rng.choice([4, 6, 8, 11, 15, 22, 38, 55, 70, 140])
        source = rng.choices([LeadSource.FORM, LeadSource.WEBHOOK, LeadSource.TELEGRAM], [6, 3, 2])[
            0
        ]
        email = f"{first}.{last}{i}@example.com".lower()
        lead = Lead(
            id=uuid.uuid4().hex,
            created_at=created,
            source=source,
            source_detail="facebook" if source == LeadSource.WEBHOOK else None,
            name=f"{first} {last}",
            email=email if source != LeadSource.TELEGRAM else None,
            telegram_chat_id=str(100000 + i) if source == LeadSource.TELEGRAM else None,
            service_interest=script["service"].capitalize(),
            timezone=profile.timezone,
            consent=True,
            consent_text=CONSENT,
            status=script["status"],
            score=script["score"],
            answers=dict(script["answers"]),
            ai_paused=bool(script.get("paused")),
            first_response_at=created + timedelta(seconds=reply_seconds),
        )
        link = profile.booking_link(lead.id, lead.name, lead.email) or ""
        channel = "telegram" if source == LeadSource.TELEGRAM else "email"
        at = created
        for index, (author, text) in enumerate(script["chat"]):
            if index:
                at += (
                    timedelta(seconds=reply_seconds)
                    if author != "lead"
                    else timedelta(minutes=rng.uniform(1, 30))
                )
            at = min(at, now - timedelta(seconds=30 * (len(script["chat"]) - index)))
            lead.messages.append(
                LeadMessage(
                    created_at=at,
                    direction=Direction.INBOUND if author == "lead" else Direction.OUTBOUND,
                    # The first message arrives through the form, webhook or Telegram.
                    channel=str(source)
                    if index == 0 and source != LeadSource.TELEGRAM
                    else channel,
                    content=text.format(first=first, link=link),
                    author=None if author == "lead" else author,
                )
            )
        lead.last_inbound_at = max(
            (m.created_at for m in lead.messages if m.direction == Direction.INBOUND),
            default=created,
        )
        lead.last_outbound_at = max(
            (m.created_at for m in lead.messages if m.direction == Direction.OUTBOUND),
            default=None,
        )
        lead.updated_at = max(filter(None, [lead.last_inbound_at, lead.last_outbound_at]))
        _events(lead, script, created, reply_seconds, rng)
        session.add(lead)
    return count


def _events(
    lead: Lead, script: dict[str, Any], created: datetime, reply: int, rng: random.Random
) -> None:
    def add(kind: str, minutes: float, **detail: Any) -> None:
        lead.events.append(
            LeadEvent(type=kind, detail=detail, created_at=created + timedelta(minutes=minutes))
        )

    add("lead.created", 0, source=lead.source, consent=True)
    add("lead.first_response", reply / 60)
    if script["answers"]:
        add("answers.recorded", 5, **script["answers"])
        add("lead.scored", 5, score=script["score"], reasons=[])
    if lead.status != LeadStatus.NEW:
        add("lead.status_changed", 6, to=lead.status)
    if script.get("paused"):
        add("lead.handed_off", 1, reason="Emergency", summary="Active leak, wants a call now.")
        add("lead.taken_over", 3, by="team")
    if lead.status == LeadStatus.BOOKED:
        lead.appointment_at = utcnow() + timedelta(days=rng.randint(1, 9), hours=rng.randint(0, 6))
        lead.booking_uid = f"demo_{rng.randrange(10**8)}"
        add("lead.booked", 40, booking_uid=lead.booking_uid)
    if lead.status == LeadStatus.OPTED_OUT:
        lead.opted_out_at = created + timedelta(minutes=20)
        add("lead.opted_out", 20)
    if lead.status in {LeadStatus.QUALIFYING, LeadStatus.QUALIFIED} and not lead.ai_paused:
        lead.next_follow_up_at = utcnow() + timedelta(hours=rng.randint(2, 40))


def main() -> None:
    parser = argparse.ArgumentParser(description="Fill the database with demo leads.")
    parser.add_argument("--count", type=int, default=60)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--seed", type=int, default=7, help="Random seed, for repeatable data.")
    parser.add_argument("--force", action="store_true", help="Add leads even if some exist.")
    args = parser.parse_args()

    settings = get_settings()
    sessions = make_session_factory(make_engine(settings.database_url))
    with sessions.begin() as session:
        existing = session.scalar(select(func.count(Lead.id))) or 0
        if existing and not args.force:
            raise SystemExit(
                f"The database already has {existing} leads. Use --force to add demo leads anyway."
            )
        rng = random.Random(args.seed)  # noqa: S311 - demo data, not security
        added = seed(session, args.count, args.days, rng)
    print(f"Added {added} demo leads to {settings.database_url}")


if __name__ == "__main__":
    main()
