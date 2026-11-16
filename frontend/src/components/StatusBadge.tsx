import { Badge } from "@/components/ui";
import type { LeadStatus } from "@/lib/api";
import { STATUS } from "@/lib/labels";

export function StatusBadge({ status }: { status: LeadStatus }) {
  const info = STATUS[status] ?? { label: status, tone: "neutral" as const };
  return <Badge tone={info.tone}>{info.label}</Badge>;
}

/** Response time, coloured by whether it hit the one-minute target. */
export function SpeedBadge({ seconds, target = 60 }: { seconds: number | null; target?: number }) {
  if (seconds == null) return <span className="text-zinc-400">–</span>;
  const tone = seconds <= target ? "success" : seconds <= target * 5 ? "warning" : "danger";
  const text =
    seconds < 60
      ? `${Math.round(seconds)}s`
      : seconds < 3600
        ? `${Math.round(seconds / 60)}m`
        : `${Math.round(seconds / 3600)}h`;
  return <Badge tone={tone}>{text}</Badge>;
}
