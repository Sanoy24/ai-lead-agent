import { CheckCircle2, Clock, Home, ShieldCheck, Sun } from "lucide-react";
import { useState, type FormEvent, type ReactNode } from "react";

import { Button } from "@/components/ui";
import { publicApi } from "@/lib/api";
import { useAsync } from "@/lib/useAsync";

const SERVICES = ["Solar installation", "Roof repair", "Roof replacement"];

const inputClass =
  "mt-1.5 h-10 w-full rounded-lg border border-zinc-200 bg-white px-3 text-sm outline-none focus:border-brand-600 focus:ring-2 focus:ring-brand-600/15";

/** The demo business's website, with the lead form the agent replies to. */
export function Landing() {
  const config = useAsync(publicApi.formConfig);
  const business = config.data?.business_name ?? "Brightline Solar & Roofing";

  return (
    <div className="min-h-screen bg-zinc-50">
      <header className="border-b border-zinc-200 bg-white">
        <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-4 sm:px-6">
          <div className="flex items-center gap-2 font-semibold tracking-tight">
            <span className="flex size-8 items-center justify-center rounded-lg bg-brand-700 text-white">
              <Sun className="size-4" aria-hidden />
            </span>
            {business}
          </div>
          <a href="tel:+13035550100" className="text-sm font-medium text-zinc-700">
            (303) 555-0100
          </a>
        </div>
      </header>

      <main className="mx-auto grid max-w-6xl gap-10 px-4 py-12 sm:px-6 lg:grid-cols-[1fr_440px] lg:py-20">
        <section className="max-w-xl">
          <p className="text-sm font-medium text-brand-700">Serving the Denver metro area</p>
          <h1 className="mt-3 text-4xl font-semibold tracking-tight text-balance sm:text-5xl">
            Solar and roofing, done right the first time.
          </h1>
          <p className="mt-5 text-lg text-zinc-600">
            Family owned, licensed and insured, with 12 years of installs and repairs across Denver.
            Tell us about your project and we&apos;ll get back to you within minutes, day or night.
          </p>
          <ul className="mt-8 space-y-3 text-sm text-zinc-700">
            <Point icon={<Clock className="size-4" />}>A reply in minutes, not days</Point>
            <Point icon={<Home className="size-4" />}>Free on-site assessment</Point>
            <Point icon={<ShieldCheck className="size-4" />}>Licensed, insured, 12 years</Point>
          </ul>
        </section>

        <LeadFormCard consentText={config.data?.consent_text} />
      </main>

      <footer className="border-t border-zinc-200 bg-white">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-2 px-4 py-6 text-xs text-zinc-500 sm:px-6">
          <span>{business} is a fictional company used to demo ai-lead-agent.</span>
          <a href="/admin" className="hover:text-zinc-900">
            Team sign-in
          </a>
        </div>
      </footer>
    </div>
  );
}

function Point({ icon, children }: { icon: ReactNode; children: ReactNode }) {
  return (
    <li className="flex items-center gap-3">
      <span className="flex size-7 items-center justify-center rounded-full bg-brand-100 text-brand-700">
        {icon}
      </span>
      {children}
    </li>
  );
}

function LeadFormCard({ consentText }: { consentText?: string }) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [service, setService] = useState(SERVICES[0]!);
  const [message, setMessage] = useState("");
  const [consent, setConsent] = useState(false);
  const [website, setWebsite] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string>();
  const [sentTo, setSentTo] = useState<string>();

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setSending(true);
    setError(undefined);
    try {
      await publicApi.submitLead({
        name,
        email: email || undefined,
        phone: phone || undefined,
        service_interest: service,
        message,
        consent,
        website,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      });
      setSentTo(email || phone);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong. Please try again.");
    } finally {
      setSending(false);
    }
  }

  if (sentTo) {
    return (
      <div className="self-start rounded-2xl border border-zinc-200 bg-white p-8 text-center shadow-sm">
        <CheckCircle2 className="mx-auto size-10 text-emerald-600" aria-hidden />
        <h2 className="mt-4 text-lg font-semibold">Thanks, {name.split(" ")[0]}!</h2>
        <p className="mt-2 text-sm text-zinc-600">
          We&apos;ve got your request. Alex from our team will reply to {sentTo} in a minute or two.
        </p>
      </div>
    );
  }

  return (
    <form
      onSubmit={onSubmit}
      className="space-y-4 self-start rounded-2xl border border-zinc-200 bg-white p-6 shadow-sm sm:p-8"
    >
      <div>
        <h2 className="text-lg font-semibold">Get your free assessment</h2>
        <p className="mt-1 text-sm text-zinc-500">No obligation. We&apos;ll reply in minutes.</p>
      </div>

      <Field label="Full name" id="name">
        <input
          id="name"
          required
          maxLength={120}
          autoComplete="name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          className={inputClass}
        />
      </Field>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Email" id="email">
          <input
            id="email"
            type="email"
            autoComplete="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className={inputClass}
          />
        </Field>
        <Field label="Phone" id="phone" hint="optional">
          <input
            id="phone"
            type="tel"
            autoComplete="tel"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            className={inputClass}
          />
        </Field>
      </div>
      <Field label="What do you need?" id="service">
        <select
          id="service"
          value={service}
          onChange={(e) => setService(e.target.value)}
          className={inputClass}
        >
          {SERVICES.map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
      </Field>
      <Field label="Tell us a little about the project" id="message" hint="optional">
        <textarea
          id="message"
          rows={3}
          maxLength={2000}
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          className={`${inputClass} h-auto py-2`}
        />
      </Field>

      {/* Spam trap: hidden from people, so only bots fill it in. */}
      <div aria-hidden className="absolute -left-[9999px]">
        <label htmlFor="website">Website</label>
        <input
          id="website"
          tabIndex={-1}
          autoComplete="off"
          value={website}
          onChange={(e) => setWebsite(e.target.value)}
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
        <span>{consentText ?? "You may contact me about my enquiry."}</span>
      </label>

      {error && (
        <p role="alert" className="text-sm text-red-700">
          {error}
        </p>
      )}
      <Button
        type="submit"
        loading={sending}
        className="h-10 w-full bg-brand-700 hover:bg-brand-600"
      >
        Request my free assessment
      </Button>
    </form>
  );
}

function Field({
  label,
  id,
  hint,
  children,
}: {
  label: string;
  id: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div>
      <label htmlFor={id} className="text-sm font-medium text-zinc-700">
        {label}
        {hint && <span className="ml-1 font-normal text-zinc-400">({hint})</span>}
      </label>
      {children}
    </div>
  );
}
