import { useEffect, useState, type ReactNode } from "react";
import { Link, Navigate } from "react-router-dom";
import { useMe } from "../App";
import { workspacePath } from "../enter";
import {
  IconChart, IconCheckCircle, IconDesign, IconDocument, IconGlobe, IconList, IconReports, IconShield, IconSparkle, IconUpload, IconUsers,
} from "../components/Icons";
import "../landing.css";

/* Every claim on this page is something the product does today. Figures in the stats
 * strip are measured results from our own test runs, and are labelled as such. */

const STEPS = [
  {
    key: "extract", title: "Extract & verify", icon: <IconUpload size={20} />,
    lead: "Upload the PDF. Every figure is read and checked against the page before anything is built.",
    points: ["Text pages and scanned pages both supported", "Nine independent checks on every figure", "Only real disagreements reach a person"],
  },
  {
    key: "design", title: "Design", icon: <IconDesign size={20} />,
    lead: "Keep your saved look, or take colours, fonts and logo from your website, a palette or a brand PDF.",
    points: ["Live preview of your report as you tweak", "Text contrast kept readable automatically", "Save it as the default for future reports"],
  },
  {
    key: "review", title: "Review", icon: <IconCheckCircle size={20} />,
    lead: "See the web page side by side with the PDF. Flagged values show a crop of the PDF next to what was read.",
    points: ["Confirm many values at once in a side-by-side grid", "Every decision logged with name and time", "Colleagues can flag anything for an admin"],
  },
  {
    key: "publish", title: "Publish", icon: <IconGlobe size={20} />,
    lead: "Approve and it goes live on your own domain — one page per report, with the PDF one click away.",
    points: ["Your domain, with a preview host before going live", "Versions kept; roll back in one click", "Markdown, JSON and CSV downloads alongside"],
  },
];

const CHECKS: [string, string, string][] = [
  ["Source trace", "Finds each value at its recorded place in the PDF.", "A figure whose position doesn't hold that value"],
  ["Second PDF parser", "Re-reads every position with a different PDF engine.", "Text-layer quirks one engine misreads"],
  ["Visual re-read", "Renders the page and reads each figure again from the image.", "Hidden text saying 46.20 over a printed 48.20"],
  ["Arithmetic", "Totals add up; stated % changes match their figures.", "A total that isn't the sum of its rows"],
  ["Cross-table", "The same line and period agree in every table.", "Revenue shown differently in two tables"],
  ["Periods & units", "Each value's period and unit match its table.", "Crore and lakh mixed without a label"],
  ["Web page check", "Every number on the page exists in the data, character for character.", "Any number the page would show that the PDF doesn't"],
  ["Completeness", "Numbers on each PDF page vs numbers captured.", "A table that was only partly picked up"],
  ["Human review", "Low-confidence scans go to a person unless an independent re-read agrees.", "A smudged digit on a scanned page"],
];

const FEATURES: [ReactNode, string, string][] = [
  [<IconReports size={22} />, "Looks like your PDF", "Photos, colours, charts and columns stay as designed — the words are real text on top, and running headers, page numbers and blank margins are trimmed so it reads as one web page."],
  [<IconDocument size={22} />, "Readable on phones", "On small screens each section re-flows into a single column: headings, paragraphs, tables, and the charts as crops with their labels."],
  [<IconGlobe size={22} />, "Found by search and AI", "Server-built HTML with every figure as text, structured data for search engines, a sitemap, llms.txt, and Markdown/JSON/CSV copies."],
  [<IconDesign size={22} />, "On brand", "Match your website, a palette or a brand PDF; fonts, colours, header and spacing are yours to fine-tune."],
  [<IconList size={22} />, "Contents that work", "The PDF's contents page becomes a menu that jumps to each section; links printed in the PDF become working links."],
  [<IconUsers size={22} />, "Built for teams", "Admins and reviewers, an audit log of every action, version history with one-click rollback."],
  [<IconChart size={22} />, "Analytics", "See how each published report is read."],
  [<IconShield size={22} />, "Your data stays yours", "Each workspace is isolated, and the PDF always remains the official document linked from every page."],
];

