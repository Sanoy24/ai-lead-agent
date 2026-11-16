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
