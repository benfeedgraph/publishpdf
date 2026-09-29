"""Read a PDF into per-page words with bounding boxes, from the text layer when it is
usable and from OCR otherwise. Also finds image regions and vector-chart regions."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Literal

import pymupdf

from app.extraction import ocr

Method = Literal["text_layer", "ocr"]


@dataclass
class Word:
    text: str
    bbox: tuple[float, float, float, float]
    conf: float = 1.0
    size: float = 0.0          # font size (text layer) or glyph height (OCR)
    bold: bool = False
    src: tuple[float, float, float, float] | None = None   # original-page bbox if bbox was deskewed

    @property
    def source_bbox(self) -> tuple[float, float, float, float]:
        return self.src or self.bbox

    @property
    def xc(self) -> float:
        return (self.bbox[0] + self.bbox[2]) / 2

    @property
    def yc(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2

    @property
    def h(self) -> float:
        return self.bbox[3] - self.bbox[1]


@dataclass
class PageData:
    number: int                      # 1-based
    width: float
    height: float
    method: Method
    words: list[Word]
    text_layer_reason: str
    image_regions: list[tuple[float, float, float, float]] = field(default_factory=list)
    chart_regions: list[tuple[float, float, float, float]] = field(default_factory=list)
    hidden_numbers: int = 0          # digit-bearing words too small to see (excluded)


class PdfError(Exception):
    """Plain-language problem with the uploaded file."""


def open_pdf(data: bytes) -> pymupdf.Document:
    if not data.startswith(b"%PDF-"):
        raise PdfError("This file isn't a PDF. Upload the report as a PDF file.")
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:  # noqa: BLE001
        raise PdfError("This PDF is damaged and can't be opened. Try exporting it again.") from e
    if doc.needs_pass or doc.is_encrypted:
        raise PdfError("This PDF is password-protected. Upload an unlocked copy.")
    if doc.page_count == 0:
        raise PdfError("This PDF has no pages.")
    return doc


def _bad_char(ch: str) -> bool:
    cp = ord(ch)
    return ch == "�" or cp == 0 or 0xE000 <= cp <= 0xF8FF or unicodedata.category(ch) == "Cc"


MIN_VISIBLE_PT = 2.5   # text smaller than this can't be read by a person (hidden layers behind images)


def text_layer_words(page: pymupdf.Page, *, hidden: list | None = None) -> tuple[list[Word], int, int]:
    """Words from the text layer, built from characters so every word has an exact bbox.
    Microscopic text (under MIN_VISIBLE_PT, e.g. chart labels hidden behind a chart
    image) is left out — nobody can see it on the page — and appended to `hidden`.
    Returns (words, total_chars, bad_chars)."""
    raw = page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP)
    words: list[Word] = []
    total = bad = 0
    for block in raw["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            if abs(line["dir"][1]) > 0.1:        # skip rotated text (watermarks, axis labels)
                continue
            cur: list[tuple[str, tuple]] = []
            cur_size, cur_bold = 0.0, False

            def flush() -> None:
                nonlocal cur
                if cur:
                    text = "".join(c for c, _ in cur)
                    xs0 = min(b[0] for _, b in cur)
                    ys0 = min(b[1] for _, b in cur)
                    xs1 = max(b[2] for _, b in cur)
                    ys1 = max(b[3] for _, b in cur)
                    w = Word(text, (xs0, ys0, xs1, ys1), 1.0, cur_size, cur_bold)
                    if cur_size < MIN_VISIBLE_PT:
                        if hidden is not None:
                            hidden.append(w)
                    else:
                        words.append(w)
                cur = []

            for span in line["spans"]:
                bold = bool(span["flags"] & 16) or "bold" in span["font"].lower()
                for ch in span["chars"]:
                    c = ch["c"]
                    total += 1
                    if _bad_char(c):
                        bad += 1
                    if c.isspace():
                        flush()
                        continue
                    cur_size, cur_bold = span["size"], bold
                    cur.append((c, tuple(ch["bbox"])))
            flush()
    return words, total, bad


def _image_regions(page: pymupdf.Page) -> list[tuple[float, float, float, float]]:
    area = page.rect.width * page.rect.height
    out = []
    for img in page.get_images(full=True):
        for r in page.get_image_rects(img[0]):
            if r.width * r.height > 0.03 * area:
                out.append((r.x0, r.y0, r.x1, r.y1))
    return out


def _chart_regions(page: pymupdf.Page) -> list[tuple[float, float, float, float]]:
    """Groups of >=3 filled, coloured shapes (bars, pie slices) = likely vector chart."""
    shapes = []
    for d in page.get_drawings():
        fill = d.get("fill")
        r = d["rect"]
        if not fill or r.width * r.height < 40:
            continue
        if all(abs(c - fill[0]) < 0.05 for c in fill) and fill[0] > 0.85:   # white/near-white
            continue
        if r.height < 2 or r.width < 2:                                     # rules / lines
            continue
        shapes.append(r)
    # Transitive clustering: shapes within GAP of each other belong to one chart.
    GAP = 60.0
    groups: list[pymupdf.Rect] = []
    counts: list[int] = []
    for r in shapes:
        groups.append(pymupdf.Rect(r))
        counts.append(1)
    merged = True
    while merged:
        merged = False
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                if (groups[i] + (-GAP, -GAP, GAP, GAP)).intersects(groups[j]):
                    groups[i] |= groups[j]
                    counts[i] += counts[j]
                    del groups[j], counts[j]
                    merged = True
                    break
            if merged:
                break
    out = []
    for g, n in zip(groups, counts):
        if n >= 3:
            e = (g + (-10, -30, 10, 30)) & page.rect
            out.append((e.x0, e.y0, e.x1, e.y1))
    return out


def _classify(page: pymupdf.Page):
    hidden: list = []
    words, total, bad = text_layer_words(page, hidden=hidden)
    image_regions = _image_regions(page)
    area = page.rect.width * page.rect.height
    image_cover = sum((r[2] - r[0]) * (r[3] - r[1]) for r in image_regions) / area
    if total < 20 and image_cover > 0.5:
        method, reason = "ocr", "no text layer (scanned page)"
    elif total >= 20 and bad / total > 0.02:
        method, reason = "ocr", f"text layer unusable ({bad} of {total} characters unreadable)"
    elif total < 20 and not image_regions:
        method, reason = "text_layer", "page has little or no text"
    elif total < 20:
        method, reason = "ocr", "text layer nearly empty"
    else:
        method, reason = "text_layer", "usable text layer"
    # A page-sized image under a usable text layer is a scan with an (invisible) OCR
    # layer, not a chart. Keep the text layer; re-extraction will check it visually.
    if method == "text_layer" and image_cover > 0.6:
        reason = "text layer over a full-page image (previously OCR'd scan)"
        image_regions = []
    hidden_numbers = sum(1 for w in hidden if any(ch.isdigit() for ch in w.text))
    return words, method, reason, image_regions, hidden_numbers


def read_page(page: pymupdf.Page) -> PageData:
    return read_pages(page.parent, [page.number])[0]


def read_pages(doc: pymupdf.Document, numbers: list[int] | None = None, *, workers: int = 4,
               progress=None) -> list[PageData]:
    """Read pages; scanned pages are OCR'd in parallel (rendering stays single-threaded,
    since PyMuPDF documents aren't thread-safe)."""
    from concurrent.futures import ThreadPoolExecutor

    numbers = list(range(doc.page_count)) if numbers is None else numbers
    results: dict[int, PageData] = {}
    pending = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, n in enumerate(numbers):
            page = doc[n]
            words, method, reason, image_regions, hidden_numbers = _classify(page)
            if method == "ocr":
                pending[n] = (pool.submit(ocr.run_prepared, ocr.prepare_page(page)), reason)
            else:
                results[n] = PageData(n + 1, page.rect.width, page.rect.height, method, words, reason,
                                      image_regions, _chart_regions(page), hidden_numbers)
            if progress:
                progress(i + 1, len(numbers))
        for n, (fut, reason) in pending.items():
            page = doc[n]
            words = [Word(w.text, w.bbox, w.conf, w.bbox[3] - w.bbox[1], src=w.src) for w in fut.result()]
            results[n] = PageData(n + 1, page.rect.width, page.rect.height, "ocr", words, reason, [], [])
    return [results[n] for n in numbers]
