"""Static site generation from an approved schema.

Output is a dict {relative_path: bytes}. HTML carries every figure as its exact PDF
string. URLs use ORIGIN_PLACEHOLDER, substituted when served, so a published
(immutable) bundle picks up the client's custom domain the moment it goes live
without re-rendering its content.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import re
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from app.render import pages as pages_mod
from app.render import story
from app.render import theme as theming

ORIGIN_PLACEHOLDER = "__PPDF_ORIGIN__"
TEMPLATES = Path(__file__).parent / "templates"

REPORT_TYPE_SLUG = {"quarterly_results": "results", "investor_presentation": "investor-presentation",
                    "annual_report": "annual-report", "other": "report"}
REPORT_TYPE_LABEL = {"quarterly_results": "results", "investor_presentation": "investor presentation",
                     "annual_report": "annual report", "other": "report"}
SECTION_TYPE_LABEL = {"highlights": "Highlights", "profit_and_loss": "Profit and loss",
                      "balance_sheet": "Balance sheet", "cash_flow": "Cash flow",
                      "segment_results": "Segment results", "management_commentary": "Management commentary",
                      "outlook": "Outlook", "notes": "Notes", "other": "Overview"}
UNIT_LABEL = {"crore": "crore", "lakh": "lakh", "million": "million", "billion": "billion",
              "thousand": "thousand", "percent": "percent", "per_share": "per share", "absolute": "units"}


def report_base_path(meta: dict) -> str:
    """/fy2026/q2/results/   ·   /fy2026/annual-report/   (PLAN D8)."""
    fy = f"fy{meta['fiscal_year']}"
    t = REPORT_TYPE_SLUG[meta["report_type"]]
    if meta["period"] == "fy":
        return f"/{fy}/{t}/"
    return f"/{fy}/{meta['period']}/{t}/"


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"]),
                      trim_blocks=False, lstrip_blocks=False)
    env.globals["section_type_label"] = lambda t: SECTION_TYPE_LABEL.get(t, "Section")
    env.globals["unit_label"] = lambda u: UNIT_LABEL.get(u, u)
    env.globals["bullet_runs"] = story.bullet_runs
    env.globals["lead_label"] = story.lead_label
    env.globals["linkify"] = story.linkify
    return env


_ENV = _env()


def disclaimer_html(text: str | None) -> str:
    """Client-supplied disclaimer -> safe HTML. Plain text only: paragraphs from blank
    lines, **bold**, and https links written as [label](https://...)."""
    if not text:
        return ""
    out = []
    for para in re.split(r"\n\s*\n", text.strip()):
        esc = html.escape(para)
        esc = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc)
        esc = re.sub(r"\[([^\]]{1,200})\]\((https://[^\s)\"'<>]{1,500})\)", r'<a href="\2" rel="nofollow">\1</a>', esc)
        out.append(f"<p>{esc.replace(chr(10), '<br>')}</p>")
    return "".join(out)


