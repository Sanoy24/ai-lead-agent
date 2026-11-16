# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Project scaffolding and community files.
- Lead data model with messages and an append-only audit trail of events.
- Lead intake from the website form, with required consent, a spam trap and rate limiting.
- Signed webhook intake (HMAC-SHA256) for ad platforms, form builders and Zapier.
- Duplicate detection: repeat submissions within 24 hours join the existing lead.
- Business profile (TOML) with the company description, rules, qualification questions and scoring.
- Qualifying agent that replies to new leads and records their answers through a tool generated from the profile.
- Deterministic scoring and routing: qualified leads get the booking link, weak ones are nurtured, poor fits get a polite close.
- Opt-outs (STOP, unsubscribe) are handled before the model sees the message and are permanent.
- SMS through Twilio: leads with a phone number get texts, a text from a new number starts a lead, and incoming texts are verified with Twilio's signature. The first text carries an opt-out line.
- Replies go back on the channel the lead last used (SMS, email or Telegram).
- Form and webhook phone numbers written with parentheses, such as (303) 555-0100, are now accepted.
- README rewritten for clients and developers, with screenshots of the dashboard and demo site.
- HubSpot sync: leads become contacts (matched by email, no duplicates) with their lead status kept in step with the pipeline, and key moments (new enquiry, status changes, bookings, handoffs, opt-outs) added as notes. Runs in the background and catches up after outages.
- Columns added in new versions are added to existing databases automatically.
- Dashboard (React, TypeScript, Tailwind): overview with speed-to-lead metrics and the pipeline, a searchable lead list, and a lead page with the conversation, answers, follow-ups and activity, where a person can take over and reply.
- Demo landing page for the fictional business with the lead form, served by the same app.
- `python -m lead_agent.demo.seed` fills the database with realistic demo leads.
- Admin API for the dashboard: password sign-in with a signed HttpOnly cookie, lead list with search and filters, lead detail with the conversation and audit trail, take over and hand back, reply as a person, manual status changes, and pipeline and speed-to-lead metrics.
- Follow-ups: when a lead goes quiet the agent sends a short nudge on a schedule from the business profile (1, 3 and 7 days by default), never during quiet hours in the lead's time zone. A reply restarts the sequence; a booking, opt-out or takeover stops it. Qualified leads get the booking link again.
- Cal.com booking: booking links are prefilled with the lead's name, email and id, and a signed webhook marks leads booked, rescheduled or cancelled, stops their follow-ups, and confirms the time in the lead's time zone.
- Email channel: the agent emails leads by SMTP in one thread per lead, reads their replies by IMAP, and honours STOP and one-click unsubscribe. Auto-replies are ignored.
- Telegram as a lead channel: anyone who messages the bot becomes a lead, the agent replies in the chat, and /stop opts out.
- Responder that sends the first reply in the background, pauses the AI on handoff, and sends a holding reply if the model fails.
