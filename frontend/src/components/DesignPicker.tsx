import { useEffect, useRef, useState } from "react";
import { post, upload } from "../api";

export interface ThemeT {
  colors: Record<string, string>;
  typography: { heading_font: string; body_font: string; base_size: number };
  spacing: string;
  header: { style: string; show_company_name: boolean };
  logo: { key: string; alt: string } | null;
  footer: Record<string, unknown>;
}
interface Proposal { theme: ThemeT; logo_url?: string | null; fonts_seen?: string[]; source: string }
type Source = "website" | "palette" | "file" | "current";

// "Keep the current design" comes first and is the default: most reports should simply
// reuse the workspace's saved look; the other sources are for changing it.
const SOURCES: [Source, string, string, string][] = [
  ["current", "Keep the current design", "Your workspace's saved colours, fonts and logo.", "✓"],
  ["website", "Match a website", "Paste a link — we take its colours, fonts and logo.", "🌐"],
  ["palette", "Use a colour palette", "Enter brand colours; we build a theme around them.", "🎨"],
  ["file", "From a PDF or images", "Upload a brand PDF or screenshots you like.", "📄"],
];
const FONTS = ["system", "serif", "Inter", "Roboto", "Open Sans", "Lato", "Montserrat", "Source Sans 3", "Noto Sans", "Nunito Sans", "Libre Franklin",
  "Mulish", "Raleway", "Merriweather", "Playfair Display", "Poppins", "IBM Plex Sans", "Work Sans", "DM Sans", "Manrope", "Lora", "PT Serif"];