BASE_CSS = """
:root{--c-primary-soft:color-mix(in srgb,var(--c-primary) 9%,var(--c-background));--c-primary-line:color-mix(in srgb,var(--c-primary) 22%,var(--c-border));--c-secondary-soft:color-mix(in srgb,var(--c-secondary) 14%,var(--c-background));--radius:18px;--shadow-1:0 1px 2px rgba(16,24,40,.05),0 1px 3px rgba(16,24,40,.06);--shadow-2:0 12px 32px -12px rgba(16,24,40,.18),0 4px 10px -4px rgba(16,24,40,.08);--shadow-3:0 32px 64px -24px rgba(16,24,40,.32),0 12px 24px -12px rgba(16,24,40,.14)}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%;scroll-behavior:smooth}
@media (prefers-reduced-motion:reduce){html{scroll-behavior:auto}*{transition:none!important}}
body{margin:0;background:var(--c-background);color:var(--c-text);font:var(--base-size)/1.65 var(--font-body);-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
img{max-width:100%;height:auto;display:block}
h1,h2,h3{font-family:var(--font-heading);line-height:1.15;letter-spacing:-.02em;margin:0 0 .5em;text-wrap:balance}
h1{font-size:clamp(2em,4.6vw,3.4em);font-weight:800;letter-spacing:-.035em}h2{font-size:clamp(1.4em,2.4vw,1.9em);font-weight:750}h3{font-size:1.15em}
p{margin:0 0 1em;text-wrap:pretty}a{color:var(--c-primary);text-underline-offset:.18em;text-decoration-thickness:1px}
a:focus-visible,[tabindex]:focus-visible,summary:focus-visible,.rows-toggle:focus-visible+label{outline:3px solid var(--c-primary);outline-offset:3px;border-radius:6px}
.wrap{max-width:1200px;margin:0 auto;padding:0 24px}
.skip{position:absolute;left:-999px}.skip:focus{left:8px;top:8px;background:var(--c-background);padding:8px;z-index:9}
.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.muted{color:var(--c-muted)}.fine{color:var(--c-muted);font-size:.85em}
/* header */
.site-header{background:color-mix(in srgb,var(--c-header-bg) 82%,transparent);-webkit-backdrop-filter:saturate(1.6) blur(14px);backdrop-filter:saturate(1.6) blur(14px);color:var(--c-header-text);border-bottom:1px solid color-mix(in srgb,var(--c-border) 70%,transparent);position:sticky;top:0;z-index:20}
.site-header .wrap{display:flex;align-items:center;justify-content:space-between;gap:16px;min-height:68px;flex-wrap:wrap}
.site-header a{color:var(--c-header-text);text-decoration:none}
.site-header nav{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.site-header nav a{padding:8px 12px;border-radius:999px;font-weight:550;font-size:.95em;opacity:.85}
.site-header nav a:hover{opacity:1;background:color-mix(in srgb,var(--c-header-text) 7%,transparent)}
.site-header nav a.nav-cta{background:var(--c-primary);color:#fff;opacity:1;padding:8px 16px;box-shadow:var(--shadow-1)}
.site-header nav a.nav-cta:hover{background:color-mix(in srgb,var(--c-primary) 88%,#000)}
.header-solid .site-header{background:var(--c-primary);border-bottom:0}
.header-solid .site-header a{color:#fff}.header-solid .site-header nav a.nav-cta{background:#fff;color:var(--c-primary)}
.header-minimal .site-header{border-bottom:0;position:static;background:transparent;backdrop-filter:none}
.brand{display:flex;align-items:center;gap:12px;font-weight:750;font-size:1.05em;letter-spacing:-.01em}.brand img{display:block;max-height:38px;width:auto}
/* buttons, chips */
.btn{display:inline-flex;align-items:center;gap:8px;padding:.8em 1.25em;border-radius:12px;border:1px solid var(--c-border);text-decoration:none;font-weight:650;color:var(--c-text);background:var(--c-background);box-shadow:var(--shadow-1);transition:transform .15s,box-shadow .15s,border-color .15s}
.btn:hover{transform:translateY(-1px);box-shadow:var(--shadow-2);border-color:var(--c-primary-line)}
.btn-primary{background:var(--c-primary);border-color:var(--c-primary);color:#fff}
.btn-primary:hover{background:color-mix(in srgb,var(--c-primary) 88%,#000);border-color:transparent}
.btn .arr{transition:transform .15s}.btn:hover .arr{transform:translateX(3px)}
.toc-title{font-weight:700;text-transform:uppercase;letter-spacing:.1em;font-size:.76em;color:var(--c-muted);margin:0 0 10px 12px}
.table-wrap{position:relative;overflow-x:auto;margin:1.4em 0;border:1px solid var(--c-border);border-radius:var(--radius);background:var(--c-background);box-shadow:var(--shadow-1)}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;font-size:.94em}
caption{text-align:left;padding:16px 18px;background:var(--c-surface);border-bottom:1px solid var(--c-border)}
caption>span{display:block}.cap-title{font-weight:750;font-size:1.05em;font-family:var(--font-heading)}.cap-note,.cap-meta{color:var(--c-muted);font-size:.86em}
th,td{padding:calc(10px*var(--space)) 18px;border-bottom:1px solid var(--c-border);vertical-align:top}
thead th{text-align:right;background:var(--c-surface);font-weight:700;white-space:nowrap;font-size:.86em;color:var(--c-muted);text-transform:uppercase;letter-spacing:.04em}
tbody th{text-align:left;font-weight:500}td{text-align:right;white-space:nowrap}
tbody tr:hover{background:var(--c-primary-soft)}
tbody tr[data-emph] th,tbody tr[data-emph] td{font-weight:750}
tbody tr:last-child th,tbody tr:last-child td{border-bottom:0}
data.neg{color:#B42318}
.rows-toggle{position:absolute;opacity:0;width:1px;height:1px}
.rows-toggle-label{display:inline-block;margin:.2em 0 -.4em;font-weight:650;color:var(--c-primary);cursor:pointer;font-size:.9em}
.rows-toggle-label .hide,.rows-toggle:checked+.rows-toggle-label .show{display:none}
.rows-toggle:checked+.rows-toggle-label .hide{display:inline}
.rows-toggle:not(:checked)~.table-wrap.is-long tr.extra{display:none}
.stat-tile{display:inline-flex;flex-direction:column;gap:2px;min-width:190px;margin:0 12px 12px 0;padding:16px 18px;border:1px solid var(--c-border);border-radius:var(--radius);background:var(--c-background);box-shadow:var(--shadow-1);border-top:4px solid var(--c-primary);vertical-align:top}
.stat-label{color:var(--c-muted);font-weight:600;font-size:.9em}.stat-value{font-family:var(--font-heading);font-size:1.8em;font-weight:800;font-variant-numeric:tabular-nums}
.chart-missing{background:var(--c-surface);border:1px dashed var(--c-border);padding:16px;border-radius:14px;margin:1em 0}
.archive{list-style:none;padding:0;display:grid;gap:10px}.archive a{display:block;padding:16px 18px;border:1px solid var(--c-border);border-radius:14px;text-decoration:none;font-weight:650;box-shadow:var(--shadow-1)}
.site-index{padding:56px 0}
/* footer */
.site-footer{margin-top:5em;background:var(--c-surface);border-top:1px solid var(--c-border);padding:40px 0 48px;font-size:.94em}
.site-footer .official{font-weight:700;font-size:1.02em}
.downloads a{font-weight:600}
@media (max-width:640px){.wrap{padding:0 16px}body{font-size:calc(var(--base-size) - 1px)}th,td{padding:8px 10px}.site-header nav a:not(.nav-cta){display:none}}
@media print{.site-header,.skip,.rows-toggle-label{display:none}.rows-toggle~.table-wrap tr.extra{display:table-row!important}}
/* ---- document layout ("same as the PDF") ---- */
.layout-document{background:color-mix(in srgb,var(--c-primary) 5%,var(--c-surface))}
.layout-document .site-footer{margin-top:3em}
.doc-shell{max-width:1320px;margin:0 auto;padding:32px 24px 0;display:grid;grid-template-columns:230px minmax(0,900px);gap:40px;justify-content:center}
.doc-rail{position:sticky;top:92px;align-self:start;max-height:calc(100vh - 110px);overflow:auto;font-size:.84em;padding:4px 6px 12px 0}
.doc-rail ol{list-style:none;margin:0;padding:0;border-left:2px solid var(--c-border)}
.doc-rail a{display:flex;gap:8px;padding:6px 10px;margin-left:-2px;border-left:2px solid transparent;color:var(--c-muted);text-decoration:none;line-height:1.35}
.doc-rail a:hover{color:var(--c-primary);border-left-color:var(--c-primary);background:color-mix(in srgb,var(--c-background) 70%,transparent)}
.doc-rail .tq{font-weight:800;color:var(--c-text);min-width:2.3em;flex:none}
@media (max-width:1180px){.doc-shell{grid-template-columns:minmax(0,900px)}.doc-rail{display:none}}
.sheet{position:relative;background:var(--c-background);border-radius:6px;box-shadow:0 1px 2px rgba(16,24,40,.06),0 18px 48px -24px rgba(16,24,40,.28);padding:clamp(40px,6vw,72px) clamp(20px,6.5vw,80px) clamp(28px,5vw,56px);margin:0 0 28px}
.sheet::before{content:"";position:absolute;top:clamp(18px,2.6vw,28px);left:clamp(20px,6.5vw,80px);right:clamp(20px,6.5vw,80px);height:3px;border-radius:3px;background:var(--c-primary)}
.cover-sheet{text-align:center;padding-bottom:clamp(36px,5vw,64px)}
.cover-art{width:var(--w);max-width:100%;margin:clamp(16px,3vw,40px) auto 20px}
.cover-page{padding-top:clamp(28px,4vw,44px)}.cover-page::before{display:none}
.cover-img{width:min(100%,520px);margin:0 auto 28px;border-radius:4px;box-shadow:0 0 0 1px var(--c-border),0 24px 48px -28px rgba(16,24,40,.45)}
.cover-page h1{font-size:clamp(1.5em,3vw,2.1em)}
.cover-eyebrow{margin:0;color:var(--c-primary);font-weight:800;letter-spacing:.14em;text-transform:uppercase;font-size:.82em}
.cover-sheet h1{margin:.25em 0 .6em;font-size:clamp(1.9em,4.4vw,3em)}.cover-sheet h1 span{display:block;font-weight:600;color:var(--c-muted);font-size:.5em;letter-spacing:-.01em;margin-top:.3em}
.cover-sheet .dv{margin-left:auto;margin-right:auto}
.cover-actions{display:flex;gap:12px;justify-content:center;flex-wrap:wrap;margin:28px 0 0}
.doc-sec{display:flow-root;padding:30px 0;border-top:1px solid var(--c-border);scroll-margin-top:92px}
.sheet>.doc-sec:first-child{border-top:0;padding-top:4px}
.doc-q{display:flex;gap:.55em;align-items:baseline;font-size:clamp(1.06em,1.6vw,1.18em);font-weight:750;line-height:1.38;letter-spacing:-.012em;margin:0 0 .85em}
h1.doc-q{font-size:clamp(1.45em,3vw,2em);line-height:1.25}
.q-mark{flex:none;color:var(--c-primary);font-weight:850}
.doc-sub{font-weight:650;color:var(--c-text)}
.doc-body{font-size:1.04em;line-height:1.76;color:color-mix(in srgb,var(--c-text) 88%,var(--c-background))}
.doc-body p{margin:0 0 .95em}
.doc-body p.li{position:relative;padding-left:1.5em;margin-bottom:.65em}
.doc-body p.li::before{content:"";position:absolute;left:.3em;top:.74em;width:.42em;height:.42em;border-radius:1px;background:var(--c-primary);transform:rotate(45deg)}
.lead-label{color:var(--c-text);font-weight:750}
.doc-h{font-size:1.3em;margin:.2em 0 .7em;text-align:center}
.doc .ext{color:var(--c-primary);word-break:break-word;overflow-wrap:anywhere;font-style:italic}
.dv{width:var(--w);max-width:100%;margin:22px auto 24px}
.dv-img{display:block;border-radius:8px;cursor:zoom-in;transition:box-shadow .2s,transform .2s}
.dv-img:hover{box-shadow:0 12px 30px -12px rgba(16,24,40,.35);transform:translateY(-1px)}
.dv img{width:100%;border-radius:8px}
.dv figcaption{margin-top:6px;text-align:right;font-size:.76em}.dv figcaption a{color:var(--c-muted);text-decoration:none}.dv figcaption a:hover{color:var(--c-primary)}
.dv.fl-right{float:right;margin:4px 0 14px 30px}.dv.fl-left{float:left;margin:4px 30px 14px 0}
.dv-data{clear:both;margin:-10px 0 20px;font-size:.92em}
@media (max-width:720px){.dv{width:100%}.dv.fl-right,.dv.fl-left{float:none;margin:18px 0}.doc-shell{padding:0}.sheet{border-radius:0;margin-bottom:10px;box-shadow:none}}
.doc .table-wrap{box-shadow:none;border-radius:10px}
.doc thead th{background:color-mix(in srgb,var(--c-primary) 82%,#000);color:#fff;text-transform:none;letter-spacing:0;font-size:.9em}
.toc-wrap{max-width:720px;margin:10px auto 8px}
.toc-table{font-size:.95em}.toc-table thead th:first-child{text-align:left}
.toc-table tbody th{font-weight:500;padding:9px 16px}.toc-table td{width:1%;color:var(--c-muted);padding:9px 16px}
.toc-table tbody th a{color:var(--c-text);text-decoration:underline;text-decoration-color:color-mix(in srgb,var(--c-primary) 45%,transparent);text-underline-offset:.22em}
.toc-table tbody th a:hover{color:var(--c-primary);text-decoration-color:var(--c-primary)}
.toc-table tbody tr:nth-child(even){background:color-mix(in srgb,var(--c-primary) 4%,var(--c-background))}
.doc-note{text-align:center;margin:8px auto 0;max-width:40em}
@media print{.layout-document{background:#fff}.sheet{box-shadow:none;padding:0}.doc-rail,.cover-actions{display:none}}
.powered-by{margin:1.2em 0 0;font-size:.85em}.powered-by a{display:inline-flex;align-items:center;gap:6px;color:var(--c-muted);text-decoration:none}.powered-by a:hover span{text-decoration:underline}.powered-by svg{flex:none}.disclaimer{color:var(--c-muted);font-size:.9em;max-width:60em}
.card{font-size:1.1em}
details.chart-data{margin:-.6em 0 1.6em}details.chart-data summary{cursor:pointer;font-weight:650;color:var(--c-primary);font-size:.9em}
.doc-sec.is-q .doc-body{margin-left:var(--qm)}
.doc{--qm:3.1em}
.doc-sec.is-q>.doc-q{display:grid;grid-template-columns:var(--qm) minmax(0,1fr);gap:0}
.lead-label{display:block;margin-bottom:.15em}
@media (max-width:640px){.doc{--qm:2.6em}}
"""


