import { IconCheck, IconSparkle } from "./Icons";

export type BuildMode = "none" | "layout" | "full";

export interface BuildOption {
  mode: BuildMode;
  max_credits: number;
  max_usd: number;
  parts: { layout: number; labels: number; check: number };
  available: boolean;
  unavailable_reason: string | null;
  labels_available: boolean;
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

const SUMMARY: Record<BuildMode, string> = {
  none: "Our built-in layout. Nothing to pay.",
  layout: "The AI arranges design-heavy pages.",
  full: "AI layout, plus AI help checking figures.",
};

function points(o: BuildOption): string[] {
  if (o.mode === "none") return ["Every page as web text, charts and pictures", "Design-heavy pages keep the PDF's arrangement", "All nine figure checks"];
  if (o.mode === "layout") return ["Cards, stat tiles and galleries instead of page images", "AI never writes words or numbers", "All nine figure checks"];
  return ["Everything in AI just for layout", "AI double-checks flagged figures before you do",
    ...(o.labels_available ? ["AI names sections our rules can't"] : [])];
}

const BEST: Record<BuildMode, string> = {
  none: "Text-led reports and results tables",
  layout: "Investor decks and magazine-style pages",
  full: "Annual reports and scanned PDFs: least for you to review",
};

const ROWS: { label: string; cells: Record<BuildMode, string> }[] = [
  { label: "Words and numbers", cells: { none: "Always from the PDF", layout: "Always from the PDF", full: "Always from the PDF" } },
  { label: "All nine figure checks", cells: { none: "Yes", layout: "Yes", full: "Yes" } },
  { label: "Design-heavy pages", cells: { none: "Built-in layout", layout: "Arranged by AI", full: "Arranged by AI" } },
  { label: "Figures flagged by checks", cells: { none: "You review them", layout: "You review them", full: "AI double-checks first, then you" } },
  { label: "Section labels", cells: { none: "Rules", layout: "Rules", full: "Rules, then AI for the rest" } },
];

const creditText = (o: BuildOption) => (o.max_credits === 0 ? "Free" : `Up to ${o.max_credits.toLocaleString()} credits`);

/** The three ways to build a report's page: what each does and the most it can spend. */
export default function BuildModePicker({ data, value, onChange, current }: {
  data: BuildOptions; value: BuildMode; onChange: (m: BuildMode) => void; current?: BuildMode | null;
}) {
  const a = data.allowance;
  const chosen = data.options.find((o) => o.mode === value);
  const short = !!chosen && !!a && a.remaining !== null && chosen.max_credits > a.remaining;
  return (
    <fieldset className="build-picker">
      <legend>How should we build the web page?</legend>
      <div className="build-options" role="radiogroup">
        {data.options.map((o) => {
          const on = value === o.mode;
          const rec = o.mode === data.default;
          return (
            <label key={o.mode} className={`build-option${on ? " on" : ""}${rec ? " rec" : ""}${o.available ? "" : " disabled"}`}>
              {rec && <span className="bo-ribbon">Recommended</span>}
              <input type="radio" name="build_mode" value={o.mode} checked={on} disabled={!o.available} onChange={() => onChange(o.mode)} />
              <span className="bo-head">
                <span className="bo-radio" aria-hidden />
                <span className="bo-title">{BUILD_MODE_LABEL[o.mode]}</span>
                {o.mode !== "none" && <span className="bo-spark" aria-hidden><IconSparkle size={16} /></span>}
              </span>
              <span className="bo-price">
                <span className={`bo-credits${o.max_credits ? "" : " free"}`}>{creditText(o)}</span>
                {o.max_credits > 0 && <span className="bo-usd">≈ ${o.max_usd.toFixed(2)} at most</span>}
                {current === o.mode && <span className="badge">Current</span>}
              </span>
              <span className="bo-summary">{SUMMARY[o.mode]}</span>
              <ul className="bo-points">
                {points(o).map((p) => <li key={p}><IconCheck size={14} />{p}</li>)}
              </ul>
              <span className="bo-best">Best for: {BEST[o.mode]}</span>
              {o.mode === "full" && o.max_credits > 0 && (
                <span className="bo-split">Layout up to {o.parts.layout} · double-check up to {o.parts.check}{o.labels_available ? ` · labels up to ${o.parts.labels}` : ""}</span>
              )}
              {!o.available && <span className="error small">{o.unavailable_reason}</span>}
            </label>
          );
        })}
      </div>
      <p className="bo-note">
        Credits shown are the most an option can use. Most reports use far less: only design-heavy pages go to the AI, a layout
        already made for this PDF is reused for free, and the double-check only runs if figures are flagged.
        {a && (a.limit === null ? " Your workspace has no monthly limit." :
          ` You have ${a.remaining!.toLocaleString()} of ${a.limit.toLocaleString()} credits left this month.`)}
      </p>
      {short && (
        <p className="callout warn small" style={{ marginTop: 8 }}>
          This could need more than the {a!.remaining} credits you have left. AI stops when they run out: the rest of the page
          is built without AI, and nothing fails.
        </p>
      )}
      <details className="bo-compare">
        <summary className="small">Compare the options</summary>
        <div className="table-wrap">
          <table>
            <thead><tr><th />{data.options.map((o) => <th key={o.mode}>{BUILD_MODE_LABEL[o.mode]}</th>)}</tr></thead>
            <tbody>
              {ROWS.map((r) => (
                <tr key={r.label}><th scope="row">{r.label}</th>{data.options.map((o) => <td key={o.mode}>{r.cells[o.mode]}</td>)}</tr>
              ))}
              <tr><th scope="row">Most credits for this PDF</th>{data.options.map((o) => <td key={o.mode}><strong>{creditText(o)}</strong></td>)}</tr>
            </tbody>
          </table>
        </div>
      </details>
    </fieldset>
  );
}