const FAQS: [string, string][] = [
  ["Can a number ever be changed or made up?", "No. Figures flow from the PDF by reference and are shown exactly as printed. If any number on the generated page isn't in the extracted data, character for character, the page can't be published."],
  ["What happens when a check disagrees?", "Only genuine disagreements reach a person. The review screen shows a crop of the PDF next to the value we read; you confirm, correct, or mark it as not a figure — one at a time or many at once."],
  ["Does it use AI?", "The pipeline is deterministic. An optional AI double-check can look at flagged figures only; it can confirm a value but never supplies one, and you see the estimated credits before it runs."],
  ["Will the page look like our report?", "Yes — the web edition keeps the report's own layout, photos and charts, with the text as real HTML on top. On phones it re-flows into one readable column."],
  ["How long does processing take?", "In our tests a 20-page FAQ document went from upload to a fully checked page in about 5 seconds, and a 404-page annual report in about 3 minutes."],
  ["Can we use our own domain?", "Yes. Reports publish to a preview host first, then to your domain once it's connected."],
];

/** Fade sections in as they scroll into view. Content is visible by default; the effect
 * only switches on once the sections exist (and never for reduced-motion users). */
function useReveal(ready: boolean) {
  useEffect(() => {
    const root = document.querySelector<HTMLElement>(".lp");
    if (!ready || !root || !("IntersectionObserver" in window) || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const els = Array.from(root.querySelectorAll<HTMLElement>("[data-reveal]"));
    const below = els.filter((e) => e.getBoundingClientRect().top > innerHeight * 0.9);   // what's already on screen stays put
    below.forEach((e) => e.classList.add("pending"));
    const io = new IntersectionObserver((entries) => entries.forEach((e) => {
      if (e.isIntersecting) { e.target.classList.remove("pending"); io.unobserve(e.target); }
    }), { threshold: 0.12 });
    below.forEach((e) => io.observe(e));
    return () => { io.disconnect(); below.forEach((e) => e.classList.remove("pending")); };
  }, [ready]);
}

/** Drag to compare a PDF page with its web edition. */
function BeforeAfter() {
  const [pos, setPos] = useState(52);
  return (
    <div className="ba" style={{ ["--pos" as string]: `${pos}%` }}>
      <div className="ba-pane ba-pdf" aria-hidden>
        <div className="mock-page">
          <div className="mp-rule" />
          <div className="mp-head">ITC Limited · Report and Accounts</div>
          <div className="mp-title">Financial Highlights</div>
          <div className="mp-lines"><i /><i /><i /><i className="s" /></div>
          <div className="mp-bars">{[40, 55, 48, 70, 62, 84].map((h, i) => <b key={i} style={{ height: `${h}%` }} />)}</div>
          <div className="mp-lines"><i /><i className="s" /></div>
          <div className="mp-foot">Page 12</div>
        </div>
        <span className="ba-tag">PDF</span>
      </div>
      <div className="ba-pane ba-web" aria-hidden>
        <div className="mock-web">
          <div className="mw-bar"><span className="mw-dot" /><span className="mw-url">ir.yourcompany.com/fy2026/report</span></div>
          <div className="mw-body">
            <div className="mw-side"><i /><i className="on" /><i /><i /></div>
            <div className="mw-main">
              <div className="mp-title">Financial Highlights</div>
              <p className="mw-text">Revenue rose to <mark>₹ 80,867 cr</mark> while EBITDA reached <mark>₹ 25,208 cr</mark>.</p>
              <div className="mp-bars">{[40, 55, 48, 70, 62, 84].map((h, i) => <b key={i} style={{ height: `${h}%` }} />)}</div>
              <div className="mw-chips"><span>Selectable text</span><span>Search-ready</span><span>Phone-friendly</span></div>
            </div>
          </div>
        </div>
        <span className="ba-tag web">Web page</span>
      </div>
      <div className="ba-handle" aria-hidden><span>⟷</span></div>
      <input className="ba-range" type="range" min={6} max={94} value={pos} onChange={(e) => setPos(Number(e.target.value))}
        aria-label="Compare the PDF with the web page" />
    </div>
  );
}

function HowItWorks() {
  const [i, setI] = useState(0);
  const [paused, setPaused] = useState(false);
  useEffect(() => {
    if (paused || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const t = setTimeout(() => setI((x) => (x + 1) % STEPS.length), 5200);
    return () => clearTimeout(t);
  }, [i, paused]);
  const s = STEPS[i];
  return (
    <div className="hiw" onMouseEnter={() => setPaused(true)} onMouseLeave={() => setPaused(false)}>
      <div className="hiw-tabs" role="tablist" aria-label="How it works">
        {STEPS.map((st, k) => (
          <button key={st.key} role="tab" aria-selected={k === i} className={`hiw-tab ${k === i ? "on" : ""} ${k < i ? "done" : ""}`} onClick={() => { setI(k); setPaused(true); }}>
            <span className="hiw-n">{k + 1}</span>
            <span className="hiw-t">{st.title}</span>
            {k === i && !paused && <span className="hiw-timer" />}
          </button>
        ))}
      </div>
      <div className="hiw-panel" role="tabpanel" key={s.key}>
        <div className="hiw-copy">
          <span className="icon-chip lg">{s.icon}</span>
          <h3>{s.title}</h3>
          <p>{s.lead}</p>
          <ul>{s.points.map((p) => <li key={p}><IconCheckCircle size={16} />{p}</li>)}</ul>
        </div>
        <div className={`hiw-art art-${s.key}`} aria-hidden>
          {s.key === "extract" && (<div className="art-scan"><div className="scan-page"><i /><i /><i /><i /><i /></div><div className="scan-beam" /><div className="scan-list">{["48.20", "1,234.56", "(12.30)", "12.4%"].map((v) => <span key={v}><IconCheckCircle size={14} /> {v}</span>)}</div></div>)}
          {s.key === "design" && (<div className="art-design">{["#6246EA", "#1F8A80", "#E0457B", "#F5A623"].map((c) => <span key={c} style={{ background: c }} />)}<div className="art-card"><b /><i /><i /><em /></div></div>)}
          {s.key === "review" && (<div className="art-review">{[0, 1, 2, 3].map((k) => <div key={k} className={`rv ${k === 2 ? "flag" : ""}`}><i /><span>{k === 2 ? "check" : "confirmed"}</span></div>)}</div>)}
          {s.key === "publish" && (<div className="art-publish"><div className="pub-url"><IconGlobe size={14} /> ir.yourcompany.com</div><div className="pub-live"><span className="dot" /> Live</div><div className="pub-ver"><span>v3 · live</span><span>v2</span><span>v1</span></div></div>)}
        </div>
      </div>
    </div>
  );
}

function Checks() {
  const [open, setOpen] = useState(2);
  return (
    <div className="checks">
      {CHECKS.map(([name, what, catches], k) => (
        <button key={name} type="button" className={`check-card ${open === k ? "on" : ""}`} onClick={() => setOpen(k)} onMouseEnter={() => setOpen(k)} aria-expanded={open === k}>
          <span className="check-n">{String(k + 1).padStart(2, "0")}</span>
          <strong>{name}</strong>
          <span className="check-what">{what}</span>
          <span className="check-catch"><em>Catches</em> {catches}</span>
        </button>
      ))}
    </div>
  );
}

/** The same arithmetic as the platform's estimate, at its default rates. */
function CreditEstimator() {
  const [items, setItems] = useState(144);
  const batches = Math.ceil(items / 12);
  const tin = items * (258 + 8) + batches * 160;
  const tout = items * 18;
  const usd = tin * 0.30 / 1e6 + tout * 2.50 / 1e6;
  const credits = usd > 0 ? Math.ceil(usd / 0.01 - 1e-9) : 0;
  return (
    <div className="est">
      <label className="est-label" htmlFor="est-range">Flagged figures to double-check: <strong>{items}</strong></label>
      <input id="est-range" type="range" min={0} max={1000} step={4} value={items} onChange={(e) => setItems(Number(e.target.value))} />
      <div className="est-out">
        <div><span>{(tin + tout).toLocaleString()}</span><em>tokens</em></div>
        <div><span>${usd.toFixed(3)}</span><em>AI cost</em></div>
        <div className="hl"><span>{credits}</span><em>credits</em></div>
      </div>
      <p className="est-note">At the platform's default rates (1 credit = $0.01). Your admin sets the actual prices; every report shows its own estimate before anything runs.</p>
    </div>
  );
}

export default function Landing() {
  const me = useMe();
  useReveal(!me.isPending);
  if (me.isPending) return <p className="center muted">Loading…</p>;
  if (me.data?.fully_authenticated) {
    const path = workspacePath(me.data);
    if (path !== "/") return <Navigate to={path} replace />;
  }

  return (
    <div className="lp">
      <header className="lp-bar">
        <img src="/brand/publishpdf-logo-full-color.svg" alt="PublishPDF" width="148" height="32" />
        <nav className="lp-nav" aria-label="Page">
          <a href="#how">How it works</a><a href="#accuracy">Accuracy</a><a href="#features">Features</a><a href="#faq">FAQ</a>
        </nav>
        <div className="lp-bar-actions">
          <Link className="button secondary" to="/login">Sign in</Link>
          <Link className="button primary" to="/login">Try the demo</Link>
        </div>
      </header>

      <main>
        <section className="lp-hero">
          <div className="lp-hero-copy" data-reveal>
            <p className="lp-pill"><IconSparkle size={14} /> PDF reports, rebuilt for the web</p>
            <h1>Where your <span className="hl-box">reports</span> meet the web</h1>
            <p className="lp-lead">Upload a results PDF, annual report or investor deck. PublishPDF checks every figure against the PDF and publishes a page that looks like your report — readable on any screen, found by search and AI.</p>
            <div className="lp-cta">
              <Link className="button primary lg" to="/login">Start with the demo <span aria-hidden>→</span></Link>
              <a className="button secondary lg" href="#how">See how it works</a>
            </div>
            <p className="lp-trust"><IconShield size={16} /> The PDF stays the official document — every page links back to it.</p>
          </div>
          <div className="lp-hero-art" data-reveal><BeforeAfter /><p className="lp-hint">Drag to compare</p></div>
        </section>

        <section className="lp-stats" data-reveal aria-label="Measured in our tests">
          <div><strong>~5 s</strong><span>20-page report, upload to fully checked page</span></div>
          <div><strong>~3 min</strong><span>404-page annual report, end to end</span></div>
          <div><strong>40 / 40</strong><span>deliberately corrupted figures caught</span></div>
          <div><strong>0</strong><span>numbers re-typed — every figure shown as printed</span></div>
          <p className="lp-stats-note">Measured in our own test runs.</p>
        </section>

        <section className="lp-section" id="how">
          <div className="lp-head" data-reveal><p className="lp-kicker">How it works</p><h2>From PDF to published page in four steps</h2></div>
          <div data-reveal><HowItWorks /></div>
        </section>

        <section className="lp-section lp-tint" id="accuracy">
          <div className="lp-head" data-reveal><p className="lp-kicker">Zero-tolerance figures</p><h2>Nine independent checks on every number</h2><p>A number is published only when nothing blocks it. Hover a check to see what it catches.</p></div>
          <div data-reveal><Checks /></div>
        </section>

        <section className="lp-section" id="features">
          <div className="lp-head" data-reveal><p className="lp-kicker">Features</p><h2>Everything a report page needs</h2></div>
          <div className="features">
            {FEATURES.map(([icon, title, body]) => (
              <div className="feature" key={title} data-reveal>
                <span className="icon-chip lg">{icon}</span>
                <h3>{title}</h3>
                <p>{body}</p>
              </div>
            ))}
          </div>
        </section>

        <section className="lp-section lp-split" id="ai">
          <div data-reveal>
            <p className="lp-kicker">Optional AI double-check</p>
            <h2>Clear flagged figures faster — and know the cost first</h2>
            <p className="lp-lead small">The AI looks at a crop of the PDF for each flagged figure. It can only confirm the value we read, never type one. Try the estimate:</p>
          </div>
          <div data-reveal><CreditEstimator /></div>
        </section>

        <section className="lp-section" id="faq">
          <div className="lp-head" data-reveal><p className="lp-kicker">FAQ</p><h2>Questions teams ask</h2></div>
          <div className="faq" data-reveal>
            {FAQS.map(([q, a], k) => (
              <details key={q} open={k === 0}><summary>{q}<span className="faq-x" aria-hidden /></summary><p>{a}</p></details>
            ))}
          </div>
        </section>

        <section className="lp-final" data-reveal>
          <h2>See your own report as a web page</h2>
          <p>Try the demo workspace — no setup needed.</p>
          <div className="lp-cta center-cta">
            <Link className="button primary lg" to="/login">Try the demo <span aria-hidden>→</span></Link>
            <Link className="button secondary lg" to="/login">Sign in</Link>
          </div>
        </section>
      </main>

      <footer className="lp-foot">
        <img src="/brand/publishpdf-logo-full-color.svg" alt="PublishPDF" width="120" height="26" />
        <span>The PDF stays the official document. Your page links back to it.</span>
      </footer>
    </div>
  );
}
