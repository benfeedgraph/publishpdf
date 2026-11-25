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
    corner_words: list[Word] = field(default_factory=list)   # the PDF's own header over the row labels
    unplaced: int = 0                        # numbers in the value columns that found no free cell
    stray_rows: list[int] = field(default_factory=list)   # rows whose name took a value (see _build_table)


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
    title: list[Word] = field(default_factory=list)   # the line over all its values (a small chart's name)
    from_table: bool = False                 # a table that failed the checks, kept as it looks in the PDF
    from_image: bool = False                 # read from a picture on the page (not drawn shapes)


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

    # Charts first: words inside chart/image regions belong to the chart. A picture that holds
    # running text is a text box drawn as an image: its text is the page's text, not a label.
    vector = _real_chart_regions(p.chart_regions, words)
    # Overlapping pictures are one picture (a banner set inside a text box): judged together.
    prose = [g for g in merge_overlapping(p.image_regions) if _holds_prose([w for w in words if _inside(w, g)])]
    regions = [(r, False) for r in vector] + \
        [(r, True) for r in p.image_regions if not any(_inside_box(r, g) for g in prose)]
    for region, is_image in regions:
        inside = [w for w in words if _inside(w, region)]
        if is_image and any(_is_label(w) for w in inside):
            # A chart drawn as a picture: its name, the value over its tallest bar and its axis
            # sit just outside the image. Take them in with the margin a vector chart's region
            # gets (pdf._chart_regions), never from another picture (a text box's words stay
            # its own) or chart.
            others = [r for r in vector + p.image_regions if r != region]
            own = region
            region = (max(0.0, region[0] - 10), max(0.0, region[1] - 30),
                      min(p.width, region[2] + 10), min(p.height, region[3] + 30))
            # Only what reads as the chart's own labels comes in from round it: numbers, a row of
            # years or periods, and one short line just over it (its name). Running text beside
            # the picture, and text set at heading size, stays the page's text.
            near = [w for w in words if _inside(w, region) and not _inside(w, own) and not any(_inside(w, o) for o in others)
                    and not (body_size and (w.size or w.h) >= 1.25 * body_size)]
            taken_near = []
            name_taken = False
            for ln in group_lines(near):
                if all(_is_label(w) or _period_word(w.text) for w in ln):
                    taken_near += ln
                elif not name_taken and len(ln) <= 5 and max(w.bbox[3] for w in ln) <= own[1] + 2:
                    taken_near += ln
                    name_taken = True
            inside = [w for w in words if _inside(w, own) and not any(_inside(w, o) for o in others)] + taken_near
        labels = [w for w in inside if _is_label(w)]
        ticks = _axis_ticks(labels)                  # "2020 2025 2030" under the bars name them
        labels = [w for w in labels if w not in ticks]
        axis = [w for w in inside if w not in labels]
        title = _chart_title(axis, labels)
        axis = [w for w in axis if w not in title]
        elements.append(ChartEl(region, extracted=bool(labels), labels=labels, axis=axis, title=title,
                                from_image=is_image))
        taken = {id(w) for w in inside}
        words = [w for w in words if id(w) not in taken]
    if not words:
        return _order(elements)

    heights = [w.h for w in words]
    med_h = statistics.median(heights)
    gap = max(14.0, 2.2 * med_h)
    lines = group_lines(words)
    rows = [Row(split_segments(ln, gap)) for ln in lines]

    used = [False] * len(rows)
    for tbl, idxs in _find_tables(rows, med_h):
        if table_problems(tbl):
            # Its numbers don't line up into rows and columns (a chart's data labels, a
            # diagram): kept as it looks in the PDF, its numbers still checked one by one.
            tw = [w for i in idxs for sg in rows[i].segments for w in sg.words]
            labels = [w for w in tw if _is_label(w)]
            elements.append(ChartEl(tbl.bbox, extracted=bool(labels), labels=labels,
                                    axis=[w for w in tw if w not in labels], from_table=True))
        else:
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

    # A small figure printed right before a line set larger ("100.00%" in 8 pt beside the 12 pt
    # "Meeting Details") belongs to the text next to it, not to that line: it is its own piece.
    for r in rows:
        k = 0
        while k < len(r.segments):
            sg = r.segments[k]
            rest_sizes = [w.size or w.h for w in sg.words if not is_numeric_cell(w.text)]
            lead = []
            for w in sg.words:
                if rest_sizes and is_numeric_cell(w.text) and (w.size or w.h) < 0.8 * statistics.median(rest_sizes):
                    lead.append(w)
                else:
                    break
            if lead and len(lead) < len(sg.words) and _letters(" ".join(w.text for w in sg.words[len(lead):])):
                r.segments[k:k + 1] = [Segment(lead), Segment(sg.words[len(lead):])]
                k += 1
            k += 1

    # A line split in two by a wide gap ("Q2 FY26 Highlights (UltraTech existing assets –"
    # | "166.76 Mtpa)") is one line when its first piece is unfinished and both are set alike.
    for r in rows:
        k = 0
        while k + 1 < len(r.segments):
            a, b = r.segments[k], r.segments[k + 1]
            sa = statistics.median(w.size or w.h for w in a.words)
            sb = statistics.median(w.size or w.h for w in b.words)
            if unfinished(a.text) and abs(sa - sb) <= 1.0 and b.x0 - a.x1 <= max(sa, sb):
                r.segments[k:k + 2] = [Segment(a.words + b.words)]
            else:
                k += 1

    # Remaining rows -> headings and paragraphs, per column.
    blocks: list[TextEl] = []
    open_blocks: list[TextEl] = []
    for i, row in enumerate(rows):
        if used[i]:
            open_blocks = []
            continue
        for seg in row.segments:
            size = statistics.median(w.size or w.h for w in seg.words)
            # a line that starts in lower case carries on the text above it: never a heading
            is_heading = _is_heading(seg, size, body_size, med_h) and not _starts_lower(seg.text)
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
            if target is None and (is_heading or _starts_lower(seg.text)):
                # A heading set on two lines ("Staples, Convenience Foods and" / "Health &
                # Hygiene Products", "… build and re-" / "invent …") is one heading, never two.
                target = next((b for b in open_blocks if b.kind == "heading" and _heading_continues(b, seg, size)), None)
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
    _demote_labels(blocks, elements)
    elements.extend(blocks)
    return _order(elements)


