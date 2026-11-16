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
- Responder that sends the first reply in the background, pauses the AI on handoff, and sends a holding reply if the model fails.
