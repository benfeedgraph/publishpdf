import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api, type Figure, type Report, type Run, type SchemaDoc, type VersionDetail } from "../../api";
import PdfRegion from "../../components/PdfRegion";
import { Loading } from "../../components/Spinner";

const SECTION_LABEL: Record<string, string> = {
  highlights: "Highlights", profit_and_loss: "Profit and loss", balance_sheet: "Balance sheet", cash_flow: "Cash flow",
  segment_results: "Segment results", management_commentary: "Management commentary", outlook: "Outlook", notes: "Notes", other: "Other",
};

export function Runs({ runs, figures, onFigure, selected }: { runs: Run[]; figures: Record<string, Figure>; onFigure?: (f: Figure) => void; selected?: string | null }) {
  return (
    <>
      {runs.map((r, i) =>
        "f" in r ? (
          <button key={i} type="button" className={`fig fig-${figures[r.f]?.kind} ${selected === r.f ? "sel" : ""} ${figures[r.f]?.status !== "active" ? "nf" : ""}`}
            onClick={() => onFigure?.(figures[r.f])} title={`${figures[r.f]?.kind} · page ${figures[r.f]?.source.page}`}>
            {figures[r.f]?.raw}
          </button>
        ) : <span key={i}>{r.t}</span>,
      )}
    </>
  );
}

export default function SchemaTab({ tenantId, report, version }: { tenantId: string; report: Report; version: VersionDetail }) {
  const [mode, setMode] = useState<"tree" | "json">("tree");
  const [sel, setSel] = useState<Figure | null>(null);
  const base = `/api/tenants/${tenantId}/reports/${report.id}/versions/${version.id}`;
  const schema = useQuery({ queryKey: ["schema", version.id, version.schema_sha256], queryFn: () => api<SchemaDoc>(`${base}/schema`), enabled: !!version.schema_sha256 });
  const raw = useMemo(() => (schema.data ? JSON.stringify(schema.data, null, 2) : ""), [schema.data]);
  if (!version.schema_sha256) return <p className="muted">The schema will appear when the PDF has been read.</p>;
  if (schema.isPending) return <Loading label="Loading the extracted data" />;
  if (schema.isError) return <p className="error">{schema.error.message}</p>;
  const s = schema.data;
  const figs = Object.values(s.figures);
  const page = sel ? s.pages.find((p) => p.page === sel.source.page) : null;

  return (
    <div className="stack">
      <div className="toolbar">
        <div className="seg" role="group" aria-label="Schema view">
          <button className={mode === "tree" ? "active" : ""} onClick={() => setMode("tree")}>Readable view</button>
          <button className={mode === "json" ? "active" : ""} onClick={() => setMode("json")}>Raw JSON</button>
        </div>
        <a className="button secondary" href={`${base}/schema?download=true`}>Download JSON</a>
        <a className="button secondary" href="/api/schema/report.schema.json" target="_blank" rel="noreferrer">JSON Schema definition</a>
        <span className="muted small">{figs.length} figures · {s.sections.length} sections · {s.pages.length} pages</span>
      </div>
      {mode === "json" ? (
        <pre className="json" tabIndex={0}>{raw.length > 400000 ? raw.slice(0, 400000) + "\n… (truncated in the viewer — download for the full file)" : raw}</pre>
      ) : (
        <div className={sel ? "split" : ""}>
          <div className="schema-tree">
            {s.sections.map((sec) => (
              <section key={sec.id} className="card">
                <p className="eyebrow small">{SECTION_LABEL[sec.type] ?? sec.type} · {sec.id}</p>
                <h2><Runs runs={sec.heading} figures={s.figures} onFigure={setSel} selected={sel?.id} /> {!sec.heading.length && <span className="muted">(untitled)</span>}</h2>
                {sec.blocks.map((b) => (
                  <div key={b.id} className="schema-block">
                    {b.type === "paragraph" && <p><Runs runs={b.runs ?? []} figures={s.figures} onFigure={setSel} selected={sel?.id} /></p>}
                    {b.type === "chart" && (
                      b.extracted ? (
                        <table><caption>Chart data (from data labels)</caption><tbody>
                          {b.points?.map((p, i) => <tr key={i}><th scope="row"><Runs runs={p.label} figures={s.figures} onFigure={setSel} /></th><td><Runs runs={p.value} figures={s.figures} onFigure={setSel} selected={sel?.id} /></td></tr>)}
                        </tbody></table>
                      ) : <p className="callout warn small">Chart on page {b.source.page}: image only, not extracted.</p>
                    )}
                    {b.type === "table" && (
                      <div className="table-scroll">
                        <table className="schema-table">
                          <caption>
                            {b.caption && b.caption.length > 0 && <><Runs runs={b.caption} figures={s.figures} onFigure={setSel} /> · </>}
                            unit: {b.unit} · currency: {b.currency} · page {b.source.page}
                          </caption>
                          <thead>
                            {b.header_rows?.map((hr, i) => (
                              <tr key={i}><th scope="col"></th>{hr.map((c, j) => <th scope="col" key={j} colSpan={c.colspan}><Runs runs={c.runs} figures={s.figures} onFigure={setSel} /></th>)}</tr>
                            ))}
                          </thead>
                          <tbody>
                            {b.rows?.map((row, i) => (
                              <tr key={i}><th scope="row">{row.label_text}</th>{row.cells.map((c, j) => <td key={j}><Runs runs={c.runs} figures={s.figures} onFigure={setSel} selected={sel?.id} /></td>)}</tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                ))}
              </section>
            ))}
          </div>
          {sel && page && (
            <aside className="card sticky" aria-label="Figure details">
              <div className="toolbar"><h2>Figure <code>{sel.id}</code></h2><button className="link" onClick={() => setSel(null)}>Close</button></div>
              <dl className="kv">
                <dt>As printed</dt><dd><code>{sel.raw}</code></dd>
                <dt>Normalised</dt><dd>{sel.value ?? sel.iso ?? "—"}</dd>
                <dt>Kind</dt><dd>{sel.kind}{sel.status !== "active" && " (marked not a figure)"}</dd>
                <dt>Unit / currency</dt><dd>{sel.unit ?? "—"} / {sel.currency ?? "—"}</dd>
                <dt>Period</dt><dd>{sel.period?.raw ?? "not stated"}</dd>
                <dt>Row / column</dt><dd>{sel.row_label ?? "—"} / {sel.col_label ?? "—"}</dd>
                <dt>Source</dt><dd>page {sel.source.page}, box [{sel.source.bbox.map((n) => n.toFixed(1)).join(", ")}]</dd>
                <dt>Method</dt><dd>{sel.method === "ocr" ? "OCR" : "text layer"} · confidence {(sel.confidence * 100).toFixed(0)}%</dd>
                {sel.edited && <><dt>Edited</dt><dd>was <code>{sel.edited.original_raw}</code></dd></>}
              </dl>
              <PdfRegion src={`${base}/pages/${page.page}.png?zoom=2`} pageWidth={page.width} pageHeight={page.height} bbox={sel.source.bbox} label={`Page ${page.page}`} />
            </aside>
          )}
        </div>
      )}
    </div>
  );
}
