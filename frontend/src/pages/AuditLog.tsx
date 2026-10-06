import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { api, formatDateTime } from "../api";
import { SkeletonRows } from "../components/Spinner";

interface Entry {
  id: number;
  at: string;
  actor: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
}

function Payload({ value }: { value: Record<string, unknown> | null }) {
  const [open, setOpen] = useState(false);
  if (!value) return <span className="muted">—</span>;
  const text = JSON.stringify(value, null, 2);
  const long = text.length > 180;
  return (
    <div className={`payload ${open ? "open" : ""}`}>
      <pre>{text}</pre>
      {long && (
        <button type="button" className="link" onClick={() => setOpen((v) => !v)}>
          {open ? "Show less" : "Show more"}
        </button>
      )}
    </div>
  );
}

export default function AuditLog() {
  const { tenantId } = useParams();
  const q = useQuery({
    queryKey: ["audit", tenantId],
    queryFn: () => api<{ entries: Entry[] }>(`/api/tenants/${tenantId}/audit?limit=200`),
  });
  return (
    <>
      <h1>Audit log</h1>
      <p className="muted">Every upload, figure edit, approval, publish, and team, domain and analytics change. Entries can't be edited or deleted.</p>
      {q.isPending && <SkeletonRows rows={8} label="Loading the audit log" />}
      {q.isError && <p className="error">{q.error.message}</p>}
      {q.data && q.data.entries.length === 0 && <p className="muted">No activity yet.</p>}
      {q.data && q.data.entries.length > 0 && (
        <div className="table-scroll">
          <table className="audit">
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">Who</th>
                <th scope="col">Action</th>
                <th scope="col">Before</th>
                <th scope="col">After</th>
              </tr>
            </thead>
            <tbody>
              {q.data.entries.map((e) => (
                <tr key={e.id}>
                  <td className="nowrap">{formatDateTime(e.at)}</td>
                  <td className="wrap">{e.actor ?? "System"}</td>
                  <td className="wrap"><code>{e.action}</code></td>
                  <td><Payload value={e.before} /></td>
                  <td><Payload value={e.after} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
