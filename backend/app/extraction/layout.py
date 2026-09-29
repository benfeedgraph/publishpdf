"""Page layout analysis on word boxes: headers/footers, lines, tables, headings,
paragraphs and charts. Geometry-only, so text-layer and OCR pages go through the
same logic."""

from __future__ import annotations

import difflib
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field

from app.extraction.dates import parse_date, parse_period
from app.extraction.numbers import try_parse_number
from app.extraction.pdf import PageData, Word

BAND = 0.075                   # top/bottom fraction of the page treated as header/footer band
_PAGE_NO = re.compile(r"^(page\s*)?\d{1,4}(\s*(of|/)\s*\d{1,4})?$", re.I)


# ------------------------------------------------------------------ header / footer


def _sig(text: str) -> str:
    """Signature for repeated header/footer lines; letters only so OCR digit/symbol
    misreads ('FY26' vs 'F¥26') still match."""
    return re.sub(r"[^a-z]", "", text.lower())


def _band_lines(p: PageData) -> list[tuple[str, list[Word]]]:
    band = [w for w in p.words if w.bbox[3] < p.height * BAND or w.bbox[1] > p.height * (1 - BAND)]
    return [(" ".join(w.text for w in ln), ln) for ln in group_lines(band)]


def header_footer_words(pages: list[PageData]) -> dict[int, set[int]]:
    """{page_no: {id(word)...}} of repeated headers/footers and page numbers to exclude."""
    counts: Counter[str] = Counter()
    per_page = {}
    for p in pages:
        lines = _band_lines(p)
        per_page[p.number] = lines
        for sig in {_sig(t) for t, _ in lines}:
            counts[sig] += 1
    threshold = max(2, len(pages) // 2)
    sigs = list(counts)

    def repeated(sig: str) -> bool:
        if len(sig) < 4:
            return False
        n = sum(c for other, c in counts.items() if other == sig or
                (abs(len(other) - len(sig)) <= 3 and difflib.SequenceMatcher(None, sig, other).ratio() >= 0.88))
        return n >= threshold

    del sigs
    out: dict[int, set[int]] = {}
    for p in pages:
        ex: set[int] = set()
        for text, ln in per_page[p.number]:
            if (len(pages) >= 2 and repeated(_sig(text))) or _PAGE_NO.match(text.strip()):
                ex.update(id(w) for w in ln)
        out[p.number] = ex
    return out


# ------------------------------------------------------------------ lines & segments


def group_lines(words: list[Word]) -> list[list[Word]]:
    """Cluster words into visual lines by vertical centre."""
    lines: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w.yc, w.bbox[0])):
        for ln in reversed(lines[-6:]):
            ref = statistics.median(x.yc for x in ln)
            tol = 0.45 * min(w.h, statistics.median(x.h for x in ln))
            if abs(w.yc - ref) <= tol:
                ln.append(w)
                break
        else:
            lines.append([w])
    for ln in lines:
        ln.sort(key=lambda w: w.bbox[0])
    lines.sort(key=lambda ln: statistics.median(w.yc for w in ln))
    return lines


@dataclass
class Segment:
    words: list[Word]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def x0(self) -> float:
        return self.words[0].bbox[0]

    @property
    def x1(self) -> float:
        return self.words[-1].bbox[2]

    @property
    def y0(self) -> float:
        return min(w.bbox[1] for w in self.words)

    @property
    def y1(self) -> float:
        return max(w.bbox[3] for w in self.words)

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)

    @property
    def is_numeric(self) -> bool:
        return is_numeric_cell(self.text)


_NUMERIC_LIKE = re.compile(r"^[(\-−–]?[₹$€£]?\d[\d,.\-–]*\)?\s?%?[*#^]?$")


def is_numeric_cell(text: str) -> bool:
    """Structural test: does this look like a table value? True also for OCR misreads
    such as '55-00' so a table isn't split; those still become figures that fail
    parsing and are flagged for mandatory review."""
    t = text.strip()
    if try_parse_number(t) is not None:
        return True
    t2 = t.rstrip("*#^")                    # footnote markers
    if t2 != t and try_parse_number(t2) is not None:
        return True
    return bool(_NUMERIC_LIKE.match(t)) and sum(ch.isdigit() for ch in t) >= 2