# A list item, never a heading, and never the second line of one: a bullet mark ("❖Housing",
# "• Revenue"), or a dash followed by a space ("– Operating leverage"; "-5%" is a number). Not an em
# dash: some reports set it before their titles ("— Auf einen Blick").
_BULLET = re.compile(r"^(?:[•▪◦●‣·■□❖➢►✓]|[–-]\s)")


def _reads_as_text(b: "TextEl") -> bool:
    """A multi-line 'heading' that is really running text: longer than any heading, or with a
    sentence ending inside it. (Used to tell a page's title from a passage set large.)"""
    text = " ".join(w.text for w in b.words).strip()
    return len(b.words) > 18 or len(text) > 140 or text.endswith((".", ";")) or bool(re.search(r"[.!?;]\s+\S", text))


_URLISH = re.compile(r"https?:|www\.|/|\.(pdf|html?|aspx)\b", re.I)
_QMARK = re.compile(r"^(Q\s?\d{1,3}[.:)]|\([a-z]\)(\s|$))")


def _is_heading(seg: Segment, size: float, body_size: float, med_h: float) -> bool:
    text = seg.text.strip()
    bold = all(w.bold for w in seg.words)
    # FAQ documents: a bold numbered question is a heading at body size, whatever its length.
    if bold and _QMARK.match(text) and len(text) <= 320:
        return True
    if _BULLET.match(text):                    # "• Revenue grew 12%", "– Operating leverage of 5.6%"
        return False
    if len(text) > 90 or len(seg.words) > 12 or text.endswith((".", ",", ";")):
        return False
    alpha_words = [w for w in seg.words if sum(ch.isalpha() for ch in w.text) >= 2]
    if len(alpha_words) < max(1, len(seg.words) / 2) or (len(seg.words) <= 2 and any(ch.isdigit() for ch in text) and len(alpha_words) < 2):
        return False
    if body_size and size >= body_size * 1.25:
        return True
    return all(w.bold for w in seg.words) and len(seg.words) <= 8 and size >= body_size * 1.1


