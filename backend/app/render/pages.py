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


def artwork(page: pymupdf.Page, pieces: list[Piece]) -> bytes:
    """The page with the laid-over text lifted out, as WebP. Only glyphs we re-set as
    HTML are removed; images and vector art are untouched."""
    for p in pieces:
        if not p.chars:
            continue
        # One area per piece: its characters' extent, pulled in from every side so no
        # neighbouring glyph (a number we leave in the artwork) is touched.
        first, last = p.chars[0]["bbox"], p.chars[-1]["bbox"]
        y0 = min(c["bbox"][1] for c in p.chars)
        y1 = max(c["bbox"][3] for c in p.chars)
        h = y1 - y0
        x0 = first[0] + (first[2] - first[0]) * 0.3
        x1 = last[2] - (last[2] - last[0]) * 0.3
        if x1 > x0 and h > 0:
            page.add_redact_annot(pymupdf.Rect(x0, y0 + h * 0.3, x1, y1 - h * 0.3))
    if pieces:
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(BG_SCALE, BG_SCALE), alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=BG_QUALITY, method=4)
    return buf.getvalue()


def _hex(c: int) -> str:
    return f"#{c:06x}"


def _n(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".")


FAMILY_CODE = {"sans": "a", "poppins": "p", "serif": "r"}


def face_class(fam: str, weight: int, italic: bool) -> str:
    return f"f{FAMILY_CODE[fam]}{weight // 100}{'i' if italic else ''}"


def piece_html(p: Piece, W: float, H: float, figures: dict) -> str:
    asc, desc = _metrics(p.stem, p.weight)
    top = p.baseline - p.size * (1 + asc - desc) / 2          # line-height:1 box top
    target = max(0.1, p.x1 - p.x0)
    natural = measure(p.text, p.stem, p.weight, p.size)
    k = target / natural if natural > 0 else 1.0
    k = min(1.6, max(0.55, k))
    cls = face_class(p.family, p.weight, p.italic)
    style = f"--x:{_n(p.x0 / W * 100)};--y:{_n(top / H * 100)};--s:{_n(p.size / W * 100)}"
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


def links_html(page: pymupdf.Page, W: float, H: float, page_count: int) -> str:
    out = []
    for ln in page.get_links():
        r = ln.get("from")
        if not r:
            continue
        style = (f"--x:{_n(r.x0 / W * 100)};--y:{_n(r.y0 / H * 100)};"
                 f"--lw:{_n((r.x1 - r.x0) / W * 100)};--lh:{_n((r.y1 - r.y0) / H * 100)}")
        if ln.get("kind") == pymupdf.LINK_GOTO and 0 <= ln.get("page", -1) < page_count:
            out.append(f'<a class="pl" style="{style}" href="#p{ln["page"] + 1}" aria-label="Go to the linked page"></a>')
        elif ln.get("kind") == pymupdf.LINK_URI and _URL.match(ln.get("uri") or ""):
            out.append(f'<a class="pl" style="{style}" href="{html.escape(ln["uri"])}" rel="noopener" '
                       f'aria-label="Open the linked website"></a>')
    return "".join(out)


def render_pages(schema: dict, pdf_bytes: bytes, *, page_methods: dict[int, str], progress=None,
                 max_pages: int | None = None) -> tuple[list[dict], dict[str, bytes]]:
    """Per page: {"n", "w", "h", "bg", "html", "ocr"} plus the artwork files."""
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")      # a private copy: redactions never persist
    links_doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    figures = schema["figures"]
    pages, files = [], {}
    try:
        for i in range(min(doc.page_count, max_pages or doc.page_count)):
            n = i + 1
            page = doc[i]
            W, H = page.rect.width, page.rect.height
            ocr = page_methods.get(n) == "ocr"
            pieces = [] if ocr else _merge_adjacent(page_pieces(page, schema, n))
            body = "".join(piece_html(p, W, H, figures) for p in pieces)
            links = links_html(links_doc[i], W, H, doc.page_count)
            name = f"pages/p{n:04d}.webp"
            files[name] = artwork(page, pieces)
            pages.append({"n": n, "w": W, "h": H, "bg": name, "bg_w": int(W * BG_SCALE), "bg_h": int(H * BG_SCALE),
                          "html": body + links, "ocr": ocr})
            if progress:
                progress(n, min(doc.page_count, max_pages or doc.page_count))
    finally:
        doc.close()
        links_doc.close()
    return pages, files


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
