import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, del, put, REPORT_TYPE_LABEL, useTenantRole, VERSION_STATUS_LABEL, type Report, type VersionDetail } from "../../api";
import { useMe } from "../../App";
import DesignPicker, { type DesignDraft, type ThemeT } from "../../components/DesignPicker";
import PreviewTab, { PublishPanel } from "./PreviewTab";
import SchemaTab from "./SchemaTab";
import ValidationTab from "./ValidationTab";
import VerifyStep from "./VerifyStep";
import VersionsTab from "./VersionsTab";
import { IconCheck } from "../../components/Icons";
import { Button, Loading, Spinner } from "../../components/Spinner";
import WizardFooter from "./WizardFooter";

type Step = "verify" | "design" | "review" | "publish";
const STEP_ORDER: Step[] = ["verify", "design", "review", "publish"];
const STEP_HELP: Record<Step, string> = {
  verify: "we read every figure in the PDF and check it; anything flagged needs your decision.",
  design: "keep your workspace design or choose a new look for this page.",
  review: "compare the web page with the PDF before you approve it.",
  publish: "approve the page and put it live.",
};
export const STEP_TITLE: Record<Step, string> = { verify: "Extract & verify", design: "Design", review: "Review page", publish: "Approve & publish" };
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

  if (report.isPending) return <><div className="sk sk-title" /><div className="sk sk-line" /><div className="sk sk-block" /></>;
  if (report.isError) return <p className="error">{report.error.message}</p>;
  const r = report.data;
  const verifyDone = !!v && !["processing", "failed", "validation_issues"].includes(v.status);
  const at = STEP_ORDER.indexOf(step);
  // Design is optional: once you've moved past it, the workspace design is the choice.
  const steps: { key: Step; sub: string; state: "done" | "blocked" | "" }[] = [
    { key: "verify", sub: v?.status === "processing" ? "In progress" : blocked ? (v?.status === "failed" ? "Failed" : `${v?.open_blocking ?? ""} to resolve`) : "All figures checked", state: verifyDone ? "done" : blocked ? "blocked" : "" },
    { key: "design", sub: r.theme_override ? "Custom design" : "Workspace design", state: r.theme_override || (verifyDone && at > 1) ? "done" : "" },
    { key: "review", sub: "Check it against the PDF", state: v?.published_at || (verifyDone && at > 2) ? "done" : "" },
    { key: "publish", sub: v?.is_live ? "Live" : r.live_version_id ? "Older version live" : "Not published", state: v?.is_live ? "done" : "" },
  ];
  const back = at > 0 ? { label: STEP_TITLE[STEP_ORDER[at - 1]], onClick: () => go(STEP_ORDER[at - 1]) } : undefined;

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
                {r.versions.map((x) => <option key={x.id} value={x.id}>v{x.version_no} · {x.is_live ? "Live" : x.status === "published" ? "Published earlier" : VERSION_STATUS_LABEL[x.status]}</option>)}
              </select>
            </label>
          )}
          {isAdmin && (
            <Button className="secondary danger" type="button" busy={remove.isPending} busyLabel="Deleting" onClick={() => {
              const name = `${r.period_label} ${REPORT_TYPE_LABEL[r.report_type]}`;
              const live = r.live_version_id ? " It will also come off your public site." : "";
              if (confirm(`Delete ${name}?${live} The audit log keeps a record of this.`)) remove.mutate();
            }}>
              Delete report
            </Button>
          )}
        </div>
      </div>
      {deleteError && <p className="error" role="alert">{deleteError}</p>}

      <nav className="stepper" aria-label="Steps">
        {steps.map((s, i) => {
          const current = step === s.key;
          const said = current ? "current step" : s.state === "done" ? "done" : s.state === "blocked" ? "needs attention" : "not started";
          return (
            <button key={s.key} className={`step ${current ? "active" : ""} ${s.state}`} onClick={() => go(s.key)} aria-current={current ? "step" : undefined}>
              <span className={`step-n ${!current && !s.state && i > at ? "upcoming" : ""}`} aria-hidden>
                {s.state === "done" ? <IconCheck size={14} /> : s.state === "blocked" ? "!" : current && v?.status === "processing" && s.key === "verify" ? <Spinner decorative size={12} /> : i + 1}
              </span>
              <span><span className="step-t">{STEP_TITLE[s.key]}</span><span className="sr-only"> ({said})</span><br /><span className="step-s">{s.sub}</span></span>
            </button>
          );
        })}
      </nav>
      <div className="step-eyebrow">
        <p className="small"><span className="eyebrow">Step {at + 1} of {STEP_ORDER.length}</span> <strong>{STEP_TITLE[step]}</strong> — <span className="muted">{STEP_HELP[step]}</span></p>
      </div>

      {!v && <Loading label="Loading this version" />}
      {v && step === "verify" && <VerifyStep tenantId={tenantId!} report={r} version={v} onContinue={() => go("design")} />}
      {v && step === "design" && <DesignStep tenantId={tenantId!} report={r} isAdmin={isAdmin} back={back} onDone={() => go("review")} />}
      {v && step === "review" && <ReviewStep tenantId={tenantId!} report={r} version={v} back={back} onContinue={() => go("publish")} />}
      {v && step === "publish" && <PublishStep tenantId={tenantId!} report={r} version={v} isAdmin={isAdmin} back={back} />}
    </>
  );
}

type Back = { label: string; onClick: () => void } | undefined;

