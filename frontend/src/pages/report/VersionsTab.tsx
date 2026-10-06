import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, formatDateTime, post, useTenantRole, VERSION_STATUS_LABEL, type Report } from "../../api";
import { useMe } from "../../App";
import { Button } from "../../components/Spinner";
import { STATUS_CLASS } from "./ReportPage";

export default function VersionsTab({ tenantId, report }: { tenantId: string; report: Report }) {
  const me = useMe().data;
  const { isAdmin } = useTenantRole(me, tenantId);
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [err, setErr] = useState<string | null>(null);
  const r = useQuery({ queryKey: ["report", report.id], queryFn: () => api<Report>(`/api/tenants/${tenantId}/reports/${report.id}`) });
  const done = () => { qc.invalidateQueries({ queryKey: ["report", report.id] }); qc.invalidateQueries({ queryKey: ["reports", tenantId] }); };
  const rollback = useMutation({
    mutationFn: (vid: string) => post(`/api/tenants/${tenantId}/reports/${report.id}/rollback`, { version_id: vid }),
    onSuccess: done, onError: (e: Error) => setErr(e.message),
  });
  const draft = useMutation({
    mutationFn: (vid: string) => post<{ id: string }>(`/api/tenants/${tenantId}/reports/${report.id}/versions/${vid}/draft`),
    onSuccess: (nv) => { done(); navigate(`/t/${tenantId}/reports/${report.id}?v=${nv.id}&tab=status`); },
    onError: (e: Error) => setErr(e.message),
  });
  const versions = r.data?.versions ?? report.versions ?? [];
  return (
    <div className="stack">
      <p className="muted">Every upload and every change after publishing creates a new version. Published versions never change. Rolling back makes an earlier published version live again.</p>
      {err && <p className="error" role="alert">{err}</p>}
      <table>
        <thead><tr><th scope="col">Version</th><th scope="col">Status</th><th scope="col">Created</th><th scope="col">Published</th><th scope="col">Checks</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
        <tbody>
          {versions.map((v) => (
            <tr key={v.id}>
              <td><button className="link" onClick={() => navigate(`/t/${tenantId}/reports/${report.id}?v=${v.id}`)}>v{v.version_no}</button>{v.created_from_version_id && <div className="muted small">copied from an earlier version</div>}</td>
              <td><span className={`badge ${STATUS_CLASS[v.status]}`}>{VERSION_STATUS_LABEL[v.status]}</span>{v.is_live && <span className="badge ok">Live</span>}</td>
              <td className="nowrap small">{formatDateTime(v.created_at)}</td>
              <td className="nowrap small">{formatDateTime(v.published_at)}</td>
              <td className="small">{v.validation ? `${v.validation.blocking} blocking · ${v.validation.warnings} warnings` : "—"}</td>
              <td className="right nowrap">
                {isAdmin && v.published_at && !v.is_live && (
                  <Button className="link" disabled={rollback.isPending} busy={rollback.isPending && rollback.variables === v.id} busyLabel="Rolling back" onClick={() => confirm(`Make v${v.version_no} live again?`) && rollback.mutate(v.id)}>Roll back to this</Button>
                )}
                {isAdmin && v.schema_sha256 && v.published_at && (
                  <> {" "}<Button className="link" disabled={draft.isPending} busy={draft.isPending && draft.variables === v.id} busyLabel="Creating a draft" onClick={() => draft.mutate(v.id)}>New draft from this</Button></>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small">SHA-256 of each version's source PDF and generated files is recorded in the audit log when it's published.</p>
    </div>
  );
}
