import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api, formatDateTime, REPORT_TYPE_LABEL, useTenantRole, type Report } from "../api";
import { useMe } from "../App";
import { Dropzone } from "../components/UploadFlow";
import { cachedReports, rememberReports } from "../session";

const STATUS_CLASS: Record<string, string> = {
  Live: "ok", "Needs review": "info", "Validation issues": "bad", Processing: "warn", Failed: "bad", "Draft changes": "info",
};

function progress(r: Report): ("done" | "bad" | "")[] {
  const v = r.latest_version;
  const s = v?.status;
  return [
    "done",                                                             // uploaded
    s && s !== "processing" ? (s === "failed" ? "bad" : "done") : "",   // extracted & checked
    s === "needs_review" || s === "published" || r.live_version_id ? "done" : s === "validation_issues" ? "bad" : "",
    r.live_version_id ? "done" : "",                                    // live
  ];
}

export default function Home() {
  const { tenantId } = useParams();
  const me = useMe().data;
  const { isAdmin } = useTenantRole(me, tenantId);
  const reports = useQuery({
    queryKey: ["reports", tenantId],
    queryFn: async () => {
      const data = await api<{ reports: Report[] }>(`/api/tenants/${tenantId}/reports`);
      rememberReports(tenantId, data);
      return data;
    },
    // Last visit's list shows at once; it's marked stale so the server is asked right away.
    initialData: () => cachedReports<{ reports: Report[] }>(tenantId),
    initialDataUpdatedAt: 0,
    refetchInterval: (q) => (q.state.data?.reports.some((r) => r.status === "Processing") ? 2500 : false),
  });
  const domain = useQuery({
    queryKey: ["domain", tenantId],
    queryFn: () => api<{ domain: { hostname: string; status: string } | null; site_origin: string; custom_domain_live: boolean }>(`/api/tenants/${tenantId}/domain`),
  });
  const name = me?.user.name?.split(" ")[0];
  const list = reports.data?.reports ?? [];

  return (
    <div className="stack" style={{ gap: 24 }}>
      <div className="page-head" style={{ marginBottom: 0 }}>
        <div>
          <p className="eyebrow">{me?.tenants.find((t) => t.id === tenantId)?.name}</p>
          <h1>{name ? `Welcome, ${name}` : "Reports"}</h1>
          <p className="muted">Upload a report, pick a design, review what we built, then publish.</p>
        </div>
        {domain.data && (
          <a className="button secondary" href={domain.data.site_origin} target="_blank" rel="noreferrer">
            View your site ↗
          </a>
        )}
      </div>

      {isAdmin ? <Dropzone tenantId={tenantId!} compact={list.length > 0} /> : (
        <div className="callout info">You're a reviewer in this workspace: you can review reports, comment and flag issues. Ask an admin to upload new reports.</div>
      )}

      {reports.isPending && <p className="muted">Loading reports…</p>}
      {reports.isError && <p className="error">{reports.error.message}</p>}
      {list.length > 0 && (
        <section>
          <div className="toolbar" style={{ marginBottom: 12 }}>
            <h2 style={{ margin: 0 }}>Your reports</h2>
            <span className="muted small">{list.length}</span>
          </div>
          <div className="report-grid">
            {list.map((r) => {
              const v = r.latest_version;
              const steps = progress(r);
              return (
                <Link key={r.id} className="report-card" to={`/t/${tenantId}/reports/${r.id}`}>
                  <div className="rc-top">
                    <div className="rc-thumb">{v && v.schema_sha256 && <img loading="lazy" decoding="async" alt="" src={`/api/tenants/${tenantId}/reports/${r.id}/versions/${v.id}/thumb.webp`}
                      onError={(e) => { e.currentTarget.style.display = "none"; }} />}</div>
                    <div style={{ minWidth: 0 }}>
                      <div className="rc-title">{r.period_label} {REPORT_TYPE_LABEL[r.report_type]}</div>
                      <div className="muted small" style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{r.company_name}</div>
                      <div style={{ marginTop: 6 }}><span className={`badge ${STATUS_CLASS[r.status] ?? ""}`}>{r.status}</span></div>
                    </div>
                  </div>
                  <div className="rc-steps" aria-label="Progress: uploaded, checked, ready to publish, live">
                    {steps.map((s, i) => <span key={i} className={s} />)}
                  </div>
                  <div className="rc-foot">
                    <span>
                      {r.status === "Processing" && v?.stage ? v.stage.replace(/^(\w+):\s*/, "") :
                        v?.validation ? `${v.validation.passed.toLocaleString()} of ${v.validation.figures_checked.toLocaleString()} figures verified` : "—"}
                    </span>
                    <span>{formatDateTime(r.updated_at).split(",")[0]}</span>
                  </div>
                </Link>
              );
            })}
          </div>
        </section>
      )}
    </div>
  );
}
