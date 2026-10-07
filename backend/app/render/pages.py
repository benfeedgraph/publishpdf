"""Page-faithful web edition: every PDF page becomes a web page that looks the same as
the PDF, with its words as real HTML text laid over the page artwork.

For each page:
  * background — the page rendered to an image with the text we lay over it removed
    (photos, colours, rules and charts stay exactly as designed);
  * text — the page's own words, positioned where the PDF prints them, sized as a
    share of the page width so the page scales on any screen, and stretched to the
    PDF's exact line width (measured server-side with the very font file the browser
    loads) so lines land where they do in the PDF;
  * links — the PDF's own link areas, as working links.

Numbers follow the zero-discrepancy rule: a digit reaches the HTML only as a figure
(its exact PDF string, via data-fig). Any other digit — page numbers, footnote marks,
a figure that wraps across lines, rotated text — stays in the background image,
visible exactly as printed but never re-typed.
"""

from __future__ import annotations

import html
import io
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import pymupdf
from PIL import Image, ImageFont

FONT_DIR = Path(__file__).parent / "assets" / "fonts"
BG_SCALE = 2.0            # background pixels per PDF point (sharp on high-density screens)
BG_QUALITY = 80           # WebP quality of the page artwork
MIN_PT = 2.5              # text smaller than this is hidden text (behind images), never shown

# family -> {(weight, italic): font file stem}. Arimo is metric-compatible with Helvetica
# and Arial, the commonest report faces; Poppins where the report uses Poppins; Libre
# Bodoni for serif display faces. All are SIL OFL fonts shipped with the site.
FACES = {
    "sans": {(400, False): "Arimo-Regular", (700, False): "Arimo-Bold",
             (400, True): "Arimo-Italic", (700, True): "Arimo-BoldItalic"},
    "poppins": {(400, False): "Poppins-Regular", (600, False): "Poppins-SemiBold",
                (700, False): "Poppins-Bold", (400, True): "Poppins-Italic"},
    "serif": {(400, False): "LibreBodoni", (700, False): "LibreBodoni",
              (400, True): "LibreBodoni-Italic", (700, True): "LibreBodoni-Italic"},
}
_SERIF = re.compile(r"bodoni|times|serif|garamond|georgia|baskerville|caslon|minion|palatino|didot|cambria", re.I)
_URL = re.compile(r"^(https?://|mailto:)", re.I)
# Symbol and icon fonts map glyphs (₹, ticks, arrows) onto ordinary letters; re-setting
# them in a text face would print the wrong character, so they stay in the artwork.
_SYMBOL_FONT = re.compile(r"rupee|symbol|dingbat|wingding|webding|zapf|awesome|icon|glyph|marlett|mt ?extra", re.I)


def face_for(font_name: str, flags: int) -> tuple[str, int, bool, str]:
    """(family, css weight, italic, font file stem) for a PDF font."""
    n = font_name.split("+", 1)[-1]
    italic = bool(flags & 2) or bool(re.search(r"italic|oblique", n, re.I))
    if re.search(r"black|heavy|extrabold|bold", n, re.I) or flags & 16:
        weight = 600 if re.search(r"semi|demi", n, re.I) else 700
    elif re.search(r"semi|demi|medium", n, re.I):
        weight = 600
    else:
        weight = 400
    # By name only: PDF "serif" flags are unreliable (Calibri, a sans, is often flagged serif).
    fam = "poppins" if "poppins" in n.lower() else "serif" if _SERIF.search(n) else "sans"
    faces = FACES[fam]
    for w in ([weight, 700, 400] if weight >= 600 else [400]):
        for it in (italic, False):
            if (w, it) in faces:
                return fam, w, it, faces[(w, it)]
    return fam, 400, False, faces[(400, False)]


@lru_cache(maxsize=64)
def _font(stem: str, weight: int) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(FONT_DIR / f"{stem}.ttf"), size=1000)
    if stem.startswith("LibreBodoni"):
        try:
            f.set_variation_by_axes([weight])
        except Exception:  # noqa: BLE001 - static fallback keeps default weight
            pass
    return f


@lru_cache(maxsize=64)
def _metrics(stem: str, weight: int) -> tuple[float, float]:
    """(ascent, descent) in em, as the browser uses them for line-height:1 layout."""
    a, d = _font(stem, weight).getmetrics()
    return a / 1000, d / 1000


def measure(text: str, stem: str, weight: int, size: float) -> float:
    return _font(stem, weight).getlength(text) / 1000 * size


@dataclass
class Piece:
    x0: float
    x1: float
    baseline: float
    size: float
    color: int
    stem: str
    weight: int
    italic: bool
    family: str
    text: str = ""
    fid: str | None = None
    chars: list = field(default_factory=list)      # char bboxes to lift out of the artwork