# A page is design-led — shown as designed, like the PDF — when (no tables, under 300 words):
DESIGNED_MIN_SHARE = 0.40          # pictures cover this much of the page, or
DESIGNED_MANY_VISUALS = 5          # this many pictures covering
DESIGNED_MANY_SHARE = 0.20         #   at least this much, or
DESIGNED_MIN_VISUALS = 3           # a few pictures with
DESIGNED_MAX_CAPTION_WORDS = 14    #   captions (not prose) for text
DESIGNED_MAX_WORDS = 300           # never a page of prose with a photo on it
GRAPHIC_FRAGMENT_WORDS = 5   # a "paragraph" this short, among many, is a label inside a graphic


def _ai_pages(schema: dict, pdf_bytes: bytes, layouts: dict[int, dict], base: str, art: dict) -> dict[int, Markup]:
    """Web sections for pages with an AI layout. The layout is used only if it was made for
    exactly these elements (same fingerprint); otherwise the page keeps its designed look."""
    if not layouts:
        return {}
    import pymupdf

    from app.render import ai_layout, elements
    out: dict[int, Markup] = {}
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        for n, lay in sorted(layouts.items()):
            printed = f"pages/p{n:04d}-print.webp"
            if printed not in art or n > doc.page_count:
                continue
            page = doc[n - 1]
            els = elements.page_elements(page, schema, n, chart_boxes(schema, n))
            if ai_layout.fingerprint(els) != lay.get("fingerprint"):
                continue
            secs = ai_layout.validate({"sections": lay.get("sections") or []}, els)
            out[n] = Markup(ai_layout.render(secs, els, printed_src=f"{ORIGIN_PLACEHOLDER}{base}{printed}",
                                             W=page.rect.width, H=page.rect.height))
    finally:
        doc.close()
    return out


def design_led_pages(schema: dict) -> set[int]:
    """Pages led by pictures, not prose (see DESIGNED_*): shown whole, or as AI-laid-out
    web sections. One definition, used by the renderer and by the AI layout step."""
    sizes = {p["page"]: (p["width"], p["height"]) for p in schema.get("pages") or []}
    page_blocks: dict[int, list] = {}
    for sec in schema["sections"]:
        for b in sec["blocks"]:
            pg = (b.get("source") or {}).get("page")
            if pg:
                page_blocks.setdefault(pg, []).append(b)
    out = set()
    for pg, bl in page_blocks.items():
        if any(b["type"] == "table" for b in bl):
            continue
        charts = [b for b in bl if b["type"] == "chart" and b["source"].get("bbox")]
        paras = [b for b in bl if b["type"] == "paragraph"]
        w, h = sizes.get(pg, (595.0, 842.0))
        share = sum((b["source"]["bbox"][2] - b["source"]["bbox"][0]) * (b["source"]["bbox"][3] - b["source"]["bbox"][1])
                    for b in charts) / max(1.0, w * h)
        total_words = sum(_words(b) for b in paras)
        avg_words = (total_words / len(paras)) if paras else 0
        picture_led = (share >= DESIGNED_MIN_SHARE
                       or (len(charts) >= DESIGNED_MANY_VISUALS and share >= DESIGNED_MANY_SHARE)
                       or (len(charts) >= DESIGNED_MIN_VISUALS and avg_words < DESIGNED_MAX_CAPTION_WORDS))
        if total_words < DESIGNED_MAX_WORDS and picture_led:
            out.add(pg)
    return out


