import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError, CHECK_LABEL, post, useTenantRole, type Issue, type Report, type VersionDetail } from "../../api";
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

/** Decisions are refused only while the PDF itself is being re-read (that would overwrite
 *  them). During a page rebuild or re-check they're saved and the newest one wins. */
export const decisionsLocked = (v: VersionDetail) =>
  v.status === "processing" && !/^(render|validate)/.test(v.stage ?? "");

/** One figure can be flagged by several checks ("Needs a person" and "Independent re-reading"
 *  for the same misread value). It's one decision, so it's shown once. */
export interface IssueGroup { key: string; lead: Issue; all: Issue[] }
export function groupByFigure(items: Issue[]): IssueGroup[] {
  const out: IssueGroup[] = [];
  const byFid = new Map<string, IssueGroup>();
  for (const i of items) {
    const k = i.fid && i.status === "open" ? `fig:${i.fid}` : `issue:${i.id}`;
    const g = byFid.get(k);
    if (g) {
      g.all.push(i);
      if (g.lead.severity !== "blocking" && i.severity === "blocking") g.lead = i;
    } else {
      const ng = { key: k, lead: i, all: [i] };
      byFid.set(k, ng);
      out.push(ng);
    }
  }
  return out;
}

/** OCR'd headings can be noise ("gtaaliyy", "MM ENA"): only offer real-looking ones by name. */
export function plausibleHeading(text: string | null | undefined): boolean {
  const t = (text ?? "").trim();
  if (t.length < 4) return false;
  const letters = (t.match(/[A-Za-z]/g) ?? []).length;
  if (letters / t.length < 0.6) return false;
  const words = t.split(/\s+/).filter((w) => /[A-Za-z]/.test(w));
  if (!words.length) return false;
  // a run of short all-caps fragments ("MM ENA") reads as noise
  if (words.every((w) => w.length <= 3 && w === w.toUpperCase())) return false;
  const ok = (w: string) => {
    const x = w.toLowerCase().replace(/[^a-z]/g, "");
    if (x.length < 2) return true;
    const vowels = (x.match(/[aeiou]/g) ?? []).length;
    if (x.length >= 4 && (vowels / x.length < 0.2 || vowels / x.length > 0.75)) return false;
    if (/([a-z])\1\1/.test(x) || /(yy|ii|uu|qq|jj|vv|ww|xx|hh)$/.test(x)) return false;
    if (/[bcdfghjklmnpqrstvwxz]{5,}/.test(x)) return false;
    if (/^[bcdfghjklmnpqrstvwxz]{2}/.test(x) && !/^(bl|br|ch|cl|cr|dr|fl|fr|gl|gr|kn|ph|pl|pr|sc|sh|sk|sl|sm|sn|sp|st|sw|th|tr|tw|wh|wr|sch|scr|shr|spl|spr|str|thr)/.test(x)) return false;
    return true;
  };
  return words.filter(ok).length / words.length >= 0.75;
}

