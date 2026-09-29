import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, formatDateTime, post, useTenantRole, type Report, type VersionDetail } from "../../api";
import { useMe } from "../../App";

interface CommentRow { id: string; section_id: string; body: string; by: string; at: string; resolved: boolean }

export default function PreviewTab({ tenantId, report, version, hidePublish = false }: { tenantId: string; report: Report; version: VersionDetail; hidePublish?: boolean }) {
  const me = useMe().data;
  const { isAdmin } = useTenantRole(me, tenantId);
  const qc = useQueryClient();
  const base = `/api/tenants/${tenantId}/reports/${report.id}/versions/${version.id}`;
  const [width, setWidth] = useState<"desktop" | "mobile">("desktop");
  const [sideBySide, setSideBySide] = useState(true);
  const [section, setSection] = useState<string | null>(version.sections[0]?.id ?? null);
  const iframe = useRef<HTMLIFrameElement>(null);
  const pdfPane = useRef<HTMLDivElement>(null);
  const followPdf = useRef(true);
  // Scroll only the PDF pane (scrollIntoView would also scroll the whole window).
  const showPdfPage = (page: number | null | undefined) => {
    const pane = pdfPane.current;
    const el = page ? pane?.querySelector<HTMLElement>(`[data-page="${page}"]`) : null;
    if (pane && el) pane.scrollTo({ top: el.offsetTop - 8, behavior: "smooth" });
  };

  useEffect(() => {
    function onMsg(ev: MessageEvent) {
      if (ev.source !== iframe.current?.contentWindow || !ev.data?.ppdf) return;
      if (ev.data.ppdf === "section") {
        setSection(ev.data.id);
        const sec = version.sections.find((s) => s.id === ev.data.id);
        if (followPdf.current) showPdfPage(sec?.page);
      }
    }
    addEventListener("message", onMsg);
    return () => removeEventListener("message", onMsg);
  }, [version.sections]); // eslint-disable-line react-hooks/exhaustive-deps

  const goto = (id: string) => {
    setSection(id);
    iframe.current?.contentWindow?.postMessage({ ppdf: "goto", id }, "*");
    showPdfPage(version.sections.find((s) => s.id === id)?.page);
  };

  const comments = useQuery({ queryKey: ["comments", version.id], queryFn: () => api<{ comments: CommentRow[] }>(`${base}/comments`) });
  const settings = useQuery({ queryKey: ["settings", tenantId], queryFn: () => api<{ effective_disclaimer: string | null; site_origin: string }>(`/api/tenants/${tenantId}/settings`) });

  if (!version.bundle_sha256) return <p className="muted">The web page will appear here once it has been generated.</p>;
  const previewUrl = `${base}/preview/`;
  const count = (sid: string) => comments.data?.comments.filter((c) => c.section_id === sid && !c.resolved).length ?? 0;

  return (
    <div className="stack">
      <div className="toolbar">
        <div className="seg" role="group" aria-label="Width">
          <button className={width === "desktop" ? "active" : ""} onClick={() => setWidth("desktop")}>Desktop</button>
          <button className={width === "mobile" ? "active" : ""} onClick={() => setWidth("mobile")}>Mobile</button>
        </div>
        <label className="check"><input type="checkbox" checked={sideBySide} onChange={(e) => setSideBySide(e.target.checked)} /> Side by side with the PDF</label>
        {sideBySide && <label className="check"><input type="checkbox" defaultChecked onChange={(e) => (followPdf.current = e.target.checked)} /> Sync scrolling</label>}
        <a href={previewUrl} target="_blank" rel="noreferrer">Open preview in a new tab</a>
        <span className="muted small">This is exactly the page that will publish (the preview adds only scroll syncing).</span>
      </div>

      <div className="preview-layout">
        <aside className="card sections-nav" aria-label="Sections">
          <h2>Sections</h2>
          <ol>
            {version.sections.map((s) => (
              <li key={s.id}>
                <button className={`link ${section === s.id ? "strong" : ""}`} onClick={() => goto(s.id)}>{s.heading_text || s.type.replace(/_/g, " ")}</button>
                {count(s.id) > 0 && <span className="count">{count(s.id)}</span>}
              </li>
            ))}
          </ol>
          {section && <Comments base={base} sectionId={section} rows={comments.data?.comments ?? []} readonly={version.published_at !== null} onChange={() => qc.invalidateQueries({ queryKey: ["comments", version.id] })} />}
        </aside>
        <div className={`preview-panes ${sideBySide ? "two" : ""}`}>
          {sideBySide && (
            <div className="pdf-pane" ref={pdfPane} aria-label="Original PDF">
              {version.pages.map((p) => (
                <img key={p.page} data-page={p.page} loading="lazy" src={`${base}/pages/${p.page}.png?zoom=1.25`} alt={`PDF page ${p.page}`}
                     width={Math.round(p.width * 1.25)} style={{ aspectRatio: `${p.width} / ${p.height}` }} />
              ))}
            </div>
          )}
          <div className="frame-wrap">
            <iframe ref={iframe} title="Page preview" src={previewUrl} style={{ width: width === "mobile" ? 390 : "100%" }} />
          </div>
        </div>
      </div>

      {!hidePublish && <PublishPanel tenantId={tenantId} report={report} version={version} isAdmin={isAdmin} disclaimer={settings.data?.effective_disclaimer ?? null} siteOrigin={settings.data?.site_origin} />}
    </div>
  );
}