def split_segments(line: list[Word], gap: float) -> list[Segment]:
    segs: list[list[Word]] = [[line[0]]]
    for w in line[1:]:
        prev = segs[-1][-1]
        g = w.bbox[0] - prev.bbox[2]
        # Numbers never share a segment with a neighbour across a column-sized gap,
        # and two numeric tokens side by side are always separate cells.
        if g > gap or (g > 4 and is_numeric_cell(prev.text) and is_numeric_cell(w.text)):
            segs.append([w])
        else:
            segs[-1].append(w)
    return [Segment(s) for s in segs]


@dataclass
class Row:
    segments: list[Segment]

    @property
    def y0(self) -> float:
        return min(s.y0 for s in self.segments)

    @property
    def y1(self) -> float:
        return max(s.y1 for s in self.segments)

    @property
    def numeric(self) -> list[Segment]:
        return [s for s in self.segments if s.is_numeric]

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments)


# ------------------------------------------------------------------ elements


@dataclass
class TableCell:
    words: list[Word]
    colspan: int = 1


@dataclass
class TableEl:
    bbox: tuple[float, float, float, float]
    anchors: list[float]                     # right edges of numeric columns
    header_rows: list[list[TableCell]]       # one list per header row, spans across numeric columns
    header_label_words: list[list[Word]]     # per column: all header words stacked (for col labels)
    rows: list[tuple[list[Word], list[list[Word] | None]]]   # (label words, cells per column)
    caption_words: list[Word] = field(default_factory=list)
    kind: str = "table"


@dataclass
class TextEl:
    kind: str                                # 'heading' | 'paragraph'
    lines: list[list[Word]]
    size: float

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        ws = [w for ln in self.lines for w in ln]
        return (min(w.bbox[0] for w in ws), min(w.bbox[1] for w in ws),
                max(w.bbox[2] for w in ws), max(w.bbox[3] for w in ws))

    @property
    def words(self) -> list[Word]:
        return [w for ln in self.lines for w in ln]


@dataclass
class StatEl:
    """A KPI tile: a short label directly above its value (common on slides)."""
    label: list[Word]
    value: list[Word]
    kind: str = "stat"

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        ws = self.label + self.value
        return (min(w.bbox[0] for w in ws), min(w.bbox[1] for w in ws),
                max(w.bbox[2] for w in ws), max(w.bbox[3] for w in ws))


@dataclass
class ChartEl:
    bbox: tuple[float, float, float, float]
    extracted: bool
    labels: list[Word]                       # numeric data labels inside the chart
    axis: list[Word]                         # non-numeric/period labels inside the chart
    kind: str = "chart"


Element = TableEl | TextEl | ChartEl | StatEl


def _inside(w: Word, r: tuple[float, float, float, float]) -> bool:
    return r[0] <= w.xc <= r[2] and r[1] <= w.yc <= r[3]


def _real_chart_regions(regions: list, words: list[Word]) -> list:
    """Coloured shapes also shade table headers and banded rows. A vector region is table
    shading, not a chart, when a table found on the whole page runs through it and most of
    that table's rows lie outside it (a chart's own data labels stay inside its region)."""
    if not regions or not words:
        return regions
    med_h = statistics.median(w.h for w in words)
    rows = [Row(split_segments(ln, max(14.0, 2.2 * med_h))) for ln in group_lines(words)]
    tables = [idxs for _t, idxs in _find_tables(rows, med_h) if len(idxs) >= 4]
    keep = []
    for r in regions:
        shading = False
        for idxs in tables:
            inside = sum(1 for i in idxs if rows[i].y0 >= r[1] - 2 and rows[i].y1 <= r[3] + 2)
            if 0 < inside <= len(idxs) / 2:
                shading = True
                break
        if not shading:
            keep.append(r)
    return keep


