# Security Policy

## Supported versions

This project is in early development. Security fixes are applied to the latest release on `main`.

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Report vulnerabilities privately through [GitHub Security Advisories](https://github.com/Sanoy24/ai-lead-agent/security/advisories/new). Include:

- A description of the issue and its impact
- Steps to reproduce, or a proof of concept
- Any suggested fix

You can expect an acknowledgement within 3 business days and a status update within 10 business days.

## Scope

Issues of particular interest include:

- Sending messages to people who didn't ask to be contacted, or after they opted out
- Accepting forged leads or webhook calls
- Exposing one lead's personal data to another person
- Prompt injection that makes the agent promise prices, discounts or commitments it shouldn't
- Leaking secrets such as API keys, CRM tokens or bot tokens
