"""Words that must never become figures: page numbers, chart axis ticks, OCR read out of
logos/icons/photos, and digits set in icon fonts. Also a sanity test for OCR'd headings.

Everything here is conservative: a word is dropped only when several independent signals
agree (position AND page sequence for page numbers; alignment AND even spacing AND an
arithmetic run of round values for axis ticks; a context signal AND a content signal for
OCR junk). Dropped words are excluded like headers/footers (listed per page, with a
reason, under `excluded_header_footer`), so the completeness check doesn't count them.
"""

from __future__ import annotations

import math
import re
import statistics
from collections import defaultdict
from decimal import Decimal

from app.extraction.numbers import has_digit, try_parse_number
from app.extraction.pdf import PageData, Word

# ------------------------------------------------------------------ helpers


def _inside(w: Word, r: tuple[float, float, float, float], pad: float = 0.0) -> bool:
    return r[0] - pad <= w.xc <= r[2] + pad and r[1] - pad <= w.yc <= r[3] + pad


def _alpha_word(w: Word) -> bool:
    return sum(ch.isalpha() for ch in w.text) >= 2


def _same_line(a: Word, b: Word) -> bool:
    return abs(a.yc - b.yc) <= 0.5 * min(a.h, b.h) if a.h and b.h else False


# ------------------------------------------------------------------ page numbers

PAGE_NO_BAND_Y = 0.12      # top/bottom fraction searched for running page numbers
PAGE_NO_BAND_X = 0.10      # left/right fraction (page numbers set in the side margin)
_PAGE_NO_CORE = re.compile(r"^\d{1,4}$")
_PAGE_NO_STRIP = "|·•-–—()[]"      # never '.', ',' or ':' — '4.' is a list marker


def _page_no_value(w: Word) -> int | None:
    t = w.text.strip(_PAGE_NO_STRIP)
    if not _PAGE_NO_CORE.match(t):
        return None
    v = int(t)
    return v if 0 < v <= 3000 else None


def _in_margin(w: Word, p: PageData) -> bool:
    return (w.bbox[3] <= p.height * PAGE_NO_BAND_Y or w.bbox[1] >= p.height * (1 - PAGE_NO_BAND_Y)
            or w.bbox[2] <= p.width * PAGE_NO_BAND_X or w.bbox[0] >= p.width * (1 - PAGE_NO_BAND_X))


def page_number_words(pages: list[PageData]) -> dict[int, set[int]]:
    """Running page numbers: a bare integer in the page margin whose value follows the PDF
    page index (printed = index + offset, or 2*index + offset for two-up spreads) on
    several pages. A lone number in a margin that doesn't follow the sequence is kept.

    The older header/footer remover only catches a whole band line that is just a page
    number ('27', 'Page 27 of 90') within 7.5% of the edge, or a line repeated on half
    the pages. It misses '27' sharing a line with a per-chapter running title, numbers set
    a little higher than 7.5%, and numbers in the side margin."""
    cands: dict[int, list[tuple[Word, int]]] = {}
    for p in pages:
        cs = []
        for w in p.words:
            v = _page_no_value(w)
            if v is not None and _in_margin(w, p):
                cs.append((w, v))
        cands[p.number] = cs
    support: dict[tuple[int, int], set[int]] = defaultdict(set)
    for n, cs in cands.items():
        for _w, v in cs:
            support[(1, v - n)].add(n)
            off2 = v - 2 * n                      # spreads: left = 2n+c, right = 2n+c+1
            support[(2, off2)].add(n)
            support[(2, off2 - 1)].add(n)
    if len(pages) < 2:
        return {p.number: set() for p in pages}
    # Real page numbers run through most pages; list markers or note numbers in a margin
    # line up with the page index only by accident, on a few pages.
    need = len(pages) if len(pages) <= 3 else max(3, math.ceil(0.3 * len(pages)))
    keys = {k for k, ns in support.items() if len(ns) >= (need if k[0] == 1 else max(3, need))}
    out: dict[int, set[int]] = {}
    for p in pages:
        ex: set[int] = set()
        for w, v in cands[p.number]:
            if (1, v - p.number) in keys or (2, v - 2 * p.number) in keys or (2, v - 2 * p.number - 1) in keys:
                ex.add(id(w))
        out[p.number] = ex
    return out


