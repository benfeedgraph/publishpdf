import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, formatDateTime, post, useTenantRole, type Report, type VersionDetail } from "../../api";
import { useMe } from "../../App";
import { Button } from "../../components/Spinner";

const DESKTOP_W = 1280;   // width the desktop preview is laid out at

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
  // The desktop preview renders at a real desktop width and is scaled to fit: squeezed
  // beside the PDF at its natural size it would fall into the phone layout.
  const stage = useRef<HTMLDivElement>(null);
  const [stageW, setStageW] = useState(0);
  useEffect(() => {
    const el = stage.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setStageW(e.contentRect.width));
    ro.observe(el);
    return () => ro.disconnect();
  }, [version.bundle_sha256, width]);
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
      } else if (ev.data.ppdf === "page") {
        onPage(ev.data.page);
        if (followPdf.current) showPdfPage(ev.data.page);
      }
    }
    addEventListener("message", onMsg);
    return () => removeEventListener("message", onMsg);
  }, [version.sections]); // eslint-disable-line react-hooks/exhaustive-deps

  // Comments stay keyed by section: a page's comments go to the last section starting on or before it.
  const onPage = (n: number) => {
    const sec = [...version.sections].reverse().find((x) => (x.page ?? 0) <= n) ?? version.sections[0];
    if (sec) setSection(sec.id);
  };
  const comments = useQuery({ queryKey: ["comments", version.id], queryFn: () => api<{ comments: CommentRow[] }>(`${base}/comments`) });
  const settings = useQuery({ queryKey: ["settings", tenantId], queryFn: () => api<{ effective_disclaimer: string | null; site_origin: string }>(`/api/tenants/${tenantId}/settings`) });

  if (!version.bundle_sha256) return <p className="muted">The web page will appear here once it has been generated.</p>;
  const previewUrl = `${base}/preview`;      // no trailing slash: the hosted /api forwarding drops those

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
            {width === "mobile" ? (
              <iframe ref={iframe} title="Page preview" src={previewUrl} style={{ width: 390 }} />
            ) : (
              <div className="frame-stage" ref={stage}>
                {(() => {
                  const scale = stageW && stageW < DESKTOP_W ? stageW / DESKTOP_W : 1;
                  return <iframe ref={iframe} title="Page preview" src={previewUrl}
                                 style={scale < 1 ? { width: DESKTOP_W, height: `calc(76vh / ${scale})`, transform: `scale(${scale})` } : { width: "100%" }} />;
                })()}
              </div>
            )}
          </div>
        </div>
      </div>

      {section && (
        <div className="card">
          <Comments base={base} sectionId={section} rows={comments.data?.comments ?? []} readonly={version.published_at !== null} onChange={() => qc.invalidateQueries({ queryKey: ["comments", version.id] })} />
        </div>
      )}

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
            {!c.resolved && !readonly && <Button className="link small" busy={resolve.isPending && resolve.variables === c.id} disabled={resolve.isPending} busyLabel="Resolving" onClick={() => resolve.mutate(c.id)}>Resolve</Button>}
          </li>
        ))}
      </ul>
      {!readonly && (
        <form onSubmit={submit}>
          <textarea aria-label="Add a comment" rows={3} value={body} onChange={(e) => setBody(e.target.value)} placeholder="Add a comment…" />
          <Button className="secondary" disabled={!body.trim()} busy={add.isPending} busyLabel="Posting the comment">Comment</Button>
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
          <Button className="primary" disabled={!ready || !confirmed} busy={publish.isPending} busyLabel="Publishing" onClick={() => publish.mutate()}>
            Approve and publish
          </Button>
          <p className="muted small">Publishing creates a permanent, unchangeable version. Later changes become a new draft; you can roll back at any time.</p>
        </>
      )}
    </div>
  );
}
