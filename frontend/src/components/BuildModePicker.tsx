import { IconSparkle } from "./Icons";

export type BuildMode = "none" | "layout" | "full";

export interface BuildOption {
  mode: BuildMode;
  max_credits: number;
  max_usd: number;
  parts: { layout: number; labels: number; check: number };
  available: boolean;
  unavailable_reason: string | null;
  labels_available: boolean;
  models: string[];
}

export interface BuildOptions {
  default: BuildMode;
  options: BuildOption[];
  allowance: { limit: number | null; used: number; remaining: number | null } | null;
  credit_usd: number;
}

export const BUILD_MODE_LABEL: Record<BuildMode, string> = {
  none: "Build without AI",
  layout: "AI just for layout",
  full: "Use AI to build the page",
};

const COPY: Record<BuildMode, { what: string; best: string }> = {
  none: {
    what: "Our built-in layout turns every page into web text, charts and pictures. Design-heavy pages (photo spreads, brand grids) follow the PDF's arrangement as closely as rules allow.",
    best: "Text-led reports, results tables, or when you want to spend nothing.",
  },
  layout: {
    what: "The AI arranges design-heavy pages into clean web sections: cards, stat tiles, galleries. It sees only positions and short snippets of text, and never writes words or numbers.",
    best: "Investor decks and magazine-style annual report pages.",
  },
  full: {
    what: "AI layout, plus an automatic AI double-check of figures our checks flag (it reads a small crop and can only confirm a value), plus AI labels for sections our rules can't name.",
    best: "Scanned PDFs and long annual reports: fewer items left for you to review.",
  },
};

const ROWS: { label: string; cells: Record<BuildMode, string> }[] = [
  { label: "Words and numbers", cells: { none: "Always from the PDF", layout: "Always from the PDF", full: "Always from the PDF" } },
  { label: "All nine figure checks", cells: { none: "Yes", layout: "Yes", full: "Yes" } },
  { label: "Design-heavy pages", cells: { none: "Built-in layout", layout: "Arranged by AI", full: "Arranged by AI" } },
  { label: "Figures flagged by checks", cells: { none: "You review them (AI check on request)", layout: "You review them (AI check on request)", full: "AI double-checks first, then you" } },
  { label: "Section labels", cells: { none: "Rules", layout: "Rules", full: "Rules, then AI for the rest" } },
];

function credits(o: BuildOption) {
  return o.max_credits === 0 ? "0 credits" : `up to ${o.max_credits.toLocaleString()} credits`;
}

/** The three ways to build a report's page, with what each does and the most it can spend. */
export default function BuildModePicker({ data, value, onChange, current }: {
  data: BuildOptions; value: BuildMode; onChange: (m: BuildMode) => void; current?: BuildMode | null;
}) {
  const a = data.allowance;
  const chosen = data.options.find((o) => o.mode === value);
  const short = !!chosen && !!a && a.remaining !== null && chosen.max_credits > a.remaining;
  return (
    <fieldset className="build-picker">
      <legend className="label">How should we build the web page?</legend>
      <div className="build-options" role="radiogroup">
        {data.options.map((o) => {
          const on = value === o.mode;
          return (
            <label key={o.mode} className={`build-option${on ? " on" : ""}${o.available ? "" : " disabled"}`}>
              <input type="radio" name="build_mode" value={o.mode} checked={on} disabled={!o.available}
                onChange={() => onChange(o.mode)} />
              <span className="bo-head">
                <span className="bo-title">{o.mode !== "none" && <IconSparkle size={14} />}{BUILD_MODE_LABEL[o.mode]}</span>
                {o.mode === data.default && <span className="badge info">Recommended</span>}
                {current === o.mode && <span className="badge">Current</span>}
              </span>
              <span className={`bo-credits${o.max_credits ? "" : " free"}`}>
                {credits(o)}{o.max_credits > 0 && <span className="muted"> · ≈ ${o.max_usd.toFixed(2)} at most</span>}
              </span>
              <span className="bo-what">{COPY[o.mode].what}</span>
              <span className="bo-best"><strong>Best for:</strong> {COPY[o.mode].best}</span>
              {o.mode === "full" && o.max_credits > 0 && (
                <span className="bo-parts muted small">
                  Layout up to {o.parts.layout} · double-check up to {o.parts.check}
                  {o.labels_available ? ` · section labels up to ${o.parts.labels}` : " · section labels not set up yet"}
                </span>
              )}
              {o.models.length > 0 && <span className="bo-parts muted small">Model{o.models.length > 1 ? "s" : ""}: {o.models.join(", ")}</span>}
              {!o.available && <span className="error small">{o.unavailable_reason}</span>}
            </label>
          );
        })}
      </div>
      <p className="muted small" style={{ margin: "8px 0 0" }}>
        Credit figures are the most an option can use. Most reports use far less: only design-heavy pages go to the AI,
        a layout already made for this PDF is reused for free, and the double-check only runs if figures are flagged.
        One credit = ${data.credit_usd.toFixed(2)}.
        {a && (a.limit === null ? " Your workspace has no monthly credit limit." :
          ` This month: ${a.used.toLocaleString()} of ${a.limit.toLocaleString()} credits used, ${a.remaining!.toLocaleString()} left.`)}
      </p>
      {short && (
        <p className="callout warn small" style={{ marginTop: 8 }}>
          This could need more than the {a!.remaining} credits you have left this month. AI stops when they run out: the rest
          of the page is built without AI, and nothing fails.
        </p>
      )}
      <details className="bo-compare">
        <summary className="small">Compare the options in detail</summary>
        <div className="table-wrap">
          <table>
            <thead><tr><th />{data.options.map((o) => <th key={o.mode}>{BUILD_MODE_LABEL[o.mode]}</th>)}</tr></thead>
            <tbody>
              {ROWS.map((r) => (
                <tr key={r.label}><th scope="row">{r.label}</th>{data.options.map((o) => <td key={o.mode}>{r.cells[o.mode]}</td>)}</tr>
              ))}
              <tr><th scope="row">Most credits for this PDF</th>{data.options.map((o) => <td key={o.mode}><strong>{credits(o)}</strong></td>)}</tr>
            </tbody>
          </table>
        </div>
      </details>
    </fieldset>
  );
}