# ------------------------------------------------------------------ chart axis ticks

_ROUND_MANTISSAS = (Decimal(1), Decimal(2), Decimal("2.5"), Decimal(5))


def _round_step(step: Decimal) -> bool:
    s = abs(step)
    if s == 0:
        return False
    e = math.floor(math.log10(float(s)))
    m = s / (Decimal(10) ** e)
    return any(abs(m - r) < Decimal("0.0001") for r in _ROUND_MANTISSAS) or abs(m - 10) < Decimal("0.0001")


def _tick_value(w: Word) -> tuple[str, Decimal] | None:
    p = try_parse_number(w.text.strip())
    if p is None or p.value is None or p.kind not in ("number", "percent") or p.currency_symbol:
        return None
    return p.kind, p.value


def _is_year(v: Decimal) -> bool:
    return v == v.to_integral_value() and 1900 <= v <= 2100


def _even(positions: list[float], tol: float) -> bool:
    gaps = [b - a for a, b in zip(positions, positions[1:])]
    if not gaps or min(gaps) <= 0:
        return False
    med = statistics.median(gaps)
    return all(abs(g - med) <= max(tol, 0.15 * med) for g in gaps)


def _runs(items: list[tuple[Word, str, Decimal]], pos, tol: float, min_len: int, descending: bool):
    """Maximal runs (in position order) that are evenly spaced, one kind, an arithmetic
    progression with a round non-zero step, and not all years."""
    items = sorted(items, key=lambda it: pos(it[0]))
    found = []
    i = 0
    while i < len(items):
        j = i + 1
        while j < len(items):
            seg = items[i:j + 1]
            kinds = {k for _, k, _ in seg}
            vals = [v for _, _, v in seg]
            steps = {b - a for a, b in zip(vals, vals[1:])}
            if len(kinds) != 1 or len(steps) != 1 or not _round_step(next(iter(steps))) \
                    or (descending and next(iter(steps)) > 0) or (not descending and next(iter(steps)) < 0) \
                    or not _even([pos(w) for w, _, _ in seg], tol):
                break
            j += 1
        run = items[i:j]
        if len(run) >= min_len and not all(_is_year(v) for _, _, v in run):
            found.append(run)
            i = j
        else:
            i += 1
    return found


