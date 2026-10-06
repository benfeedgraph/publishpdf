"""AI-assisted layout of design-led pages: an AI model arranges a page's element IDs into
web sections (headings, paragraphs, cards, stat tiles, picture grids).

The model only ever returns IDs. Every word and number on the web page comes from the PDF
elements (app/render/elements.py) through the usual figure rules; the model cannot type
anything into the page. Its answer is checked: unknown IDs are dropped, an ID is used at
most once, and any text it left out is added back in reading order — content is never lost.

Layouts are cached per PDF and page (re-renders cost nothing), recorded in the AI usage
ledger, and capped per report version (AI_LAYOUT_MAX_CREDITS). No key, cap reached, or a
bad answer: that page keeps its designed look.
"""

from __future__ import annotations

import hashlib
import html as html_mod
import json
from typing import Any

from app.render.elements import Element

LAYOUT_VERSION = "1"
PROMPT_TOKENS = 700            # instructions, per page
TOKENS_PER_ELEMENT = 45        # id, box, style and up to 140 characters of text
OUTPUT_TOKENS_PER_ELEMENT = 6

PROMPT = """You turn one page of a company's annual or quarterly report into the structure of a clean, modern web page.
You get the page's elements: text blocks (with position as % of the page, font size, bold, colour) and pictures.
Return JSON only, in this shape:
{"sections": [ ... ]}
where each section is one of:
  {"type": "heading", "level": 1|2|3, "ids": [text ids]}            the page title is level 1, sub-titles 2, small titles 3
  {"type": "paragraph", "ids": [text ids]}                           consecutive lines of ONE paragraph, in reading order
  {"type": "list", "items": [[text ids], ...]}                       bullet points
  {"type": "cards", "cards": [{"title": [text ids], "body": [text ids], "picture": picture id or null}, ...]}
                                                                     side-by-side columns/boxes, each with its own title and text
  {"type": "stats", "items": [{"ids": [text ids]}, ...]}             short highlight lines built around a number (e.g. "Over 23 lakh children ...")
  {"type": "gallery", "items": [{"picture": picture id, "caption": [text ids]}, ...]}   pictures, each with its nearby caption
  {"type": "picture", "picture": picture id, "caption": [text ids]}  one picture on its own
  {"type": "skip", "ids": [ids]}                                     ONLY page numbers, running headers and stray single characters
Rules: use only the given ids; use each id at most once; keep the page's reading order; group the lines of a
column under its column heading; put each caption with the picture it sits next to. Never write any text yourself.
"""


def fingerprint(elements: list[Element]) -> str:
    """What the layout depends on: the page's elements (not how figures are reviewed)."""
    sig = [(e.id, e.kind, [round(v) for v in e.bbox], e.text[:140]) for e in elements]
    return hashlib.sha256(json.dumps([LAYOUT_VERSION, sig]).encode()).hexdigest()[:16]


def describe(elements: list[Element], W: float, H: float) -> str:
    rows = []
    for e in elements:
        x0, y0, x1, y1 = e.bbox
        box = f"x{100 * x0 / W:.0f} y{100 * y0 / H:.0f} w{100 * (x1 - x0) / W:.0f} h{100 * (y1 - y0) / H:.0f}"
        if e.kind == "picture":
            rows.append(f"{e.id} picture {box}")
        else:
            style = f"size {e.size:g}{' bold' if e.bold else ''} {e.color}"
            rows.append(f"{e.id} text {box} {style} | {e.text[:140]}")
    return "\n".join(rows)


def estimate_tokens(elements: list[Element]) -> tuple[int, int]:
    return PROMPT_TOKENS + TOKENS_PER_ELEMENT * len(elements), 40 + OUTPUT_TOKENS_PER_ELEMENT * len(elements)


