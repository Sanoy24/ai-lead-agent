import type { LeadStatus } from "./api";

export type Tone = "neutral" | "warning" | "success" | "info" | "danger" | "brand";

export const STATUS: Record<LeadStatus, { label: string; tone: Tone; bar: string }> = {
  new: { label: "New", tone: "info", bar: "bg-sky-400" },
  contacted: { label: "Contacted", tone: "info", bar: "bg-sky-500" },
  qualifying: { label: "Qualifying", tone: "brand", bar: "bg-brand-500" },
  qualified: { label: "Qualified", tone: "success", bar: "bg-emerald-400" },
  booked: { label: "Booked", tone: "success", bar: "bg-emerald-600" },
  nurturing: { label: "Nurturing", tone: "warning", bar: "bg-amber-400" },
  lost: { label: "Not a fit", tone: "neutral", bar: "bg-zinc-300" },
  opted_out: { label: "Opted out", tone: "danger", bar: "bg-red-300" },
};

/** Pipeline order, from first contact to closed. */
export const PIPELINE: LeadStatus[] = [
  "new",
  "contacted",
  "qualifying",
  "qualified",
  "booked",
  "nurturing",
  "lost",
  "opted_out",
];

export const SOURCE: Record<string, string> = {
  form: "Website form",
  webhook: "Webhook",
  telegram: "Telegram",
  sms: "Text message",
  reactivation: "Reactivation campaign",
};

export const CHANNEL: Record<string, string> = {
  email: "Email",
  telegram: "Telegram",
  sms: "SMS",
  form: "Website form",
  webhook: "Webhook",
  log: "Not sent (log only)",
};

const EVENTS: Record<string, string> = {
  "lead.created": "Lead created",
  "lead.resubmitted": "Submitted again",
  "lead.first_response": "First reply sent",
  "lead.scored": "Score updated",
  "answers.recorded": "Answers recorded",
  "lead.status_changed": "Status changed",
  "lead.handed_off": "Agent asked for a person",
  "lead.taken_over": "Taken over by the team",
  "lead.resumed": "Handed back to the AI",
  "lead.opted_out": "Opted out",
  "lead.booked": "Booked an assessment",
  "lead.rescheduled": "Rescheduled",
  "lead.booking_cancelled": "Cancelled the booking",
  "follow_up.sent": "Follow-up sent",
  "reactivation.sent": "Reactivation message sent",
  "follow_ups.finished": "Follow-ups finished",
  "message.failed": "Message not delivered",
  "agent.failed": "AI unavailable, holding reply sent",
};

export function eventLabel(type: string): string {
  return EVENTS[type] ?? type;
}