def analyse_page(p: PageData, exclude: set[int], body_size: float) -> list[Element]:
    words = [w for w in p.words if id(w) not in exclude]
    elements: list[Element] = []

    # Charts first: words inside chart/image regions belong to the chart.
    for region, is_image in [(r, False) for r in _real_chart_regions(p.chart_regions, words)] + \
            [(r, True) for r in p.image_regions]:
        inside = [w for w in words if _inside(w, region)]
        labels = [w for w in inside if is_numeric_cell(w.text.rstrip("%")) or is_numeric_cell(w.text)]
        axis = [w for w in inside if w not in labels]
        elements.append(ChartEl(region, extracted=bool(labels), labels=labels, axis=axis))
        words = [w for w in words if not _inside(w, region)]
    if not words:
        return _order(elements)

    heights = [w.h for w in words]
    med_h = statistics.median(heights)
    gap = max(14.0, 2.2 * med_h)
    lines = group_lines(words)
    rows = [Row(split_segments(ln, gap)) for ln in lines]

    used = [False] * len(rows)
    for tbl, idxs in _find_tables(rows, med_h):
        elements.append(tbl)
        for i in idxs:
            used[i] = True

    # KPI tiles: a short text label directly above a short numeric value.
    consumed: set[int] = set()
    _UNITISH = {"cr", "cr.", "crore", "lakh", "mn", "bn", "million", "billion", "%", "x", "bps"}

    def valueish(seg: Segment) -> bool:
        ws = [w.text.lower().strip(",.") for w in seg.words]
        if any(_URLISH.search(w) for w in ws):          # a wrapped link, not a KPI value
            return False
        return 1 <= len(ws) <= 3 and any(any(ch.isdigit() for ch in w) for w in ws) and \
            all(any(ch.isdigit() for ch in w) or w in _UNITISH for w in ws)

    def labelish(seg: Segment) -> bool:
        t = seg.text
        return 1 <= len(seg.words) <= 6 and not any(ch.isdigit() for ch in t) and sum(ch.isalpha() for ch in t) >= 3 \
            and not _URLISH.search(t)

    for i, row in enumerate(rows):
        if used[i]:
            continue
        for vseg in row.segments:
            if id(vseg) in consumed or not valueish(vseg):
                continue
            for j in range(i - 1, max(-1, i - 3), -1):
                if used[j]:
                    continue
                cand = next((ls for ls in rows[j].segments if id(ls) not in consumed and labelish(ls)
                             and abs(ls.x0 - vseg.x0) < 25 and 0 <= vseg.y0 - ls.y1 < 2.2 * max(w.h for w in vseg.words)), None)
                if cand is not None:
                    elements.append(StatEl(cand.words, vseg.words))
                    consumed.update({id(cand), id(vseg)})
                    break
    rows = [Row([sg for sg in r.segments if id(sg) not in consumed]) for r in rows]
    used = [u or not r.segments for u, r in zip(used, rows)]

    # A bold numbered question ("Q5.", "Q14. (a)") is one heading even when the marker
    # and the question text sit far enough apart to split into two segments.
    for r in rows:
        if len(r.segments) > 1 and _QMARK.match(r.segments[0].text) and all(w.bold for sg in r.segments for w in sg.words):
            r.segments[:] = [Segment([w for sg in r.segments for w in sg.words])]

    # Remaining rows -> headings and paragraphs, per column.
    blocks: list[TextEl] = []
    open_blocks: list[TextEl] = []
    for i, row in enumerate(rows):
        if used[i]:
            open_blocks = []
            continue
        for seg in row.segments:
            size = statistics.median(w.size or w.h for w in seg.words)
            is_heading = (_is_heading(seg, size, body_size, med_h))
            target = None
            if all(w.bold for w in seg.words) and not _QMARK.match(seg.text):
                # A question that wraps: bold continuation lines join the heading above.
                for b in open_blocks:
                    if b.kind == "heading" and _QMARK.match(b.lines[0][0].text) \
                            and not " ".join(w.text for w in b.lines[-1]).rstrip().endswith("?") \
                            and seg.y0 - max(w.bbox[3] for w in b.lines[-1]) < 1.2 * med_h \
                            and abs(size - b.size) < 1.5:
                        target = b
                        break
            if target is None and not is_heading:
                for b in open_blocks:
                    if b.kind != "paragraph":
                        continue
                    last = b.lines[-1]
                    if abs(last[0].bbox[0] - seg.x0) < 18 and seg.y0 - max(w.bbox[3] for w in last) < 1.2 * med_h \
                            and abs(size - b.size) < 1.5:
                        target = b
                        break
            if target:
                target.lines.append(seg.words)
            else:
                b = TextEl("heading" if is_heading else "paragraph", [seg.words], size)
                blocks.append(b)
                open_blocks = [x for x in open_blocks if abs(x.lines[-1][0].bbox[0] - seg.x0) >= 18] + [b]
    elements.extend(blocks)
    return _order(elements)


