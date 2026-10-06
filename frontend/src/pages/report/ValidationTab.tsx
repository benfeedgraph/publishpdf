import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, CHECK_LABEL, post, useTenantRole, type Issue, type Report, type VersionDetail } from "../../api";
import { useMe } from "../../App";
import PdfCrop from "../../components/PdfCrop";
import PdfRegion from "../../components/PdfRegion";
import { Button, Loading, SkeletonRows, Spinner } from "../../components/Spinner";

interface IssuesResp {
  run: number;
  summary: VersionDetail["validation"];
  validated_current: boolean;
  status: string;
  issues: Issue[];
}

export default function ValidationTab({ tenantId, report, version, embedded = false }: { tenantId: string; report: Report; version: VersionDetail; embedded?: boolean }) {
  const me = useMe().data;
  const { isAdmin } = useTenantRole(me, tenantId);
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const base = `/api/tenants/${tenantId}/reports/${report.id}/versions/${version.id}`;
  // Open on what blocks publishing; warnings are one filter away.
  const [severity, setSeverity] = useState("blocking");
  const [status, setStatus] = useState("open");
  const [page, setPage] = useState("");
  const [section, setSection] = useState("");
  const [check, setCheck] = useState("");
  const q = useQuery({
    queryKey: ["issues", version.id, version.status, version.schema_sha256],
    queryFn: () => api<IssuesResp>(`${base}/issues`),
    refetchInterval: version.status === "processing" ? 2500 : false,
  });
  const issues = useMemo(() => (q.data?.issues ?? []).filter((i) =>
    (!severity || i.severity === severity) && (!status || i.status === status) && (!page || String(i.page) === page)
    && (!section || i.section_id === section) && (!check || i.check === check)), [q.data, severity, status, page, section, check]);
  const selectedId = params.get("issue");
  const selected = issues.find((i) => i.id === selectedId) ?? issues[0] ?? null;
  const select = (id: string) => { const n = new URLSearchParams(params); n.set("issue", id); setParams(n, { replace: true }); };

  // Items a person can decide in bulk: a value tied to a spot in the PDF. Not a page-build
  // fault or a colleague's flag (those need their own answer), and not a completeness note
  // ("1 of 17 numbers here weren't captured") — there is no value in it to confirm.
  const bulkable = useMemo(() => issues.filter((i) => i.status === "open" && i.page && i.bbox
    && !["rendered_page", "schema", "reviewer_flag", "completeness"].includes(i.check)), [issues]);
  const [view, setView] = useState<"grid" | "list" | null>(null);
  const mode = view ?? (bulkable.length > 12 ? "grid" : "list");

  const pagesWithIssues = [...new Set((q.data?.issues ?? []).map((i) => i.page).filter(Boolean))].sort((a, b) => a! - b!);
  const checks = [...new Set((q.data?.issues ?? []).map((i) => i.check))];
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["issues", version.id] });
    qc.invalidateQueries({ queryKey: ["version", version.id] });
    qc.invalidateQueries({ queryKey: ["report", report.id] });
  };

  if (version.status === "processing" && !q.data?.issues.length) return <Loading label="Checks are running">Checks are running — this page updates automatically.</Loading>;
  if (q.isPending) return <SkeletonRows label="Loading issues" />;
  if (q.isError) return <p className="error">{q.error.message}</p>;
  const s = q.data.summary;

  return (
    <div className="stack">
      {s && !embedded && (
        <div className="stat-row">
          <div className="stat"><span className="stat-n">{s.figures_checked}</span><span className="stat-l">figures checked</span></div>
          <div className="stat"><span className="stat-n ok-text">{s.passed}</span><span className="stat-l">passed</span></div>
          <div className="stat"><span className="stat-n muted">{s.warnings}</span><span className="stat-l">warnings</span></div>
          <div className="stat"><span className={`stat-n ${s.blocking ? "error" : ""}`}>{s.blocking}</span><span className="stat-l">blocking</span></div>
        </div>
      )}
      {version.status === "processing" && <p className="callout info small row" style={{ gap: 8 }}><Spinner label="Re-checking" /> Re-checking after your changes — results below may be from the previous run.</p>}
      {!embedded && s && s.blocking === 0 && version.status === "needs_review" && (
        <p className="callout ok">No blocking issues. Review the page in <strong>Preview & publish</strong>.</p>
      )}
      <div className="filters">
        <select aria-label="Severity" value={severity} onChange={(e) => setSeverity(e.target.value)}><option value="">All severities</option><option value="blocking">Blocking</option><option value="warning">Warnings</option></select>
        <select aria-label="Status" value={status} onChange={(e) => setStatus(e.target.value)}><option value="open">Open</option><option value="resolved">Resolved</option><option value="">All</option></select>
        <select aria-label="Check" value={check} onChange={(e) => setCheck(e.target.value)}><option value="">All checks</option>{checks.map((c) => <option key={c} value={c}>{CHECK_LABEL[c] ?? c}</option>)}</select>
        <select aria-label="Page" value={page} onChange={(e) => setPage(e.target.value)}><option value="">All pages</option>{pagesWithIssues.map((p) => <option key={p} value={String(p)}>Page {p}</option>)}</select>
        <select aria-label="Section" value={section} onChange={(e) => setSection(e.target.value)}><option value="">All sections</option>{version.sections.map((x) => <option key={x.id} value={x.id}>{x.heading_text || x.type}</option>)}</select>
        <span className="muted small">{issues.length} shown</span>
        {bulkable.length > 0 && (
          <span className="seg" role="group" aria-label="Review mode">
            <button className={mode === "grid" ? "on" : ""} onClick={() => setView("grid")}>Side by side</button>
            <button className={mode === "list" ? "on" : ""} onClick={() => setView("list")}>One at a time</button>
          </span>
        )}
      </div>
      {mode === "grid" && bulkable.length > 0 ? (
        <ReviewGrid key={`${page}|${check}|${section}|${severity}`} items={bulkable} base={base} version={version}
          canDecide={isAdmin && version.published_at === null} onDone={refresh} />
      ) : issues.length === 0 ? (
        <p className="muted">No issues match these filters.</p>
      ) : (
        <div className="split issues-split">
          <ul className="issue-list" aria-label="Issues">
            {issues.map((i) => (
              <li key={i.id}>
                <button className={`issue ${selected?.id === i.id ? "sel" : ""} ${i.status}`} onClick={() => select(i.id)}>
                  <span className={`badge ${i.severity === "blocking" ? "bad" : "warn"}`}>{i.severity}</span>{" "}
                  <span className="small muted">{CHECK_LABEL[i.check] ?? i.check}{i.page ? ` · p${i.page}` : ""}</span>
                  <div className="issue-msg">{i.figure?.raw ? <code>{i.figure.raw}</code> : null} {i.message}</div>
                  {i.status === "resolved" && <div className="small ok-text">Resolved: {i.resolution}</div>}
                </button>
              </li>
            ))}
          </ul>
          {selected && <IssueDetail key={selected.id} issue={selected} base={base} version={version} isAdmin={isAdmin} onDone={refresh} />}
        </div>
      )}
    </div>
  );
}

