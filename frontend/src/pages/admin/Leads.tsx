import { Bot, ChevronLeft, ChevronRight, Search, UserRound, Users } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";

import { SpeedBadge, StatusBadge } from "@/components/StatusBadge";
import { Button, Card, EmptyState, ErrorMessage, Spinner } from "@/components/ui";
import { admin } from "@/lib/api";
import { SOURCE } from "@/lib/labels";
import { useAsync } from "@/lib/useAsync";
import { usePolling } from "@/lib/usePolling";
import { cn, timeAgo } from "@/lib/utils";

import { PageHeader } from "./Layout";

const PAGE_SIZE = 25;

const FILTERS: { key: string; label: string }[] = [
  { key: "", label: "All" },
  { key: "attention", label: "Needs a person" },
  { key: "qualifying", label: "Qualifying" },
  { key: "qualified", label: "Qualified" },
  { key: "booked", label: "Booked" },
  { key: "nurturing", label: "Nurturing" },
  { key: "lost", label: "Not a fit" },
  { key: "opted_out", label: "Opted out" },
];

export function Leads() {
  const [params, setParams] = useSearchParams();
  const filter = params.get("attention") ? "attention" : (params.get("status") ?? "");
  const q = params.get("q") ?? "";
  const page = Number(params.get("page") ?? "0");
  const [search, setSearch] = useState(q);

  // Search as you type, without a request per keystroke.
  useEffect(() => {
    const id = window.setTimeout(() => {
      if (search !== q) update({ q: search, page: "" });
    }, 300);
    return () => window.clearTimeout(id);
  });

  function update(changes: Record<string, string>) {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(changes)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    setParams(next, { replace: true });
  }

  function setFilter(key: string) {
    update({
      status: key === "attention" ? "" : key,
      attention: key === "attention" ? "1" : "",
      page: "",
    });
  }

  const leads = useAsync(
    () =>
      admin.leads({
        status: filter === "attention" ? undefined : filter || undefined,
        attention: filter === "attention",
        q: q || undefined,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      }),
    [filter, q, page],
  );
  usePolling(leads.reload, 10000);

  const total = leads.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <>
      <PageHeader title="Leads" description="Every enquiry, newest activity first." />

      <div className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex [scrollbar-width:none] gap-1 overflow-x-auto">
          {FILTERS.map(({ key, label }) => (
            <button
              key={key}
              type="button"
              onClick={() => setFilter(key)}
              className={cn(
                "shrink-0 rounded-lg px-3 py-1.5 text-sm font-medium",
                key === filter
                  ? "bg-zinc-900 text-white"
                  : "text-zinc-600 hover:bg-zinc-100 hover:text-zinc-900",
              )}
            >
              {label}
            </button>
          ))}
        </div>
        <label className="relative block lg:w-72">
          <span className="sr-only">Search leads</span>
          <Search
            className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-zinc-400"
            aria-hidden
          />
          <input
            type="search"
            placeholder="Search name, email or phone"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="h-9 w-full rounded-lg border border-zinc-200 bg-white pr-3 pl-9 text-sm outline-none focus:border-brand-600 focus:ring-2 focus:ring-brand-600/15"
          />
        </label>
      </div>

      {leads.error && <ErrorMessage error={leads.error} onRetry={leads.reload} />}
      <Card className="overflow-hidden">
        {!leads.data && !leads.error && <Spinner />}
        {leads.data && leads.data.leads.length === 0 && (
          <EmptyState
            icon={Users}
            title="No leads here"
            description={
              q || filter
                ? "Try another filter or search."
                : "Leads from your website form, webhooks and Telegram will show up here."
            }
          />
        )}
        {leads.data && leads.data.leads.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-zinc-100 bg-zinc-50/60 text-xs text-zinc-500">
                <tr>
                  <th className="px-5 py-2.5 font-medium">Lead</th>
                  <th className="px-3 py-2.5 font-medium">Status</th>
                  <th className="hidden px-3 py-2.5 font-medium xl:table-cell">Score</th>
                  <th className="px-3 py-2.5 font-medium whitespace-nowrap">First reply</th>
                  <th className="hidden px-3 py-2.5 font-medium lg:table-cell">Source</th>
                  <th className="px-5 py-2.5 text-right font-medium whitespace-nowrap">
                    Last activity
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-100">
                {leads.data.leads.map((lead) => (
                  <tr key={lead.id} className="group relative hover:bg-zinc-50">
                    <td className="max-w-xs px-5 py-3">
                      <Link
                        to={`/admin/leads/${lead.id}`}
                        className="font-medium text-zinc-900 after:absolute after:inset-0"
                      >
                        {lead.name}
                      </Link>
                      <p className="truncate text-zinc-500">
                        {lead.last_message ?? lead.service_interest ?? lead.email ?? lead.phone}
                      </p>
                    </td>
                    <td className="px-3 py-3">
                      <div className="flex items-center gap-1.5">
                        <StatusBadge status={lead.status} />
                        {lead.ai_paused ? (
                          <UserRound
                            className="size-3.5 text-amber-600"
                            aria-label="A person is handling this"
                          />
                        ) : (
                          <Bot
                            className="size-3.5 text-zinc-300"
                            aria-label="AI is handling this"
                          />
                        )}
                      </div>
                    </td>
                    <td className="px-3 py-3 text-zinc-700 tabular-nums">{lead.score ?? "–"}</td>
                    <td className="px-3 py-3">
                      <SpeedBadge seconds={lead.first_response_seconds} />
                    </td>
                    <td className="hidden px-3 py-3 whitespace-nowrap text-zinc-600 lg:table-cell">
                      {SOURCE[lead.source] ?? lead.source}
                    </td>
                    <td className="px-5 py-3 text-right whitespace-nowrap text-zinc-500">
                      {timeAgo(lead.last_activity_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {total > PAGE_SIZE && (
        <div className="mt-4 flex items-center justify-between text-sm text-zinc-600">
          <span>
            Page {page + 1} of {pages} · {total} leads
          </span>
          <div className="flex gap-2">
            <Button
              variant="secondary"
              disabled={page === 0}
              onClick={() => update({ page: page - 1 ? String(page - 1) : "" })}
            >
              <ChevronLeft className="size-4" aria-hidden />
              Previous
            </Button>
            <Button
              variant="secondary"
              disabled={page + 1 >= pages}
              onClick={() => update({ page: String(page + 1) })}
            >
              Next
              <ChevronRight className="size-4" aria-hidden />
            </Button>
          </div>
        </div>
      )}
    </>
  );
}
