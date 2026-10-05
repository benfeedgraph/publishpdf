export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(detail);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (init.body !== undefined) headers.set("Content-Type", "application/json");
  if (method !== "GET") headers.set("X-PPDF-CSRF", "1");
  const res = await fetch(path, { ...init, headers, credentials: "same-origin" });
  if (res.status === 204) return undefined as T;
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof body.detail === "string" ? body.detail : "Something went wrong. Please try again.";
    throw new ApiError(res.status, detail);
  }
  return body as T;
}

export const post = <T>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export type Role = "client_admin" | "client_reviewer";

export interface Me {
  user: { id: string; email: string; name: string | null; is_platform_admin: boolean };
  mfa: { required: boolean; enrolled: boolean; verified: boolean };
  fully_authenticated: boolean;
  tenants: { id: string; slug: string; name: string; status: string; role: Role }[];
}

export interface Job {
  id: string;
  kind: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  attempts: number;
  max_attempts: number;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  tenant_id?: string;
  last_error?: string | null;
}

export const ROLE_LABEL: Record<Role, string> = {
  client_admin: "Admin",
  client_reviewer: "Reviewer",
};

export function formatDateTime(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}

export async function upload<T>(path: string, form: FormData): Promise<T> {
  const res = await fetch(path, { method: "POST", body: form, headers: { "X-PPDF-CSRF": "1" }, credentials: "same-origin" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, typeof body.detail === "string" ? body.detail : "Upload failed. Please try again.");
  return body as T;
}

export const put = <T>(path: string, body: unknown) => api<T>(path, { method: "PUT", body: JSON.stringify(body) });
export const del = <T>(path: string) => api<T>(path, { method: "DELETE" });

export type ReportType = "quarterly_results" | "investor_presentation" | "annual_report" | "other";
export const REPORT_TYPE_LABEL: Record<ReportType, string> = {
  quarterly_results: "Quarterly results",
  investor_presentation: "Investor presentation",
  annual_report: "Annual report",
  other: "Other",
};
export const PERIOD_OPTIONS: [string, string][] = [
  ["q1", "Q1"], ["q2", "Q2"], ["q3", "Q3"], ["q4", "Q4"], ["h1", "H1 (half year)"], ["h2", "H2"], ["9m", "Nine months"], ["fy", "Full year"],
];

export interface ValidationSummary {
  figures_checked: number;
  passed: number;
  warnings: number;
  blocking: number;
  resolved: number;
  by_check: Record<string, number>;
}

export interface Version {
  id: string;
  version_no: number;
  status: "processing" | "failed" | "needs_review" | "validation_issues" | "published" | "superseded";
  stage: string | null;
  error: string | null;
  created_at: string;
  published_at: string | null;
  is_live: boolean;
  source_sha256: string;
  schema_sha256: string | null;
  bundle_sha256: string | null;
  validation: ValidationSummary | null;
  validated_current: boolean;
  created_from_version_id: string | null;
  theme_adjustments: { token: string; from: string; to: string; reason: string }[] | null;
}

export interface VersionDetail extends Version {
  jobs: Job[];
  open_blocking: number;
  page_count: number | null;
  pages: { page: number; width: number; height: number; method: "text_layer" | "ocr"; method_reason?: string }[];
  sections: { id: string; slug: string; type: string; heading_text: string; page: number | null }[];
  /** Set while the next step is queued: how long it has waited and whether any worker is running. */
  queue: { waiting: boolean; waiting_seconds: number; workers_alive: number | null; stalled: boolean } | null;
}

export interface Report {
  id: string;
  company_name: string;
  report_type: ReportType;
  fiscal_year: number;
  period: string;
  period_label: string;
  currency: string;
  reporting_unit: string;
  status: string;
  live_version_id: string | null;
  latest_version: Version | null;
  path: string;
  updated_at: string;
  versions?: Version[];
  theme_override: Record<string, unknown> | null;
}

export interface Figure {
  id: string;
  kind: string;
  raw: string;
  value: string | null;
  iso: string | null;
  unit: string | null;
  currency: string | null;
  period: { raw: string; key: string } | null;
  row_label: string | null;
  col_label: string | null;
  table_id: string | null;
  section_id: string;
  role: string;
  source: { page: number; bbox: [number, number, number, number] };
  method: "text_layer" | "ocr";
  confidence: number;
  status: "active" | "not_a_figure";
  review: Record<string, unknown> | null;
  edited: { original_raw: string; by: string; at: string } | null;
}

export type Run = { t: string } | { f: string };

export interface SchemaDoc {
  metadata: Record<string, unknown> & { company: string; period_label: string; source_pdf: { sha256: string; page_count: number } };
  pages: VersionDetail["pages"];
  sections: {
    id: string; type: string; heading: Run[]; heading_text: string; slug: string;
    blocks: {
      id: string; type: "paragraph" | "table" | "chart"; runs?: Run[]; caption?: Run[]; unit?: string; currency?: string;
      header_rows?: { colspan: number; runs: Run[] }[][];
      rows?: { label: Run[]; label_text: string; cells: { runs: Run[] }[] }[];
      extracted?: boolean; points?: { label: Run[]; value: Run[] }[]; note?: string;
      source: { page: number; bbox: number[] };
    }[];
  }[];
  figures: Record<string, Figure>;
}

export interface Issue {
  id: string;
  check: string;
  severity: "blocking" | "warning";
  fid: string | null;
  page: number | null;
  section_id: string | null;
  bbox: [number, number, number, number] | null;
  message: string;
  expected: string | null;
  actual: string | null;
  status: "open" | "resolved";
  resolution: string | null;
  figure: Partial<Figure> | null;
}

export const CHECK_LABEL: Record<string, string> = {
  traceability: "Source traceability",
  re_extraction: "Independent re-reading",
  arithmetic: "Arithmetic",
  period_unit: "Period & unit",
  rendered_page: "Web page check",
  completeness: "Completeness",
  low_confidence: "Needs a person",
  schema: "Schema",
  reviewer_flag: "Reviewer flag",
};

export const VERSION_STATUS_LABEL: Record<Version["status"], string> = {
  processing: "Processing",
  failed: "Failed",
  needs_review: "Ready for review",
  validation_issues: "Validation issues",
  published: "Live",
  superseded: "Superseded",
};

export function useTenantRole(me: Me | null | undefined, tenantId: string | undefined) {
  const t = me?.tenants.find((x) => x.id === tenantId);
  const admin = !!me?.user.is_platform_admin || t?.role === "client_admin";
  return { tenant: t, isAdmin: admin };
}