def axis_tick_words(p: PageData, regions: list[tuple[float, float, float, float]] | None = None) -> set[int]:
    """Chart axis tick labels ('0 20 40 60 80', '0% 25% 50%'): bare numbers aligned in a
    column (y axis) or a row (x axis), evenly spaced, forming an arithmetic run with a round
    step (1, 2, 2.5, 5 x 10^k). Inside a chart/image region 3 ticks are enough. Elsewhere
    (line charts have no filled shapes, OCR pages have no regions) it takes 4+, the run must
    include 0 or have 5+ ticks, and no word label may sit on the tick's line — table rows
    always carry a row label, axis ticks never do. Years never count as ticks."""
    regions = list(regions if regions is not None else (p.chart_regions + p.image_regions))
    cands = [(w, *tv) for w in p.words if (tv := _tick_value(w)) is not None]
    if len(cands) < 3:
        return set()
    alpha = [w for w in p.words if _alpha_word(w)]
    med_h = statistics.median(w.h for w in p.words) if p.words else 10.0
    out: set[int] = set()

    def in_region(run) -> bool:
        return bool(regions) and any(sum(_inside(w, r, 4) for w, _, _ in run) * 2 >= len(run) for r in regions)

    def accept(run, group, vertical: bool) -> bool:
        if len(run) < 0.75 * len(group):
            return False                               # ticks are alone on their axis line
        has_zero = any(v == 0 for _, _, v in run)
        step_one = abs(run[1][2] - run[0][2]) == 1
        if in_region(run):
            # '1 2 3' in an infographic is a list of steps, not an axis
            return len(run) >= 3 and (has_zero or not step_one or len(run) >= 5)
        if len(run) < 4 or not (has_zero or len(run) >= 5):
            return False
        for w, _, _ in run:
            for a in alpha:
                if not _same_line(w, a):
                    continue
                if not vertical:
                    return False                       # an x-axis row never has a word label
                gap = w.bbox[0] - a.bbox[2] if a.xc < w.xc else a.bbox[0] - w.bbox[2]
                if (a.xc < w.xc and gap < 150) or (a.xc > w.xc and gap < 3 * max(w.h, med_h)):
                    return False                       # a row label: this is a table column
        return True

    # Vertical axes: cluster by right edge (ticks are right-aligned) and by centre.
    for key in (lambda w: w.bbox[2], lambda w: w.xc):
        cols: list[list] = []
        for it in sorted(cands, key=lambda it: key(it[0])):
            if cols and key(it[0]) - key(cols[-1][-1][0]) <= 3.0:
                cols[-1].append(it)
            else:
                cols.append([it])
        for col in cols:
            if len(col) < 3:
                continue
            # y grows downwards and values grow upwards, so values descend along y.
            for run in _runs(col, lambda w: w.yc, 0.6 * med_h, 3, descending=True):
                if accept(run, col, vertical=True):
                    out.update(id(w) for w, _, _ in run)
    # Horizontal axes: same line, values ascend left to right.
    rows: list[list] = []
    for it in sorted(cands, key=lambda it: it[0].yc):
        if rows and _same_line(rows[-1][0][0], it[0]):
            rows[-1].append(it)
        else:
            rows.append([it])
    for row in rows:
        if len(row) < 3:
            continue
        for run in _runs(row, lambda w: w.xc, 0.6 * med_h, 3, descending=False):
            if accept(run, row, vertical=False):
                out.update(id(w) for w, _, _ in run)
    return out


# ------------------------------------------------------------------ OCR junk

OCR_LOW_CONF = 0.5
LOGO_TRUST_CONF = 0.85
_REPEAT_DIGIT = re.compile(r"^(\d)\1{2,}$")
_CUR_ONE_DIGIT = re.compile(r"^[(]?(₹|Rs\.?|INR|US\$|USD|\$|€|EUR|£|GBP)\s?\d[)]?$")


def degenerate_number(text: str) -> bool:
    """A digit string no report prints on purpose but OCR invents from icons, textures and
    logos: one digit repeated ('888', '$8888', '1111'), a currency sign with a single digit
    ('€8', '$4'), or a token that is mostly symbols ('#8~%')."""
    t = text.strip().strip("()")
    digits = re.sub(r"[^0-9]", "", t)
    core = re.sub(r"^(₹|Rs\.?|INR|US\$|USD|\$|€|EUR|£|GBP)\s?", "", t).replace(",", "").replace(".", "")
    if digits and _REPEAT_DIGIT.match(core):
        return True
    if _CUR_ONE_DIGIT.match(t):
        return True
    if try_parse_number(t) is not None:
        return False                       # '-1%', '(5)' are well-formed, however short
    alnum = sum(ch.isalnum() for ch in t)
    return len(t) >= 2 and alnum / len(t) < 0.5