def request_body(elements: list[Element], W: float, H: float) -> dict:
    return {"contents": [{"role": "user", "parts": [{"text": PROMPT + "\nElements:\n" + describe(elements, W, H)}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}


def parse(resp: dict) -> dict | None:
    text = "".join(p.get("text", "") for c in resp.get("candidates", [])[:1]
                   for p in (c.get("content") or {}).get("parts", []))
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("sections"), list) else None


# ------------------------------------------------------------------ checking the answer


def validate(layout: dict, elements: list[Element]) -> list[dict]:
    """The model's sections, made safe: known ids only, each at most once, every text
    element present (left-outs are added back as paragraphs where they fall)."""
    by_id = {e.id: e for e in elements}
    used: set[str] = set()

    def take(ids, kind: str | None = None) -> list[str]:
        out = []
        for i in ids if isinstance(ids, list) else [ids]:
            if isinstance(i, str) and i in by_id and i not in used and (kind is None or by_id[i].kind == kind):
                used.add(i)
                out.append(i)
        return out

    sections: list[dict] = []
    for sec in layout.get("sections", []):
        if not isinstance(sec, dict):
            continue
        t = sec.get("type")
        if t == "heading":
            ids = take(sec.get("ids", []), "text")
            if ids:
                lvl = sec.get("level") if sec.get("level") in (1, 2, 3) else 2
                sections.append({"type": "heading", "level": lvl, "ids": ids})
        elif t == "paragraph":
            ids = take(sec.get("ids", []), "text")
            if ids:
                sections.append({"type": "paragraph", "ids": ids})
        elif t == "list":
            items = [x for x in (take(it, "text") for it in sec.get("items", []) if isinstance(it, list)) if x]
            if items:
                sections.append({"type": "list", "items": items})
        elif t == "cards":
            cards = []
            for c in sec.get("cards", []):
                if not isinstance(c, dict):
                    continue
                card = {"title": take(c.get("title", []), "text"), "body": take(c.get("body", []), "text"),
                        "picture": (take([c.get("picture")], "picture") or [None])[0]}
                if card["title"] or card["body"] or card["picture"]:
                    cards.append(card)
            if cards:
                sections.append({"type": "cards", "cards": cards})
        elif t == "stats":
            items = [x for x in (take(it.get("ids", []), "text") for it in sec.get("items", []) if isinstance(it, dict)) if x]
            if items:
                sections.append({"type": "stats", "items": items})
        elif t == "gallery":
            items = []
            for it in sec.get("items", []):
                if isinstance(it, dict):
                    pic = (take([it.get("picture")], "picture") or [None])[0]
                    cap = take(it.get("caption", []), "text")
                    if pic or cap:
                        items.append({"picture": pic, "caption": cap})
            if items:
                sections.append({"type": "gallery", "items": items})
        elif t == "picture":
            pic = (take([sec.get("picture")], "picture") or [None])[0]
            if pic:
                sections.append({"type": "picture", "picture": pic, "caption": take(sec.get("caption", []), "text")})
        elif t == "skip":
            # only what can't be content: page numbers, stray characters
            take([i for i in sec.get("ids", []) if isinstance(i, str) and i in by_id
                  and by_id[i].kind == "text" and len(by_id[i].text.strip()) <= 3])
    # nothing is lost: text the model left out goes back where it falls in reading order
    order = [e.id for e in elements]
    for e in elements:
        if e.kind == "text" and e.id not in used and len(e.text.strip()) > 3:
            pos = order.index(e.id)
            at = len(sections)
            for k, sec in enumerate(sections):
                ids = _section_ids(sec)
                if ids and min(order.index(i) for i in ids) > pos:
                    at = k
                    break
            sections.insert(at, {"type": "paragraph", "ids": [e.id]})
            used.add(e.id)
    return sections


def _section_ids(sec: dict) -> list[str]:
    t = sec["type"]
    if t in ("heading", "paragraph"):
        return sec["ids"]
    if t == "list":
        return [i for it in sec["items"] for i in it]
    if t == "cards":
        return [i for c in sec["cards"] for i in c["title"] + c["body"] + ([c["picture"]] if c["picture"] else [])]
    if t == "stats":
        return [i for it in sec["items"] for i in it]
    if t == "gallery":
        return [i for it in sec["items"] for i in ([it["picture"]] if it["picture"] else []) + it["caption"]]
    if t == "picture":
        return [sec["picture"], *sec["caption"]]
    return []


# ------------------------------------------------------------------ HTML


def render(sections: list[dict], elements: list[Element], *, printed_src: str, W: float, H: float) -> str:
    """The page as web HTML. Text comes from the elements' safe HTML (figures as data-fig);
    pictures, and any text block holding an unverified number, are crops of the printed page."""
    by_id = {e.id: e for e in elements}

    def crop(e: Element, cls: str = "ai-pic") -> str:
        x0, y0, x1, y1 = e.bbox
        w, h = max(1.0, x1 - x0), max(1.0, y1 - y0)
        return (f'<div class="{cls}" style="aspect-ratio:{w:.1f}/{h:.1f};--vw:{round(w * 1.6)}px">'
                f'<img src="{html_mod.escape(printed_src)}" alt="" loading="lazy" decoding="async" '
                f'style="width:{100 * W / w:.3f}%;left:-{100 * x0 / w:.3f}%;top:-{100 * y0 / h:.3f}%"></div>')

    def text(ids: list[str], sep: str = " ") -> str:
        out = []
        for i in ids:
            e = by_id[i]
            out.append(e.html if e.safe else crop(e, "ai-pic ai-textcrop"))
        return sep.join(out)

    parts = []
    for sec in sections:
        t = sec["type"]
        if t == "heading":
            tag = {1: "h2", 2: "h3", 3: "h4"}[sec["level"]]
            parts.append(f'<{tag} class="ai-h ai-h-{"abc"[sec["level"] - 1]}">{text(sec["ids"])}</{tag}>')
        elif t == "paragraph":
            parts.append(f'<p class="ai-p">{text(sec["ids"])}</p>')
        elif t == "list":
            parts.append('<ul class="ai-list">' + "".join(f"<li>{text(it)}</li>" for it in sec["items"]) + "</ul>")
        elif t == "cards":
            cards = []
            for c in sec["cards"]:
                pic = crop(by_id[c["picture"]]) if c["picture"] else ""
                title = f'<h4 class="ai-card-t">{text(c["title"])}</h4>' if c["title"] else ""
                body = f'<p>{text(c["body"])}</p>' if c["body"] else ""
                cards.append(f'<div class="ai-card">{pic}{title}{body}</div>')
            parts.append(f'<div class="ai-cards" style="--cols:{min(4, max(1, len(cards)))}">' + "".join(cards) + "</div>")
        elif t == "stats":
            parts.append('<div class="ai-stats">' + "".join(f'<div class="ai-stat">{text(it)}</div>' for it in sec["items"]) + "</div>")
        elif t == "gallery":
            items = []
            for it in sec["items"]:
                pic = crop(by_id[it["picture"]]) if it["picture"] else ""
                cap = f'<figcaption>{text(it["caption"])}</figcaption>' if it["caption"] else ""
                items.append(f'<figure class="ai-tile">{pic}{cap}</figure>')
            parts.append('<div class="ai-gallery">' + "".join(items) + "</div>")
        elif t == "picture":
            cap = f'<figcaption>{text(sec["caption"])}</figcaption>' if sec["caption"] else ""
            parts.append(f'<figure class="ai-figure">{crop(by_id[sec["picture"]])}{cap}</figure>')
    return '<section class="ai-page">' + "".join(parts) + "</section>"


def credits_for(input_tokens: int, output_tokens: int) -> int:
    from app.ai_usage import shown, usd_for
    from app.config import get_settings
    return shown(usd_for("gemini", input_tokens, output_tokens) / get_settings().ai_credit_usd)


def estimate_credits(pages: dict[int, list[Element]]) -> int:
    tin = tout = 0
    for els in pages.values():
        a, b = estimate_tokens(els)
        tin, tout = tin + a, tout + b
    return credits_for(tin, tout) if pages else 0