function DesignStep({ tenantId, report, isAdmin, back, onDone }: { tenantId: string; report: Report; isAdmin: boolean; back: Back; onDone: () => void }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [err, setErr] = useState<string | null>(null);
  // What the picker currently shows; "dirty" means it differs from the design already applied.
  const [draft, setDraft] = useState<DesignDraft | null>(null);
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
  // Memoised: the picker resets to `current` whenever it changes identity.
  const current = useMemo(() => settings.data && { ...settings.data.theme, ...(report.theme_override as Partial<ThemeT> | null ?? {}) } as ThemeT,
    [settings.data, report.theme_override]);
  const next = { label: "Next: Review page →", onClick: onDone };
  if (settings.isError) return <p className="error">{settings.error.message}</p>;
  if (!current) return <Loading label="Loading your design" />;
  if (!isAdmin) return (
    <>
      <p className="callout info">Only workspace admins can change the design. You can review the page in the next step.</p>
      <WizardFooter back={back} next={next} />
    </>
  );
  const changed = !!draft?.dirty;
  return (
    <div className="stack">
      <p className="muted" style={{ margin: 0 }}>Choose where the look of this page should come from. Only colours, fonts and spacing change — the figures, tables and structure stay exactly the same.</p>
      {err && <p className="error">{err}</p>}
      <DesignPicker tenantId={tenantId} reportId={report.id} current={current} busy={apply.isPending} hideApply onDraftChange={setDraft}
        applyLabel="Use this design and rebuild the page" onApply={(theme, opts) => apply.mutate({ theme, ...opts })} />
      <WizardFooter back={back}
        hint={changed ? "Your changes are applied when you continue — the page is rebuilt in the new design." : "Keeping the current design. Change anything above to use a new one."}
        next={changed
          ? { label: "Apply design & review →", busy: apply.isPending, busyLabel: "Applying the design", onClick: () => draft && apply.mutate({ theme: draft.theme, makeDefault: draft.makeDefault, mode: draft.mode }) }
          : next} />
    </div>
  );
}

function ReviewStep({ tenantId, report, version, back, onContinue }: { tenantId: string; report: Report; version: VersionDetail; back: Back; onContinue: () => void }) {
  const [tab, setTab] = useState<"page" | "issues" | "data">("page");
  const rebuilding = version.status === "processing";
  const open = version.open_blocking;
  return (
    <div className="stack">
      <div className="toolbar" style={{ justifyContent: "space-between" }}>
        <div className="seg" role="tablist" aria-label="Review">
          <button className={tab === "page" ? "active" : ""} onClick={() => setTab("page")}>Web page</button>
          <button className={tab === "issues" ? "active" : ""} onClick={() => setTab("issues")}>Checks & issues{version.open_blocking ? ` (${version.open_blocking})` : ""}</button>
          <button className={tab === "data" ? "active" : ""} onClick={() => setTab("data")}>Extracted data</button>
        </div>
      </div>
      {rebuilding && <div className="callout info row" style={{ gap: 10 }}><Spinner label="Rebuilding" /> Rebuilding the page — this view updates when it's ready.</div>}
      {tab === "page" && <PreviewTab tenantId={tenantId} report={report} version={version} hidePublish />}
      {tab === "issues" && <ValidationTab tenantId={tenantId} report={report} version={version} />}
      {tab === "data" && <SchemaTab tenantId={tenantId} report={report} version={version} />}
      <WizardFooter back={back}
        hint={tab === "page" ? "Looks right? Continue to approve and publish." : "When you're happy with the page, continue to approve and publish."}
        next={{
          label: "Next: Approve & publish →", onClick: onContinue,
          disabled: rebuilding || open > 0, waiting: rebuilding,
          reason: rebuilding ? "Rebuilding the page — this unlocks automatically when it's ready." : `Resolve ${open} blocking figure${open === 1 ? "" : "s"} first (Checks & issues).`,
        }} />
    </div>
  );
}

function PublishStep({ tenantId, report, version, isAdmin, back }: { tenantId: string; report: Report; version: VersionDetail; isAdmin: boolean; back: Back }) {
  const settings = useQuery({ queryKey: ["settings", tenantId], queryFn: () => api<{ effective_disclaimer: string | null; site_origin: string }>(`/api/tenants/${tenantId}/settings`) });
  const [copied, setCopied] = useState(false);
  const liveUrl = version.is_live && settings.data?.site_origin ? `${settings.data.site_origin}${report.path}` : null;
  const copy = async () => {
    if (!liveUrl) return;
    try { await navigator.clipboard.writeText(liveUrl); setCopied(true); window.setTimeout(() => setCopied(false), 2000); }
    catch { window.prompt("Copy the link to the live page:", liveUrl); }
  };
  return (
    <div className="stack">
      <PublishPanel tenantId={tenantId} report={report} version={version} isAdmin={isAdmin}
        disclaimer={settings.data?.effective_disclaimer ?? null} siteOrigin={settings.data?.site_origin} />
      <div className="card">
        <h2>Version history</h2>
        <VersionsTab tenantId={tenantId} report={report} />
      </div>
      {version.is_live ? (
        <WizardFooter back={back}
          hint={<>Your page is live. <Link to={`/t/${tenantId}`}>Back to all reports</Link></>}
          extra={liveUrl && <button type="button" className="secondary" onClick={copy} aria-live="polite">{copied ? <><IconCheck size={14} /> Copied</> : "Copy link"}</button>}
          next={liveUrl ? { label: "View live page ↗", href: liveUrl } : { label: "View live page ↗", disabled: true, waiting: true, reason: "Getting the page address…" }} />
      ) : (
        <WizardFooter back={back}
          hint={version.published_at ? "This version was replaced by a newer one. Roll back from Version history to make it live again."
            : !isAdmin ? "A workspace admin approves and publishes this page."
            : version.status === "processing" || version.open_blocking > 0 || !version.validated_current || (settings.data && !settings.data.effective_disclaimer)
              ? "Publishing unlocks once the items listed above are sorted."
              : "Last step: tick the review box above, then Approve and publish."} />
      )}
    </div>
  );
}
