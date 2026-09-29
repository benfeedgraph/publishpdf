import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, del, put, REPORT_TYPE_LABEL, useTenantRole, VERSION_STATUS_LABEL, type Report, type VersionDetail } from "../../api";
import { useMe } from "../../App";
import DesignPicker, { type ThemeT } from "../../components/DesignPicker";
import PreviewTab, { PublishPanel } from "./PreviewTab";
import SchemaTab from "./SchemaTab";
import ValidationTab from "./ValidationTab";
import VerifyStep from "./VerifyStep";
import VersionsTab from "./VersionsTab";

type Step = "verify" | "design" | "review" | "publish";
export const STATUS_CLASS: Record<string, string> = {
  processing: "warn", failed: "bad", needs_review: "info", validation_issues: "bad", published: "ok", superseded: "",
};

export default function ReportPage() {
  const { tenantId, reportId } = useParams();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const me = useMe().data;
  const { isAdmin } = useTenantRole(me, tenantId);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const remove = useMutation({
    mutationFn: () => del(`/api/tenants/${tenantId}/reports/${reportId}`),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ["reports", tenantId] });
      navigate(`/t/${tenantId}`);
    },
    onError: (e: Error) => setDeleteError(e.message),
  });
  const report = useQuery({
    queryKey: ["report", reportId],
    queryFn: () => api<Report>(`/api/tenants/${tenantId}/reports/${reportId}`),
    refetchInterval: (q) => (q.state.data?.versions?.[0]?.status === "processing" ? 2500 : false),
  });
  const versionId = params.get("v") ?? report.data?.versions?.[0]?.id;
  const version = useQuery({
    queryKey: ["version", versionId],
    queryFn: () => api<VersionDetail>(`/api/tenants/${tenantId}/reports/${reportId}/versions/${versionId}`),
    enabled: !!versionId,
    refetchInterval: (q) => (q.state.data?.status === "processing" ? 1500 : false),
  });
  const v = version.data;
  const blocked = !!v && (v.status === "validation_issues" || v.status === "failed");
  const defaultStep: Step = !v || v.status === "processing" || blocked ? "verify" : v.published_at ? "publish" : "review";
  const step = (params.get("step") as Step) || defaultStep;
  const go = (s: Step) => { const n = new URLSearchParams(params); n.set("step", s); if (versionId) n.set("v", versionId); n.delete("tab"); setParams(n); window.scrollTo(0, 0); };

  if (report.isPending) return <p className="muted">Loading…</p>;
  if (report.isError) return <p className="error">{report.error.message}</p>;
  const r = report.data;
  const verifyDone = !!v && !["processing", "failed", "validation_issues"].includes(v.status);
  const steps: { key: Step; title: string; sub: string; state: "done" | "blocked" | "" }[] = [
    { key: "verify", title: "Extract & verify", sub: v?.status === "processing" ? "In progress…" : blocked ? `${v?.open_blocking ?? ""} to resolve` : "All figures checked", state: verifyDone ? "done" : blocked ? "blocked" : "" },
    { key: "design", title: "Design", sub: r.theme_override ? "Custom design" : "Workspace design", state: r.theme_override ? "done" : "" },
    { key: "review", title: "Review page", sub: "Check it against the PDF", state: v?.published_at ? "done" : "" },
    { key: "publish", title: "Approve & publish", sub: r.live_version_id ? "Live" : "Not published", state: r.live_version_id ? "done" : "" },
  ];

  return (
    <>
      <p className="crumbs"><Link to={`/t/${tenantId}`}>Reports</Link> / {r.period_label}</p>
      <div className="page-head">
        <div>
          <h1>{r.period_label} {REPORT_TYPE_LABEL[r.report_type]}</h1>
          <p className="muted small">{r.company_name} · {r.currency} · {r.reporting_unit}{v?.page_count ? ` · ${v.page_count} pages` : ""}</p>
        </div>
        <div className="btn-row">
          {r.versions && r.versions.length > 1 && (
            <label className="field inline">
              <span className="label">Version</span>
              <select value={versionId} onChange={(e) => { const n = new URLSearchParams(params); n.set("v", e.target.value); n.delete("step"); setParams(n); }}>
                {r.versions.map((x) => <option key={x.id} value={x.id}>v{x.version_no} · {VERSION_STATUS_LABEL[x.status]}{x.is_live ? " (live)" : ""}</option>)}
              </select>
            </label>
          )}
          {isAdmin && (
            <button className="secondary danger" type="button" disabled={remove.isPending} onClick={() => {
              const name = `${r.period_label} ${REPORT_TYPE_LABEL[r.report_type]}`;
              const live = r.live_version_id ? " It will also come off your public site." : "";
              if (confirm(`Delete ${name}?${live} The audit log keeps a record of this.`)) remove.mutate();
            }}>
              {remove.isPending ? "Deleting…" : "Delete report"}
            </button>
          )}
        </div>
      </div>
      {deleteError && <p className="error" role="alert">{deleteError}</p>}

      <nav className="stepper" aria-label="Steps">
        {steps.map((s, i) => (
          <button key={s.key} className={`step ${step === s.key ? "active" : ""} ${s.state}`} onClick={() => go(s.key)} aria-current={step === s.key ? "step" : undefined}>
            <span className="step-n">{s.state === "done" ? "✓" : s.state === "blocked" ? "!" : i + 1}</span>
            <span><span className="step-t">{s.title}</span><br /><span className="step-s">{s.sub}</span></span>
          </button>
        ))}
      </nav>

      {!v && <p className="muted">Loading…</p>}
      {v && step === "verify" && <VerifyStep tenantId={tenantId!} report={r} version={v} onContinue={() => go("design")} />}
      {v && step === "design" && <DesignStep tenantId={tenantId!} report={r} isAdmin={isAdmin} onDone={() => go("review")} />}
      {v && step === "review" && <ReviewStep tenantId={tenantId!} report={r} version={v} onContinue={() => go("publish")} />}
      {v && step === "publish" && <PublishStep tenantId={tenantId!} report={r} version={v} isAdmin={isAdmin} />}
    </>
  );
}