export default function DesignPicker({ tenantId, reportId, current, onApply, applyLabel = "Use this design", busy }: {
  tenantId: string;
  reportId?: string;
  current: ThemeT;
  onApply: (theme: ThemeT, opts: { makeDefault: boolean; mode: string }) => void;
  applyLabel?: string;
  busy?: boolean;
}) {
  const [source, setSource] = useState<Source>("current");
  const [url, setUrl] = useState("");
  const [palette, setPalette] = useState<string[]>(["#1F4FD1", "#0F766E", "#FFFFFF", "#111827"]);
  const [files, setFiles] = useState<FileList | null>(null);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [theme, setTheme] = useState<ThemeT>(current);
  const [makeDefault, setMakeDefault] = useState(!reportId);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => { if (source === "current") { setTheme(current); setProposal(null); } }, [source, current]);

  async function propose() {
    setErr(null);
    setLoading(true);
    try {
      let p: Proposal;
      if (source === "website") p = await post<Proposal>(`/api/tenants/${tenantId}/theme/analyze-website`, { url });
      else if (source === "palette") p = await post<Proposal>(`/api/tenants/${tenantId}/theme/from-palette`, { colors: palette });
      else {
        const f = new FormData();
        Array.from(files ?? []).forEach((x) => f.append("images", x));
        p = await upload<Proposal>(`/api/tenants/${tenantId}/theme/analyze-references`, f);
      }
      setProposal(p);
      setTheme({ ...p.theme, logo: current.logo });
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }
  const set = (fn: (t: ThemeT) => void) => setTheme((t) => { const c = structuredClone(t); fn(c); return c; });

  return (
    <div className="theme-layout">
      <div className="stack">
        <div className="design-sources" role="radiogroup" aria-label="Where should the design come from?">
          {SOURCES.map(([k, title, desc, icon]) => (
            <button key={k} type="button" role="radio" aria-checked={source === k}
              className={`source-card ${source === k ? "active" : ""} ${k === "current" ? "is-default" : ""}`} onClick={() => setSource(k)}>
              <span className="source-top">
                <span className="source-icon" aria-hidden>{icon}</span>
                {k === "current" && <span className="badge ok">Default</span>}
                <span className="source-radio" aria-hidden />
              </span>
              <strong>{title}</strong>
              <span className="muted small">{desc}</span>
              {k === "current" && (
                <span className="source-swatches" aria-hidden>
                  {(["primary", "secondary", "text", "background"] as const).map((c) => <i key={c} style={{ background: current.colors[c] }} />)}
                  <em>{current.typography.heading_font}</em>
                </span>
              )}
            </button>
          ))}
        </div>

        <div className="card flat">
          {source === "website" && (
            <form className="inline-form" onSubmit={(e) => { e.preventDefault(); propose(); }}>
              <input required placeholder="https://www.yourcompany.com" value={url} onChange={(e) => setUrl(e.target.value)} aria-label="Website address" />
              <button className="primary" disabled={loading}>{loading ? "Looking…" : "Get design"}</button>
            </form>
          )}
          {source === "palette" && (
            <form onSubmit={(e) => { e.preventDefault(); propose(); }}>
              <p className="small muted">Put your main brand colour first. Add a light background and a dark text colour if you have them.</p>
              <div className="palette-row">
                {palette.map((c, i) => (
                  <span key={i} className="palette-swatch">
                    <input type="color" value={/^#[0-9a-f]{6}$/i.test(c) ? c : "#000000"} onChange={(e) => setPalette((p) => p.map((x, j) => (j === i ? e.target.value.toUpperCase() : x)))} aria-label={`Colour ${i + 1}`} />
                    <input className="hex" value={c} onChange={(e) => setPalette((p) => p.map((x, j) => (j === i ? e.target.value : x)))} aria-label={`Colour ${i + 1} hex`} />
                    {palette.length > 1 && <button type="button" className="link small" onClick={() => setPalette((p) => p.filter((_, j) => j !== i))} aria-label="Remove colour">✕</button>}
                  </span>
                ))}
                {palette.length < 8 && <button type="button" className="secondary" onClick={() => setPalette((p) => [...p, "#888888"])}>+ Add</button>}
              </div>
              <div style={{ marginTop: 12 }}><button className="primary" disabled={loading}>{loading ? "Building…" : "Build theme"}</button></div>
            </form>
          )}
          {source === "file" && (
            <form onSubmit={(e) => { e.preventDefault(); propose(); }}>
              <label className="field"><span className="label">Brand PDF, or up to 5 images (PNG/JPEG)</span>
                <input type="file" multiple accept="application/pdf,image/png,image/jpeg" onChange={(e) => setFiles(e.target.files)} /></label>
              <button className="primary" disabled={loading || !files?.length}>{loading ? "Analysing…" : "Get design"}</button>
            </form>
          )}
          {source === "current" && (
            <div className="current-summary">
              <div className="current-swatches">
                {(["primary", "secondary", "text", "background"] as const).map((c) => (
                  <span key={c}><i style={{ background: current.colors[c] }} /><span className="small">{{ primary: "Primary", secondary: "Accent", text: "Text", background: "Background" }[c]}</span><code>{current.colors[c]}</code></span>
                ))}
              </div>
              <p className="small muted" style={{ margin: "10px 0 0" }}>
                Headings in <strong>{current.typography.heading_font}</strong>, body in <strong>{current.typography.body_font}</strong> · {current.header.style} header{current.logo ? " · logo included" : ""}. It's shown in the live preview — adjust anything below, or pick another source above to change it.
              </p>
            </div>
          )}
          {err && <p className="error" role="alert" style={{ marginTop: 10 }}>{err}</p>}
          {proposal && <p className="small ok-text" style={{ marginTop: 10, marginBottom: 0 }}>Design taken from {proposal.source}{proposal.fonts_seen?.length ? ` · fonts seen: ${proposal.fonts_seen.slice(0, 3).join(", ")}` : ""}.</p>}
        </div>

        <div className="card flat">
          <h3 style={{ marginTop: 0 }}>Fine-tune</h3>
          <div className="swatches">
            {(["primary", "secondary", "text", "background"] as const).map((k) => (
              <label key={k} className="palette-swatch">
                <input type="color" value={theme.colors[k]} onChange={(e) => set((t) => { t.colors[k] = e.target.value.toUpperCase(); })} aria-label={k} />
                <span className="small">{{ primary: "Primary", secondary: "Accent", text: "Text", background: "Background" }[k]}</span>
              </label>
            ))}
          </div>
          <div className="form-grid">
            <label className="field"><span className="label">Heading font</span>
              <select value={theme.typography.heading_font} onChange={(e) => set((t) => { t.typography.heading_font = e.target.value; })}>{FONTS.map((f) => <option key={f}>{f}</option>)}</select></label>
            <label className="field"><span className="label">Body font</span>
              <select value={theme.typography.body_font} onChange={(e) => set((t) => { t.typography.body_font = e.target.value; })}>{FONTS.map((f) => <option key={f}>{f}</option>)}</select></label>
            <label className="field"><span className="label">Header</span>
              <select value={theme.header.style} onChange={(e) => set((t) => { t.header.style = e.target.value; })}><option value="light">Light</option><option value="solid">Brand colour</option><option value="minimal">Minimal</option></select></label>
            <label className="field"><span className="label">Spacing</span>
              <select value={theme.spacing} onChange={(e) => set((t) => { t.spacing = e.target.value; })}><option value="compact">Compact</option><option value="normal">Normal</option><option value="relaxed">Relaxed</option></select></label>
          </div>
          <label className="check"><input type="checkbox" checked={!!theme.footer?.show_powered_by} onChange={(e) => set((t) => { t.footer = { ...t.footer, show_powered_by: e.target.checked }; })} /> Show “Powered by PublishPDF” in the page footer</label>
          <p className="hint">Text contrast is always kept readable: colours that are too light are darkened automatically on the page.</p>
          {reportId && <label className="check"><input type="checkbox" checked={makeDefault} onChange={(e) => setMakeDefault(e.target.checked)} /> Also save as the design for future reports</label>}
          <button className="primary lg" disabled={busy} onClick={() => onApply(theme, { makeDefault, mode: source === "website" ? "match_website" : source === "current" ? "custom" : source === "palette" ? "custom" : "reference" })}>
            {busy ? "Applying…" : applyLabel}
          </button>
        </div>
      </div>
      <LivePreview tenantId={tenantId} reportId={reportId} theme={theme} />
    </div>
  );
}

function LivePreview({ tenantId, reportId, theme }: { tenantId: string; reportId?: string; theme: ThemeT }) {
  const [html, setHtml] = useState("");
  const timer = useRef<number>();
  useEffect(() => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(async () => {
      const res = await fetch(`/api/tenants/${tenantId}/theme/preview`, {
        method: "POST", headers: { "Content-Type": "application/json", "X-PPDF-CSRF": "1" },
        body: JSON.stringify({ theme, report_id: reportId }),
      });
      if (res.ok) setHtml(await res.text());
    }, 300);
    return () => window.clearTimeout(timer.current);
  }, [tenantId, reportId, theme]);
  return (
    <div className="card live-preview" style={{ padding: 12 }}>
      <div className="toolbar" style={{ marginBottom: 8 }}><strong>Live preview</strong><span className="muted small">Your report in this design</span></div>
      <iframe title="Design preview" srcDoc={html} sandbox="allow-same-origin" />
    </div>
  );
}
