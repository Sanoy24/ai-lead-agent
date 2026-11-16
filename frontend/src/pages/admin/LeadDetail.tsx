import {
  ArrowLeft,
  Bot,
  CalendarClock,
  Hand,
  Mail,
  MessageCircle,
  Phone,
  Play,
  Send,
} from "lucide-react";
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link, useParams } from "react-router";

import { SpeedBadge, StatusBadge } from "@/components/StatusBadge";
import { Badge, Button, Card, CardHeader, ErrorMessage, Spinner } from "@/components/ui";
import {
  admin,
  type LeadDetail as Lead,
  type LeadEvent,
  type ManualStatus,
  type Message,
} from "@/lib/api";
import { CHANNEL, eventLabel, SOURCE, STATUS } from "@/lib/labels";
import { useAsync } from "@/lib/useAsync";
import { usePolling } from "@/lib/usePolling";
import { cn, formatDateTime, formatTime, humanize, timeAgo } from "@/lib/utils";

const MANUAL_STATUSES: ManualStatus[] = ["qualified", "nurturing", "booked", "lost"];

export function LeadDetail() {
  const { id = "" } = useParams();
  const lead = useAsync(() => admin.lead(id), [id]);
  // An action's result is shown until the next poll brings fresher data.
  const [override, setOverride] = useState<{ base: Lead | undefined; lead: Lead }>();
  const [actionError, setActionError] = useState<Error>();
  usePolling(lead.reload, 5000);
  const data = override && override.base === lead.data ? override.lead : lead.data;

  async function act(action: () => Promise<Lead>) {
    setActionError(undefined);
    const base = lead.data;
    try {
      setOverride({ base, lead: await action() });
    } catch (err) {
      setActionError(err instanceof Error ? err : new Error(String(err)));
      lead.reload();
    }
  }

  if (lead.error && !data) return <ErrorMessage error={lead.error} onRetry={lead.reload} />;
  if (!data) return <Spinner />;

  const closed = data.status === "opted_out";

  return (
    <>
      <Link
        to="/admin/leads"
        className="mb-4 inline-flex items-center gap-1.5 text-sm text-zinc-600 hover:text-zinc-900"
      >
        <ArrowLeft className="size-4" aria-hidden />
        All leads
      </Link>

      <div className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-xl font-semibold tracking-tight">{data.name}</h1>
            <StatusBadge status={data.status} />
            {data.score != null && <Badge>Score {data.score}</Badge>}
          </div>
          <p className="mt-1 text-sm text-zinc-500">
            {SOURCE[data.source] ?? data.source}
            {data.source_detail ? ` (${data.source_detail})` : ""} · {timeAgo(data.created_at)} ·
            first reply <SpeedBadge seconds={data.first_response_seconds} />
          </p>
        </div>
        {!closed &&
          (data.ai_paused ? (
            <Button onClick={() => void act(() => admin.resume(id))}>
              <Play className="size-4" aria-hidden />
              Hand back to AI
            </Button>
          ) : (
            <Button variant="secondary" onClick={() => void act(() => admin.takeOver(id))}>
              <Hand className="size-4" aria-hidden />
              Take over
            </Button>
          ))}
      </div>

      {actionError && (
        <div className="mb-4">
          <ErrorMessage error={actionError} />
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-[1fr_320px]">
        <Card className="flex min-h-[28rem] flex-col">
          <CardHeader
            title="Conversation"
            description={
              data.ai_paused
                ? "A person is handling this conversation. The AI won't reply or follow up."
                : "The AI is replying. Take over at any time."
            }
          />
          <Conversation messages={data.messages} />
          {closed ? (
            <p className="border-t border-zinc-100 px-5 py-4 text-sm text-zinc-500">
              This lead opted out, so no more messages can be sent.
            </p>
          ) : (
            <Composer
              paused={data.ai_paused}
              onSend={(text) => act(() => admin.sendMessage(id, text))}
            />
          )}
        </Card>

        <div className="space-y-6">
          <Card>
            <CardHeader title="Contact" />
            <dl className="space-y-3 px-5 py-4 text-sm">
              {data.email && (
                <Row icon={<Mail className="size-4" />}>
                  <a href={`mailto:${data.email}`} className="hover:underline">
                    {data.email}
                  </a>
                </Row>
              )}
              {data.phone && (
                <Row icon={<Phone className="size-4" />}>
                  <a href={`tel:${data.phone}`} className="hover:underline">
                    {data.phone}
                  </a>
                </Row>
              )}
              {data.telegram_chat_id && (
                <Row icon={<MessageCircle className="size-4" />}>Telegram chat</Row>
              )}
              {data.appointment_at && (
                <Row icon={<CalendarClock className="size-4" />}>
                  Assessment {formatDateTime(data.appointment_at)}
                </Row>
              )}
            </dl>
          </Card>

          <Card>
            <CardHeader title="Qualification" />
            <dl className="divide-y divide-zinc-100 text-sm">
              {data.service_interest && !("service" in data.answers) && (
                <Answer label="Interested in" value={data.service_interest} />
              )}
              {Object.entries(data.answers).map(([key, value]) => (
                <Answer key={key} label={humanize(key)} value={formatAnswer(value)} />
              ))}
              {Object.keys(data.answers).length === 0 && (
                <p className="px-5 py-3 text-zinc-500">No answers recorded yet.</p>
              )}
            </dl>
            {!closed && (
              <div className="border-t border-zinc-100 px-5 py-4">
                <label htmlFor="status" className="text-xs font-medium text-zinc-500">
                  Change status
                </label>
                <select
                  id="status"
                  value=""
                  onChange={(e) => {
                    const next = e.target.value as ManualStatus;
                    if (next) void act(() => admin.setStatus(id, next));
                  }}
                  className="mt-1.5 h-9 w-full rounded-lg border border-zinc-200 bg-white px-2 text-sm"
                >
                  <option value="">Choose a status…</option>
                  {MANUAL_STATUSES.filter((s) => s !== data.status).map((s) => (
                    <option key={s} value={s}>
                      Mark as {STATUS[s].label.toLowerCase()}
                    </option>
                  ))}
                </select>
              </div>
            )}
          </Card>

          <Card>
            <CardHeader title="Follow-ups" />
            <p className="px-5 py-4 text-sm text-zinc-600">
              {data.next_follow_up_at
                ? `Next follow-up ${timeAgo(data.next_follow_up_at)} (${formatDateTime(data.next_follow_up_at)}).`
                : "None scheduled."}{" "}
              {data.follow_ups_sent > 0 && `${data.follow_ups_sent} sent so far.`}
            </p>
          </Card>

          <Card>
            <CardHeader title="Activity" />
            <Timeline events={data.events} />
          </Card>

          {data.consent_text && (
            <Card className="px-5 py-4">
              <p className="text-xs font-medium text-zinc-500">Consent given</p>
              <p className="mt-1 text-xs leading-relaxed text-zinc-600">
                &ldquo;{data.consent_text}&rdquo;
              </p>
            </Card>
          )}
        </div>
      </div>
    </>
  );
}

function Conversation({ messages }: { messages: Message[] }) {
  const end = useRef<HTMLDivElement>(null);
  const count = messages.length;
  useEffect(() => {
    end.current?.scrollIntoView({ block: "nearest" });
  }, [count]);

  if (count === 0) {
    return <p className="flex-1 px-5 py-8 text-center text-sm text-zinc-500">No messages yet.</p>;
  }
  return (
    <div className="max-h-[36rem] flex-1 space-y-4 overflow-y-auto px-5 py-5">
      {messages.map((message) =>
        message.author === "system" ? (
          <p key={message.id} className="text-center text-xs text-zinc-500">
            {message.content} · {formatTime(message.created_at)}
          </p>
        ) : (
          <Bubble key={message.id} message={message} />
        ),
      )}
      <div ref={end} />
    </div>
  );
}

function Bubble({ message }: { message: Message }) {
  const inbound = message.direction === "inbound";
  const team = message.author === "team";
  const who = inbound ? "Lead" : team ? "Team" : "AI agent";
  return (
    <div className={cn("flex flex-col", inbound ? "items-start" : "items-end")}>
      <div
        className={cn(
          "max-w-[85%] rounded-2xl px-3.5 py-2.5 text-sm leading-relaxed whitespace-pre-wrap",
          inbound && "rounded-bl-sm bg-zinc-100 text-zinc-900",
          !inbound && !team && "rounded-br-sm bg-zinc-900 text-white",
          team && "rounded-br-sm bg-brand-700 text-white",
        )}
      >
        {linkify(message.content)}
      </div>
      <p className="mt-1 flex items-center gap-1 text-xs text-zinc-500">
        {!inbound && !team && <Bot className="size-3" aria-hidden />}
        {who} · {CHANNEL[message.channel] ?? message.channel} · {formatTime(message.created_at)}
      </p>
    </div>
  );
}

/** Make URLs in a message clickable and short, without rendering any HTML from it. */
function linkify(text: string): ReactNode[] {
  return text.split(/(https?:\/\/\S+)/g).map((part, i) =>
    /^https?:\/\//.test(part) ? (
      <a key={i} href={part} target="_blank" rel="noreferrer" className="underline" title={part}>
        {shortUrl(part)}
      </a>
    ) : (
      part
    ),
  );
}

function shortUrl(url: string): string {
  try {
    const { host, pathname } = new URL(url);
    return `${host}${pathname === "/" ? "" : pathname}`;
  } catch {
    return url;
  }
}

function Composer({
  paused,
  onSend,
}: {
  paused: boolean;
  onSend: (text: string) => Promise<void>;
}) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!text.trim()) return;
    setSending(true);
    try {
      await onSend(text.trim());
      setText("");
    } finally {
      setSending(false);
    }
  }

  return (
    <form onSubmit={submit} className="border-t border-zinc-100 p-4">
      <label htmlFor="reply" className="sr-only">
        Reply as the team
      </label>
      <textarea
        id="reply"
        rows={2}
        maxLength={2000}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void submit(e);
        }}
        placeholder="Write a reply as the team…"
        className="w-full resize-none rounded-lg border border-zinc-200 px-3 py-2 text-sm outline-none focus:border-brand-600 focus:ring-2 focus:ring-brand-600/15"
      />
      <div className="mt-2 flex items-center justify-between gap-3">
        <p className="text-xs text-zinc-500">
          {paused ? "You're handling this conversation." : "Sending a reply pauses the AI."}
        </p>
        <Button type="submit" loading={sending} disabled={!text.trim()}>
          <Send className="size-4" aria-hidden />
          Send
        </Button>
      </div>
    </form>
  );
}

