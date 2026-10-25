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
import re
from typing import Any

from app.render.elements import Element, is_bullet

_LEAD_BULLET = re.compile(r"^(\s*)(?:[•▪●◦‣■□➢➤►▶✓✔]|[-–](?=\s))\s*")


def _light(colour: str) -> bool:
    """Too pale to read on a white page (white text from a coloured panel)."""
    try:
        r, g, b = (int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    except (ValueError, IndexError):
        return True
    return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.8


def _panel_dark(colour: str) -> bool:
    """A panel dark enough that text on it should go white."""
    try:
        r, g, b = (int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    except (ValueError, IndexError):
        return False
    return 0.2126 * r + 0.7152 * g + 0.0722 * b < 0.5


def _unbullet(h: str) -> str:
    """A list item without its printed bullet (the list draws one)."""
    return _LEAD_BULLET.sub(r"\1", h, count=1)


# Bump when the prompt or the id→HTML mapping changes in a way that should re-ask cached pages.
LAYOUT_VERSION = "5"
PROMPT_TOKENS = 1800           # instructions, per page
TOKENS_PER_ELEMENT = 45        # id, box, style and up to 140 characters of text
OUTPUT_TOKENS_PER_ELEMENT = 6

PROMPT = """You are a senior web designer building the definitive web version of a company's financial report —
pages that will represent big, listed companies to investors, so they must look genuinely professional. You are
given ONE page of the report and you re-compose it as the structure of a rich, modern, magazine-quality web page:
the kind of page a reader opens on their phone or laptop and finds a pleasure to read — with a clear hierarchy, a
confident visual rhythm, and the same richness and care as the printed page. It is a responsive web page, not a
photograph of the PDF, so you reflow the content to work at any width; but it is NOT a stripped-back page — never
flatten a designed, colour-blocked, multi-panel spread into a thin single column of plain grey paragraphs, and never
leave a flat wall of text.

Think of the page as web content that honours the original's design. You decide how the content reads best on the
web, but you preserve what the designer built: how the page groups into sections, columns, boxes and colour-blocked
panels, and how dense and considered it feels. Reflow the geometry for the web; keep the structure, the grouping and
the richness. Open with a strong lead, then carry the reader through self-contained, scannable sections, with
repetition pulled into real components.

Accuracy is absolute and comes first. Every word and number already exists as a page element; you only decide how
those elements are grouped and arranged. You return element IDs only — never text, numbers or dates of your own, and
you never drop, merge, reword, re-order within an element, or duplicate content. The figures a reader sees must be
exactly the report's figures; your job is presentation, never authorship.

Each element you are given is a text block (with position as % of the page, font size, whether it is bold, and
its colour) or a picture. Return JSON only, in this exact shape:
{"sections": [ ... ]}
where each section is ONE of:
  {"type": "heading", "level": 1|2|3, "ids": [text ids]}            the page's main title is level 1, section titles 2, small sub-titles 3
  {"type": "paragraph", "ids": [text ids]}                           the consecutive lines of ONE paragraph, in reading order
  {"type": "list", "items": [[text ids], ...]}                       a bullet / numbered list; each item is its own group of ids
  {"type": "cards", "cards": [{"title": [text ids], "body": [text ids], "picture": picture id or null}, ...]}
                                                                     parallel items shown side by side, each with its own mini-title, text and optional picture
  {"type": "stats", "items": [{"ids": [text ids]}, ...]}             a row of highlight tiles, each built around one headline number (e.g. "₹1,234 cr revenue", "23 lakh customers")
  {"type": "gallery", "items": [{"picture": picture id, "caption": [text ids]}, ...]}   a row of pictures, each with its nearby caption
  {"type": "picture", "picture": picture id, "caption": [text ids]}  one picture shown on its own
  {"type": "skip", "ids": [ids]}                                     ONLY page numbers, running headers/footers, and stray single characters

How to make it look like a real web page that keeps the original's richness, not a stripped-back reflow:
- Lead with structure. Open the page with a clear level-1 heading when the page has a title, and give every distinct
  topic its own level-2 heading so the reader can scan. Use level 3 for sub-points under a section.
- Never emit one giant paragraph. Keep each paragraph to the lines that truly belong to the same thought; start a
  new paragraph (or heading) when the topic turns.
- Turn repetition into components. When several short lines each carry a headline figure or metric (revenue, margin,
  growth %, counts like "23 lakh children"), group them as a "stats" row — this is the single biggest win for a
  report page and makes the numbers pop. When the page shows parallel blocks (pillars, segments, product lines,
  business units, "our values"), each with its own little title and maybe a picture and a few lines, put them ALL
  in ONE "cards" section — one card per block — so they tile into a compact grid. Do NOT emit each block as its own
  separate full-width section: that strands each one in a wide empty row and makes the page feel blank. When you have
  a simple enumeration, make it a "list" rather than running the items together as prose.
- Fill the width; match the page's density. A report page is information-rich: when the source packs several panels,
  tiles or boxes across the page, your output should be an equally dense grid of cards/stats that uses the full
  width — never a thin column of one item per row with large empty margins beside it. Aim for the richness of the
  original: group aggressively into stats rows and card grids so the page looks full and considered, not sparse.
- Preserve the design's structure and its colour. Where the page is built as columns, boxes or coloured panels, keep
  that: mirror columns and boxes with cards or a gallery rather than flattening everything into one column, and group
  the lines of a column under that column's heading. When a run of blocks sits together inside one coloured panel,
  card or box in the source, keep those blocks together and in their order — do not interleave content from outside it
  — so that section reads, and is coloured, as one coherent unit. Put each caption with the picture it sits beside.
- Handle pictures well. A wide picture — a banner, or a row of product packs shown as one strip — is a SINGLE
  picture: give it its own "picture" section (with its caption) at full width, never slice it up or drop it into a
  multi-column grid. Use a "gallery" ONLY for several separate pictures of similar size that belong together (a set
  of logos, a set of photos). A picture that illustrates a short block of text pairs well as a "cards" item (picture
  + its title + blurb); keep that picture with its own text, not someone else's.
- Keep prose whole. Each paragraph element is already a full paragraph — place it as one "paragraph"; never split a
  paragraph across sections or print a heading between its sentences.
- Prefer a few well-chosen, varied section types over a long chain of plain paragraphs. Visual rhythm — heading,
  stats, short paragraph, cards, picture — is what makes the page feel designed.

Hard rules: use only the given ids; use each id at most once; preserve the page's overall reading order; put an id in
the section where it belongs and nowhere else. Choose "stats" only for genuinely short, number-led highlight lines;
keep ordinary sentences and explanations as paragraphs. Never invent, rewrite, translate, summarise or re-order the
words inside any element — you arrange ids, you do not author text.

The finished page must read as one cohesive, professional web page — accurate to the last figure, easy to read, and
clearly designed — not as a PDF forced onto the web.
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


def render(sections: list[dict], elements: list[Element], *, crop_url, W: float, H: float) -> str:
    """The page as web HTML. Text comes from the elements' safe HTML (figures as data-fig);
    pictures, and any text block holding an unverified number, are crops of the printed page.

    `crop_url(element)` returns the URL of that element's own cropped image (and arranges for the
    file to be produced): each picture or kept line is its own small file, so the reader isn't
    waiting on a full-page image to decode before anything appears."""
    by_id = {e.id: e for e in elements}

    def crop(e: Element, cls: str = "ai-pic") -> str:
        x0, y0, x1, y1 = e.bbox
        w, h = max(1.0, x1 - x0), max(1.0, y1 - y0)
        return (f'<div class="{cls}" style="aspect-ratio:{w:.1f}/{h:.1f};--vw:{round(w * 1.6)}px">'
                f'<img src="{html_mod.escape(crop_url(e))}" alt="" loading="lazy" decoding="async"></div>')

    def text(ids: list[str], sep: str = " ") -> str:
        out = []
        for i in ids:
            e = by_id[i]
            out.append(e.html if e.safe else textcrop(e))
        return sep.join(out)

    sized = sorted(e.size for e in elements if e.kind == "text" and e.size)
    body = sized[len(sized) // 2] if sized else 10.0

    def textcrop(e: Element) -> str:
        """A line kept as printed (it holds a number not yet verified): shown at the size of
        the text around it — the page's body size maps to ~16px."""
        x0, y0, x1, y1 = e.bbox
        w, h = max(1.0, x1 - x0), max(1.0, y1 - y0)
        px = 15.5 / body                                                   # px per point
        return (f'<span class="ai-pic ai-textcrop" style="aspect-ratio:{w:.1f}/{h:.1f};width:min(100%,{w * px:.0f}px)">'
                f'<img src="{html_mod.escape(crop_url(e))}" alt="" loading="lazy" decoding="async"></span>')

    def look(ids: list[str], heading: bool = False) -> tuple[str, str]:
        """(extra classes, inline style) that carry the PDF's own design: its typeface, a
        heading's colour and relative size, a label bar's colour."""
        e = by_id[ids[0]] if ids else None
        if e is None or e.kind != "text":
            return "", ""
        cls, style = [], []
        if e.meta.get("face"):
            cls.append(e.meta["face"])
        if e.meta.get("bg"):
            cls.append("ai-lbl")
            style.append(f"--lbl:{e.meta['bg']}")
            style.append(f"color:{e.color if _light(e.color) else '#fff'}")
        elif heading and not _light(e.color):
            style.append(f"color:{e.color}")
        if heading:
            style.append(f"font-size:{max(1.0, min(2.4, e.size / body * 0.62)):.2f}em")
        return " ".join(cls), ";".join(style)

    def attrs(cls: str, ids: list[str], heading: bool = False) -> str:
        extra, style = look(ids, heading)
        c = " ".join(x for x in (cls, extra) if x)
        return f' class="{c}"' + (f' style="{style}"' if style else "")

    def bullets_style(ids: list[str]) -> str:
        colour = next((by_id[i].meta.get("bullet") for i in ids if isinstance(by_id[i].meta.get("bullet"), str)), None)
        face = next((by_id[i].meta.get("face") for i in ids if by_id[i].meta.get("face")), "")
        cls = f"ai-list {face}".strip()
        return f' class="{cls}"' + (f' style="--bul:{colour}"' if colour else "")

    def items(ids: list[str]) -> str:
        """Blocks one after another: each its own paragraph; bullet points as a list."""
        out, bullets = [], []
        for i in ids:
            e = by_id[i]
            if is_bullet(e):
                bullets.append(i)
                continue
            if bullets:
                out.append(f"<ul{bullets_style(bullets)}>" + "".join(f"<li>{_unbullet(text([b]))}</li>" for b in bullets) + "</ul>")
                bullets = []
            out.append(crop(e) if e.kind == "picture" else f"<p{attrs('', [i])}>{text([i])}</p>")
        if bullets:
            out.append(f"<ul{bullets_style(bullets)}>" + "".join(f"<li>{_unbullet(text([b]))}</li>" for b in bullets) + "</ul>")
        return "".join(out)

    def draw(secs: list[dict]) -> list[str]:
        parts = []
        for sec in secs:
            t = sec["type"]
            if t == "heading":
                tag = {1: "h2", 2: "h3", 3: "h4"}[sec["level"]]
                parts.append(f'<{tag}{attrs("ai-h ai-h-" + "abc"[sec["level"] - 1], sec["ids"], True)}>{text(sec["ids"])}</{tag}>')
            elif t == "paragraph":
                parts.append(f'<p{attrs("ai-p", sec["ids"])}>{text(sec["ids"])}</p>')
            elif t == "list":
                flat = [i for it in sec["items"] for i in it]
                cols = f' data-cols="{sec["cols"]}"' if sec.get("cols", 1) > 1 else ""
                parts.append(f"<ul{bullets_style(flat)}{cols}>" + "".join(f"<li>{_unbullet(text(it))}</li>" for it in sec["items"]) + "</ul>")
            elif t == "chips":
                bgs = {by_id[it[0]].meta.get("bg") for it in sec["items"]}
                faces = {by_id[it[0]].meta.get("face") for it in sec["items"]} - {None}
                face = f" {faces.pop()}" if len(faces) == 1 else ""
                if len(bgs) == 1 and None not in bgs:      # one coloured bar, as printed
                    parts.append(f'<ul class="ai-chips ai-bar{face}" style="--lbl:{bgs.pop()}">'
                                 + "".join(f"<li>{text(it)}</li>" for it in sec["items"]) + "</ul>")
                else:
                    parts.append(f'<ul class="ai-chips{face}">' + "".join(
                        f'<li{attrs("", it)}>{text(it)}</li>' for it in sec["items"]) + "</ul>")
            elif t == "cards":
                cards = []
                for c in sec["cards"]:
                    pic = crop(by_id[c["picture"]]) if c["picture"] else ""
                    title = f'<h4{attrs("ai-card-t", c["title"], True)}>{text(c["title"])}</h4>' if c["title"] else ""
                    body = "".join(draw(c["flow"])) if "flow" in c else items(c["body"])
                    # the label above or below the photo, as printed
                    above = c["title"] and c["picture"] and by_id[c["title"][0]].bbox[1] < by_id[c["picture"]].bbox[1]
                    cards.append(f'<div class="ai-card">{title}{pic}{body}</div>' if above
                                 else f'<div class="ai-card">{pic}{title}{body}</div>')
                parts.append(f'<div class="ai-cards" style="--cols:{min(4, max(1, len(cards)))}">' + "".join(cards) + "</div>")
            elif t == "stats":
                parts.append('<div class="ai-stats">' + "".join(f'<div class="ai-stat">{text(it)}</div>' for it in sec["items"]) + "</div>")
            elif t == "gallery":
                tiles = []
                for it in sec["items"]:
                    pic = crop(by_id[it["picture"]]) if it["picture"] else ""
                    cap = f'<figcaption>{items(it["caption"])}</figcaption>' if it["caption"] else ""
                    tiles.append(f'<figure class="ai-tile">{pic}{cap}</figure>')
                parts.append(f'<div class="ai-gallery" style="--cols:{min(4, max(1, len(tiles)))}">' + "".join(tiles) + "</div>")
            elif t == "picture":
                cap = f'<figcaption>{text(sec["caption"])}</figcaption>' if sec["caption"] else ""
                parts.append(f'<figure class="ai-figure">{crop(by_id[sec["picture"]])}{cap}</figure>')
            elif t == "media":
                side = "left" if sec.get("side") == "left" else "right"
                parts.append(f'<div class="ai-media ai-media-{side}"><figure class="ai-media-pic">{crop(by_id[sec["picture"]])}</figure>'
                             f'<div class="ai-media-body">{"".join(draw(sec["body"]))}</div></div>')
        return parts

    parts = draw(sections)

    def panel_of(sec: dict):
        """The colour panel (id, colour) most of a top-level section's text sits on, if any."""
        try:
            ids = _section_ids(sec)
        except Exception:  # noqa: BLE001 - an unusual section shape just gets no panel
            return None
        counts: dict = {}
        for i in ids:
            p = by_id[i].meta.get("panel") if i in by_id else None
            if p:
                counts[p] = counts.get(p, 0) + 1
        return max(counts, key=counts.get) if counts else None

    # Paint a coloured card/section behind the runs of sections that share one panel, so the
    # page keeps the PDF's colour blocking instead of flattening every block onto white.
    pids = [panel_of(s) for s in sections]
    grouped: list[str] = []
    i = 0
    while i < len(parts):
        pid = pids[i]
        if not pid:
            grouped.append(parts[i])
            i += 1
            continue
        j = i
        while j < len(parts) and pids[j] == pid:
            j += 1
        cls = "ai-panel ai-panel-dark" if _panel_dark(pid[1]) else "ai-panel"
        grouped.append(f'<div class="{cls}" style="--panel:{pid[1]}">' + "".join(parts[i:j]) + "</div>")
        i = j
    return '<section class="ai-page">' + "".join(grouped) + "</section>"


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