# A heading that stops on one of these is cut off mid-phrase (it continues on the next line).
# The same words the web page's chapter list already uses to drop such titles (render/site.py).
DANGLING = re.compile(r"(\b(on|of|and|the|to|for|in|with|by|at|from|a|an)|&)\s*$", re.I)
_UNFINISHED = re.compile(r"[,、，/+\-–—]\s*$")
_LIST_MARK = re.compile(r"^[•▪●◦·–\-]\s")   # "• CET1 ratio of 11.6%", "– Net charge-off ratio": list items


def unfinished(text: str) -> bool:
    """A line that doesn't end as a title does: on a connector ("Connect &"), a comma or a dash
    ("Financial performance—"), or with a bracket still open ("Highlights (UltraTech assets")."""
    t = text.strip()
    return bool(DANGLING.search(t) or _UNFINISHED.search(t)) or t.count("(") > t.count(")")


def _starts_lower(text: str) -> bool:
    first = text.split()[0] if text.split() else ""
    return first.isalpha() and first.islower()


def not_a_title(text: str) -> bool:
    """Text that can't be a heading whatever its type: unfinished, the second half of a line
    (it starts in lower case, "customers to its franchise…"), a list item, or no words at all."""
    t = text.strip()
    return unfinished(t) or _starts_lower(t) or bool(_LIST_MARK.match(t)) or not _letters(t)


LABEL_MAX_WORDS = 6        # a heading this short, among several with nothing under them, is a label
HEADING_MAX_WORDS, HEADING_MAX_CHARS = 16, 120   # longer than this, a heading joined from lines is text


def _text(el: TextEl) -> str:
    return " ".join(w.text for w in el.words)


def _letters(text: str) -> bool:
    return any(sum(ch.isalpha() for ch in t) >= 2 for t in text.split())


def _is_label(w: Word) -> bool:
    """A number printed on a chart (a data label)."""
    return is_numeric_cell(w.text.rstrip("%")) or is_numeric_cell(w.text)


def reads_as_prose(lines: list[list[str]]) -> bool:
    """Words set on a picture that read as running text — phrases rather than one- or two-word
    labels (on average 2.5 words a line, or ten words or more), hardly any numbers — rather than
    a chart's or diagram's short labels: the picture is a text box drawn as an image, and its
    text is the page's text. Used by the extractor and by the web page builder
    (render/elements.py), so both treat the picture the same way."""
    toks = [t for ln in lines for t in ln]
    alpha = [[t for t in ln if sum(ch.isalpha() for ch in t) >= 2] for ln in lines]
    n_alpha = sum(len(a) for a in alpha)
    if n_alpha < 6 or sum(1 for t in toks if is_numeric_cell(t.rstrip("%")) or is_numeric_cell(t)) > 0.25 * len(toks):
        return False
    return n_alpha >= 10 or n_alpha / max(1, sum(1 for a in alpha if a)) >= 2.5


