import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api, post, REPORT_TYPE_LABEL, ROLE_LABEL, type Job, type Report, type Role } from "../api";
import JobTable from "../components/JobTable";

interface Detail {
  tenant: { id: string; slug: string; name: string; status: string; created_at: string };
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
  if (q.isPending) return <p className="muted">Loading…</p>;
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
      <JobTable jobs={d.jobs} admin onRerun={(jid) => rerun.mutate(jid)} />
    </>
  );
}