def whole_pages(schema: dict) -> set[int]:
    """Pages shown whole rather than as web text: design-led pages, and pages with little
    text, no table and nothing to cut out (a photo spread). AI layout may rebuild these."""
    words: dict[int, int] = {}
    kinds: dict[int, set] = {}
    for sec in schema["sections"]:
        for b in sec["blocks"]:
            pg = (b.get("source") or {}).get("page")
            if not pg:
                continue
            kinds.setdefault(pg, set()).add(b["type"] if b["type"] != "chart" or b["source"].get("bbox") else "x")
            if b["type"] == "paragraph":
                words[pg] = words.get(pg, 0) + _words(b)
    sparse = {pg for pg, k in kinds.items()
              if words.get(pg, 0) < REFLOW_MIN_WORDS and "table" not in k and "chart" not in k}
    return design_led_pages(schema) | sparse


def chart_boxes(schema: dict, page: int) -> list:
    return [b["source"]["bbox"] for sec in schema["sections"] for b in sec["blocks"]
            if b["type"] == "chart" and (b.get("source") or {}).get("page") == page and b["source"].get("bbox")]


_CONTENTS_TITLE = re.compile(r"^\s*(table of )?contents\s*$", re.I | re.M)


def _contents_pages(pdf_bytes: bytes) -> set[int]:
    """The PDF's own contents page(s) near the front: page numbers mean nothing on the web."""
    import pymupdf
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        return {n for n in range(1, min(6, doc.page_count + 1)) if _CONTENTS_TITLE.search(doc[n - 1].get_text())}
    finally:
        doc.close()


def _words(b: dict) -> int:
    return sum(len(r.get("t", "").split()) + ("f" in r) for r in b.get("runs") or [])


REFLOW_MIN_WORDS = 40     # below this a page is mostly pictures: it is shown as designed, not as text

WEB_CSS = """
/* ---- web edition: the report as a web page ---- */
.layout-document:has(.web){background:var(--c-background)}
.web-hero{background:linear-gradient(180deg,var(--c-primary-soft),var(--c-background) 92%);border-bottom:1px solid var(--c-border)}
.web-hero-in{max-width:1120px;margin:0 auto;padding:clamp(40px,7vw,96px) 24px clamp(36px,5vw,72px);display:grid;gap:clamp(28px,5vw,72px);align-items:center}
.web-hero-in.has-cover{grid-template-columns:minmax(0,1.2fr) minmax(0,.8fr)}
.web-eyebrow{margin:0;color:var(--c-primary);font-weight:800;letter-spacing:.14em;text-transform:uppercase;font-size:.82em}
.web-hero h1{margin:.3em 0 0;font-size:clamp(2.2em,5.2vw,3.8em);line-height:1.05}
.web-hero h1>span:not([data-meta]){display:block;margin-top:.35em;font-weight:600;color:var(--c-muted);font-size:.46em;letter-spacing:-.01em;line-height:1.25}
.web-actions{display:flex;gap:12px;flex-wrap:wrap;margin:32px 0 0}
.web-cover{margin:0;justify-self:end;width:100%;max-width:420px;border-radius:14px;overflow:hidden;background:#fff;box-shadow:0 0 0 1px var(--c-border),var(--shadow-3);transform:rotate(1.2deg)}
.web-body{max-width:828px;margin:0 auto;padding:clamp(8px,2vw,24px) 24px 0}
.web .doc-sec{padding:clamp(34px,4.5vw,52px) 0}
.web .doc-sec:first-of-type{border-top:0}
.web .doc-q{font-size:clamp(1.3em,2.3vw,1.65em);line-height:1.28;letter-spacing:-.02em}
.web .doc-body{font-size:1.09em;line-height:1.78}
.web .doc-body p:not(.li){text-align:left;hyphens:manual}
.pa{display:block;height:0;scroll-margin-top:92px}
.web .doc-sec{scroll-margin-top:80px}
/* Visuals may run wider than the text column, up to their own size. */
.web-vis{--ww:min(var(--vw),1040px,calc(100vw - 48px - 2 * var(--qm)))}
.web-page{--ww:min(1040px,calc(100vw - 48px - 2 * var(--qm)))}
.web-vis,.web-page{width:var(--ww);margin:30px 0 34px calc((100% - var(--ww)) / 2)}
.rf-vis{position:relative;overflow:hidden;container-type:inline-size;border-radius:12px;background:#fff;box-shadow:0 0 0 1px var(--c-border),var(--shadow-1)}
.rf-vis img{position:absolute;max-width:none;height:auto}
.rf-vis-t{position:absolute;inset:0}
.rf-vis .w,.part .w{position:absolute;left:calc(var(--x)*1%);top:calc(var(--y)*1%);font-size:calc(var(--s)*1cqw);line-height:1;white-space:pre;
  word-spacing:calc(var(--ws,0)*1em);transform-origin:0 0;transform:scaleX(var(--k,1));color:#000;font-kerning:normal}
.part{position:relative;width:100%;aspect-ratio:var(--pw)/var(--ph);container-type:inline-size;overflow:hidden;background:#fff}
.web-page .part{border-radius:12px;box-shadow:0 0 0 1px var(--c-border),var(--shadow-2)}
.part-bg{position:absolute;inset:0;width:100%;height:100%;display:block;user-select:none}
.part-t{position:absolute;inset:0}
.part .pl{position:absolute;left:calc(var(--x)*1%);top:calc(var(--y)*1%);width:calc(var(--lw)*1%);height:calc(var(--lh)*1%);border-radius:2px}
.part .pl:hover,.part .pl:focus-visible{background:color-mix(in srgb,var(--c-primary) 12%,transparent)}
.web .doc-note{margin:40px auto 0;padding-top:28px;border-top:1px solid var(--c-border)}
/* AI-laid-out pages: a design page rebuilt as web sections, in the site's own style */
.ai-page{padding:clamp(28px,4vw,48px) 0;border-top:1px solid var(--c-border)}
.ai-h{color:var(--c-text);text-wrap:balance}
.ai-h-a{font-size:clamp(1.6em,3vw,2.2em);margin:.1em 0 .5em;letter-spacing:-.025em}
.ai-h-b{font-size:clamp(1.25em,2.2vw,1.55em);margin:1.4em 0 .6em}
.ai-h-c{font-size:1.05em;margin:1.2em 0 .5em;color:var(--c-primary);text-transform:uppercase;letter-spacing:.06em}
.ai-p{font-size:1.06em;line-height:1.75;margin:0 0 1em}
.ai-list{margin:0 0 1.2em;padding-left:1.2em;line-height:1.7}.ai-list li{margin:.3em 0}
.ai-cards{display:grid;grid-template-columns:repeat(var(--cols),minmax(0,1fr));gap:16px;margin:18px 0 26px}
.ai-card{background:var(--c-surface);border:1px solid var(--c-border);border-radius:16px;padding:18px 18px 8px;
  border-top:4px solid var(--c-primary)}
.ai-card-t{margin:0 0 .5em;font-size:1em;letter-spacing:.04em;text-transform:uppercase;color:var(--c-primary)}
.ai-card p{margin:0 0 .8em;line-height:1.6}
.ai-card .ai-pic{margin:0 0 12px}
.ai-stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px;margin:14px 0 26px}
.ai-stat{background:var(--c-primary-soft);border-radius:14px;padding:14px 16px;line-height:1.45;font-weight:550}
.ai-stat data:not([value=""]){font-family:var(--font-heading);font-size:1.35em;font-weight:800;color:var(--c-primary);
  letter-spacing:-.02em}
.ai-gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:18px;margin:18px 0 28px}
.ai-tile{margin:0;background:var(--c-background);border:1px solid var(--c-border);border-radius:16px;padding:12px;
  display:flex;flex-direction:column;gap:10px}
.ai-tile .ai-pic,.ai-card .ai-pic{width:100%}
.ai-tile figcaption,.ai-figure figcaption{font-size:.92em;line-height:1.5;color:var(--c-muted)}
.ai-figure{margin:22px 0 28px}
.ai-pic{position:relative;overflow:hidden;width:min(100%,var(--vw));margin:0 auto;border-radius:10px;background:#fff}
.ai-pic img{position:absolute;max-width:none;height:auto}
.ai-textcrop{display:inline-block;vertical-align:middle;margin:0}
@media (max-width:760px){.ai-cards{grid-template-columns:1fr}}
.to-top{position:fixed;right:20px;bottom:20px;width:44px;height:44px;border-radius:50%;display:grid;place-items:center;background:var(--c-primary);color:#fff;text-decoration:none;font-weight:700;box-shadow:0 8px 20px -8px rgba(0,0,0,.45)}
@media (max-width:860px){
  .web-hero-in.has-cover{grid-template-columns:1fr}
  .web-cover{justify-self:start;max-width:280px;transform:none}
}
@media (max-width:640px){
  .web-hero-in,    .web-vis{--ww:min(var(--vw),100%)}
  .web-page{--ww:100%}
  .web .doc-body{font-size:1.04em}
}
@media print{"""