def _has_solid_neighbour(w: Word, words: list[Word], med_h: float) -> bool:
    """Real text has neighbours: a confidently read word on the same line nearby, or one
    directly above/below overlapping it. A logo or icon read stands alone (or next to
    other junk)."""
    h = max(w.h, med_h)
    for o in words:
        if o is w or o.conf < 0.3 or degenerate_number(o.text):
            continue
        if has_digit(o.text):
            if try_parse_number(o.text.strip(",.;:")) is None:
                continue
        elif not _alpha_word(o) or (o.conf < 0.6 and not wordlike(re.sub(r"[^A-Za-z]", "", o.text))):
            continue                           # other junk read off the same logo doesn't count
        if _same_line(w, o):
            gap = max(o.bbox[0] - w.bbox[2], w.bbox[0] - o.bbox[2])
            if gap < 2.5 * h:
                return True
        else:
            xov = min(w.bbox[2], o.bbox[2]) - max(w.bbox[0], o.bbox[0])
            vgap = max(o.bbox[1] - w.bbox[3], w.bbox[1] - o.bbox[3])
            if xov > -h and vgap < 1.5 * h:
                return True
    return False


def ocr_junk_words(p: PageData) -> set[int]:
    """OCR words to drop on an OCR page. A digit-bearing word goes when it has a CONTENT
    signal (low confidence, or a degenerate string) AND a CONTEXT signal (no solid
    neighbour, an odd glyph size, or it lies on a small logo/icon image). Strongly
    degenerate reads (a digit repeated 4+ times at low confidence) go on their own. A
    word-free read that is mostly symbols at low confidence goes too."""
    if p.method != "ocr" or not p.words:
        return set()
    med_h = statistics.median(w.h for w in p.words)
    out: set[int] = set()
    for w in p.words:
        t = w.text.strip()
        if not has_digit(t):
            alnum = sum(ch.isalnum() for ch in t)
            if len(t) >= 2 and alnum / len(t) < 0.5 and w.conf < OCR_LOW_CONF:
                out.add(id(w))
            elif any(_inside(w, r) for r in p.logo_regions) and w.conf < LOGO_TRUST_CONF:
                out.add(id(w))                 # text OCR'd out of a logo
            continue
        degenerate = degenerate_number(t)
        lone_digit = len(re.sub(r"[^0-9A-Za-z]", "", t)) == 1
        content = w.conf < OCR_LOW_CONF or (degenerate and w.conf < 0.8)
        on_logo = any(_inside(w, r) for r in p.logo_regions)
        odd_size = med_h > 0 and (w.h < 0.5 * med_h or w.h > 3.0 * med_h)
        isolated = not _has_solid_neighbour(w, p.words, med_h)
        if on_logo and (w.conf < LOGO_TRUST_CONF or degenerate or lone_digit):
            out.add(id(w))
        elif content and (isolated or odd_size):
            out.add(id(w))
        elif lone_digit and isolated and w.conf < LOGO_TRUST_CONF:
            out.add(id(w))                     # a single digit floating on its own: an icon
        elif degenerate and _REPEAT_DIGIT.match(re.sub(r"[^0-9]", "", t) or "x") \
                and len(re.sub(r"[^0-9]", "", t)) >= 4 and w.conf < 0.4:
            out.add(id(w))
    return out


# ------------------------------------------------------------------ icon fonts

_ICON_FONT = re.compile(r"wingding|webding|dingbat|zapfding|fontawesome|font-awesome|icomoon|"
                        r"materialicons|material-icons|glyphicon|icofont|ionicons", re.I)


def icon_font_words(p: PageData) -> set[int]:
    """Digits set in a pictogram font are icons (a phone, a tick), not numbers."""
    return {id(w) for w in p.words if w.font and _ICON_FONT.search(w.font) and has_digit(w.text)}


# ------------------------------------------------------------------ all of it