function DesignStep({ tenantId, report, isAdmin, onDone }: { tenantId: string; report: Report; isAdmin: boolean; onDone: () => void }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [err, setErr] = useState<string | null>(null);
  const settings = useQuery({ queryKey: ["settings", tenantId], queryFn: () => api<{ theme: ThemeT }>(`/api/tenants/${tenantId}/settings`) });
  const apply = useMutation({
    mutationFn: async ({ theme, makeDefault, mode }: { theme: ThemeT; makeDefault: boolean; mode: string }) => {
      if (makeDefault) await put(`/api/tenants/${tenantId}/theme`, { mode, theme });
      const { logo: _logo, ...override } = theme;
      return put<{ version_id: string | null }>(`/api/tenants/${tenantId}/reports/${report.id}/theme`, { theme_override: override });
    },
    onSuccess: async (res) => {
      await Promise.all(["report", "version", "settings", "reports"].map((k) => qc.invalidateQueries({ queryKey: [k] })));
      if (res.version_id) navigate(`/t/${tenantId}/reports/${report.id}?v=${res.version_id}&step=review`);
      else onDone();
    },
    onError: (e: Error) => setErr(e.message),
  });
  if (!settings.data) return <p className="muted">Loading…</p>;
  const current = { ...settings.data.theme, ...(report.theme_override as Partial<ThemeT> | null ?? {}) } as ThemeT;
  if (!isAdmin) return <p className="callout info">Only workspace admins can change the design. You can review the page in the next step.</p>;
  return (
    <div className="stack">
      <p className="muted" style={{ margin: 0 }}>Choose where the look of this page should come from. Only colours, fonts and spacing change — the figures, tables and structure stay exactly the same.</p>
      {err && <p className="error">{err}</p>}
      <DesignPicker tenantId={tenantId} reportId={report.id} current={current} busy={apply.isPending}
        applyLabel="Use this design and rebuild the page" onApply={(theme, opts) => apply.mutate({ theme, ...opts })} />
    </div>
  );
}

function ReviewStep({ tenantId, report, version, onContinue }: { tenantId: string; report: Report; version: VersionDetail; onContinue: () => void }) {
  const [tab, setTab] = useState<"page" | "issues" | "data">("page");
  return (
    <div className="stack">
      <div className="toolbar" style={{ justifyContent: "space-between" }}>
        <div className="seg" role="tablist" aria-label="Review">
          <button className={tab === "page" ? "active" : ""} onClick={() => setTab("page")}>Web page</button>
          <button className={tab === "issues" ? "active" : ""} onClick={() => setTab("issues")}>Checks & issues{version.open_blocking ? ` (${version.open_blocking})` : ""}</button>
          <button className={tab === "data" ? "active" : ""} onClick={() => setTab("data")}>Extracted data</button>
        </div>
        <button className="primary" onClick={onContinue}>Continue to approve →</button>
      </div>
      {version.status === "processing" && <div className="callout info">Rebuilding the page… this view updates when it's ready.</div>}
      {tab === "page" && <PreviewTab tenantId={tenantId} report={report} version={version} hidePublish />}
      {tab === "issues" && <ValidationTab tenantId={tenantId} report={report} version={version} />}
      {tab === "data" && <SchemaTab tenantId={tenantId} report={report} version={version} />}
    </div>
  );
}

function PublishStep({ tenantId, report, version, isAdmin }: { tenantId: string; report: Report; version: VersionDetail; isAdmin: boolean }) {
  const settings = useQuery({ queryKey: ["settings", tenantId], queryFn: () => api<{ effective_disclaimer: string | null; site_origin: string }>(`/api/tenants/${tenantId}/settings`) });
  return (
    <div className="stack">
      <PublishPanel tenantId={tenantId} report={report} version={version} isAdmin={isAdmin}
        disclaimer={settings.data?.effective_disclaimer ?? null} siteOrigin={settings.data?.site_origin} />
      <div className="card">
        <h2>Version history</h2>
        <VersionsTab tenantId={tenantId} report={report} />
      </div>
    </div>
  );
}