_ASSETS = Path(__file__).parent / "assets"
_MARK = {"light": (_ASSETS / "publishpdf-mark-color.svg").read_text(),
         "dark": (_ASSETS / "publishpdf-mark-on-dark.svg").read_text()}


def powered_by_html(theme: dict) -> Markup:
    """Opt-in footer credit (theme.footer.show_powered_by). The mark variant follows the
    footer surface so the logo stays inside the brand's light/dark rules."""
    if not theme.get("footer", {}).get("show_powered_by"):
        return Markup("")
    dark = theming.luminance(theme["colors"]["surface"]) < 0.25
    mark = _MARK["dark" if dark else "light"].replace('width="32" height="32"', 'width="18" height="18" aria-hidden="true"')
    return Markup(f'<p class="powered-by"><a href="https://publishpdf.ai" rel="noopener">{mark}'
                  f'<span>Powered by PublishPDF</span></a></p>')


def page_css(theme: dict) -> str:
    return theming.css_variables(theme) + BASE_CSS


# ------------------------------------------------------------------ report bundle


def render_report(schema: dict, *, theme: dict, disclaimer: str | None, logo_src: str | None = None,
                  pdf_bytes: bytes | None = None, progress=None, max_pages: int | None = None,
                  pdf_page_sink=None, layouts: dict[int, dict] | None = None) -> dict[str, bytes]:
    """All files for one report version, keyed by path relative to the site root.

    With the PDF (always, in the pipeline) the report page is the page-faithful web
    edition: every PDF page as designed, its text as real HTML on top. Without it (a
    design preview on sample data) the report's text is laid out as one flowing page."""
    m = schema["metadata"]
    base = report_base_path(m)
    figures = schema["figures"]
    sections = schema["sections"]
    type_label = REPORT_TYPE_LABEL[m["report_type"]]
    title = f"{m['period_label']} {type_label} · {m['company']}"
    description = f"{m['company']} {m['period_label']} {type_label}: sections, tables and key figures taken unchanged from the published PDF."
    pdf_href = f"{ORIGIN_PLACEHOLDER}{base}source.pdf"
    css = page_css(theme)
    common = dict(
        m=m, figures=figures, sections=sections, theme=theme, css=Markup(css), company=m["company"],
        report_type_label=type_label, origin=ORIGIN_PLACEHOLDER, base_path=f"{ORIGIN_PLACEHOLDER}{base}",
        pdf_href=pdf_href, disclaimer_html=Markup(disclaimer_html(disclaimer)), logo_src=logo_src,
        powered_by=powered_by_html(theme),
        fonts_href=theming.google_fonts_href(theme),
        downloads=[{"href": f"{ORIGIN_PLACEHOLDER}{base}report.md", "title": "Markdown"},
                   {"href": f"{ORIGIN_PLACEHOLDER}{base}figures.json", "title": "Figures (JSON)"},
                   {"href": f"{ORIGIN_PLACEHOLDER}{base}figures.csv", "title": "Figures (CSV)"}],
        alternates=[{"type": "text/markdown", "href": f"{ORIGIN_PLACEHOLDER}{base}report.md", "title": "Markdown version"},
                    {"type": "application/json", "href": f"{ORIGIN_PLACEHOLDER}{base}figures.json", "title": "Figures as JSON"},
                    {"type": "text/csv", "href": f"{ORIGIN_PLACEHOLDER}{base}figures.csv", "title": "Figures as CSV"}],
    )
    href = lambda rel: f"{ORIGIN_PLACEHOLDER}{base}{rel}"  # noqa: E731
    lead_text = next((t for t in (story.plain_excerpt(s) for s in sections) if t), "")
    desc = f"{m['company']} {m['period_label']} {type_label}: {lead_text}" if lead_text else description
    files: dict[str, bytes] = {}
    if pdf_bytes:
        methods = {int(k): v for k, v in (m.get("extraction") or {}).get("methods", {}).items()}
        page_count = (m.get("source_pdf") or {}).get("page_count")
        index_links = story.index_links(schema, page_count)
        # The PDF's own contents page is left out: "In this report" does that job on the web.
        tables = {b["id"]: b for x in sections for b in x["blocks"] if b["type"] == "table"}
        skip = {tables[tid]["source"]["page"] for tid in index_links if tables[tid]["source"]["page"] <= 5}
        skip |= _contents_pages(pdf_bytes)
        vis_boxes: dict[int, list] = {}
        words: dict[int, int] = {}
        table_pages: set[int] = set()
        page_blocks: dict[int, list] = {}
        for sec in sections:
            for b in sec["blocks"]:
                pg = (b.get("source") or {}).get("page")
                if not pg:
                    continue
                page_blocks.setdefault(pg, []).append(b)
                if b["type"] == "paragraph":
                    words[pg] = words.get(pg, 0) + _words(b)
                elif b["type"] == "table":
                    table_pages.add(pg)
        # A graphic whose words came out as loose fragments ("Ghee & RTE", "#1 in Bridges")
        # is one picture: one cut-out spanning the charts and fragments, which then aren't
        # repeated as text. Other charts are cut out one by one, trimmed so one never
        # shows the top of the next.
        region_at: dict[str, str] = {}      # first block of a graphic -> its cut-out
        in_graphic: set[str] = set()
        # Design-led pages (a product spread: many pictures with short captions) read far
        # better as designed than cut into pieces: shown whole — or, with an AI layout,
        # rebuilt as web sections. Text-led pages become web text.
        designed = design_led_pages(schema)
        for pg in designed:
            in_graphic.update(b["id"] for b in page_blocks.get(pg, []))
        for pg, bl in page_blocks.items():
            if pg in designed:
                vis_boxes.pop(pg, None)
                continue
            charts = [b for b in bl if b["type"] == "chart" and b["source"].get("bbox")]
            paras = [b for b in bl if b["type"] == "paragraph" and b["source"].get("bbox")]
            frags = [b for b in paras if 0 < _words(b) <= GRAPHIC_FRAGMENT_WORDS]
            if charts and len(frags) >= 4 and len(frags) * 2 >= len(paras):
                members = charts + frags
                box = [min(b["source"]["bbox"][0] for b in members), min(b["source"]["bbox"][1] for b in members),
                       max(b["source"]["bbox"][2] for b in members), max(b["source"]["bbox"][3] for b in members)]
                inside = lambda bb: bb[0] >= box[0] - 2 and bb[1] >= box[1] - 2 and bb[2] <= box[2] + 2 and bb[3] <= box[3] + 2  # noqa: E731
                members += [b for b in paras if b not in members and inside(b["source"]["bbox"])]
                rid = f"graphic-p{pg}"
                vis_boxes[pg] = [(rid, box)]
                region_at[next(b["id"] for b in bl if b in members)] = rid
                in_graphic.update(b["id"] for b in members)
                continue
            boxes = sorted(([b["id"], list(b["source"]["bbox"])] for b in charts), key=lambda x: x[1][1])
            for (_, a), (_, nxt) in zip(boxes, boxes[1:]):
                if a[3] > nxt[1] and a[0] < nxt[2] and nxt[0] < a[2]:
                    a[3] = max(a[1] + 20, nxt[1] - 2)
            vis_boxes[pg] = [(bid, tuple(bb)) for bid, bb in boxes]
        # Only pages the web page shows as pictures need artwork: a picture-led cover, pages
        # with charts or graphics to cut out, and picture pages. Text pages are web text.
        total_pages = page_count or max([p for x in sections for p in [(x.get("source") or {}).get("page") or 0]] + [1])
        wanted = {n for n in range(1, total_pages + 1)
                  if n == 1 or n in vis_boxes or n in designed or not (words.get(n, 0) >= REFLOW_MIN_WORDS or n in table_pages)}
        shown_whole = whole_pages(schema)
        layouts = {n: lay for n, lay in (layouts or {}).items() if n in shown_whole}
        page_list, art, _ = pages_mod.render_pages(schema, pdf_bytes, page_methods=methods, progress=progress,
                                                   max_pages=max_pages, visuals=vis_boxes, pdf_page_sink=pdf_page_sink,
                                                   skip_pages=skip | (set(range(1, total_pages + 1)) - wanted),
                                                   keep_printed=set(layouts))
        files.update({base.lstrip("/") + k: v for k, v in art.items()})
        files.update({base.lstrip("/") + k: v for k, v in pages_mod.font_files().items()})
        # Charts and graphics: cut from their page's artwork, labels as real text on top.
        crops = {bid: {**v, "bg": p["bg"]} for p in page_list for bid, v in p["vis"].items()}
        for first_id, rid in region_at.items():
            if rid in crops:
                crops[first_id] = crops[rid]
        crop_pages = {p["n"] for p in page_list if p["vis"]}

        def text_page(n: int) -> bool:
            return words.get(n, 0) >= REFLOW_MIN_WORDS or n in table_pages
        # A picture-led first page is the cover; other picture pages (an infographic spread)
        # are shown as designed, where they fall. Everything else is web text.
        cover = page_list[0] if page_list and page_list[0]["n"] == 1 and not text_page(1) else None
        owner: dict[int, str] = {}            # page -> last section with content on it
        first: dict[int, str] = {}            # page -> first section with content on it
        for sec in sections:
            pgs = {b["source"]["page"] for b in sec["blocks"] if (b.get("source") or {}).get("page")}
            if (sec.get("source") or {}).get("page"):
                pgs.add(sec["source"]["page"])
            for pg in sorted(pgs):
                owner[pg] = sec["id"]
                first.setdefault(pg, sec["id"])

        def owner_of(n: int) -> str | None:
            if n in owner:
                return owner[n]
            prior = [x for x in sections if ((x.get("source") or {}).get("page") or 10 ** 9) <= n]
            return (prior[-1] if prior else sections[0])["id"] if sections else None
        pictures: dict[str, list] = {}
        for p in page_list:
            if p is not cover and (p["n"] in designed or (not text_page(p["n"]) and p["n"] not in crop_pages)):
                pictures.setdefault(owner_of(p["n"]), []).append(p)
        # Pages shown whole (the cover, design-led and picture pages) carry their own words:
        # blocks from them aren't repeated as text, and a section with nothing else to show
        # appears only as its pictures (no stray heading above a page that already has it).
        picture_pages = ({p["n"] for ps in pictures.values() for p in ps}
                         | ({cover["n"]} if cover else set()) | skip)
        for x in sections:
            for b in x["blocks"]:
                if (b.get("source") or {}).get("page") in picture_pages:
                    in_graphic.add(b["id"])
        lead = sections[0] if sections and not sections[0].get("heading") else None
        quiet = {x["id"] for x in sections
                 if x is not lead and ((x.get("source") or {}).get("page") in picture_pages)
                 and all(b["id"] in in_graphic for b in x["blocks"])}
        # "#p12" links (from the PDF's own links) land on where page 12's content starts.
        anchors: dict[str, list[int]] = {}
        for pg, sid in sorted(first.items()):
            anchors.setdefault(sid, []).append(pg)
        ai_pages = _ai_pages(schema, pdf_bytes, layouts, base, art)
        qparts = {x["id"]: story.question_parts(schema, x) for x in sections}
        front = sections[0] if sections and not sections[0].get("heading") else None
        front_blocks = [b for b in story.document_order(schema, story.document_layout(schema, {})).get(front["id"], [])
                        if (b.get("source") or {}).get("page") not in picture_pages and b["id"] not in in_graphic] \
            if front else []
        doclayout = story.document_layout(schema, {})
        css_all = Markup(css + pages_mod.fonts_css(href) + WEB_CSS)
        files[base.lstrip("/") + "index.html"] = _ENV.get_template("web.html").render(
            **{**common, "css": css_all}, page_title=title, canonical_path=base, description=desc,
            jsonld=Markup(_jsonld(schema, base)), qparts=qparts, faq=story.is_faq(schema), front=front,
            front_blocks=front_blocks, first_q=next((x for x in sections if qparts[x["id"]]), None),
            doclayout=doclayout,
            doc_order=story.document_order(schema, doclayout), heading_ids=story.heading_like(schema),
            index_links=index_links, crops=crops, graphic_start=set(region_at), in_graphic=in_graphic, cover=cover,
            quiet=quiet, ai_pages=ai_pages, pictures=pictures, anchors=anchors,
            og_cover={"href": href(page_list[0]["bg"])} if page_list else None).encode()
    else:
        qparts = {s["id"]: story.question_parts(schema, s) for s in sections}
        page_count = (m.get("source_pdf") or {}).get("page_count")
        front = sections[0] if sections and not sections[0].get("heading") else None
        doclayout = story.document_layout(schema, {})
        files[base.lstrip("/") + "index.html"] = _ENV.get_template("report.html").render(
            **common, page_title=title, canonical_path=base, jsonld=Markup(_jsonld(schema, base)), description=desc,
            visuals={}, qparts=qparts, faq=story.is_faq(schema), front=front,
            first_q=next((s for s in sections if qparts[s["id"]]), None),
            doclayout=doclayout, doc_order=story.document_order(schema, doclayout),
            heading_ids=story.heading_like(schema), index_links=story.index_links(schema, page_count)).encode()
    files[base.lstrip("/") + "report.md"] = render_markdown(schema, base).encode()
    js, cs = figures_exports(schema, base)
    files[base.lstrip("/") + "figures.json"] = js.encode()
    files[base.lstrip("/") + "figures.csv"] = cs.encode()
    return files