export default function ValidationTab({ tenantId, report, version, embedded = false }: { tenantId: string; report: Report; version: VersionDetail; embedded?: boolean }) {
  const me = useMe().data;
  const { isAdmin } = useTenantRole(me, tenantId);
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const base = `/api/tenants/${tenantId}/reports/${report.id}/versions/${version.id}`;
  // Open on what blocks publishing; warnings are one filter away.
  const [severity, setSeverity] = useState(version.open_blocking > 0 ? "blocking" : "");
  const [status, setStatus] = useState("open");
  const [page, setPage] = useState("");
  const [section, setSection] = useState("");
  const [check, setCheck] = useState("");
  const processing = version.status === "processing";
  const q = useQuery({
    queryKey: ["issues", version.id, version.status, version.schema_sha256],
    queryFn: () => api<IssuesResp>(`${base}/issues`),
    refetchInterval: processing ? 2500 : false,
    placeholderData: keepPreviousData,
  });
  const issues = useMemo(() => (q.data?.issues ?? []).filter((i) =>
    (!severity || i.severity === severity) && (!status || i.status === status) && (!page || String(i.page) === page)
    && (!section || i.section_id === section) && (!check || i.check === check)), [q.data, severity, status, page, section, check]);
  const groups = useMemo(() => groupByFigure(issues), [issues]);
  const selectedId = params.get("issue");
  const selected = groups.find((g) => g.all.some((i) => i.id === selectedId)) ?? groups[0] ?? null;
  const select = (id: string) => { const n = new URLSearchParams(params); n.set("issue", id); setParams(n, { replace: true }); };

  // Items a person can decide in bulk: a value tied to a spot in the PDF. Not a page-build
  // fault or a colleague's flag (those need their own answer), and not a completeness note
  // ("1 of 17 numbers here weren't captured") — there is no value in it to confirm.
  const bulkable = useMemo(() => groupByFigure(issues.filter((i) => i.status === "open" && i.page && i.bbox
    && !["rendered_page", "schema", "reviewer_flag", "completeness"].includes(i.check))), [issues]);
  const [view, setView] = useState<"grid" | "list" | null>(null);
  const mode = view ?? "grid";                    // Side by side is the default

  const all = q.data?.issues ?? [];
  const pagesWithIssues = [...new Set(all.map((i) => i.page).filter(Boolean))].sort((a, b) => a! - b!);
  const checks = [...new Set(all.map((i) => i.check))];
  // Only sections that actually have items, labelled so OCR noise can't pass for a heading.
  const sectionIds = new Set(all.map((i) => i.section_id).filter(Boolean));
  const sectionOptions = version.sections.filter((x) => sectionIds.has(x.id)).map((x) => ({
    id: x.id,
    label: plausibleHeading(x.heading_text)
      ? `${x.page ? `p${x.page} · ` : ""}${x.heading_text.length > 60 ? `${x.heading_text.slice(0, 57)}…` : x.heading_text}`
      : `Section on page ${x.page ?? "?"}`,
  }));
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["issues", version.id] });
    qc.invalidateQueries({ queryKey: ["version", version.id] });
    qc.invalidateQueries({ queryKey: ["report", report.id] });
  };

  if (processing && !q.data?.issues.length) return <Loading label="Checks are running">Checks are running — this page updates automatically.</Loading>;
  if (q.isPending) return <SkeletonRows label="Loading issues" />;
  if (!q.data) return <p className="error">{q.error?.message ?? "The issues couldn't be loaded."}</p>;
  const s = q.data.summary;
  const shownCount = mode === "grid" && bulkable.length > 0 ? bulkable.length : groups.length;

  return (
    <div className="stack">
      {s && !embedded && (
        <div className={`stat-row ${processing ? "is-stale" : ""}`} aria-busy={processing || undefined}>
          <div className="stat"><span className="stat-n">{s.figures_checked}</span><span className="stat-l">figures checked</span></div>
          <div className="stat"><span className="stat-n ok-text">{s.passed}</span><span className="stat-l">passed</span></div>
          <div className="stat"><span className="stat-n muted">{s.warnings}</span><span className="stat-l">warnings</span></div>
          <div className="stat"><span className={`stat-n ${s.blocking ? "error" : ""}`}>{processing ? "…" : s.blocking}</span><span className="stat-l">blocking{processing ? " · updating" : ""}</span></div>
        </div>
      )}
      {processing && <p className="callout info small row" style={{ gap: 8 }}><Spinner label="Re-checking" /> Re-checking after your changes. You can keep deciding — the newest decisions are always included.</p>}
      {!embedded && s && s.blocking === 0 && version.status === "needs_review" && (
        <p className="callout ok">No blocking issues. Review the page in <strong>Preview & publish</strong>.</p>
      )}
      {q.isError && <p className="muted small" role="status">Couldn't refresh the list just now — showing the last version; retrying.</p>}
      <div className="filters">
        <select aria-label="Severity" value={severity} onChange={(e) => setSeverity(e.target.value)}><option value="">All severities</option><option value="blocking">Blocking</option><option value="warning">Warnings</option></select>
        <select aria-label="Status" value={status} onChange={(e) => setStatus(e.target.value)}><option value="open">Open</option><option value="resolved">Resolved</option><option value="">All</option></select>
        <select aria-label="Check" value={check} onChange={(e) => setCheck(e.target.value)}><option value="">All checks</option>{checks.map((c) => <option key={c} value={c}>{CHECK_LABEL[c] ?? c}</option>)}</select>
        <select aria-label="Page" value={page} onChange={(e) => setPage(e.target.value)}><option value="">All pages</option>{pagesWithIssues.map((p) => <option key={p} value={String(p)}>Page {p}</option>)}</select>
        {sectionOptions.length > 0 && (
          <select aria-label="Section" value={section} onChange={(e) => setSection(e.target.value)}><option value="">All sections</option>{sectionOptions.map((x) => <option key={x.id} value={x.id}>{x.label}</option>)}</select>
        )}
        <span className="muted small">{shownCount} item{shownCount === 1 ? "" : "s"}</span>
        {bulkable.length > 0 && (
          <span className="seg" role="group" aria-label="Review mode">
            <button className={mode === "grid" ? "on" : ""} onClick={() => setView("grid")}>Side by side</button>
            <button className={mode === "list" ? "on" : ""} onClick={() => setView("list")}>One at a time</button>
          </span>
        )}
      </div>
      {mode === "grid" && bulkable.length > 0 ? (
        <ReviewGrid key={`${page}|${check}|${section}|${severity}`} groups={bulkable} base={base} version={version}
          canDecide={isAdmin && version.published_at === null} onDone={refresh}
          onOpen={(id) => { select(id); setView("list"); }} />
      ) : groups.length === 0 ? (
        <p className="muted">No issues match these filters.</p>
      ) : (
        <div className="split issues-split">
          <ul className="issue-list" aria-label="Issues">
            {groups.map(({ key, lead: i, all: dup }) => (
              <li key={key}>
                <button className={`issue ${selected?.key === key ? "sel" : ""} ${i.status}`} onClick={() => select(i.id)}>
                  <span className={`badge ${i.severity === "blocking" ? "bad" : "warn"}`}>{i.severity}</span>{" "}
                  <span className="small muted">{[...new Set(dup.map((d) => CHECK_LABEL[d.check] ?? d.check))].join(" + ")}{i.page ? ` · p${i.page}` : ""}</span>
                  <div className="issue-msg">{i.figure?.raw ? <code>{i.figure.raw}</code> : null} {i.message}</div>
                  {i.status === "resolved" && <div className="small ok-text">Resolved: {i.resolution}</div>}
                </button>
              </li>
            ))}
          </ul>
          {selected && <IssueDetail key={selected.key} issue={selected.lead} also={selected.all.filter((x) => x.id !== selected.lead.id)} base={base} version={version} isAdmin={isAdmin} onDone={refresh} />}
        </div>
      )}
    </div>
  );
}

