import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, post, useTenantRole } from "../api";
import { useMe } from "../App";
import AiUsagePanel, { type AiUsageSummary } from "../components/AiUsage";
import { Button, SkeletonRows } from "../components/Spinner";

interface Plan { key: string; name: string; price_usd_month: number | null; credits: number | null; blurb: string; features: string[] }
interface PlanState {
  plan: string | null; plan_info: Plan | null; requested: string | null; requested_at: string | null;
  allowance: { limit: number | null; used: number; remaining: number | null }; plans: Plan[];
}
interface History {
  month: string; credit_usd: number;
  months: { month: string; requests: number; credits: number; usd: number }[];
  reports: { report_id: string | null; report: string | null; requests: number; credits: number; usd: number;
             features: { feature: string; label: string; credits: number }[]; versions: number[] }[];
  total: number; page: number; page_size: number;
  rows: { at: string; feature: string; label: string; provider: string; model: string; input_tokens: number; output_tokens: number;
          usd: number; credits: number; ok: boolean; report: string | null; report_id: string | null; version_no: number | null }[];
}

const monthName = (m: string) => new Date(`${m}-01T00:00:00Z`).toLocaleDateString(undefined, { month: "long", year: "numeric", timeZone: "UTC" });
const fmtUsd = (n: number) => `$${n.toFixed(n < 1 ? 4 : 2)}`;

/** The workspace's plan, this month's AI credits and the full credit history. */
export default function Credits() {
  const { tenantId } = useParams();
  const me = useMe().data;
  const { isAdmin } = useTenantRole(me, tenantId);
  const base = `/api/tenants/${tenantId}`;
  const plan = useQuery({ queryKey: ["plan", tenantId], queryFn: () => api<PlanState>(`${base}/plan`) });
  const usage = useQuery({ queryKey: ["ai-usage", tenantId], queryFn: () => api<AiUsageSummary>(`${base}/ai-usage`) });
  return (
    <>
      <h1>Credits &amp; plan</h1>
      <p className="muted" style={{ marginTop: -6 }}>
        Building pages without AI and all nine figure checks are free on every plan. AI credits are only used when a report is
        built with AI, and each upload shows the most an option can use before anything runs.
      </p>
      {plan.data ? <PlanCard tenantId={tenantId!} data={plan.data} isAdmin={isAdmin} /> : plan.isError
        ? <p className="error">{plan.error.message}</p> : <div className="card"><SkeletonRows rows={3} label="Loading plan" /></div>}
      <div className="card">
        <h2>This month</h2>
        {usage.isError ? <p className="error small">{usage.error.message}</p>
          : usage.data ? <AiUsagePanel data={usage.data} tenantId={tenantId} /> : <SkeletonRows rows={3} label="Loading AI usage" />}
      </div>
      <HistoryCard tenantId={tenantId!} />
    </>
  );
}

