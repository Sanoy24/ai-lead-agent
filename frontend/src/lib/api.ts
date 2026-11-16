export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
    throw new ApiError(errorMessage(body?.detail) || response.statusText, response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** FastAPI returns a string, or a list of validation errors. */
function errorMessage(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item: { msg?: string }) => (item.msg ?? "").replace(/^Value error, /, ""))
      .filter(Boolean)
      .join(" ");
  }
  return "";
}

// --- Public: the lead form ------------------------------------------------------

export interface FormConfig {
  business_name: string;
  consent_text: string;
}

export interface LeadForm {
  name: string;
  email?: string;
  phone?: string;
  message: string;
  service_interest?: string;
  timezone?: string;
  consent: boolean;
  website?: string;
}

export const publicApi = {
  formConfig: () => request<FormConfig>("/api/form-config"),
  submitLead: (form: LeadForm) =>
    request<{ status: string }>("/api/leads", { method: "POST", body: JSON.stringify(form) }),
};

// --- Admin -----------------------------------------------------------------------

export type LeadStatus =
  "new" | "contacted" | "qualifying" | "qualified" | "booked" | "nurturing" | "lost" | "opted_out";

export type ManualStatus = "qualified" | "nurturing" | "booked" | "lost";

export interface LeadSummary {
  id: string;
  name: string;
  email: string | null;
  phone: string | null;
  source: string;
  source_detail: string | null;
  service_interest: string | null;
  status: LeadStatus;
  score: number | null;
  ai_paused: boolean;
  created_at: string;
  first_response_seconds: number | null;
  last_activity_at: string;
  last_message: string | null;
  appointment_at: string | null;
  next_follow_up_at: string | null;
}

export interface LeadPage {
  total: number;
  leads: LeadSummary[];
}

export interface Message {
  id: number;
  direction: "inbound" | "outbound";
  author: "lead" | "agent" | "team" | "system";
  channel: string;
  content: string;
  created_at: string;
}

export interface LeadEvent {
  id: number;
  type: string;
  detail: Record<string, unknown>;
  created_at: string;
}

export interface LeadDetail extends LeadSummary {
  telegram_chat_id: string | null;
  timezone: string | null;
  answers: Record<string, unknown>;
  consent_text: string | null;
  follow_ups_sent: number;
  opted_out_at: string | null;
  messages: Message[];
  events: LeadEvent[];
}

export interface Metrics {
  days: number;
  total_leads: number;
  by_status: Partial<Record<LeadStatus, number>>;
  by_source: Record<string, number>;
  booked: number;
  booking_rate: number | null;
  responded: number;
  median_first_response_seconds: number | null;
  p90_first_response_seconds: number | null;
  responded_within_target: number | null;
  target_seconds: number;
  needs_attention: number;
  daily: { date: string; leads: number; booked: number }[];
}

export interface LeadQuery {
  status?: string;
  q?: string;
  attention?: boolean;
  limit?: number;
  offset?: number;
}

function toQuery(params: LeadQuery): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "" && value !== false) search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

const leadPath = (id: string) => `/api/admin/leads/${encodeURIComponent(id)}`;

export const admin = {
  login: (password: string) =>
    request<void>("/api/admin/login", { method: "POST", body: JSON.stringify({ password }) }),
  logout: () => request<void>("/api/admin/logout", { method: "POST" }),
  session: () => request<void>("/api/admin/session"),
  metrics: (days = 30) => request<Metrics>(`/api/admin/metrics?days=${days}`),
  leads: (params: LeadQuery = {}) => request<LeadPage>(`/api/admin/leads${toQuery(params)}`),
  lead: (id: string) => request<LeadDetail>(leadPath(id)),
  takeOver: (id: string) => request<LeadDetail>(`${leadPath(id)}/takeover`, { method: "POST" }),
  resume: (id: string) => request<LeadDetail>(`${leadPath(id)}/resume`, { method: "POST" }),
  sendMessage: (id: string, text: string) =>
    request<LeadDetail>(`${leadPath(id)}/messages`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  setStatus: (id: string, status: ManualStatus) =>
    request<LeadDetail>(`${leadPath(id)}/status`, {
      method: "POST",
      body: JSON.stringify({ status }),
    }),
};