def _runs_html(runs: list[dict], figures: dict) -> str:
    """Runs as inline HTML; figures keep their exact PDF string and data-fig."""
    out = []
    for r in runs:
        if "f" not in r:
            out.append(html.escape(r["t"]))
            continue
        f = figures[r["f"]]
        if f["kind"] in ("number", "percent", "bps", "multiple", "nil"):
            out.append(f'<data value="{html.escape(str(f.get("value") or ""))}" data-fig="{f["id"]}">{html.escape(f["raw"])}</data>')
        else:
            out.append(f'<span data-fig="{f["id"]}">{html.escape(f["raw"])}</span>')
    return "".join(out).strip()


def _plain_heading(s: dict) -> str:
    """Heading text without figure tokens — for places that can't carry figure markup
    (e.g. <title>, JSON-LD names)."""
    return story._WRAP_HYPHEN.sub("-", " ".join(r["t"].strip() for r in s["heading"] if "t" in r and r["t"].strip())).strip(" -–·,.:") \
        if s.get("heading") else ""


def _jsonld(schema: dict, base: str) -> str:
    m = schema["metadata"]
    type_label = REPORT_TYPE_LABEL[m["report_type"]]
    datasets = []
    for s in schema["sections"]:
        for b in s["blocks"]:
            if b["type"] == "table":
                name = _plain_heading(s) or SECTION_TYPE_LABEL.get(s["type"], "Table")
                datasets.append({
                    "@type": "Dataset", "name": f"{m['company']} {m['period_label']} {name}",
                    "description": f"Table from the {type_label} PDF, figures unchanged.",
                    "url": f"{ORIGIN_PLACEHOLDER}{base}#{b['id']}",
                    "isBasedOn": f"{ORIGIN_PLACEHOLDER}{base}source.pdf",
                    "distribution": [{"@type": "DataDownload", "encodingFormat": "text/csv",
                                      "contentUrl": f"{ORIGIN_PLACEHOLDER}{base}figures.csv"},
                                     {"@type": "DataDownload", "encodingFormat": "application/json",
                                      "contentUrl": f"{ORIGIN_PLACEHOLDER}{base}figures.json"}],
                })
    doc = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "Organization", "@id": f"{ORIGIN_PLACEHOLDER}/#org", "name": m["company"],
             "url": f"{ORIGIN_PLACEHOLDER}/"},
            {"@type": "Report", "@id": f"{ORIGIN_PLACEHOLDER}{base}#report",
             "name": f"{m['company']} {m['period_label']} {type_label}",
             "url": f"{ORIGIN_PLACEHOLDER}{base}", "inLanguage": "en",
             "publisher": {"@id": f"{ORIGIN_PLACEHOLDER}/#org"}, "about": {"@id": f"{ORIGIN_PLACEHOLDER}/#org"},
             "isBasedOn": f"{ORIGIN_PLACEHOLDER}{base}source.pdf",
             "encoding": [{"@type": "MediaObject", "encodingFormat": "application/pdf",
                           "contentUrl": f"{ORIGIN_PLACEHOLDER}{base}source.pdf"}]},
            *datasets,
        ],
    }
    return json.dumps(doc, ensure_ascii=False).replace("</", "<\\/")