function IssueDetail({ issue, base, version, isAdmin, onDone }: { issue: Issue; base: string; version: VersionDetail; isAdmin: boolean; onDone: () => void }) {
  const [value, setValue] = useState(issue.figure?.raw ?? "");
  const [note, setNote] = useState("");
  const [editing, setEditing] = useState(false);
  const [flagNote, setFlagNote] = useState("");
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { setErr(null); }, [issue.id]);
  const act = useMutation({
    mutationFn: (body: { action: string; value?: string; note?: string }) => post(`${base}/issues/${issue.id}/resolve`, body),
    onSuccess: onDone,
    onError: (e: Error) => setErr(e.message),
  });
  const flag = useMutation({
    mutationFn: () => post(`${base}/flags`, { fid: issue.fid, section_id: issue.section_id, note: flagNote }),
    onSuccess: () => { setFlagNote(""); onDone(); },
    onError: (e: Error) => setErr(e.message),
  });
  const page = version.pages.find((p) => p.page === issue.page);
  const platform = issue.check === "rendered_page" || issue.check === "schema";
  const readonly = version.published_at !== null || issue.status !== "open";

  return (
    <aside className="card sticky issue-detail" aria-label="Issue detail">
      <p className="small muted">{CHECK_LABEL[issue.check] ?? issue.check} · {issue.severity}{issue.page ? ` · page ${issue.page}` : ""}</p>
      <h2 className="issue-title">{issue.message}</h2>
      <div className="side-by-side">
        <div>
          <div className="label">In the PDF</div>
          {page && issue.page ? (
            <PdfRegion src={`${base}/pages/${issue.page}.png?zoom=2`} pageWidth={page.width} pageHeight={page.height} bbox={issue.bbox} label={`Page ${issue.page} of the PDF`} />
          ) : <p className="muted small">Not tied to one place in the PDF.</p>}
        </div>
        <div>
          <div className="label">Extracted</div>
          <dl className="kv">
            {issue.figure && <><dt>Value</dt><dd><code className="big">{issue.figure.raw}</code></dd></>}
            {issue.expected != null && <><dt>Expected</dt><dd><code>{issue.expected}</code></dd></>}
            {issue.actual != null && <><dt>Found</dt><dd><code>{issue.actual}</code></dd></>}
            {issue.figure?.row_label && <><dt>Row</dt><dd>{issue.figure.row_label}</dd></>}
            {issue.figure?.col_label && <><dt>Column</dt><dd>{issue.figure.col_label}</dd></>}
            {issue.figure?.method && <><dt>Read by</dt><dd>{issue.figure.method === "ocr" ? "OCR" : "text layer"} · {Math.round((issue.figure.confidence ?? 1) * 100)}%</dd></>}
            {issue.figure?.edited && <><dt>Edited</dt><dd>originally <code>{issue.figure.edited.original_raw}</code></dd></>}
          </dl>
        </div>
      </div>
      {err && <p className="error" role="alert">{err}</p>}
      {platform && <p className="callout bad small">This is a problem with the generated page, not with a figure, so it can't be waived. Re-run the page build; if it persists, contact support.</p>}
      {!readonly && !platform && isAdmin && (
        <div className="actions">
          <label className="field"><span className="label">Note (optional, saved in the audit trail)</span><input value={note} onChange={(e) => setNote(e.target.value)} /></label>
          {editing && issue.fid ? (
            <form className="inline-form" onSubmit={(e) => { e.preventDefault(); act.mutate({ action: "edit", value, note }); }}>
              <input aria-label="Corrected value exactly as printed" value={value} onChange={(e) => setValue(e.target.value)} autoFocus />
              <Button className="primary" busy={act.isPending} busyLabel="Saving the value">Save value</Button>
              <button type="button" className="link" onClick={() => setEditing(false)}>Cancel</button>
            </form>
          ) : (
            <div className="btn-row">
              <Button className="primary" busy={act.isPending && act.variables?.action === "confirm"} disabled={act.isPending} busyLabel="Confirming" onClick={() => act.mutate({ action: "confirm", note })}>Confirm correct</Button>
              {issue.fid && <button className="secondary" disabled={act.isPending} onClick={() => setEditing(true)}>Edit value</button>}
              {issue.fid && <Button className="secondary" busy={act.isPending && act.variables?.action === "not_a_figure"} disabled={act.isPending} busyLabel="Saving" onClick={() => act.mutate({ action: "not_a_figure", note })}>Mark as not a figure</Button>}
            </div>
          )}
          <p className="muted small">Every action is logged with your name, the time and the old and new value, and the checks run again.</p>
        </div>
      )}
      {!readonly && !isAdmin && (
        <form className="actions" onSubmit={(e) => { e.preventDefault(); flag.mutate(); }}>
          <label className="field"><span className="label">Flag this for an admin</span><input required minLength={3} value={flagNote} onChange={(e) => setFlagNote(e.target.value)} placeholder="What looks wrong?" /></label>
          <Button className="secondary" busy={flag.isPending} busyLabel="Sending the flag">Flag issue</Button>
        </form>
      )}
    </aside>
  );
}