function Comments({ base, sectionId, rows, readonly, onChange }: { base: string; sectionId: string; rows: CommentRow[]; readonly: boolean; onChange: () => void }) {
  const [body, setBody] = useState("");
  const add = useMutation({ mutationFn: () => post(`${base}/comments`, { section_id: sectionId, body }), onSuccess: () => { setBody(""); onChange(); } });
  const resolve = useMutation({ mutationFn: (id: string) => post(`${base}/comments/${id}/resolve`), onSuccess: onChange });
  const mine = rows.filter((c) => c.section_id === sectionId);
  function submit(e: FormEvent) { e.preventDefault(); if (body.trim()) add.mutate(); }
  return (
    <div className="comments">
      <h3>Comments on this section</h3>
      {mine.length === 0 && <p className="muted small">No comments yet.</p>}
      <ul>
        {mine.map((c) => (
          <li key={c.id} className={c.resolved ? "muted" : ""}>
            <div className="small"><strong>{c.by}</strong> · {formatDateTime(c.at)}</div>
            <div>{c.body}</div>
            {!c.resolved && !readonly && <button className="link small" onClick={() => resolve.mutate(c.id)}>Resolve</button>}
          </li>
        ))}
      </ul>
      {!readonly && (
        <form onSubmit={submit}>
          <textarea aria-label="Add a comment" rows={3} value={body} onChange={(e) => setBody(e.target.value)} placeholder="Add a comment…" />
          <button className="secondary" disabled={add.isPending || !body.trim()}>Comment</button>
        </form>
      )}
    </div>
  );
}

export function PublishPanel({ tenantId, report, version, isAdmin, disclaimer, siteOrigin }: {
  tenantId: string; report: Report; version: VersionDetail; isAdmin: boolean; disclaimer: string | null; siteOrigin?: string;
}) {
  const qc = useQueryClient();
  const [confirmed, setConfirmed] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const publish = useMutation({
    mutationFn: () => post(`/api/tenants/${tenantId}/reports/${report.id}/versions/${version.id}/publish`, { confirm_reviewed: confirmed }),
    onSuccess: () => { ["report", "version", "reports"].forEach((k) => qc.invalidateQueries({ queryKey: [k] })); },
    onError: (e: Error) => setErr(e.message),
  });
  if (version.is_live) {
    return (
      <div className="callout ok">
        <strong>This version is live.</strong> Published {formatDateTime(version.published_at)}.{" "}
        {siteOrigin && <a href={`${siteOrigin}${report.path}`} target="_blank" rel="noreferrer">View the live page</a>}
      </div>
    );
  }
  if (version.published_at) return <p className="callout info">This version was published earlier and has been replaced. You can roll back to it from Versions.</p>;
  const blocking = version.open_blocking;
  const reasons: string[] = [];
  if (version.status === "processing") reasons.push("Checks are still running.");
  if (blocking > 0) reasons.push(`${blocking} blocking issue(s) are open in Validation.`);
  if (!version.validated_current && version.status !== "processing") reasons.push("The latest changes haven't been checked yet.");
  if (!disclaimer) reasons.push("Add a disclaimer in Settings.");
  const ready = reasons.length === 0;
  return (
    <div className="card publish">
      <h2>Approve and publish</h2>
      {!isAdmin ? (
        <p className="muted">Only a workspace admin can publish. You can leave comments and flag issues.</p>
      ) : (
        <>
          {!ready && <ul className="reasons">{reasons.map((r) => <li key={r}>{r}</li>)}</ul>}
          <label className="check">
            <input type="checkbox" checked={confirmed} disabled={!ready} onChange={(e) => setConfirmed(e.target.checked)} />
            I have reviewed the figures on this page against the PDF.
          </label>
          {err && <p className="error" role="alert">{err}</p>}
          <button className="primary" disabled={!ready || !confirmed || publish.isPending} onClick={() => publish.mutate()}>
            {publish.isPending ? "Publishing…" : "Approve and publish"}
          </button>
          <p className="muted small">Publishing creates a permanent, unchangeable version. Later changes become a new draft; you can roll back at any time.</p>
        </>
      )}
    </div>
  );
}