# ------------------------------------------------------------------ markdown / json / csv


def _md_runs(runs: list[dict], figures: dict) -> str:
    return "".join(figures[r["f"]]["raw"] if "f" in r else r["t"] for r in runs).replace("|", "\\|")


def render_markdown(schema: dict, base: str) -> str:
    m = schema["metadata"]
    f = schema["figures"]
    type_label = REPORT_TYPE_LABEL[m["report_type"]]
    out = [f"# {m['company']} — {m['period_label']} {type_label}", "",
           f"> The PDF filing is the official document: [original PDF]({ORIGIN_PLACEHOLDER}{base}source.pdf). "
           "Figures below are copied from it unchanged.", ""]
    for s in schema["sections"]:
        heading = _md_runs(s["heading"], f) or SECTION_TYPE_LABEL.get(s["type"], "Section")
        out += [f"## {heading}", ""]
        if s.get("subheading"):
            out += [_md_runs(s["subheading"], f), ""]
        for b in s["blocks"]:
            if b["type"] == "paragraph":
                out += [_md_runs(b["runs"], f), ""]
            elif b["type"] == "table":
                ncols = len(b["columns"])
                head = [""] + ([_md_runs(c["runs"], f) for c in _expand(b["header_rows"][-1])] if b["header_rows"] else [""] * ncols)
                head = (head + [""] * (ncols + 1))[: ncols + 1]
                if b.get("caption"):
                    out += [f"_{_md_runs(b['caption'], f)}_", ""]
                out += [f"Unit: {UNIT_LABEL.get(b['unit'], b['unit'])} · Currency: {b['currency']}", ""]
                out.append("| " + " | ".join(head) + " |")
                out.append("|" + "---|" * (ncols + 1))
                for r in b["rows"]:
                    cells = [_md_runs(c["runs"], f) for c in r["cells"]]
                    out.append("| " + " | ".join([_md_runs(r["label"], f)] + cells) + " |")
                out.append("")
            elif b["type"] == "stat":
                out += [f"**{_md_runs(b['label'], f)}:** {_md_runs(b['value'], f)}", ""]
            elif b["type"] == "chart":
                if b.get("extracted"):
                    out += ["Chart data (from the chart's data labels):", "", "| Label | Value |", "|---|---|"]
                    for p in b["points"]:
                        out.append(f"| {_md_runs(p['label'], f)} | {_md_runs(p['value'], f)} |")
                    out.append("")
                else:
                    out += ["_Chart shown as an image in the PDF; values not reproduced._", ""]
    return "\n".join(out) + "\n"


def _expand(header_row: list[dict]) -> list[dict]:
    out = []
    for c in header_row:
        out.extend([c] * max(1, c.get("colspan", 1)))
    return out