def _figure_index(schema: dict, page_no: int) -> list[dict]:
    """Figures this page may show as text: active, on this page, in one piece."""
    out = []
    for f in schema["figures"].values():
        src = f.get("source") or {}
        if f.get("status", "active") != "active" or src.get("page") != page_no:
            continue
        if len(src.get("parts") or []) > 1:
            continue          # wraps across lines: left in the artwork, shown as printed
        out.append(f)
    return out


def _fig_at(figs: list[dict], cx: float, cy: float) -> dict | None:
    for f in figs:
        x0, y0, x1, y1 = f["source"]["bbox"]
        if x0 - 0.6 <= cx <= x1 + 0.6 and y0 - 0.6 <= cy <= y1 + 0.6:
            return f
    return None


def page_pieces(page: pymupdf.Page, schema: dict, page_no: int) -> list[Piece]:
    """The page's text as positioned pieces, in the PDF's reading order."""
    figs = _figure_index(schema, page_no)
    raw = page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP)
    pieces: list[Piece] = []
    placed: set[str] = set()
    for block in raw["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            if abs(line["dir"][1]) > 0.02 or line["dir"][0] < 0:
                continue                      # rotated text stays in the artwork
            for span in line["spans"]:
                if span["size"] < MIN_PT or span.get("alpha", 255) == 0 or _SYMBOL_FONT.search(span["font"]):
                    continue
                fam, weight, italic, stem = face_for(span["font"], span["flags"])
                base = span["origin"][1]
                # words of this span
                words, cur = [], []
                for ch in span["chars"]:
                    if ch["c"].isspace():
                        if cur:
                            words.append(cur)
                        cur = []
                        words.append([ch])     # keep spaces as their own "word"
                    else:
                        cur.append(ch)
                if cur:
                    words.append(cur)
                run: Piece | None = None

                def close():
                    nonlocal run
                    if run and run.text.strip():
                        run.text = run.text.rstrip()
                        pieces.append(run)
                    run = None

                i = 0
                while i < len(words):
                    w = words[i]
                    text = "".join(c["c"] for c in w)
                    if text.isspace():
                        if run:
                            run.text += " "
                        i += 1
                        continue
                    bad = any(c["c"] == "�" or 0xE000 <= ord(c["c"]) <= 0xF8FF for c in w)
                    cx = [(c["bbox"][0] + c["bbox"][2]) / 2 for c in w]
                    cy = [(c["bbox"][1] + c["bbox"][3]) / 2 for c in w]
                    f = None if bad else next((g for g in (_fig_at(figs, x, y) for x, y in zip(cx, cy)) if g), None)
                    if f is not None:
                        close()
                        # every following word of the same figure joins it (e.g. "31st March, 2026")
                        group = list(w)
                        j = i + 1
                        while j < len(words):
                            nxt = words[j]
                            t = "".join(c["c"] for c in nxt)
                            if t.isspace():
                                if j + 1 < len(words) and _fig_at(figs, (words[j + 1][0]["bbox"][0] + words[j + 1][0]["bbox"][2]) / 2,
                                                                  (words[j + 1][0]["bbox"][1] + words[j + 1][0]["bbox"][3]) / 2) is f:
                                    group += nxt
                                    j += 1
                                    continue
                                break
                            if _fig_at(figs, (nxt[0]["bbox"][0] + nxt[0]["bbox"][2]) / 2, (nxt[0]["bbox"][1] + nxt[0]["bbox"][3]) / 2) is f:
                                group += nxt
                                j += 1
                                continue
                            break
                        shown = "".join(c["c"] for c in group).strip()
                        raw_ = f["raw"].strip()
                        # Show a figure as text only where it agrees with what the page
                        # prints — or a reviewer corrected or confirmed it. An unreviewed
                        # value that disagrees (e.g. a misread) never replaces the print.
                        vouched = f.get("edited") or (f.get("review") or {}).get("action") == "confirm"
                        if raw_ not in shown and not vouched:
                            i = j
                            continue
                        if f["id"] not in placed:
                            placed.add(f["id"])
                            # punctuation printed next to the figure ("2025," or "(12.30).")
                            head = tail = ""
                            if shown != raw_ and raw_ in shown:
                                k = shown.index(raw_)
                                head, tail = shown[:k], shown[k + len(raw_):]
                                if re.search(r"\d", head + tail):
                                    head = tail = ""          # not just punctuation: show the figure alone
                            gx0, gx1 = group[0]["bbox"][0], group[-1]["bbox"][2]
                            if head:
                                hc = [c for c in group if c["bbox"][2] <= gx0 + measure(head, stem, weight, span["size"]) + 0.5]
                                pieces.append(Piece(gx0, hc[-1]["bbox"][2] if hc else gx0, base, span["size"], span["color"],
                                                    stem, weight, italic, fam, head, None, hc))
                                gx0 = hc[-1]["bbox"][2] if hc else gx0
                            tail_x = gx1
                            if tail:
                                tw = measure(tail, stem, weight, span["size"])
                                tail_x = gx1 - tw
                            pieces.append(Piece(gx0, tail_x, base, span["size"], span["color"], stem, weight, italic, fam,
                                                raw_, f["id"], group))
                            if tail:
                                pieces.append(Piece(tail_x, gx1, base, span["size"], span["color"], stem, weight, italic,
                                                    fam, tail, None, []))
                        # a figure already placed elsewhere on this page keeps its artwork here
                        i = j
                        continue
                    if bad or any(ch.isdigit() for ch in text):
                        close()                # an uncaptured number: stays in the artwork
                        i += 1
                        continue
                    if run is None:
                        run = Piece(w[0]["bbox"][0], w[-1]["bbox"][2], base, span["size"], span["color"],
                                    stem, weight, italic, fam)
                    run.text += text
                    run.x1 = w[-1]["bbox"][2]
                    run.chars += w
                    i += 1
                close()
    return pieces


def _merge_adjacent(pieces: list[Piece]) -> list[Piece]:
    """Consecutive plain-text pieces of one line and style become one element."""
    out: list[Piece] = []
    for p in pieces:
        q = out[-1] if out else None
        if q and not q.fid and not p.fid and q.stem == p.stem and q.color == p.color and abs(q.size - p.size) < 0.05 \
                and abs(q.baseline - p.baseline) < 0.3 and 0 <= p.x0 - q.x1 < p.size * 0.6:
            gap = " " if p.x0 - q.x1 > p.size * 0.12 else ""
            q.text += gap + p.text
            q.x1 = p.x1
            q.chars += p.chars
            continue
        out.append(p)
    return out


_BT = re.compile(rb"(?<![A-Za-z0-9_])BT(?![A-Za-z0-9_])")
_INLINE_IMAGE = re.compile(rb"(?<![A-Za-z])BI(?![A-Za-z]).*?(?<![A-Za-z])EI(?![A-Za-z])", re.S)


def _hide_text(stream: bytes) -> bytes:
    """Make every text object invisible (render mode 3) — outside inline image data."""
    out, last = [], 0
    for m in _INLINE_IMAGE.finditer(stream):
        out.append(_BT.sub(b"BT 3 Tr", stream[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(_BT.sub(b"BT 3 Tr", stream[last:]))
    return b"".join(out)


class Artist:
    """Page artwork with the re-set text lifted out, made from two renders: the page as
    printed, and the page with its text made invisible. Text we don't re-set as HTML
    (numbers that aren't captured figures, symbol glyphs, rotated text) is copied back
    from the as-printed render, so it stays exactly as printed. Only page and form
    content streams are touched — never images, fonts or colour profiles."""

    def __init__(self, pdf_bytes: bytes):
        self.printed = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        self.blank = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        done: set[int] = set()
        for page in self.blank:
            refs = list(page.get_contents()) + [x[0] for x in page.get_xobjects()
                                                 if self.blank.xref_get_key(x[0], "Subtype")[1] == "/Form"]
            for xref in refs:
                if xref in done:
                    continue
                done.add(xref)
                data = self.blank.xref_stream(xref)
                if data and b"BT" in data:
                    self.blank.update_stream(xref, _hide_text(data))

    def close(self) -> None:
        self.printed.close()
        self.blank.close()

    def artwork(self, n: int, pieces: list[Piece]) -> Image.Image:
        m = pymupdf.Matrix(BG_SCALE, BG_SCALE)
        pb = self.blank[n - 1].get_pixmap(matrix=m, alpha=False)
        img = Image.frombytes("RGB", (pb.width, pb.height), pb.samples)
        emitted = {tuple(round(v, 1) for v in c["bbox"]) for p in pieces for c in p.chars}
        keep = []
        raw = self.printed[n - 1].get_text("rawdict", flags=pymupdf.TEXT_MEDIABOX_CLIP)
        for block in raw["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if span["size"] < MIN_PT or span.get("alpha", 255) == 0:
                        continue
                    for c in span["chars"]:
                        if c["c"].isspace() or tuple(round(v, 1) for v in c["bbox"]) in emitted:
                            continue
                        keep.append(c["bbox"])
        pa = self.printed[n - 1].get_pixmap(matrix=m, alpha=False)
        printed = Image.frombytes("RGB", (pa.width, pa.height), pa.samples)
        self.last_printed = printed           # the page as printed: kept for the review screen's PDF pane
        if keep:
            for x0, y0, x1, y1 in keep:
                box = (max(0, int((x0 - 0.6) * BG_SCALE)), max(0, int((y0 - 0.6) * BG_SCALE)),
                       min(img.width, int((x1 + 0.6) * BG_SCALE) + 1), min(img.height, int((y1 + 0.6) * BG_SCALE) + 1))
                if box[2] > box[0] and box[3] > box[1]:
                    img.paste(printed.crop(box), box[:2])
        return img


def webp(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=BG_QUALITY, method=2)
    return buf.getvalue()


def _hex(c: int) -> str:
    return f"#{c:06x}"


def _n(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".")


FAMILY_CODE = {"sans": "a", "poppins": "p", "serif": "r"}


def face_class(fam: str, weight: int, italic: bool) -> str:
    return f"f{FAMILY_CODE[fam]}{weight // 100}{'i' if italic else ''}"


_NO_SPACE_BEFORE = tuple(",.;:!?%)]}\u2019\u201d")


def pieces_html(ps: list[Piece], one) -> str:
    """The pieces' HTML with real word breaks between them. Each piece is positioned on its
    own, so on screen nothing changes (whitespace between absolute boxes takes no room),
    but copied text, search engines and screen readers get "Enterprise for" and "credo
    Nation First", not "Enterprisefor" and "credoNation First"."""
    out, prev = [], None
    for p in ps:
        if prev is not None:
            text, before = p.text or "0", prev.text or "0"
            same_line = abs(p.baseline - prev.baseline) < 0.3 * max(p.size, prev.size)
            touching = same_line and p.x0 - prev.x1 < 0.12 * p.size        # a font change mid-word
            if not (touching or text.startswith(_NO_SPACE_BEFORE) or (not same_line and before.endswith("-"))):
                out.append(" ")
        out.append(one(p))
        prev = p
    return "".join(out)


def piece_html(p: Piece, W: float, H: float, figures: dict, y0: float = 0.0, x0: float = 0.0) -> str:
    asc, desc = _metrics(p.stem, p.weight)
    top = p.baseline - p.size * (1 + asc - desc) / 2          # line-height:1 box top
    target = max(0.1, p.x1 - p.x0)
    text = p.text if not p.fid else figures[p.fid]["raw"]
    natural = measure(text, p.stem, p.weight, p.size)
    # Letter width: the PDF's own glyphs vs ours, measured on the letters alone (spaces
    # excluded). This ratio is the same for every line of the same font, so all lines
    # share one letter width — a justified line and the ragged line under it no longer
    # look like different sizes. Word gaps then absorb the rest, as justification does.
    ink = [c for c in p.chars if not c["c"].isspace()]
    pdf_letters = sum(c["bbox"][2] - c["bbox"][0] for c in ink)
    our_letters = measure("".join(c["c"] for c in ink), p.stem, p.weight, p.size) if ink else 0
    if pdf_letters > 0 and our_letters > 0:
        k = min(1.12, max(0.7, pdf_letters / our_letters))
    else:
        k = min(1.12, max(0.6, target / natural)) if natural > 0 else 1.0
    gaps = text.count(" ")
    ws = 0.0
    if gaps and natural > 0:
        ws = (target / k - natural) / gaps          # extra (or less) per word gap, before scaling
        ws = max(-0.25 * p.size, min(ws, 1.5 * p.size))
        rendered = k * (natural + ws * gaps)
        if rendered > target * 1.01:                # still too long: tighten letters a little more
            k = max(0.6, k * target / rendered)
    elif natural > 0:
        k = min(1.12, max(0.6, target / natural))
    cls = face_class(p.family, p.weight, p.italic)
    style = f"--x:{_n((p.x0 - x0) / W * 100)};--y:{_n((top - y0) / H * 100)};--s:{_n(p.size / W * 100)}"
    if abs(ws) > 0.01:
        style += f";--ws:{_n(ws / p.size)}"
    if abs(k - 1) > 0.004:
        style += f";--k:{_n(k)}"
    if p.color:
        style += f";color:{_hex(p.color)}"
    if p.fid:
        f = figures[p.fid]
        raw = html.escape(f["raw"])
        if f["kind"] in ("number", "percent", "bps", "multiple", "nil"):
            return (f'<data class="w {cls}" style="{style}" value="{html.escape(str(f.get("value") or ""))}" '
                    f'data-fig="{f["id"]}">{raw}</data>')
        if f["kind"] == "date":
            iso = f' datetime="{html.escape(f["iso"])}"' if f.get("iso") else ""
            return f'<time class="w {cls}" style="{style}"{iso} data-fig="{f["id"]}">{raw}</time>'
        return f'<span class="w {cls}" style="{style}" data-fig="{f["id"]}">{raw}</span>'
    return f'<span class="w {cls}" style="{style}">{html.escape(p.text)}</span>'


def links_html(page: pymupdf.Page, W: float, H: float, page_count: int, y0: float = 0.0, y1: float | None = None,
               kept: set[int] | None = None) -> str:
    out = []
    y1 = page.rect.height if y1 is None else y1
    for ln in page.get_links():
        r = ln.get("from")
        if not r or r.y1 <= y0 or r.y0 >= y1:
            continue
        if ln.get("kind") == pymupdf.LINK_GOTO and kept is not None and ln.get("page", -1) + 1 not in kept:
            continue                       # points at a page the web edition leaves out
        style = (f"--x:{_n(r.x0 / W * 100)};--y:{_n((r.y0 - y0) / H * 100)};"
                 f"--lw:{_n((r.x1 - r.x0) / W * 100)};--lh:{_n((r.y1 - r.y0) / H * 100)}")
        if ln.get("kind") == pymupdf.LINK_GOTO and 0 <= ln.get("page", -1) < page_count:
            out.append(f'<a class="pl" style="{style}" href="#p{ln["page"] + 1}" aria-label="Go to the linked page"></a>')
        elif ln.get("kind") == pymupdf.LINK_URI and _URL.match(ln.get("uri") or ""):
            out.append(f'<a class="pl" style="{style}" href="{html.escape(ln["uri"])}" rel="noopener" '
                       f'aria-label="Open the linked website"></a>')
    return "".join(out)


# ------------------------------------------------------------------ one continuous page
# The web edition is not a stack of PDF pages. Each page's content runs straight into the
# next: running headers, footers and page numbers, and blank margins, are trimmed away;
# contents pages become the site's own contents menu; blank pages are left out.

BAND = 0.13               # share of page height where running headers/footers live
PAD_PT = 10               # breathing room kept around trimmed content


def _norm(t: str) -> str:
    # Exact text (spacing aside): "Q1." and "Q4." are content, not a running header.
    return re.sub(r"\s+", " ", t).strip().lower()


def _band_lines(page: pymupdf.Page) -> list[tuple[str, float, float, str, float]]:
    """(band, y0, y1, normalised text, size) for text lines near the top or bottom edge."""
    H, W = page.rect.height, page.rect.width
    out = []
    edge = [b for clip in (pymupdf.Rect(0, 0, W, H * BAND), pymupdf.Rect(0, H * (1 - BAND), W, H))
            for b in page.get_text("dict", clip=clip)["blocks"]]
    for b in edge:
        for ln in b.get("lines", []):
            t = "".join(sp["text"] for sp in ln["spans"]).strip()
            if not t:
                continue
            y0, y1 = ln["bbox"][1], ln["bbox"][3]
            size = max(sp["size"] for sp in ln["spans"])
            if y1 <= H * BAND:
                out.append(("top", y0, y1, _norm(t), size))
            elif y0 >= H * (1 - BAND):
                out.append(("bottom", y0, y1, _norm(t), size))
    return out


def _band_art(page: pymupdf.Page) -> list[tuple[str, tuple, float, float]]:
    """(band, rounded rect, y0, y1) for vector art near the top or bottom edge — running
    headers and footers are often drawn as outlines rather than text."""
    H = page.rect.height
    out = []
    for d in page.get_cdrawings():
        r = pymupdf.Rect(d["rect"])
        if r.y1 <= H * BAND:
            band = "top"
        elif r.y0 >= H * (1 - BAND):
            band = "bottom"
        else:
            continue
        out.append((band, (round(r.x0), round(r.y0), round(r.x1), round(r.y1)), r.y0, r.y1))
    return out


SECTION_TITLE_PT = 11     # a repeated line at least this big is a running section title
ART_REPEAT = 5            # identical edge artwork on this many pages is page furniture


def running_bands(doc: pymupdf.Document, pages: range) -> dict[int, tuple[float, float]]:
    """Per page, the (top, bottom) y between the running header and footer.

    Running = page furniture repeated across the report: the same header/footer artwork
    or text in the same edge band on many pages, page numbers, and — after its first
    appearance — a section's running title repeated at the top of its following pages
    (the first stays, so the section is still introduced once)."""
    lines = {n: _band_lines(doc[n - 1]) for n in pages}
    art = {n: _band_art(doc[n - 1]) for n in pages}
    seen_t: dict[tuple[str, str], int] = {}
    seen_a: dict[tuple[str, tuple], int] = {}
    for n in pages:
        for key in {(band, t) for band, _a, _b, t, _s in lines[n]}:
            seen_t[key] = seen_t.get(key, 0) + 1
        for key in {(band, r) for band, r, _a, _b in art[n]}:
            seen_a[key] = seen_a.get(key, 0) + 1
    need = max(3, int(len(lines) * 0.15))
    out = {}
    for n in pages:
        H = doc[n - 1].rect.height
        top, bottom = 0.0, H
        prev = {(b, t) for b, _a, _c, t, _s in lines.get(n - 1, [])}
        for band, y0, y1, t, size in lines[n]:
            running = seen_t.get((band, t), 0) >= need \
                or re.fullmatch(r"[\d\s.\-–|]+|[ivxlc]+", t) is not None \
                or (band == "top" and size >= SECTION_TITLE_PT and seen_t.get((band, t), 0) >= 3 and (band, t) in prev)
            if running:
                if band == "top":
                    top = max(top, y1 + 3)
                else:
                    bottom = min(bottom, y0 - 3)
        # Repeated edge artwork counts only where it sits clear of the page's own text in
        # that band (above the first content line / below the last), so a repeated table
        # rule can never take a table's header row with it.
        content_top = min([y0 for b, y0, _y1, _t, _s in lines[n] if b == "top" and y0 >= top] + [H * BAND])
        content_bottom = max([y1 for b, _y0, y1, _t, _s in lines[n] if b == "bottom" and y1 <= bottom] + [H * (1 - BAND)])
        for band, r, y0, y1 in art[n]:
            if seen_a.get((band, r), 0) < ART_REPEAT:
                continue
            if band == "top" and y1 <= content_top:
                top = max(top, y1 + 3)
            elif band == "bottom" and y0 >= content_bottom:
                bottom = min(bottom, y0 - 3)
        out[n] = (top, bottom) if bottom > top + 40 else (0.0, H)
    return out


def _content_rows(img: Image.Image, y0: float, y1: float, pieces: list["Piece"]) -> tuple[float, float] | None:
    """The vertical extent (PDF points) of what's actually on the page between y0 and y1:
    any non-white artwork, plus the text we lay over it. None = nothing there."""
    import numpy as np
    a = np.asarray(img.convert("L"))
    r0, r1 = int(y0 * BG_SCALE), int(y1 * BG_SCALE)
    ink = (a[r0:r1] < 244).any(axis=1)
    text_rows = np.zeros_like(ink)
    for p in pieces:
        t, b = int((p.baseline - p.size) * BG_SCALE) - r0, int((p.baseline + p.size * 0.25) * BG_SCALE) - r0
        if b > 0 and t < len(ink):
            text_rows[max(0, t):min(len(ink), b)] = True
    ink = ink | text_rows
    # Runs of inked rows; a thin decorative rule alone at the top or bottom edge (the
    # line many reports print across every page) is page furniture, not content.
    runs, k = [], 0
    while k < len(ink):
        if ink[k]:
            j = k
            while j < len(ink) and ink[j]:
                j += 1
            runs.append((k, j))
            k = j
        else:
            k += 1
    thin, gap = 6 * BG_SCALE, 6 * BG_SCALE
    while len(runs) > 1 and runs[0][1] - runs[0][0] <= thin and runs[1][0] - runs[0][1] >= gap \
            and not text_rows[runs[0][0]:runs[0][1]].any():
        runs.pop(0)
    while len(runs) > 1 and runs[-1][1] - runs[-1][0] <= thin and runs[-1][0] - runs[-2][1] >= gap \
            and not text_rows[runs[-1][0]:runs[-1][1]].any():
        runs.pop()
    ext = [(r0 + runs[0][0]) / BG_SCALE, (r0 + runs[-1][1]) / BG_SCALE] if runs else []
    if not ext:
        return None
    return max(y0, min(ext) - PAD_PT), min(y1, max(ext) + PAD_PT)


def contents_page(page: pymupdf.Page) -> bool:
    """A contents page: a list of links to many different pages of the same document."""
    targets = {ln.get("page") for ln in page.get_links() if ln.get("kind") == pymupdf.LINK_GOTO}
    return len(targets) >= 5


def _inline(p: "Piece", figures: dict) -> str:
    if p.fid:
        f = figures[p.fid]
        if f["kind"] in ("number", "percent", "bps", "multiple", "nil"):
            return f'<data value="{html.escape(str(f.get("value") or ""))}" data-fig="{f["id"]}">{html.escape(f["raw"])}</data>'
        return f'<span data-fig="{f["id"]}">{html.escape(f["raw"])}</span>'
    return html.escape(p.text)


def contents_from_links(page: pymupdf.Page, pieces: list["Piece"], figures: dict) -> list[dict]:
    """The contents page's entries as menu items: the words under each link, and where it goes."""
    items, seen = [], set()
    for ln in sorted(page.get_links(), key=lambda ln: (round(ln["from"].y0), ln["from"].x0)):
        if ln.get("kind") != pymupdf.LINK_GOTO:
            continue
        r = ln["from"]
        inside = sorted((p for p in pieces if r.x0 - 1 <= (p.x0 + p.x1) / 2 <= r.x1 + 1
                         and r.y0 - 2 <= p.baseline - p.size / 2 <= r.y1 + 2), key=lambda p: p.x0)
        label = " ".join(_inline(p, figures) for p in inside).strip(" ◆•▪–-")
        key = (ln["page"], label)
        if label and key not in seen:
            seen.add(key)
            items.append({"page": ln["page"] + 1, "label": label})
    return items


# ------------------------------------------------------------------ building pages in parallel
# Pages are independent once the running header/footer bands are known, so they're built
# across CPU cores. Each worker opens its own copy of the PDF.

_W: dict = {}


def _worker_init(pdf_bytes: bytes, schema: dict, bands: dict, methods: dict, visuals: dict) -> None:
    _W.update(doc=pymupdf.open(stream=pdf_bytes, filetype="pdf"), artist=Artist(pdf_bytes), schema=schema,
              bands=bands, methods=methods, visuals=visuals)


def _build_page(n: int) -> dict:
    doc, schema, figures = _W["doc"], _W["schema"], _W["schema"]["figures"]
    page = doc[n - 1]
    W = page.rect.width
    ocr = _W["methods"].get(n) == "ocr"
    pieces = [] if ocr else _merge_adjacent(page_pieces(page, schema, n))
    if not ocr and contents_page(page):
        return {"n": n, "kind": "contents", "menu": contents_from_links(page, pieces, figures),
                "pdf_page": _pdf_page(page)}
    top, bottom = _W["bands"].get(n, (0.0, page.rect.height))
    img = _W["artist"].artwork(n, pieces)
    printed = webp(_W["artist"].last_printed)
    ext = _content_rows(img, top, bottom, pieces)
    if ext is None:
        return {"n": n, "kind": "blank", "pdf_page": printed}
    c0, c1 = ext
    H = c1 - c0
    crop = img.crop((0, int(c0 * BG_SCALE), img.width, int(c1 * BG_SCALE)))
    inside = [p for p in pieces if p.baseline - p.size * 0.8 >= c0 - 1 and p.baseline <= c1 + 1]
    # Phone view: each chart/graphic of this page as a crop of the same artwork, with
    # its own labels laid over it (positions relative to the crop).
    vis = {}
    for bid, (x0, y0, x1, y1) in _W["visuals"].get(n, []):
        y0, y1 = max(y0, c0), min(y1, c1)
        if x1 - x0 < 20 or y1 - y0 < 20:
            continue
        rw, rh = x1 - x0, y1 - y0
        # Only labels wholly inside the crop: a heading that merely touches it would be cut.
        ov = pieces_html([p for p in inside if x0 - 1 <= p.x0 and p.x1 <= x1 + 1
                          and y0 - 1 <= p.baseline - p.size and p.baseline <= y1 + 1],
                         lambda p: piece_html(p, rw, rh, figures, y0, x0))
        vis[bid] = {"w": rw, "h": rh, "img_w": _n(W / rw * 100), "left": _n((x0) / rw * 100),
                    "top": _n((y0 - c0) / rh * 100), "html": ov}
    return {"n": n, "kind": "part", "W": W, "c0": c0, "c1": c1, "webp": webp(crop), "pdf_page": printed, "bg_w": crop.width,
            "bg_h": crop.height, "body": pieces_html(inside, lambda p: piece_html(p, W, H, figures, c0)), "ocr": ocr,
            "vis": vis}


def _pdf_page(page: pymupdf.Page) -> bytes:
    pix = page.get_pixmap(matrix=pymupdf.Matrix(BG_SCALE, BG_SCALE), alpha=False)
    return webp(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))


def _workers() -> int:
    from app.runtime import process_workers
    return process_workers(cap=8)


def render_pages(schema: dict, pdf_bytes: bytes, *, page_methods: dict[int, str], progress=None,
                 max_pages: int | None = None, skip_pages: set[int] | None = None, visuals: dict | None = None,
                 pdf_page_sink=None, keep_printed: set[int] | None = None) -> tuple[list[dict], dict[str, bytes], list[dict]]:
    """Returns (parts, artwork files, contents menu). A part is one PDF page's content,
    trimmed so parts run together as one continuous page. `visuals` = {page: [(block id,
    bbox)]} for the phone view's chart/graphic crops."""
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        total = min(doc.page_count, max_pages or doc.page_count)
        bands = running_bands(doc, range(1, doc.page_count + 1))
        skip = set(skip_pages or ())
        todo = [n for n in range(1, total + 1) if n not in skip]
        args = (pdf_bytes, schema, bands, page_methods, visuals or {})
        results: list[dict] = []
        workers = _workers() if len(todo) > 8 else 1
        if workers > 1:
            import multiprocessing as mp
            from concurrent.futures import ProcessPoolExecutor
            try:
                with ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("spawn"),
                                         initializer=_worker_init, initargs=args) as pool:
                    for k, r in enumerate(pool.map(_build_page, todo, chunksize=4), start=1):
                        results.append(r)
                        if progress:
                            progress(k, len(todo))
            except Exception:  # noqa: BLE001 - no process pool here (sandbox, serverless): build serially
                results, workers = [], 1
        if workers == 1:
            _worker_init(*args)
            try:
                for k, n in enumerate(todo, start=1):
                    results.append(_build_page(n))
                    if progress:
                        progress(k, len(todo))
            finally:
                _W["doc"].close()
                _W["artist"].close()
                _W.clear()
        if pdf_page_sink:
            for r in results:
                if r.get("pdf_page"):
                    pdf_page_sink(r["n"], r["pdf_page"])
        menu = [it for r in results if r["kind"] == "contents" for it in r["menu"]]
        built = [r for r in results if r["kind"] == "part"]
        kept = {r["n"] for r in built}
        parts, files = [], {}
        for r in results:
            # the page as printed, for pages laid out as web sections (crops of its pictures)
            if keep_printed and r["n"] in keep_printed and r.get("pdf_page"):
                files[f"pages/p{r['n']:04d}-print.webp"] = r["pdf_page"]
        for r in built:
            n, W, c0, c1 = r["n"], r["W"], r["c0"], r["c1"]
            name = f"pages/p{n:04d}.webp"
            files[name] = r["webp"]
            links = links_html(doc[n - 1], W, c1 - c0, doc.page_count, c0, c1, kept)
            parts.append({"n": n, "w": W, "h": c1 - c0, "bg": name, "bg_w": r["bg_w"], "bg_h": r["bg_h"],
                          "html": r["body"] + links, "ocr": r["ocr"], "vis": r["vis"]})
        # Menu entries point at the part that holds their page (or the next one kept).
        ks = sorted(kept)
        for it in menu:
            it["page"] = next((k for k in ks if k >= it["page"]), None)
        menu = [it for it in menu if it["page"]]
    finally:
        doc.close()
    return parts, files, menu


FACE_CSS_FILES = {
    ("sans", 400, False): "Arimo-Regular", ("sans", 700, False): "Arimo-Bold",
    ("sans", 400, True): "Arimo-Italic", ("sans", 700, True): "Arimo-BoldItalic",
    ("poppins", 400, False): "Poppins-Regular", ("poppins", 600, False): "Poppins-SemiBold",
    ("poppins", 700, False): "Poppins-Bold", ("poppins", 400, True): "Poppins-Italic",
    ("serif", 400, False): "LibreBodoni", ("serif", 700, False): "LibreBodoni",
    ("serif", 400, True): "LibreBodoni-Italic", ("serif", 700, True): "LibreBodoni-Italic",
}
FAMILY_NAME = {"sans": "PP Sans", "poppins": "PP Poppins", "serif": "PP Serif"}


def fonts_css(href) -> str:
    """@font-face for the bundled faces + one class per face used on the pages."""
    out = []
    seen = set()
    for (fam, w, it), stem in FACE_CSS_FILES.items():
        key = (fam, w, it)
        if key in seen:
            continue
        seen.add(key)
        wr = "400 700" if stem.startswith("LibreBodoni") else str(w)
        out.append(f"@font-face{{font-family:'{FAMILY_NAME[fam]}';src:url({href(f'fonts/{stem}.woff2')}) format('woff2');"
                   f"font-weight:{wr};font-style:{'italic' if it else 'normal'};font-display:block}}")
    for (fam, w, it) in sorted(seen):
        out.append(f".{face_class(fam, w, it)}{{font-family:'{FAMILY_NAME[fam]}',Arial,sans-serif;font-weight:{w};"
                   f"font-style:{'italic' if it else 'normal'}}}")
    return "".join(out)


def font_files() -> dict[str, bytes]:
    return {f"fonts/{p.name}": p.read_bytes() for p in FONT_DIR.glob("*.woff2")} | \
        {f"fonts/{p.name}": p.read_bytes() for p in FONT_DIR.glob("*-OFL.txt")}