function PlanCard({ tenantId, data, isAdmin }: { tenantId: string; data: PlanState; isAdmin: boolean }) {
  const qc = useQueryClient();
  const [msg, setMsg] = useState<string | null>(null);
  const ask = useMutation({
    mutationFn: (key: string) => post(`/api/tenants/${tenantId}/plan/request`, { plan: key }),
    onSuccess: () => { setMsg("Request sent. Your PublishPDF contact will switch the plan and let you know."); qc.invalidateQueries({ queryKey: ["plan", tenantId] }); },
    onError: (e: Error) => setMsg(e.message),
  });
  const a = data.allowance;
  const current = data.plan;
  return (
    <div className="card">
      <div className="toolbar" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
        <h2 style={{ margin: 0 }}>Plan: {data.plan_info?.name ?? "No plan yet"}</h2>
        <span className="muted small">
          {a.limit === null ? "No monthly credit limit" : `${a.used.toLocaleString()} of ${a.limit.toLocaleString()} credits used this month · ${a.remaining!.toLocaleString()} left`}
        </span>
      </div>
      {!current && <p className="muted small">Your workspace isn't on a plan yet, so the limit your PublishPDF contact set applies.</p>}
      {data.requested && (
        <p className="callout info small">You asked to move to <strong>{data.plans.find((p) => p.key === data.requested)?.name}</strong>
          {data.requested_at ? ` on ${new Date(data.requested_at).toLocaleDateString()}` : ""}. We'll switch it and let you know.</p>
      )}
      <div className="plans">
        {data.plans.map((p) => {
          const isCurrent = p.key === current;
          return (
            <div key={p.key} className={`plan${isCurrent ? " current" : ""}`}>
              <div className="plan-name">{p.name}{isCurrent && <span className="badge info">Current</span>}</div>
              <div className="plan-price">{p.price_usd_month === null ? "Talk to us" : p.price_usd_month === 0 ? <>$0<span className="muted small"> / month</span></> : <>${p.price_usd_month}<span className="muted small"> / month</span></>}</div>
              <div className="plan-credits">{p.credits === null ? "Custom AI credits" : `${p.credits.toLocaleString()} AI credits a month`}</div>
              <p className="muted small" style={{ margin: 0 }}>{p.blurb}</p>
              <ul className="plan-features">{p.features.map((f) => <li key={f}>{f}</li>)}</ul>
              {!isCurrent && isAdmin && (
                <Button className="secondary" disabled={data.requested === p.key}
                  busy={ask.isPending && ask.variables === p.key} busyLabel="Sending request" onClick={() => ask.mutate(p.key)}>
                  {data.requested === p.key ? "Requested" : p.key === "enterprise" ? "Contact us" : `Switch to ${p.name}`}
                </Button>
              )}
            </div>
          );
        })}
      </div>
      <p className="muted small" style={{ marginBottom: 0 }}>
        Every upload shows the most each option can use for that PDF before anything runs. There's no card payment yet:
        switching plans is a request your PublishPDF contact confirms.
      </p>
      {msg && <p className="small" role="status">{msg}</p>}
    </div>
  );
}

function HistoryCard({ tenantId }: { tenantId: string }) {
  const [month, setMonth] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const url = `/api/tenants/${tenantId}/ai-usage/history?${new URLSearchParams({ ...(month ? { month } : {}), page: String(page) })}`;
  const q = useQuery({ queryKey: ["ai-history", tenantId, month, page], queryFn: () => api<History>(url), placeholderData: (p) => p });
  const h = q.data;
  const max = h ? Math.max(1, ...h.months.map((m) => m.credits)) : 1;
  const pages = h ? Math.max(1, Math.ceil(h.total / h.page_size)) : 1;
  return (
    <div className="card">
      <div className="toolbar" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
        <h2 style={{ margin: 0 }}>Credit history</h2>
        {h && <a className="small" href={`/api/tenants/${tenantId}/ai-usage/history.csv?${h.month ? `month=${h.month}` : ""}`}>Download {monthName(h.month)} as CSV</a>}
      </div>
      {q.isError ? <p className="error small">{q.error.message}</p> : !h ? <SkeletonRows rows={4} label="Loading credit history" /> : (
        <div className="stack">
          {h.months.length === 0 ? <p className="muted small">No AI credits used in the last 12 months.</p> : (
            <ul className="month-bars" aria-label="Credits used per month">
              {h.months.map((m) => (
                <li key={m.month}>
                  <button type="button" className={m.month === h.month ? "on" : ""} onClick={() => { setMonth(m.month); setPage(1); }}
                    aria-pressed={m.month === h.month}>
                    <span className="mb-label">{monthName(m.month)}</span>
                    <span className="mb-bar"><span style={{ width: `${(m.credits / max) * 100}%` }} /></span>
                    <span className="mb-n">{m.credits.toLocaleString()} credits</span>
                    <span className="mb-usd muted small">{fmtUsd(m.usd)} · {m.requests} requests</span>
                  </button>
                </li>
              ))}
            </ul>
          )}

          <h3 style={{ margin: "6px 0 0" }}>{monthName(h.month)} by report</h3>
          {h.reports.length === 0 ? <p className="muted small">Nothing used this month.</p> : (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Report</th><th>What used credits</th><th className="num">Requests</th><th className="num">Cost</th><th className="num">Credits</th></tr></thead>
                <tbody>
                  {h.reports.map((r, i) => (
                    <tr key={r.report_id ?? i}>
                      <td>{r.report_id ? <Link to={`/t/${tenantId}/reports/${r.report_id}`}>{r.report}</Link> : <span className="muted">Not tied to a report</span>}
                        {r.versions.length > 0 && <span className="muted small"> · v{r.versions.join(", v")}</span>}</td>
                      <td className="small">{r.features.map((f) => `${f.label} (${f.credits})`).join(" · ")}</td>
                      <td className="num">{r.requests}</td><td className="num">{fmtUsd(r.usd)}</td><td className="num"><strong>{r.credits}</strong></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <h3 style={{ margin: "6px 0 0" }}>Every request</h3>
          {h.rows.length === 0 ? <p className="muted small">No requests this month.</p> : (
            <>
              <div className="table-wrap">
                <table>
                  <thead><tr><th>When</th><th>Feature</th><th>Report</th><th>Model</th><th className="num">Tokens in / out</th><th className="num">Cost</th><th className="num">Credits</th></tr></thead>
                  <tbody>
                    {h.rows.map((r, i) => (
                      <tr key={i}>
                        <td className="small">{new Date(r.at).toLocaleString()}</td>
                        <td>{r.label}{!r.ok && <span className="muted small"> (failed)</span>}</td>
                        <td className="small">{r.report && r.report_id ? <Link to={`/t/${tenantId}/reports/${r.report_id}`}>{r.report}</Link> : r.report ?? "—"}{r.version_no ? ` · v${r.version_no}` : ""}</td>
                        <td className="small muted">{r.model}</td>
                        <td className="num small">{r.input_tokens.toLocaleString()} / {r.output_tokens.toLocaleString()}</td>
                        <td className="num small">{fmtUsd(r.usd)}</td>
                        <td className="num small">{r.credits.toFixed(2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {pages > 1 && (
                <div className="btn-row" style={{ alignItems: "center" }}>
                  <button type="button" className="secondary" disabled={page <= 1} onClick={() => setPage(page - 1)}>← Newer</button>
                  <span className="muted small">Page {page} of {pages} · {h.total.toLocaleString()} requests</span>
                  <button type="button" className="secondary" disabled={page >= pages} onClick={() => setPage(page + 1)}>Older →</button>
                </div>
              )}
              <p className="muted small" style={{ margin: 0 }}>Each request shows its exact credits; totals round up, so you're never shown less than was used.</p>
            </>
          )}
        </div>
      )}
    </div>
  );
}
