"""A design-led PDF page as web elements: its text blocks (with size, weight and colour)
and its pictures, each with an ID. An AI model may arrange these IDs into web sections
(app/render/ai_layout.py) — it never sees anything it could type back into the page.

Text reaches HTML through the same rule as everywhere else: a number only as a verified
figure (pages.page_pieces). A text block holding a number that isn't a verified figure is
shown as a crop of the printed page instead, so nothing unverified becomes text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pymupdf

from app.render import pages as pages_mod

MIN_PICTURE_SHARE = 0.008      # rasters smaller than this share of the page are decoration
COLUMN_GAP_EM = 3.0            # a gap this wide (in font sizes) between spans = separate columns
_DIGIT = re.compile(r"\d")


@dataclass
class Element:
    id: str
    kind: str                       # "text" | "picture"
    bbox: tuple[float, float, float, float]
    text: str = ""                  # plain text (for the AI's eyes and for fallbacks)
    html: str = ""                  # safe inline HTML (figures as <data data-fig>)
    size: float = 0.0               # dominant font size, points
    bold: bool = False
    color: str = "#000000"
    safe: bool = True               # False: show as a crop of the printed page
    lines: int = 1
    meta: dict = field(default_factory=dict)


def is_bullet(e: "Element") -> bool:
    """A bullet point: typed ("• ...") or drawn as a small shape before the text."""
    return e.kind == "text" and (bool(e.meta.get("bullet")) or bool(BULLET.match(e.text)))


BULLET = re.compile(r"^\s*(?:[•▪●◦‣■□➢➤►▶✓✔]|[-–](?=\s))\s*")


def _hex(c: int) -> str:
    return f"#{c:06x}"


def _inside(a, b, pad: float = 1.5) -> bool:
    return a[0] >= b[0] - pad and a[1] >= b[1] - pad and a[2] <= b[2] + pad and a[3] <= b[3] + pad


def _overlap(a, b) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0.0, w) * max(0.0, h)


def page_elements(page: pymupdf.Page, schema: dict, n: int, chart_boxes: list | None = None) -> list[Element]:
    """Elements of PDF page `n` in reading order (top to bottom, then left to right)."""
    figures = schema["figures"]
    W, H = page.rect.width, page.rect.height
    # pictures: raster images, plus vector graphics the extractor recognised as charts
    boxes = []
    for info in page.get_image_info():
        x0, y0, x1, y1 = info["bbox"]
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
        if (x1 - x0) * (y1 - y0) >= MIN_PICTURE_SHARE * W * H:
            boxes.append([x0, y0, x1, y1])
    raw = page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP)
    text_blocks = _split_columns([b for b in raw["blocks"] if b.get("type") == 0])
    for bb in chart_boxes or []:
        # a "chart" region that holds several text blocks is a panel, not a picture
        inner = sum(1 for t in text_blocks if _inside(t["bbox"], bb))
        if inner <= 2:
            boxes.append(list(bb))
    pictures = _merge(boxes)
    pieces = pages_mod.page_pieces(page, schema, n)
    marks = _bullet_marks(page)
    out: list[Element] = []
    for blk in text_blocks:
        bx = tuple(blk["bbox"])
        first = blk["lines"][0]["bbox"] if blk.get("lines") else bx
        bullet = _has_mark(bx, (first[1], first[3]), marks)
        if not bullet and any(_inside(bx, p) for p in pictures):
            continue                                   # a label on a picture stays in the picture
        spans = [s for ln in blk["lines"] for s in ln["spans"] if s["text"].strip()]
        if not spans:
            continue
        text = " ".join(" ".join(s["text"] for s in ln["spans"]).strip() for ln in blk["lines"]).strip()
        weight = {}
        for s in spans:
            weight[(round(s["size"], 1), bool(s["flags"] & 16) or "bold" in s["font"].lower(), s["color"])] = \
                weight.get((round(s["size"], 1), bool(s["flags"] & 16) or "bold" in s["font"].lower(), s["color"]), 0) + len(s["text"])
        (size, bold, color), _ = max(weight.items(), key=lambda kv: kv[1])
        mine = [p for p in pieces if bx[0] - 1 <= (p.x0 + p.x1) / 2 <= bx[2] + 1 and bx[1] - 1 <= p.baseline - p.size * 0.3 <= bx[3] + 1]
        html_ = _join(mine, figures)
        # every number printed in the block must have reached the HTML as a figure
        shown_digits = sum(len(_DIGIT.findall(figures[p.fid]["raw"])) for p in mine if p.fid)
        safe = len(_DIGIT.findall(text)) <= shown_digits and bool(mine)
        out.append(Element(f"t{len(out) + 1}", "text", bx, text=text, html=html_, size=size, bold=bold,
                           color=_hex(color), safe=safe, lines=len(blk["lines"]), meta={"bullet": True} if bullet else {}))
    n_text = len(out)
    for k, p in enumerate(pictures, start=1):
        out.append(Element(f"p{k}", "picture", _trim(p, [e.bbox for e in out[:n_text]])))
    out.sort(key=lambda e: (round(e.bbox[1] / 6), e.bbox[0]))
    assert len([e for e in out if e.kind == "text"]) == n_text
    return out


def _trim(pic: tuple, texts: list[tuple]) -> tuple:
    """A picture's crop never shows words that are also set as text: an edge that runs
    into a text block is pulled back to clear it — the
    side that keeps more of the picture. A trim that would leave less than a third of the
    picture is not made."""
    x0, y0, x1, y1 = pic
    for t in texts:
        if _overlap(t, (x0, y0, x1, y1)) <= 0:
            continue
        options = [(x0, y0, x1, min(y1, t[1] - 1)) if (t[1] + t[3]) / 2 > (y0 + y1) / 2 else (x0, max(y0, t[3] + 1), x1, y1),
                   (x0, y0, min(x1, t[0] - 1), y1) if (t[0] + t[2]) / 2 > (x0 + x1) / 2 else (max(x0, t[2] + 1), y0, x1, y1)]
        best = max(options, key=lambda o: max(0.0, o[2] - o[0]) * max(0.0, o[3] - o[1]))
        if (best[2] - best[0]) * (best[3] - best[1]) >= (pic[2] - pic[0]) * (pic[3] - pic[1]) / 3 and best[2] > best[0] and best[3] > best[1]:
            x0, y0, x1, y1 = best
    return (x0, y0, x1, y1)


def _bullet_marks(page: pymupdf.Page) -> list[tuple]:
    """Bullet points a design tool drew as small filled shapes rather than typed."""
    marks = []
    try:
        for d in page.get_drawings():
            r = d.get("rect")
            if r is not None and d.get("fill") is not None and 1.5 <= r.width <= 7 and 1.5 <= r.height <= 7 \
                    and abs(r.width - r.height) <= 1.5:
                marks.append((r.x0, r.y0, r.x1, r.y1))
    except Exception:  # noqa: BLE001 - no bullets found is a safe answer
        pass
    return marks


def _has_mark(bx: tuple, first_line: tuple, marks: list[tuple]) -> bool:
    ly0, ly1 = first_line
    return any(bx[0] - 16 <= m[2] <= bx[0] + 1 and ly0 - 2 <= (m[1] + m[3]) / 2 <= ly1 + 2 for m in marks)


def _split_columns(blocks: list[dict]) -> list[dict]:
    """A one-line block whose words sit far apart ("MISSION    VISION    VALUES") is really
    several headings set in columns: one block per word group. Works on character
    positions (rawdict), since such a line is often a single span padded with spaces."""
    out = []
    blocks = [nb for b in blocks for nb in _side_by_side(b)]
    for b in blocks:
        if len(b["lines"]) != 1:
            out.append(_with_text(b))
            continue
        line = b["lines"][0]
        chars = [(c, sp) for sp in line["spans"] for c in sp["chars"]]
        groups: list[list[tuple[dict, dict]]] = []
        last = None
        for c, sp in chars:
            if c["c"].isspace():
                continue
            if groups and last is not None and c["bbox"][0] - last["bbox"][2] <= COLUMN_GAP_EM * sp["size"]:
                groups[-1].append((c, sp))
            else:
                groups.append([(c, sp)])
            last = c
        if len(groups) <= 1:
            out.append(_with_text(b))
            continue
        for g in groups:
            bbox = (min(c["bbox"][0] for c, _ in g), min(c["bbox"][1] for c, _ in g),
                    max(c["bbox"][2] for c, _ in g), max(c["bbox"][3] for c, _ in g))
            # rebuild the group's text with its own spaces (gaps wider than a third of a size)
            text, prev = "", None
            for c, sp in g:
                if prev is not None and c["bbox"][0] - prev["bbox"][2] > 0.25 * sp["size"]:
                    text += " "
                text += c["c"]
                prev = c
            sp0 = g[0][1]
            span = {**sp0, "text": text, "bbox": bbox}
            out.append({**b, "bbox": bbox, "lines": [{**line, "spans": [span], "bbox": bbox}]})
    return out


def _side_by_side(b: dict) -> list[dict]:
    """Lines of one block set side by side ("MISSION" | "VISION" | "VALUES") rather than
    stacked: each column of lines becomes its own block."""
    lines = b["lines"]
    if len(lines) < 2:
        return [b]
    cols: list[list[dict]] = []
    for ln in sorted(lines, key=lambda ln: ln["bbox"][0]):
        size = max((sp["size"] for sp in ln["spans"]), default=10)
        for col in cols:
            cx0, cx1 = min(l["bbox"][0] for l in col), max(l["bbox"][2] for l in col)
            if ln["bbox"][0] <= cx1 + COLUMN_GAP_EM * size and ln["bbox"][2] >= cx0 - COLUMN_GAP_EM * size:
                col.append(ln)
                break
        else:
            cols.append([ln])
    if len(cols) == 1:
        return [b]
    out = []
    for col in cols:
        col.sort(key=lambda ln: ln["bbox"][1])
        bbox = (min(l["bbox"][0] for l in col), min(l["bbox"][1] for l in col),
                max(l["bbox"][2] for l in col), max(l["bbox"][3] for l in col))
        out.append({**b, "bbox": bbox, "lines": col})
    return out


def _with_text(b: dict) -> dict:
    """rawdict spans carry chars, not text: give each span its text."""
    return {**b, "lines": [{**ln, "spans": [{**sp, "text": sp.get("text") or "".join(c["c"] for c in sp.get("chars", []))}
                                             for sp in ln["spans"]]} for ln in b["lines"]]}


def _merge(boxes: list[list[float]]) -> list[tuple[float, float, float, float]]:
    """Overlapping picture boxes become one picture."""
    boxes = [list(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
                if small and _overlap(a, b) >= 0.3 * small:
                    boxes[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    boxes.pop(j)
                    changed = True
                    break
            if changed:
                break
    return [tuple(b) for b in boxes]


def _join(pieces: list, figures: dict) -> str:
    """Pieces as inline HTML in reading order: lines joined with spaces."""
    rows: list[list] = []
    for p in sorted(pieces, key=lambda p: (round(p.baseline), p.x0)):
        if rows and abs(rows[-1][0].baseline - p.baseline) < p.size * 0.5:
            rows[-1].append(p)
        else:
            rows.append([p])
    parts = []
    for row in rows:
        row.sort(key=lambda p: p.x0)
        line = ""
        for k, p in enumerate(row):
            gap = p.x0 - row[k - 1].x1 if k else 0
            line += (" " if k and gap > p.size * 0.08 else "") + pages_mod._inline(p, figures)
        parts.append(line)
    return " ".join(parts)