def merge_overlapping(boxes) -> list[tuple[float, float, float, float]]:
    """Boxes that overlap by at least 30% of the smaller one, as one box (the rule the web page
    builder uses for pictures, render/elements._merge)."""
    out = [list(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        for i in range(len(out)):
            for j in range(i + 1, len(out)):
                a, b = out[i], out[j]
                small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
                ov = max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
                if small and ov >= 0.3 * small:
                    out[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    out.pop(j)
                    changed = True
                    break
            if changed:
                break
    return [tuple(b) for b in out]


def _inside_box(a, b, pad: float = 1.0) -> bool:
    return a[0] >= b[0] - pad and a[1] >= b[1] - pad and a[2] <= b[2] + pad and a[3] <= b[3] + pad


def _holds_prose(words: list[Word]) -> bool:
    return bool(words) and reads_as_prose([[w.text for w in ln] for ln in group_lines(words)])


def _period_word(t: str) -> bool:
    t = t.strip("()*")
    return bool(re.fullmatch(r"(19|20)\d\d[A-Z]?", t) or parse_period(t) or parse_date(t))


def _axis_ticks(labels: list[Word]) -> list[Word]:
    """A chart's axis: a line of two or more years/periods ("2020 2025 2030") under its values."""
    out: list[Word] = []
    for ln in group_lines(labels):
        if len(ln) >= 2 and all(_period_word(w.text) for w in ln):
            out.extend(ln)
    return out if len(out) < len(labels) else []


def _chart_title(axis: list[Word], labels: list[Word]) -> list[Word]:
    """The line of words over all of a chart's values: the name of a small chart ("Snacks")."""
    if not labels:
        return []
    top = min(w.bbox[1] for w in labels)
    above = [w for w in axis if w.yc < top and not any(ch.isdigit() for ch in w.text)]
    lines = group_lines(above)
    return lines[0] if lines and _letters(" ".join(w.text for w in lines[0])) else []


def _heading_continues(b: TextEl, seg: Segment, size: float) -> bool:
    """`seg` is the next line of heading `b`: set the same way, directly under it, in line with it,
    and `b` is unfinished — it stops on a connector or a comma, `seg` carries on in lower case, or
    `b` is a one- or two-word line of a title stacked over several identical lines. (Two finished
    lines, a title over its subtitle, stay two.)"""
    last = b.lines[-1]
    so_far = " ".join(w.text for w in last)
    lh = max(size, b.size, 1.0)
    gap = seg.y0 - max(w.bbox[3] for w in last)
    # a title set large over several lines ("Regulatory" / "Developments", "Quarterly" /
    # "Financial" / "Snapshots"): a one- or two-word line with a line set exactly like it, at
    # the same left edge, tight under it
    stacked = len(last) <= 2 and _letters(so_far) and _letters(seg.text) and abs(size - b.size) <= 0.5 \
        and abs(last[0].bbox[0] - seg.x0) <= 3 and -0.3 * lh <= gap <= 0.3 * lh     # words, never a number ("100.00%")
    if not (stacked or DANGLING.search(so_far) or _UNFINISHED.search(so_far) or seg.text[:1].islower()):
        return False
    if len(b.lines) >= 5 or abs(size - b.size) > 1.5 or all(w.bold for w in last) != all(w.bold for w in seg.words):
        return False
    if not (-0.3 * lh <= gap <= 0.6 * lh):
        return False
    x0, x1 = last[0].bbox[0], last[-1].bbox[2]
    overlap = min(x1, seg.x1) - max(x0, seg.x0)
    return overlap > 0.3 * min(x1 - x0, seg.x1 - seg.x0) or abs(x0 - seg.x0) < 18 \
        or abs((x0 + x1) / 2 - (seg.x0 + seg.x1) / 2) < 18


def _directly_below(h: TextEl, e) -> bool:
    x0, _, x1, y1 = h.bbox
    lh = max(h.size, 1.0)
    ex0, ey0, ex1, _ = e.bbox
    return y1 - 0.25 * lh <= ey0 <= y1 + 2.5 * lh and min(x1, ex1) - max(x0, ex0) > 0


def _introduces(h: TextEl, others: list) -> bool:
    """Something of its own sits directly under heading `h`: text, a table, a tile or a chart."""
    x0, y0, x1, y1 = h.bbox
    lh = max(h.size, 1.0)                    # one line's height (font size, or glyph height on OCR)
    for e in others:
        if e is h or (isinstance(e, TextEl) and e.kind == "heading"):
            continue
        ex0, ey0, ex1, _ = e.bbox
        if y1 - 0.25 * lh <= ey0 <= y1 + 2.5 * lh and min(x1, ex1) - max(x0, ex0) > 0:
            return True
    return False


def _demote_labels(blocks: list[TextEl], others: list) -> None:
    """Headings that aren't section titles become text: one that stops mid-phrase, a short one
    printed twice on the page (column or graphic labels), and a group of three or more short
    ones with nothing of their own under them (the labels of a diagram)."""
    heads = [b for b in blocks if b.kind == "heading" and not _QMARK.match(b.lines[0][0].text)]
    for h in heads:
        t = _text(h)
        # unfinished, half a line, a list item, no words, or (joined from several lines) running text set large
        if not_a_title(t) or len(h.words) > HEADING_MAX_WORDS or len(t) > HEADING_MAX_CHARS:
            h.kind = "paragraph"
    # The page's title: its top-most heading, with no text above it. Never taken for a label.
    texts = [b for b in blocks if b.words]
    first = min(texts, key=lambda b: b.bbox[1], default=None)
    title = first if first is not None and first.kind == "heading" else None
    short = [h for h in heads if h.kind == "heading" and len(h.words) <= LABEL_MAX_WORDS]
    copies: dict[str, list[TextEl]] = {}
    for h in short:
        copies.setdefault(re.sub(r"\W+", " ", _text(h).lower()).strip(), []).append(h)
    for group in copies.values():
        if len(group) < 2:
            continue
        # a title printed again as a label ("Net interest income" over its chart's own
        # "Net interest income") keeps the larger copy; same-size copies are labels
        big = max(h.size for h in group)
        for h in group:
            if not (h.size >= big and big >= 1.15 * min(x.size for x in group)) and h is not title:
                h.kind = "paragraph"
    bare = [h for h in short if h.kind == "heading" and h is not title and not _introduces(h, others + blocks)]
    if len(bare) >= 3:
        # the labels share a size; a heading set larger than them stays a heading
        typical = statistics.median(h.size for h in bare)
        demoted = [h for h in bare if h.size < 1.15 * typical]
        for h in demoted:
            h.kind = "paragraph"
        # a column heading over its own list ("Value to ITC" over plain items) looked bare only
        # while those items counted as headings: set apart from the text under it (bold over
        # plain, or larger), it is a heading after all
        for h in demoted:
            below = [e for e in blocks if e is not h and _directly_below(h, e)]
            if below:
                e = min(below, key=lambda e: e.bbox[1])
                if (all(w.bold for w in h.words) and not all(w.bold for w in e.words)) or h.size >= 1.15 * e.size:
                    h.kind = "heading"


TABLE_MAX_EMPTY = 0.5      # a table with more empty cells than this is not laid out as a table
TABLE_MAX_UNPLACED = 0.25  # nor one with this many numbers left over for every number placed in a cell
TABLE_MAX_MIXED = 0.1      # nor one with more than this share of rows whose names and values are mixed up


def _phrase(cell: list[Word]) -> bool:
    """A value cell holding words (two or more), not a number: a row's name, a note or a
    heading read into the value columns. A short mark such as "NA" or "N/M" is one word."""
    return sum(1 for w in cell if sum(ch.isalpha() for ch in w.text) >= 2) >= 2


def table_problems(t: TableEl) -> list[str]:
    """Why a table can't be published as a table — its numbers don't line up in rows and
    columns — or [] when it can. Checked before every table goes out."""
    ncols, nrows = len(t.anchors), len(t.rows)
    if not ncols or not nrows:
        return ["no rows or columns"]
    filled = [sum(1 for c in cells if c) for _, cells in t.rows]
    empty = 1 - sum(filled) / (ncols * nrows)
    out = []
    if t.unplaced > TABLE_MAX_UNPLACED * sum(filled):     # one stray footnote number is not a broken table
        out.append(f"{t.unplaced} values with no cell of their own")
    if ncols >= 2 and nrows >= 2 and empty > TABLE_MAX_EMPTY:
        out.append(f"{round(100 * empty)}% of cells empty")
    rows = [f for f in filled if f]
    if ncols >= 3 and len(rows) >= 3 and sum(1 for f in rows if f <= max(1, ncols // 3)) >= 0.6 * len(rows):
        out.append("values scattered across rows instead of lined up")
    # (Too few column headers on their own don't make a table unreadable: its numbers still line
    # up, as in a results table whose period row wasn't read in full. It stays a table.)
    # None at all is another matter: with two or more value columns and not one column name over
    # them (a header set as white text on a band, or lost), nobody can tell the columns apart.
    if ncols >= 2 and not any(c.words for hr in t.header_rows for c in hr):
        out.append("no column names")
    # Names and values mixed up: a row's name that took a value (a number standing on its own,
    # or text from among the value columns — two tables side by side, a value glued to a label),
    # a value cell holding words, or a row with no name whose values are years or periods (column
    # names set over two lines, read as values). One such row in a long table is a slip; more is
    # not a table.
    def mixed_up(i: int, lab: list[Word], cells: list[list[Word] | None]) -> bool:
        return i in t.stray_rows or any(c and _phrase(c) for c in cells) or \
            (not lab and sum(1 for c in cells if c and _looks_like_header(Row([Segment(c)]))) >= 2)
    mixed = sum(1 for i, (lab, cells) in enumerate(t.rows) if mixed_up(i, lab, cells))
    if mixed > TABLE_MAX_MIXED * nrows:
        out.append(f"{mixed} rows with names and values mixed up")
    # Values in offset rows: two neighbouring columns that both hold values but never in the same
    # row (a value and its year in alternating rows, two tables' rows interleaved, a value set a
    # little off its column). In a table every pair of neighbouring columns shares a row.
    holds = [{i for i, (_, cells) in enumerate(t.rows) if cells[k]} for k in range(ncols)]
    if any(a and b and not a & b for a, b in zip(holds, holds[1:])):
        out.append("values in offset rows")
    # A chart's or a donut's leftover numbers: most rows that hold values hold a single one and
    # no name, so nothing says what each number is.
    data = [(lab, cells) for lab, cells in t.rows if any(cells)]
    if sum(1 for lab, cells in data if not lab and sum(1 for c in cells if c) == 1) >= 0.5 * len(data) > 0:
        out.append("lone values with no row names")
    return out


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
    unplaced = 0
    stray_rows: list[int] = []
    for k in members:
        row = rows[k]
        cells: list[list[Word] | None] = [None] * len(anchors)
        label: list[Word] = []
        stray = False
        for seg in row.segments:
            col = _col_of(seg, anchors, colw) if seg.is_numeric else None
            if col is not None and cells[col] is None:
                cells[col] = seg.words
            elif seg.x1 < anchors[0] - colw * 0.6:
                # a number standing on its own in the name column is a value, not part of a name
                stray = stray or (seg.is_numeric and not _looks_like_header(Row([seg])))
                label.extend(seg.words)
            else:
                col = _col_of(seg, anchors, colw)
                if col is not None and cells[col] is None:
                    cells[col] = seg.words
                else:
                    unplaced += seg.is_numeric
                    stray = True             # text from among the value columns, joined to the row's name
                    label.extend(seg.words)
        if stray:
            stray_rows.append(len(body))
        body.append((label, cells))

    # Header rows: up to 3 lines directly above whose segments sit over the numeric columns.
    extra: list[int] = []
    header_rows: list[list[TableCell]] = []
    corner: list[Word] = []                  # header text over the row labels ("Particulars")
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
        corner = [w for s in row.segments if s not in segs for w in s.words] + corner
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
    return TableEl(bbox, anchors, header_rows, col_words, body, caption, corner_words=corner, unplaced=unplaced,
                   stray_rows=stray_rows), extra


def _looks_like_header(row: Row) -> bool:
    return all(parse_period(s.text) or parse_date(s.text) or re.fullmatch(r"(19|20)\d\d", s.text.strip())
               for s in row.segments)


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