def noise_words(pages: list[PageData], already: dict[int, set[int]] | None = None) -> dict[int, dict[int, str]]:
    """{page_no: {id(word): reason}} for every word that must not become a figure."""
    already = already or {}
    page_nos = page_number_words(pages)
    out: dict[int, dict[int, str]] = {}
    for p in pages:
        skip = already.get(p.number, set())
        reasons: dict[int, str] = {}
        for i in page_nos.get(p.number, set()):
            reasons[i] = "page_number"
        for i in icon_font_words(p):
            reasons.setdefault(i, "icon_font")
        for i in ocr_junk_words(p):
            reasons.setdefault(i, "ocr_noise")
        live = PageData(p.number, p.width, p.height, p.method,
                        [w for w in p.words if id(w) not in skip and id(w) not in reasons],
                        p.text_layer_reason, p.image_regions, p.chart_regions)
        for i in axis_tick_words(live):
            reasons.setdefault(i, "axis_tick")
        out[p.number] = {i: r for i, r in reasons.items() if i not in skip}
    return out


# ------------------------------------------------------------------ OCR headings

_VOWELS = set("aeiouy")
_ONSETS = {
    "b", "c", "d", "f", "g", "h", "j", "k", "l", "m", "n", "p", "q", "r", "s", "t", "v", "w", "x", "z",
    "bl", "br", "ch", "cl", "cr", "dr", "dw", "fl", "fr", "gh", "gl", "gr", "kh", "kl", "kn", "kr", "ph",
    "pl", "pr", "ps", "qu", "rh", "sc", "sh", "sk", "sl", "sm", "sn", "sp", "st", "sw", "th", "tr", "ts",
    "tw", "wh", "wr", "bh", "dh", "jh", "zh", "gn", "kw", "pf", "sv", "tz", "vr", "sch", "scr", "shr", "sph", "spl", "spr", "squ", "str", "thr", "chr", "phr",
}
_BAD_DOUBLE_END = re.compile(r"(yy|hh|jj|qq|vv|ww|xx|kk|ii|uu|aa)$")


def _case_ok(tok: str) -> bool:
    letters = [ch for ch in tok if ch.isalpha()]
    if all(c.islower() for c in letters) or all(c.isupper() for c in letters):
        return True
    if letters[0].isupper() and all(c.islower() for c in letters[1:]):
        return True
    # one internal capital is fine ('McKinsey', 'iPhone'); more case flips are OCR noise
    flips = sum(1 for a, b in zip(letters, letters[1:]) if a.isupper() != b.isupper())
    return flips <= 2 and sum(c.isupper() for c in letters) <= 2


def wordlike(tok: str) -> bool:
    """Does a run of letters (4+) look like a word in a Latin-script language?"""
    t = tok.lower()
    if len(t) < 4 or not t.isalpha() or not _case_ok(tok):
        return False
    vowels = sum(ch in _VOWELS for ch in t)
    if not 0.2 <= vowels / len(t) <= 0.8:
        return False
    if re.search(r"(.)\1\1", t) or _BAD_DOUBLE_END.search(t):
        return False
    longest = max((len(m) for m in re.findall(r"[^aeiouy]+", t)), default=0)
    if longest > (5 if len(t) >= 8 else 4):
        return False
    onset = re.match(r"[^aeiouy]*", t).group(0)
    if onset and onset not in _ONSETS and not (len(onset) == 2 and onset[0] == "s"):
        return False
    return True


def plausible_ocr_heading(words: list[Word]) -> bool:
    """Reject OCR'd 'headings' that are logos or textures read as letters ('gtaaliyy',
    'MM ENA'). Needs most longer tokens to look like words and a reasonable confidence;
    a heading made only of short tokens (acronyms) needs a confident read."""
    if not words:
        return False
    toks = [re.sub(r"[^A-Za-z]", "", w.text) for w in words if not has_digit(w.text)]
    toks = [t for t in toks if t]
    letters = sum(len(t) for t in toks)
    mean_conf = statistics.mean(w.conf for w in words)
    if letters < 3:
        return False
    long = [t for t in toks if len(t) >= 4]
    if long:
        good = sum(1 for t in long if wordlike(t))
        return good >= 1 and good * 2 >= len(long) and mean_conf >= 0.4
    return mean_conf >= 0.85 and all(_case_ok(t) for t in toks) and any(len(set(t.lower())) >= 2 for t in toks)
