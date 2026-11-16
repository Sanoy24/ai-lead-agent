# ai-lead-agent

**An AI speed-to-lead agent for service businesses. Every new lead gets a personal reply within a minute, day or night. The agent asks the right questions, books qualified leads into your calendar, follows up when they go quiet, and logs everything in your CRM.**

> **Status:** early development. Not ready for production use yet.

## Why speed matters

Most leads go to whichever business answers first. A form filled in at 9 pm usually waits until the next morning, and by then the lead has called a competitor. This agent replies in seconds and keeps the conversation going until the lead is booked or politely closed.

## What it does

- **Captures leads** from a website form, a webhook (Facebook Lead Ads, Typeform, Zapier and similar) or Telegram.
- **Replies in under a minute** by email or Telegram, in your business's voice.
- **Qualifies by conversation:** asks your questions (budget, timeline, property, location), records the answers and scores the lead.
- **Books qualified leads** with a booking link and updates the lead when they book.
- **Follows up automatically** when a lead goes quiet, and stops the moment they reply, book or opt out.
- **Syncs to your CRM** (HubSpot, free tier) and shows your pipeline, response times and every conversation in a dashboard, where a person can take over at any time.

## How qualification works

The language model and plain code each do what they're good at:

- **The model talks.** It writes warm, short replies, answers questions about the business, and records the lead's answers through a `record_answers` tool generated from the business profile.
- **Code decides.** A scoring module turns the recorded answers into a score and an outcome using rules from the business profile: keep qualifying, **qualified** (booking link added automatically), **nurture** (all answers in but not ready), or **not a fit** (polite close). The outcome never depends on the model's judgement, so it's predictable and testable.
- **Opt-outs bypass the model entirely.** "STOP" or "unsubscribe" is caught before the model sees the message, and the lead is never contacted again.

Everything business-specific lives in one TOML file: the description, rules the agent must follow (such as "never quote prices"), the questions, and how answers are scored. See [the demo profile](src/lead_agent/demo/business.toml).

## Dashboard

Set `ADMIN_PASSWORD` and sign in at `/admin` to see:

- **Overview**: the median time to first reply (the speed-to-lead number), the share of leads answered within a minute, new leads, bookings, a leads-per-day chart, and the pipeline by status and source.
- **Leads**: every enquiry, searchable and filterable by status, with each lead's first-reply time.
- **Lead detail**: the full conversation across email and Telegram, the recorded answers and score, upcoming follow-ups, the consent wording, and an activity log. **Take over** pauses the AI so a person can reply from the dashboard; **Hand back to AI** resumes it.

The same server also hosts a demo landing page for the fictional business at `/`, with the lead form the agent replies to.

## HubSpot

Set `HUBSPOT_ACCESS_TOKEN` (a private app token from the free HubSpot CRM) and every lead becomes a HubSpot contact within half a minute. Contacts are matched by email, so existing ones are updated rather than duplicated. HubSpot's **Lead status** follows the pipeline (new, in progress, connected, open deal, bad timing, unqualified). Key moments are added as notes on the contact, with a link back to the conversation: the enquiry, status changes with the recorded answers and score, bookings, handoffs and opt-outs. Sync runs in the background, never delays a reply to a lead, and catches up after a HubSpot outage.

## Running it locally

```bash
uv sync
npm --prefix frontend ci && npm --prefix frontend run build
cp .env.example .env   # then set ADMIN_PASSWORD and the LLM settings
uv run python -m lead_agent.demo.seed   # optional: 60 realistic demo leads
uv run lead-agent
```

Open http://127.0.0.1:8001 for the demo site and http://127.0.0.1:8001/admin for the dashboard. For frontend development, run `npm --prefix frontend run dev`; it proxies the API to port 8001.

## Follow-ups

When a lead stops replying, the agent follows up on the schedule in the business profile (by default 1, 3 and 7 days after its last message). Each follow-up is short, re-asks the open question or offers the booking link again, and the last one says it's the last. Follow-ups are never sent during quiet hours (8 pm to 9 am by default) in the lead's time zone, and a reply restarts the schedule. Booked, opted-out, lost and taken-over leads are never followed up. First replies aren't held for quiet hours, because the lead has just written to you.

## Booking

Qualified leads get your booking link prefilled with their name and email. Cal.com sends booking events back to `/api/webhooks/calcom` (signed with `CALCOM_WEBHOOK_SECRET`), and the agent marks the lead **booked**, stops following up, and confirms the time in the lead's time zone. Reschedules and cancellations are tracked too. Bookings are matched by a lead id carried in the link, or by the attendee's email.

## Email

Set the `SMTP_*` and `EMAIL_FROM` settings and the agent emails leads who gave an address. Each lead's messages stay in one thread, and every email offers a way out ("Reply STOP", plus a one-click unsubscribe header). Set `IMAP_HOST` too and the agent reads replies every minute and continues the conversation; out-of-office and other automatic replies are ignored. A free Gmail account with an app password works for both. See [.env.example](.env.example).

## Telegram

Set `TELEGRAM_BOT_TOKEN` (from [@BotFather](https://t.me/BotFather)) and anyone who messages your bot becomes a lead. The agent replies in the chat with a typing indicator, keeps the conversation going across messages, and sends the booking link once the lead qualifies. `/stop` opts out, and `/start` lets someone who opted out begin again. The bot uses long polling, so it runs on a laptop or a server without a public URL.

## Design principles

- **Consent first.** It only contacts people who asked to be contacted, and opt-outs are permanent.
- **Respectful timing.** No messages during quiet hours in the lead's time zone.
- **A person stays in control.** Any conversation can be taken over, and an approval mode lets a person review messages before they're sent.
- **Works without paid services** for development and demos.

## License

[MIT](LICENSE)