EXPORT_KINDS = ("number", "percent", "bps", "multiple", "nil", "date")
CSV_FIELDS = ["id", "section", "table_id", "row_label", "col_label", "period", "raw", "value", "kind", "unit",
              "currency", "source_page", "source_bbox", "method", "confidence"]


def figures_exports(schema: dict, base: str) -> tuple[str, str]:
    m = schema["metadata"]
    sections = {s["id"]: s for s in schema["sections"]}
    rows = []
    for f in schema["figures"].values():
        if f.get("status") != "active" or f["kind"] not in EXPORT_KINDS or f.get("role") not in ("cell", "text", "chart_label"):
            continue
        sec = sections.get(f["section_id"])
        rows.append({
            "id": f["id"], "section": sec["slug"] if sec else None, "table_id": f.get("table_id"),
            "row_label": f.get("row_label"), "col_label": f.get("col_label"),
            "period": (f.get("period") or {}).get("raw"), "raw": f["raw"],
            "value": f.get("value") if f["kind"] != "date" else f.get("iso"), "kind": f["kind"],
            "unit": f.get("unit"), "currency": f.get("currency"), "source_page": f["source"]["page"],
            "source_bbox": f["source"]["bbox"], "method": f["method"], "confidence": f["confidence"],
        })
    js = json.dumps({
        "report": {"company": m["company"], "report_type": m["report_type"], "fiscal_year": m["fiscal_year"],
                   "period": m["period"], "period_label": m["period_label"], "currency": m["currency"],
                   "reporting_unit": m["reporting_unit"]},
        "source_pdf": {"url": f"{ORIGIN_PLACEHOLDER}{base}source.pdf", "sha256": m["source_pdf"]["sha256"]},
        "note": "raw = exactly as printed in the PDF; value = normalised decimal string. The PDF is the official source.",
        "figures": rows,
    }, ensure_ascii=False, indent=1)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    w.writeheader()
    for r in rows:
        w.writerow({**r, "source_bbox": " ".join(str(x) for x in r["source_bbox"])})
    return js, buf.getvalue()


# ------------------------------------------------------------------ tenant site index


def render_site_index(company: str, reports: list[dict], *, theme: dict, disclaimer: str | None,
                      robots_policy: str, logo_src: str | None = None) -> dict[str, bytes]:
    """Tenant-level pages: home, /archive/, /latest/ (served dynamically), sitemap,
    robots.txt, llms.txt. `reports` = live reports, newest first, each
    {path, period_label, type_label, sections: [slug...], lastmod}."""
    css = page_css(theme)
    common = dict(theme=theme, css=Markup(css), company=company, origin=ORIGIN_PLACEHOLDER,
                  disclaimer_html=Markup(disclaimer_html(disclaimer)), logo_src=logo_src, pdf_href=None,
                  powered_by=powered_by_html(theme),
                  fonts_href=theming.google_fonts_href(theme), alternates=None, downloads=None, jsonld=None,
                  m={"company": company})
    for r in reports:
        r["path"] = f"{ORIGIN_PLACEHOLDER}{r['base']}"
    latest = reports[0] if reports else None
    files = {
        "index.html": _ENV.get_template("index.html").render(
            **common, heading=company, latest=latest, reports=reports, page_title=f"{company} · Investor reports",
            description=f"Reports published by {company}, readable on the web with figures taken unchanged from the PDF.",
            canonical_path="/").encode(),
        "archive/index.html": _ENV.get_template("index.html").render(
            **common, heading=f"{company}: report archive", latest=None, reports=reports,
            page_title=f"Report archive · {company}", description=f"All reports published by {company}.",
            canonical_path="/archive/").encode(),
        "sitemap.xml": _sitemap(reports).encode(),
        "robots.txt": robots_txt(robots_policy).encode(),
        "llms.txt": _llms_txt(company, reports).encode(),
    }
    return files


def _sitemap(reports: list[dict]) -> str:
    urls = [("/", None), ("/latest/", reports[0]["lastmod"] if reports else None), ("/archive/", None)]
    for r in reports:
        urls.append((r["base"], r["lastmod"]))
        urls += [(f"{r['base']}report.md", r["lastmod"])]
    body = "".join(
        f"<url><loc>{ORIGIN_PLACEHOLDER}{html.escape(u)}</loc>{f'<lastmod>{lm}</lastmod>' if lm else ''}</url>"
        for u, lm in urls)
    return f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>\n'


def _llms_txt(company: str, reports: list[dict]) -> str:
    lines = [f"# {company}", "",
             f"> Investor reports from {company}. Each report page is a readable companion to the official PDF; "
             "every figure is copied unchanged from the PDF and each page links to it.", "", "## Reports", ""]
    for r in reports:
        lines.append(f"- [{r['period_label']} {r['type_label']}]({ORIGIN_PLACEHOLDER}{r['base']}): "
                     f"Markdown: {ORIGIN_PLACEHOLDER}{r['base']}report.md · Figures: {ORIGIN_PLACEHOLDER}{r['base']}figures.json")
    lines += ["", "## Optional", "", f"- [Archive]({ORIGIN_PLACEHOLDER}/archive/)",
              f"- [Sitemap]({ORIGIN_PLACEHOLDER}/sitemap.xml)"]
    return "\n".join(lines) + "\n"


# PLAN D9 presets. Retrieval/answer bots vs training crawlers.
AI_TRAINING_BOTS = ["GPTBot", "ClaudeBot", "CCBot", "Google-Extended", "Applebot-Extended", "Bytespider",
                    "meta-externalagent", "Amazonbot", "cohere-training-data-crawler"]
AI_ANSWER_BOTS = ["OAI-SearchBot", "ChatGPT-User", "Claude-SearchBot", "Claude-User", "PerplexityBot",
                  "Perplexity-User", "DuckAssistBot", "MistralAI-User"]


def robots_txt(policy: str, *, preview: bool = False) -> str:
    if preview:
        return "User-agent: *\nDisallow: /\n"
    sm = f"Sitemap: {ORIGIN_PLACEHOLDER}/sitemap.xml\n"
    if policy == "allow_all":
        return "User-agent: *\nAllow: /\n\n" + sm
    blocked = AI_TRAINING_BOTS if policy == "search_and_answer" else AI_TRAINING_BOTS + AI_ANSWER_BOTS
    rules = "".join(f"User-agent: {b}\nDisallow: /\n\n" for b in blocked)
    return rules + "User-agent: *\nAllow: /\n\n" + sm


CONTENT_TYPES = ((".html", "text/html; charset=utf-8"), (".md", "text/markdown; charset=utf-8"),
                 (".json", "application/json"), (".csv", "text/csv; charset=utf-8"), (".xml", "application/xml"),
                 (".txt", "text/plain; charset=utf-8"), (".png", "image/png"), (".webp", "image/webp"),
                 (".woff2", "font/woff2"), (".pdf", "application/pdf"))


def content_type(name: str) -> str:
    return next((ct for ext, ct in CONTENT_TYPES if name.endswith(ext)), "application/octet-stream")


def bundle_sha256(files: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for k in sorted(files):
        h.update(k.encode() + b"\0" + hashlib.sha256(files[k]).digest())
    return h.hexdigest()