function IssueDetail({ issue, also, base, version, isAdmin, onDone }: { issue: Issue; also: Issue[]; base: string; version: VersionDetail; isAdmin: boolean; onDone: () => void }) {
  const [value, setValue] = useState(issue.figure?.raw ?? "");
  const [note, setNote] = useState("");
  const [editing, setEditing] = useState(false);
  const [flagNote, setFlagNote] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const locked = decisionsLocked(version);
  useEffect(() => { setErr(null); }, [issue.id, locked]);
  const act = useMutation({
    mutationFn: (body: { action: string; value?: string; note?: string }) => post(`${base}/issues/${issue.id}/resolve`, body),
    onSuccess: (_r, body) => {
      setEditing(false);
      setSaved(body.action === "edit" ? "Correction saved." : body.action === "confirm" ? "Confirmed." : "Marked as not a figure.");
      onDone();
    },
    onError: (e: Error) => { setErr(e.message); if (isBusy(e)) onDone(); },
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
      <p className="small muted">{[issue, ...also].map((x) => CHECK_LABEL[x.check] ?? x.check).filter((v, i, a) => a.indexOf(v) === i).join(" + ")} · {issue.severity}{issue.page ? ` · page ${issue.page}` : ""}</p>
      <h2 className="issue-title">{issue.message}</h2>
      {also.length > 0 && <p className="small muted">Also flagged: {also.map((x) => x.message).filter((m) => m !== issue.message).join(" ")}</p>}
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
      {saved && !err && <p className="ok-text small" role="status">{saved} The page is rebuilt in the background — carry on with the next item.</p>}
      {platform && <p className="callout bad small">This is a problem with the generated page, not with a figure, so it can't be waived. Re-run the page build; if it persists, contact support.</p>}
      {!readonly && !platform && isAdmin && (
        <div className="actions">
          <label className="field"><span className="label">Note (optional, saved in the audit trail)</span><input value={note} onChange={(e) => setNote(e.target.value)} /></label>
          {editing && issue.fid ? (
            <form className="inline-form" onSubmit={(e) => { e.preventDefault(); act.mutate({ action: "edit", value, note }); }}>
              <input aria-label="Corrected value exactly as printed" value={value} onChange={(e) => setValue(e.target.value)} autoFocus />
              <Button className="primary" busy={act.isPending} disabled={locked} busyLabel="Saving the value">Save value</Button>
              <button type="button" className="link" onClick={() => setEditing(false)}>Cancel</button>
            </form>
          ) : (
            <div className="btn-row">
              <Button className="primary" busy={act.isPending && act.variables?.action === "confirm"} disabled={act.isPending || locked} busyLabel="Confirming" onClick={() => act.mutate({ action: "confirm", note })}>Confirm correct</Button>
              {issue.fid && <button className="secondary" disabled={act.isPending || locked} onClick={() => setEditing(true)}>Edit value</button>}
              {issue.fid && <Button className="secondary" busy={act.isPending && act.variables?.action === "not_a_figure"} disabled={act.isPending || locked} busyLabel="Saving" onClick={() => act.mutate({ action: "not_a_figure", note })}>Mark as not a figure</Button>}
            </div>
          )}
          {locked && <p className="muted small">Re-reading the PDF — these unlock automatically when it finishes.</p>}
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

// The server refuses decisions while the PDF is being re-read (409). The page's copy of the
// version can be a moment behind, so fetch it again: the page then shows the progress and
// polls until the decision can be made.
const isBusy = (e: Error) => e instanceof ApiError && e.status === 409 && /processing/i.test(e.message);

type BulkAction = "confirm" | "not_a_figure";

/** Every flagged value on screen at once, each as a crop of the PDF next to what was read.
 * Nothing starts ticked: the reviewer ticks the values they've checked (or the junk that
 * isn't a figure) and decides those in one step — logged per figure, re-checked once. */
function ReviewGrid({ groups, base, version, canDecide, onDone, onOpen }: {
  groups: IssueGroup[]; base: string; version: VersionDetail; canDecide: boolean; onDone: () => void; onOpen: (issueId: string) => void;
}) {
  const shown = groups.slice(0, GRID_BATCH);
  const [on, setOn] = useState<Set<string>>(new Set());
  const [asking, setAsking] = useState<BulkAction | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const locked = decisionsLocked(version);
  useEffect(() => { setErr(null); }, [locked]);
  const picked = shown.filter((g) => on.has(g.key));
  const toggle = (k: string) => { setAsking(null); setOn((o) => { const n = new Set(o); if (n.has(k)) n.delete(k); else n.add(k); return n; }); };
  const bulk = useMutation({
    mutationFn: (action: BulkAction) => post(`${base}/issues/resolve-bulk`, {
      issue_ids: picked.flatMap((g) => g.all.filter((i) => i.status === "open").map((i) => i.id)), action }),
    onSuccess: (_r, action) => {
      setDone(`${picked.length} value${picked.length === 1 ? "" : "s"} ${action === "confirm" ? "confirmed" : "marked as not figures"}. The page is rebuilt in the background — you can keep going.`);
      setOn(new Set()); setAsking(null); onDone();
    },
    onError: (e: Error) => { setErr(e.message); setAsking(null); if (isBusy(e)) onDone(); },
  });
  const pageOf = (n: number) => version.pages.find((p) => p.page === n);
  const ask = (action: BulkAction) => { setErr(null); setDone(null); setAsking(action); };
  const disabled = !picked.length || bulk.isPending || locked;

  return (
    <div className="stack">
      <div className="review-bar">
        <span><strong>{picked.length}</strong> of {shown.length} selected{groups.length > shown.length ? ` · showing the first ${shown.length} of ${groups.length} — pick a page above to work through the rest` : ""}</span>
        <span className="btn-row">
          <button className="link" onClick={() => { setAsking(null); setOn(new Set(shown.map((g) => g.key))); }}>Select all</button>
          <button className="link" onClick={() => { setAsking(null); setOn(new Set()); }}>Select none</button>
          {canDecide && <Button className="secondary" disabled={disabled} busy={bulk.isPending && bulk.variables === "not_a_figure"} busyLabel="Saving" onClick={() => ask("not_a_figure")}>
            {picked.length ? `Not figures (${picked.length})` : "Not figures"}</Button>}
          {canDecide && <Button className="primary" disabled={disabled} busy={bulk.isPending && bulk.variables === "confirm"} busyLabel="Saving" onClick={() => ask("confirm")}>
            {picked.length ? `Confirm ${picked.length} correct` : "Confirm selected"}</Button>}
        </span>
      </div>
      {asking && picked.length > 0 && (
        <div className={`callout ${asking === "confirm" ? "info" : "warn"} row`} role="alertdialog" aria-label="Confirm bulk decision" style={{ gap: 12, justifyContent: "space-between", flexWrap: "wrap" }}>
          <span>{asking === "confirm"
            ? <>Confirm that these <strong>{picked.length}</strong> value{picked.length === 1 ? "" : "s"} match the PDF exactly? Each one is logged under your name.</>
            : <>Mark these <strong>{picked.length}</strong> item{picked.length === 1 ? "" : "s"} as not figures (logos, icons, page numbers, chart ticks)? They'll be left out of the checks. Each one is logged under your name.</>}</span>
          <span className="btn-row">
            <button type="button" className="secondary" onClick={() => setAsking(null)}>Cancel</button>
            <Button className="primary" busy={bulk.isPending} busyLabel="Saving" disabled={bulk.isPending || locked} onClick={() => bulk.mutate(asking)}>
              {asking === "confirm" ? `Yes, confirm ${picked.length}` : `Yes, mark ${picked.length} as not figures`}
            </Button>
          </span>
        </div>
      )}
      {err && <p className="error" role="alert">{err}</p>}
      {done && !err && <p className="ok-text small" role="status">{done}</p>}
      {canDecide && locked && <p className="muted small">Re-reading the PDF — the buttons unlock automatically when it finishes.</p>}
      <p className="muted small">Tick each value whose highlighted spot matches what we read, then confirm them together. Tick logos, icons, page numbers or chart ticks and choose “Not figures”. Anything that's misread: open it to correct it.</p>
      <ul className="review-grid" aria-label="Values to review">
        {shown.map((g) => {
          const i = g.lead;
          const pg = i.page ? pageOf(i.page) : undefined;
          const sel = on.has(g.key);
          const labels = [...new Set(g.all.map((x) => CHECK_LABEL[x.check] ?? x.check))].join(" + ");
          return (
            <li key={g.key} className={`review-tile ${sel ? "on" : ""} ${i.severity}`}>
              <label>
                <input type="checkbox" checked={sel} onChange={() => toggle(g.key)} disabled={!canDecide} />
                <span className="small muted">p{i.page} · {labels}</span>
              </label>
              {pg && i.bbox && <PdfCrop src={`${base}/pages/${i.page}.png?zoom=2`} pageWidth={pg.width} pageHeight={pg.height} bbox={i.bbox} label={`Page ${i.page}`} />}
              <div className="review-read">
                <span className="muted small">Read</span> <code className="big">{i.figure?.raw ?? i.expected ?? "—"}</code>
                {i.actual && i.actual !== i.figure?.raw && <span className="small muted"> · re-read: <code>{i.actual}</code></span>}
                {i.figure?.method === "ocr" && <span className="small muted"> · OCR {Math.round((i.figure.confidence ?? 1) * 100)}%</span>}
              </div>
              {canDecide && <button type="button" className="link small" style={{ alignSelf: "flex-start" }} onClick={() => onOpen(i.id)}>Open to correct</button>}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
