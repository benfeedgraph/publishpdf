import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, CHECK_LABEL } from "../api";

interface Resp {
  by_check: { check: string; blocking: number; warning: number }[];
  versions: { tenant: string; tenant_id: string; version_id: string; version_no: number; report: string; status: string; blocking: number; warning: number }[];
}

export default function AdminIssues() {
  const q = useQuery({ queryKey: ["admin-issues"], queryFn: () => api<Resp>("/api/admin/issues"), refetchInterval: 10000 });
  if (q.isPending) return <p className="muted">Loading…</p>;
  if (q.isError) return <p className="error">{q.error.message}</p>;
  return (
    <>
      <h1>Validation quality</h1>
      <p className="muted">Open issues on each version's latest validation run, across all tenants. A spike in one check usually points at an extraction problem worth fixing centrally.</p>
      <div className="grid2">
        <div className="card">
          <h2>By check</h2>
          <table>
            <thead><tr><th scope="col">Check</th><th scope="col">Blocking</th><th scope="col">Warnings</th></tr></thead>
            <tbody>{q.data.by_check.map((c) => <tr key={c.check}><td>{CHECK_LABEL[c.check] ?? c.check}</td><td>{c.blocking}</td><td>{c.warning}</td></tr>)}</tbody>
          </table>
          {q.data.by_check.length === 0 && <p className="muted">No open issues.</p>}
        </div>
        <div className="card">
          <h2>By report version</h2>
          <table>
            <thead><tr><th scope="col">Tenant</th><th scope="col">Report</th><th scope="col">Blocking</th><th scope="col">Warnings</th></tr></thead>
            <tbody>{q.data.versions.map((v) => (
              <tr key={v.version_id}><td><Link to={`/admin/tenants/${v.tenant_id}`}>{v.tenant}</Link></td><td className="small">{v.report} v{v.version_no}</td><td>{v.blocking}</td><td>{v.warning}</td></tr>
            ))}</tbody>
          </table>
        </div>
      </div>
    </>
  );
}