_URLISH = re.compile(r"https?:|www\.|/|\.(pdf|html?|aspx)\b", re.I)
_QMARK = re.compile(r"^(Q\s?\d{1,3}[.:)]|\([a-z]\)(\s|$))")


def _is_heading(seg: Segment, size: float, body_size: float, med_h: float) -> bool:
    text = seg.text.strip()
    bold = all(w.bold for w in seg.words)
    # FAQ documents: a bold numbered question is a heading at body size, whatever its length.
    if bold and _QMARK.match(text) and len(text) <= 320:
        return True
    if len(text) > 90 or len(seg.words) > 12 or text.endswith((".", ",", ";")):
        return False
    alpha_words = [w for w in seg.words if sum(ch.isalpha() for ch in w.text) >= 2]
    if len(alpha_words) < max(1, len(seg.words) / 2) or (len(seg.words) <= 2 and any(ch.isdigit() for ch in text) and len(alpha_words) < 2):
        return False
    if body_size and size >= body_size * 1.25:
        return True
    return all(w.bold for w in seg.words) and len(seg.words) <= 8 and size >= body_size * 1.1


def _order(elements: list[Element]) -> list[Element]:
    """Reading order: top to bottom; blocks starting at the same height read left to right.
    Multi-column text is emitted column by column because each column is its own block."""
    return sorted(elements, key=lambda e: (round(e.bbox[1] / 6), e.bbox[0]))


# ------------------------------------------------------------------ tables


def _find_tables(rows: list[Row], med_h: float) -> list[tuple[TableEl, list[int]]]:
    out = []
    i = 0
    n = len(rows)
    while i < n:
        if len(rows[i].numeric) == 0 or not _row_is_tabular(rows[i]):
            i += 1
            continue
        j = i
        members = [i]
        while j + 1 < n:
            nxt = rows[j + 1]
            vgap = nxt.y0 - rows[j].y1
            if vgap > 2.8 * med_h:
                break
            if _row_is_tabular(nxt) and nxt.numeric:
                members.append(j + 1)
                j += 1
                continue
            # label-only group row ("Expenses") between numeric rows
            if len(nxt.segments) == 1 and not nxt.numeric and j + 2 < n and _row_is_tabular(rows[j + 2]) \
                    and rows[j + 2].numeric and rows[j + 2].y0 - nxt.y1 < 2.8 * med_h:
                members.append(j + 1)
                j += 1
                continue
            break
        # Rows of bare years/periods at the top ("2025  2024") are header rows, not data.
        while members and _looks_like_header(rows[members[0]]):
            members.pop(0)
        numeric_rows = [k for k in members if rows[k].numeric]
        if len(numeric_rows) >= 2:
            anchors = _anchors([s for k in numeric_rows for s in rows[k].numeric])
            if len(anchors) >= 1:
                tbl, extra = _build_table(rows, members, anchors, med_h)
                out.append((tbl, members + extra))
        i = j + 1
    return out


def _row_is_tabular(row: Row) -> bool:
    """A tabular row: numeric cells are single tokens and any text is on the left."""
    segs = row.segments
    if not row.numeric:
        return False
    first_num = next(k for k, s in enumerate(segs) if s.is_numeric)
    tail = segs[first_num:]
    return all(s.is_numeric or len(s.words) <= 3 for s in tail) and first_num <= 1


