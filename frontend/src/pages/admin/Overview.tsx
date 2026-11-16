import { AlertCircle, CalendarCheck, Inbox, Timer, Zap } from "lucide-react";
import type { ReactNode } from "react";
import { useState } from "react";
import { Link } from "react-router";

import { StatusBadge } from "@/components/StatusBadge";
import { Card, CardHeader, EmptyState, ErrorMessage, Spinner } from "@/components/ui";
import { admin, type Metrics } from "@/lib/api";
import { PIPELINE, SOURCE, STATUS } from "@/lib/labels";
import { useAsync } from "@/lib/useAsync";
import { usePolling } from "@/lib/usePolling";
import { cn, formatDuration, formatPercent, timeAgo } from "@/lib/utils";

import { PageHeader } from "./Layout";

const PERIODS = [7, 30, 90];

export function Overview() {
  const [days, setDays] = useState(30);
  const metrics = useAsync(() => admin.metrics(days), [days]);
  const attention = useAsync(() => admin.leads({ attention: true, limit: 5 }));
  usePolling(metrics.reload, 15000);
  usePolling(attention.reload, 15000);

  return (
    <>
      <PageHeader
        title="Overview"
        description="How fast new leads hear back, and where they are in the pipeline."
        action={
          <div className="inline-flex rounded-lg border border-zinc-200 bg-white p-0.5 text-sm">
            {PERIODS.map((p) => (
              <button
                key={p}
                type="button"
                onClick={() => setDays(p)}
                className={cn(
                  "rounded-md px-3 py-1 font-medium",
                  p === days ? "bg-zinc-900 text-white" : "text-zinc-600 hover:text-zinc-900",
                )}
              >
                {p} days
              </button>
            ))}
          </div>
        }
      />
      {metrics.error && <ErrorMessage error={metrics.error} onRetry={metrics.reload} />}
      {!metrics.data && !metrics.error && <Spinner />}
      {metrics.data && <Dashboard metrics={metrics.data} />}

      <Card className="mt-6">
        <CardHeader
          title="Needs a person"
          description="Conversations the team has taken over, or the agent passed to a person."
          action={
            <Link to="/admin/leads?attention=1" className="text-sm text-brand-700 hover:underline">
              View all
            </Link>
          }
        />
        {attention.data && attention.data.leads.length === 0 && (
          <EmptyState
            icon={Inbox}
            title="Nothing waiting"
            description="The agent is handling every open conversation."
          />
        )}
        <ul className="divide-y divide-zinc-100">
          {attention.data?.leads.map((lead) => (
            <li key={lead.id}>
              <Link
                to={`/admin/leads/${lead.id}`}
                className="flex items-center gap-3 px-5 py-3 hover:bg-zinc-50"
              >
                <AlertCircle className="size-4 shrink-0 text-amber-600" aria-hidden />
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium text-zinc-900">{lead.name}</p>
                  <p className="truncate text-sm text-zinc-500">{lead.last_message}</p>
                </div>
                <StatusBadge status={lead.status} />
                <span className="hidden text-xs text-zinc-500 sm:inline">
                  {timeAgo(lead.last_activity_at)}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      </Card>
    </>
  );
}

function Dashboard({ metrics }: { metrics: Metrics }) {
  const target = metrics.target_seconds;
  return (
    <>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Kpi
          icon={<Zap className="size-4" />}
          label="Median first reply"
          value={formatDuration(metrics.median_first_response_seconds)}
          detail={
            metrics.p90_first_response_seconds != null
              ? `90% within ${formatDuration(metrics.p90_first_response_seconds)}`
              : "No replies yet"
          }
          highlight
        />
        <Kpi
          icon={<Timer className="size-4" />}
          label={`Answered within ${target}s`}
          value={formatPercent(metrics.responded_within_target)}
          detail={`${metrics.responded} of ${metrics.total_leads} leads answered`}
        />
        <Kpi
          icon={<Inbox className="size-4" />}
          label="New leads"
          value={String(metrics.total_leads)}
          detail={`In the last ${metrics.days} days`}
        />
        <Kpi
          icon={<CalendarCheck className="size-4" />}
          label="Booked"
          value={String(metrics.booked)}
          detail={`${formatPercent(metrics.booking_rate)} of leads booked an assessment`}
        />
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-[1fr_340px]">
        <Card>
          <CardHeader title="Leads per day" description="Booked leads are shown darker." />
          <DailyChart daily={metrics.daily} />
        </Card>
        <Card>
          <CardHeader title="Pipeline" />
          <Pipeline metrics={metrics} />
          <div className="border-t border-zinc-100 px-5 py-4">
            <p className="text-xs font-medium tracking-wide text-zinc-500 uppercase">Sources</p>
            <ul className="mt-2 space-y-1.5 text-sm">
              {Object.entries(metrics.by_source).length === 0 && (
                <li className="text-zinc-500">No leads yet</li>
              )}
              {Object.entries(metrics.by_source).map(([source, count]) => (
                <li key={source} className="flex justify-between">
                  <span className="text-zinc-700">{SOURCE[source] ?? source}</span>
                  <span className="font-medium tabular-nums">{count}</span>
                </li>
              ))}
            </ul>
          </div>
        </Card>
      </div>

      {metrics.needs_attention > 0 && (
        <p className="mt-4 text-sm text-amber-800">
          {metrics.needs_attention} conversation{metrics.needs_attention === 1 ? "" : "s"} waiting
          for a person.
        </p>
      )}
    </>
  );
}

function Kpi({
  icon,
  label,
  value,
  detail,
  highlight = false,
}: {
  icon: ReactNode;
  label: string;
  value: string;
  detail: string;
  highlight?: boolean;
}) {
  return (
    <Card className={cn("p-5", highlight && "border-brand-100 bg-brand-50/60")}>
      <div className="flex items-center gap-2 text-sm text-zinc-600">
        <span className={cn("text-zinc-400", highlight && "text-brand-700")}>{icon}</span>
        {label}
      </div>
      <p className="mt-3 text-3xl font-semibold tracking-tight tabular-nums">{value}</p>
      <p className="mt-1 text-xs text-zinc-500">{detail}</p>
    </Card>
  );
}

function DailyChart({ daily }: { daily: Metrics["daily"] }) {
  const max = Math.max(1, ...daily.map((d) => d.leads));
  const labelEvery = Math.ceil(daily.length / 6);
  return (
    <div className="px-5 py-4">
      <div className="flex h-44 items-end gap-[3px]" role="img" aria-label="Leads per day">
        {daily.map((day) => (
          <div
            key={day.date}
            className="group relative flex h-full flex-1 flex-col justify-end"
            title={`${day.date}: ${day.leads} leads, ${day.booked} booked`}
          >
            <div
              className="w-full rounded-t-sm bg-brand-100 transition-colors group-hover:bg-brand-500/40"
              style={{ height: `${((day.leads - day.booked) / max) * 100}%` }}
            />
            <div
              className="w-full bg-brand-600"
              style={{ height: `${(day.booked / max) * 100}%` }}
            />
          </div>
        ))}
      </div>
      <div className="mt-2 flex justify-between text-[10px] whitespace-nowrap text-zinc-400">
        {daily
          .filter((_, i) => i % labelEvery === 0)
          .map((day) => (
            <span key={day.date}>{shortDate(day.date)}</span>
          ))}
      </div>
    </div>
  );
}

function Pipeline({ metrics }: { metrics: Metrics }) {
  const total = Math.max(1, metrics.total_leads);
  return (
    <ul className="space-y-2.5 px-5 py-4">
      {PIPELINE.map((status) => {
        const count = metrics.by_status[status] ?? 0;
        return (
          <li key={status}>
            <Link
              to={`/admin/leads?status=${status}`}
              className="group block rounded-md hover:bg-zinc-50"
            >
              <div className="flex items-center justify-between text-sm">
                <StatusBadge status={status} />
                <span className="font-medium text-zinc-700 tabular-nums">{count}</span>
              </div>
              <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-zinc-100">
                <div
                  className={cn("h-full rounded-full", STATUS[status].bar)}
                  style={{ width: `${(count / total) * 100}%` }}
                />
              </div>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

function shortDate(iso: string): string {
  return new Date(`${iso}T12:00:00`).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}