const GRID_BATCH = 60;

/** Every flagged value on screen at once, each as a crop of the PDF next to what was read.
 * The reviewer scans the sheet, unticks anything that looks wrong, and confirms the rest
 * in one step — logged per figure, re-checked once. */
function ReviewGrid({ items, base, version, canDecide, onDone }: { items: Issue[]; base: string; version: VersionDetail; canDecide: boolean; onDone: () => void }) {
  const shown = items.slice(0, GRID_BATCH);
  const [off, setOff] = useState<Set<string>>(new Set());
  const [err, setErr] = useState<string | null>(null);
  const picked = shown.filter((i) => !off.has(i.id));
  const toggle = (id: string) => setOff((o) => { const n = new Set(o); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const bulk = useMutation({
    mutationFn: (action: "confirm" | "not_a_figure") => post(`${base}/issues/resolve-bulk`, { issue_ids: picked.map((i) => i.id), action }),
    onSuccess: () => { setOff(new Set()); onDone(); },
    onError: (e: Error) => setErr(e.message),
  });
  const pageOf = (n: number) => version.pages.find((p) => p.page === n);
  const run = (action: "confirm" | "not_a_figure") => {
    const what = action === "confirm" ? "match the PDF" : "are not figures";
    if (window.confirm(`Confirm that these ${picked.length} value(s) ${what}? Each one is logged under your name.`)) bulk.mutate(action);
  };

  return (
    <div className="stack">
      <div className="review-bar">
        <span><strong>{picked.length}</strong> of {shown.length} selected{items.length > shown.length ? ` · showing the first ${shown.length} of ${items.length} — pick a page above to work through the rest` : ""}</span>
        <span className="btn-row">
          <button className="link" onClick={() => setOff(new Set())}>Select all</button>
          <button className="link" onClick={() => setOff(new Set(shown.map((i) => i.id)))}>Select none</button>
          {canDecide && <Button className="secondary" disabled={!picked.length || bulk.isPending} busy={bulk.isPending && bulk.variables === "not_a_figure"} busyLabel="Saving" onClick={() => run("not_a_figure")}>Not figures</Button>}
          {canDecide && <Button className="primary" disabled={!picked.length || bulk.isPending} busy={bulk.isPending && bulk.variables === "confirm"} busyLabel="Saving" onClick={() => run("confirm")}>{`Confirm ${picked.length} correct`}</Button>}
        </span>
      </div>
      {err && <p className="error" role="alert">{err}</p>}
      <p className="muted small">Compare each highlighted spot with the value under it. Untick anything that doesn't match and open it in “One at a time” to correct it.</p>
      <ul className="review-grid" aria-label="Values to review">
        {shown.map((i) => {
          const pg = i.page ? pageOf(i.page) : undefined;
          const on = !off.has(i.id);
          return (
            <li key={i.id} className={`review-tile ${on ? "on" : ""} ${i.severity}`}>
              <label>
                <input type="checkbox" checked={on} onChange={() => toggle(i.id)} disabled={!canDecide} />
                <span className="small muted">p{i.page} · {CHECK_LABEL[i.check] ?? i.check}</span>
              </label>
              {pg && i.bbox && <PdfCrop src={`${base}/pages/${i.page}.png?zoom=2`} pageWidth={pg.width} bbox={i.bbox} label={`Page ${i.page}`} />}
              <div className="review-read">
                <span className="muted small">Read</span> <code className="big">{i.figure?.raw ?? i.expected ?? "—"}</code>
                {i.actual && i.actual !== i.figure?.raw && <span className="small muted"> · re-read: <code>{i.actual}</code></span>}
                {i.figure?.method === "ocr" && <span className="small muted"> · OCR {Math.round((i.figure.confidence ?? 1) * 100)}%</span>}
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