def _anchors(cells: list[Segment]) -> list[float]:
    xs = sorted(s.x1 for s in cells)
    clusters: list[list[float]] = []
    for x in xs:
        if clusters and x - clusters[-1][-1] <= 7:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    keep = [c for c in clusters if len(c) >= 2 or len(cells) < 6]
    return [statistics.median(c) for c in keep]


def _col_of(seg: Segment, anchors: list[float], width: float) -> int | None:
    best, dist = None, 1e9
    for k, a in enumerate(anchors):
        if a - width - 4 <= seg.x1 <= a + 8:
            d = abs(seg.x1 - a)
            if d < dist:
                best, dist = k, d
    return best


def _build_table(rows: list[Row], members: list[int], anchors: list[float], med_h: float):
    widths = [anchors[k] - anchors[k - 1] for k in range(1, len(anchors))]
    colw = min(widths) if widths else 60.0
    body: list[tuple[list[Word], list[list[Word] | None]]] = []
    x_min = min(rows[k].segments[0].x0 for k in members)
    for k in members:
        row = rows[k]
        cells: list[list[Word] | None] = [None] * len(anchors)
        label: list[Word] = []
        for seg in row.segments:
            col = _col_of(seg, anchors, colw) if seg.is_numeric else None
            if col is not None and cells[col] is None:
                cells[col] = seg.words
            elif seg.x1 < anchors[0] - colw * 0.6:
                label.extend(seg.words)
            else:
                col = _col_of(seg, anchors, colw)
                if col is not None and cells[col] is None:
                    cells[col] = seg.words
                else:
                    label.extend(seg.words)
        body.append((label, cells))

    # Header rows: up to 3 lines directly above whose segments sit over the numeric columns.
    extra: list[int] = []
    header_rows: list[list[TableCell]] = []
    col_words: list[list[Word]] = [[] for _ in anchors]
    top = members[0]
    k = top - 1
    left_edge = anchors[0] - colw
    while k >= 0 and len(header_rows) < 3:
        row = rows[k]
        if rows[k + 1].y0 - row.y1 > 2.2 * med_h:
            break
        # Header text sits over the numeric columns; a title/caption starting at the
        # label column's left edge is not a header.
        segs = [s for s in row.segments if s.x0 >= left_edge - 12]
        if not segs or len(segs) < len(row.segments) and any(s.x0 < left_edge - 12 and s.x1 > left_edge for s in row.segments):
            break
        if row.numeric and not _looks_like_header(row):
            break
        hr: list[TableCell] = []
        for s in segs:
            seg_w = s.x1 - s.x0 + 1
            span_cols = [c for c, a in enumerate(anchors)
                         if _overlap((s.x0, s.x1), (a - colw, a + 4)) > (0.2 * seg_w if seg_w < colw else 0.35 * colw)]
            if not span_cols:
                c = _col_of(s, anchors, colw)
                span_cols = [c] if c is not None else []
            hr.append(TableCell(s.words, colspan=max(1, len(span_cols))))
            for c in span_cols:
                col_words[c] = list(s.words) + col_words[c]
        header_rows.insert(0, hr)
        extra.append(k)
        k -= 1
    # Caption (unit line) just above the header, left-aligned.
    caption: list[Word] = []
    if k >= 0 and (rows[k + 1].y0 - rows[k].y1) < 3 * med_h:
        cap_row = rows[k]
        if len(cap_row.segments) <= 2 and not cap_row.numeric:
            from app.extraction.tokens import detect_unit
            if detect_unit(cap_row.text)[0]:
                caption = [w for s in cap_row.segments for w in s.words]
                extra.append(k)
    all_rows = [rows[m] for m in members + extra]
    bbox = (min(r.segments[0].x0 for r in all_rows), min(r.y0 for r in all_rows),
            max(r.segments[-1].x1 for r in all_rows), max(r.y1 for r in all_rows))
    return TableEl(bbox, anchors, header_rows, col_words, body, caption), extra


def _looks_like_header(row: Row) -> bool:
    return all(parse_period(s.text) or parse_date(s.text) or re.fullmatch(r"(19|20)\d\d", s.text.strip())
               for s in row.segments)


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
