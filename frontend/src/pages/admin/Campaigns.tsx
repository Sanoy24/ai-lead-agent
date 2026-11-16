import { FileUp, RotateCcw } from "lucide-react";
import { useState, type ChangeEvent, type FormEvent } from "react";

import { Button, Card, CardHeader, EmptyState, ErrorMessage, Spinner } from "@/components/ui";
import { admin, type CampaignContact, type ImportResult } from "@/lib/api";
import { parseCsv, rowsToContacts } from "@/lib/csv";
import { useAsync } from "@/lib/useAsync";
import { usePolling } from "@/lib/usePolling";
import { formatPercent } from "@/lib/utils";

import { PageHeader } from "./Layout";

const inputClass =
  "mt-1.5 h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 text-sm outline-none focus:border-brand-600 focus:ring-2 focus:ring-brand-600/15";

export function Campaigns() {
  const stats = useAsync(admin.campaigns);
  usePolling(stats.reload, 15000);

  return (
    <>
      <PageHeader
        title="Campaigns"
        description="Re-contact past leads and customers who went quiet. The agent sends each a short opener, outside quiet hours, and replies continue as normal conversations."
      />
      <div className="grid gap-6 lg:grid-cols-[380px_1fr]">
        <NewCampaign onCreated={stats.reload} />
        <Card className="self-start overflow-hidden">
          <CardHeader title="Results" />
          {stats.error && (
            <div className="p-4">
              <ErrorMessage error={stats.error} onRetry={stats.reload} />
            </div>
          )}
          {!stats.data && !stats.error && <Spinner />}
          {stats.data?.length === 0 && (
            <EmptyState
              icon={RotateCcw}
              title="No campaigns yet"
              description="Upload a list of past leads or customers to start one."
            />
          )}
          {stats.data && stats.data.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="border-b border-zinc-100 bg-zinc-50/60 text-xs text-zinc-500">
                  <tr>
                    <th className="px-5 py-2.5 font-medium">Campaign</th>
                    <th className="px-3 py-2.5 text-right font-medium">Contacts</th>
                    <th className="px-3 py-2.5 text-right font-medium">Waiting</th>
                    <th className="px-3 py-2.5 text-right font-medium">Replied</th>
                    <th className="px-3 py-2.5 text-right font-medium">Booked</th>
                    <th className="px-5 py-2.5 text-right font-medium">Opted out</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-100 tabular-nums">
                  {stats.data.map((c) => (
                    <tr key={c.campaign}>
                      <td className="px-5 py-3 font-medium text-zinc-900">{c.campaign}</td>
                      <td className="px-3 py-3 text-right">{c.total}</td>
                      <td className="px-3 py-3 text-right text-zinc-500">{c.waiting}</td>
                      <td className="px-3 py-3 text-right">
                        {c.replied}{" "}
                        <span className="text-xs text-zinc-400">
                          ({formatPercent(c.contacted ? c.replied / c.contacted : null)})
                        </span>
                      </td>
                      <td className="px-3 py-3 text-right font-medium text-emerald-700">
                        {c.booked}
                      </td>
                      <td className="px-5 py-3 text-right text-zinc-500">{c.opted_out}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </>
  );
}

function NewCampaign({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("");
  const [fileName, setFileName] = useState<string>();
  const [contacts, setContacts] = useState<CampaignContact[]>([]);
  const [unknown, setUnknown] = useState<string[]>([]);
  const [consent, setConsent] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string>();
  const [result, setResult] = useState<ImportResult>();

  async function onFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    setResult(undefined);
    setError(undefined);
    if (!file) return;
    const parsed = rowsToContacts(parseCsv(await file.text()));
    setFileName(file.name);
    setContacts(parsed.contacts as unknown as CampaignContact[]);
    setUnknown(parsed.unknownColumns);
    if (!name) setName(file.name.replace(/\.csv$/i, ""));
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSending(true);
    setError(undefined);
    try {
      setResult(
        await admin.startCampaign(
          name.trim(),
          contacts,
          "Past enquiry or customer of the business.",
        ),
      );
      setContacts([]);
      setFileName(undefined);
      setConsent(false);
      onCreated();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start the campaign.");
    } finally {
      setSending(false);
    }
  }

  return (
    <Card className="self-start">
      <CardHeader
        title="New campaign"
        description="A CSV with a header row: name, email, phone, service, note."
      />
      <form onSubmit={submit} className="space-y-4 px-5 py-4">
        <label className="flex cursor-pointer flex-col items-center gap-2 rounded-lg border border-dashed border-zinc-300 px-4 py-6 text-center text-sm text-zinc-600 hover:bg-zinc-50">
          <FileUp className="size-5 text-zinc-400" aria-hidden />
          {fileName ? (
            <span>
              <span className="font-medium text-zinc-900">{fileName}</span> · {contacts.length}{" "}
              contacts
            </span>
          ) : (
            <span>Choose a CSV file</span>
          )}
          <input type="file" accept=".csv,text/csv" className="sr-only" onChange={onFile} />
        </label>
        {unknown.length > 0 && (
          <p className="text-xs text-amber-800">Ignored columns: {unknown.join(", ")}</p>
        )}
        {contacts.length > 0 && (
          <ul className="space-y-1 rounded-lg bg-zinc-50 px-3 py-2 text-xs text-zinc-600">
            {contacts.slice(0, 3).map((c, i) => (
              <li key={i} className="truncate">
                {c.name || "(no name)"} · {c.email || c.phone || "no contact"}
                {c.service_interest ? ` · ${c.service_interest}` : ""}
              </li>
            ))}
            {contacts.length > 3 && <li>…and {contacts.length - 3} more</li>}
          </ul>
        )}
        <div>
          <label htmlFor="campaign-name" className="text-sm font-medium text-zinc-700">
            Campaign name
          </label>
          <input
            id="campaign-name"
            required
            maxLength={64}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Spring quotes 2026"
            className={inputClass}
          />
        </div>
        <label className="flex items-start gap-2.5 text-xs leading-relaxed text-zinc-600">
          <input
            type="checkbox"
            required
            checked={consent}
            onChange={(e) => setConsent(e.target.checked)}
            className="mt-0.5 size-4 rounded border-zinc-300 accent-brand-700"
          />
          <span>
            Everyone on this list previously contacted us or bought from us and may be contacted
            again. People who opted out are skipped automatically.
          </span>
        </label>
        {error && (
          <p role="alert" className="text-sm text-red-700">
            {error}
          </p>
        )}
        <Button
          type="submit"
          className="w-full"
          loading={sending}
          disabled={contacts.length === 0 || !consent}
        >
          Start campaign
        </Button>
        {result && (
          <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-900">
            <p className="font-medium">
              {result.queued} contacts queued for “{result.campaign}”.
            </p>
            {result.skipped.length > 0 && (
              <details className="mt-1 text-xs">
                <summary className="cursor-pointer">{result.skipped.length} skipped</summary>
                <ul className="mt-1 space-y-0.5">
                  {result.skipped.map((s) => (
                    <li key={`${s.row}-${s.reason}`}>
                      Row {s.row}: {s.reason}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        )}
      </form>
    </Card>
  );
}