function Row({ icon, children }: { icon: ReactNode; children: ReactNode }) {
  return (
    <div className="flex items-center gap-2.5 text-zinc-700">
      <span className="text-zinc-400">{icon}</span>
      <dd className="min-w-0 truncate">{children}</dd>
    </div>
  );
}

function Answer({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4 px-5 py-2.5">
      <dt className="text-zinc-500">{label}</dt>
      <dd className="text-right font-medium text-zinc-900">{value}</dd>
    </div>
  );
}

function formatAnswer(value: unknown): string {
  if (value === true) return "Yes";
  if (value === false) return "No";
  const text = String(value);
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function Timeline({ events }: { events: LeadEvent[] }) {
  const shown = events
    .filter((e) => e.type !== "lead.scored")
    .slice(-12)
    .reverse();
  return (
    <ol className="space-y-3 px-5 py-4">
      {shown.map((event) => (
        <li key={event.id} className="flex gap-3 text-sm">
          <span className="mt-1.5 size-1.5 shrink-0 rounded-full bg-zinc-300" aria-hidden />
          <div className="min-w-0">
            <p className="text-zinc-800">{eventLabel(event.type)}</p>
            <EventDetail event={event} />
            <p className="text-xs text-zinc-500">{timeAgo(event.created_at)}</p>
          </div>
        </li>
      ))}
    </ol>
  );
}

function EventDetail({ event }: { event: LeadEvent }) {
  const d = event.detail;
  let text: string | undefined;
  if (event.type === "lead.status_changed" && typeof d.to === "string") {
    const to = STATUS[d.to as keyof typeof STATUS]?.label ?? d.to;
    text = `Now ${to.toLowerCase()}${d.by === "team" ? ", by the team" : ""}`;
  } else if (event.type === "lead.handed_off" && typeof d.summary === "string") {
    text = d.summary;
  } else if (event.type === "answers.recorded") {
    text = Object.keys(d).map(humanize).join(", ");
  } else if (event.type === "follow_up.sent") {
    text = `${String(d.attempt)} of ${String(d.of)}`;
  }
  return text ? <p className="text-xs text-zinc-600">{text}</p> : null;
}
