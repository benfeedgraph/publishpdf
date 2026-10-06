import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import AiUsagePanel, { type AiUsageSummary } from "../components/AiUsage";
import { api, post, put, REPORT_TYPE_LABEL, ROLE_LABEL, type Job, type Report, type Role } from "../api";
import JobTable from "../components/JobTable";
import { Button } from "../components/Spinner";

interface Detail {
  tenant: { id: string; slug: string; name: string; status: string; created_at: string; ai_monthly_credit_limit: number | null };
  ai_usage: AiUsageSummary;
  reports: Report[];
  members: { user_id: string; email: string; role: Role; mfa_enrolled: boolean }[];
  jobs: Job[];
  domain: { hostname: string; status: string; failure_reason: string | null; last_checked_at: string | null } | null;
}

export default function AdminTenantDetail() {
  const { id } = useParams();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["admin-tenant", id], queryFn: () => api<Detail>(`/api/admin/tenants/${id}`), refetchInterval: 5000 });
  const rerun = useMutation({ mutationFn: (jid: string) => post(`/api/admin/jobs/${jid}/rerun`), onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-tenant", id] }) });
  if (q.isPending) return <><div className="sk sk-title" /><div className="sk sk-block" /></>;
  if (q.isError) return <p className="error">{q.error.message}</p>;
  const d = q.data;
  return (
    <>
      <p className="crumbs small"><Link to="/admin/tenants">Tenants</Link> ›</p>
      <div className="page-head">
        <div><h1>{d.tenant.name}</h1><p className="muted small"><code>{d.tenant.slug}</code> · {d.tenant.status}</p></div>
        <Link className="button secondary" to={`/t/${d.tenant.id}`}>Open workspace</Link>
      </div>
      <div className="grid2">
        <div className="card">
          <h2>Domain</h2>
          {d.domain ? <p><code>{d.domain.hostname}</code> <span className={`badge ${d.domain.status === "live" ? "ok" : d.domain.status === "failed" ? "bad" : "warn"}`}>{d.domain.status}</span>{d.domain.failure_reason && <span className="error small"><br />{d.domain.failure_reason}</span>}</p> : <p className="muted">No custom domain.</p>}
        </div>
        <div className="card">
          <h2>Users</h2>
          <ul className="plain">{d.members.map((m) => <li key={m.user_id}>{m.email} · {ROLE_LABEL[m.role]}{m.role === "client_admin" && !m.mfa_enrolled && <span className="muted small"> (2FA not set up)</span>}</li>)}</ul>
        </div>
      </div>
      <div className="card">
        <h2>AI usage</h2>
        <AiUsagePanel data={d.ai_usage} />
        <AiLimitForm tenantId={d.tenant.id} current={d.tenant.ai_monthly_credit_limit} />
      </div>
      <h2>Reports</h2>
      {d.reports.length === 0 ? <p className="muted">No reports.</p> : (
        <table>
          <thead><tr><th scope="col">Report</th><th scope="col">Status</th><th scope="col">Checks</th></tr></thead>
          <tbody>{d.reports.map((r) => (
            <tr key={r.id}>
              <td><Link to={`/t/${d.tenant.id}/reports/${r.id}`}>{r.period_label} {REPORT_TYPE_LABEL[r.report_type]}</Link></td>
              <td>{r.status}</td>
              <td className="small">{r.latest_version?.validation ? `${r.latest_version.validation.blocking} blocking · ${r.latest_version.validation.warnings} warnings` : "—"}</td>
            </tr>))}</tbody>
        </table>
      )}
      <h2>Processing jobs</h2>
      <JobTable jobs={d.jobs} admin onRerun={(jid) => rerun.mutate(jid)} rerunning={rerun.isPending ? rerun.variables : null} />
    </>
  );
}

function AiLimitForm({ tenantId, current }: { tenantId: string; current: number | null }) {
  const qc = useQueryClient();
  const [value, setValue] = useState(current === null ? "" : String(current));
  const [msg, setMsg] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: (n: number | null) => put(`/api/admin/tenants/${tenantId}/ai-limit`, { monthly_credits: n }),
    onSuccess: () => { setMsg("Saved."); qc.invalidateQueries({ queryKey: ["admin-tenant", tenantId] }); },
    onError: (e: Error) => setMsg(e.message),
  });
  const parsed = value.trim() === "" ? null : Number(value);
  const valid = parsed === null || (Number.isInteger(parsed) && parsed >= 0);
  return (
    <form className="toolbar" style={{ marginTop: 12, gap: 8, flexWrap: "wrap" }}
      onSubmit={(e) => { e.preventDefault(); if (valid) save.mutate(parsed); }}>
      <label className="small" htmlFor="ai-limit">Monthly AI credit limit</label>
      <input id="ai-limit" inputMode="numeric" placeholder="No limit" value={value} style={{ width: 120 }}
        onChange={(e) => { setValue(e.target.value); setMsg(null); }} aria-invalid={!valid} />
      <Button className="secondary" disabled={!valid} busy={save.isPending} busyLabel="Saving the limit">Save limit</Button>
      <span className="muted small">Leave blank for no limit. 0 turns AI off for this workspace.</span>
      {!valid && <span className="error small">Whole number, 0 or more.</span>}
      {msg && <span className="small" role="status">{msg}</span>}
    </form>
  );
}
