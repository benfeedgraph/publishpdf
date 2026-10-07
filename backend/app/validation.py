"""The validation checker (brief §4.4). Produces issues per report version; publishing
is blocked while any blocking issue is open.

Checks
  1 traceability     every figure's raw string is found at its PDF location
  2 re_extraction    each figure re-read visually (rendered page crop -> OCR) must match
  3 arithmetic       totals, balance sheet balance, stated % changes
  4 period_unit      period/unit consistency; no crore/lakh mixing without a label
  5 rendered_page    every number/date on the generated page exists in the schema, identically
  6 completeness     numeric tokens per PDF page vs figures captured
  7 low_confidence   OCR values under the threshold need a human check
  + schema           the document validates against schema/report.schema.json
"""

from __future__ import annotations

import html.parser
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

import pymupdf

from app.extraction import ocr
from app.extraction.jsonschema_check import schema_errors
from app.extraction.numbers import NIL_TOKENS, has_digit, try_parse_number
from app.extraction.tokens import _split_punct

NUMERIC_KINDS = {"number", "percent", "bps", "multiple", "nil"}
REEXTRACT_KINDS = NUMERIC_KINDS | {"date"}


@dataclass
class Issue:
    check: str
    severity: str                  # 'blocking' | 'warning'
    message: str
    fid: str | None = None
    page: int | None = None
    section_id: str | None = None
    bbox: list[float] | None = None
    expected: str | None = None
    actual: str | None = None
    status: str = "open"
    resolution: str | None = None


def _fig_issue(check: str, severity: str, f: dict, message: str, expected=None, actual=None) -> Issue:
    return Issue(check, severity, message, f["id"], f["source"]["page"], f.get("section_id"),
                 f["source"]["bbox"], expected, actual)


def _active(schema: dict) -> list[dict]:
    return [f for f in schema["figures"].values() if f.get("status", "active") == "active"]


# ------------------------------------------------------------------ 1. traceability


def _words_in(words: list[tuple[str, list[float]]], bbox: list[float], pad: float = 1.5) -> list[str]:
    x0, y0, x1, y1 = bbox
    out = []
    for text, b in words:
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        if x0 - pad <= cx <= x1 + pad and y0 - pad <= cy <= y1 + pad:
            out.append((round(b[1] / 4), b[0], text))
    return [t for *_, t in sorted(out)]


def _parts_in(raw: str, found: list[str]) -> bool:
    """A multi-word figure that wraps onto the next line (e.g. '30th September' /
    '2026'): every part must be present at the location, in reading order."""
    parts = raw.split(" ")
    if len(parts) < 2:
        return False
    words = [_split_punct(w)[1] for w in found]
    i = 0
    for w in words:
        if i < len(parts) and w == parts[i]:
            i += 1
    return i == len(parts)


def check_traceability(schema: dict, doc: pymupdf.Document, artifact: dict,
                       verified: set[str] | None = None) -> list[Issue]:
    issues = []
    page_words: dict[int, list[tuple[str, list[float]]]] = {}
    for p in artifact["pages"]:
        if p["method"] == "ocr":
            page_words[p["page"]] = [(w["t"], w["b"]) for w in p["words"]]
        else:
            # Re-read the text layer from the PDF itself (not our extraction copy).
            page_words[p["page"]] = []
            from app.extraction.pdf import text_layer_words
            for w in text_layer_words(doc[p["page"] - 1])[0]:
                page_words[p["page"]].append((w.text, list(w.bbox)))
    for f in _active(schema):
        if f.get("edited"):
            continue           # human-entered value; checked by re-extraction instead
        found = _words_in(page_words.get(f["source"]["page"], []), f["source"]["bbox"])
        joined = " ".join(found)
        cores = {joined, _split_punct(joined)[1]} | {_split_punct(w)[1] for w in found}
        if f["raw"] in cores or f["raw"] in joined.split(" ") or f["raw"] in joined or _parts_in(f["raw"], found):
            if verified is not None:
                verified.add(f["id"])
        else:
            issues.append(_fig_issue("traceability", "blocking", f,
                                     "The value isn't found at its recorded place in the PDF.",
                                     expected=f["raw"], actual=joined or "(nothing at that location)"))
    return issues


# ------------------------------------------------------------------ 2. independent re-extraction


def _digits(s: str) -> str:
    return re.sub(r"[^0-9.]", "", s).strip(".")


_CUR_PREFIX = re.compile(r"^(₹|Rs\.?\s?|INR\s?|US\$|USD\s?|\$|€|EUR\s?|£|GBP\s?)")


def _prefix_offset(raw: str, x0: float, x1: float) -> float:
    """Skip a leading currency symbol when cropping: OCR often reads 'Rs.' or '₹' as
    digits. The number part alone is re-read (the symbol isn't a digit to verify)."""
    m = _CUR_PREFIX.match(raw)
    if not m or len(raw) <= len(m.group(0)):
        return 0.0
    return max(0.0, (x1 - x0) * len(m.group(0)) / len(raw) - 0.8)


def _canon_read(raw: str, read: str) -> list[str]:
    """Candidate readings of an OCR crop. Digits are never altered; only separator and
    symbol confusions that OCR is known to make are normalised:
      - '-' or '–' between digits where the PDF has a decimal point ('45-55' ~ '45.55')
      - a currency glyph misread as a character ('₹3.50' read as '23.50' / '%3.50')"""
    cands = [read]
    if "." in raw:
        cands.append(re.sub(r"(?<=\d)[-–](?=\d)", ".", read))
    p = try_parse_number(raw)
    if p and p.currency_symbol:
        for c in list(cands):
            m = re.search(r"[\d(]", c)
            if m and m.start() == 0 and len(c) > 1:
                cands.append(c[1:])          # symbol read as a digit
            elif m:
                cands.append(c[m.start():])  # symbol read as junk
    return cands


def reextraction_matches(raw: str, read: str, kind: str) -> bool:
    read = read.strip().strip("°\"'`")
    if kind == "nil":
        return read == "" or read in NIL_TOKENS or set(read) <= set("-–—_ ")
    if kind == "date":
        return re.sub(r"\D", "", raw) == re.sub(r"\D", "", read)
    core = _split_punct(raw)[1] if not raw.startswith("(") else raw
    for cand in _canon_read(core, read):
        cand = cand.rstrip(" -–")          # trailing noise only; a leading minus is data
        if _digits(core) != _digits(cand):
            continue
        pr, po = try_parse_number(core), try_parse_number(cand)
        if pr and po:
            # 'x' and '%' are a known OCR glyph confusion; the digits still must match exactly.
            same_kind = pr.kind == po.kind or {pr.kind, po.kind} == {"multiple", "percent"}
            if pr.negative == po.negative and same_kind:
                return True
            continue
        if pr and pr.negative and not re.search(r"[(\-−–]", cand):
            continue                     # sign lost in the re-read
        if pr and pr.kind == "percent" and "%" not in cand:
            continue
        return True
    return False


REREAD_DPIS = (300, 600)
EDGE_KEEP = 0.8        # points kept either side of a figure's box when cutting it out for OCR
BATCH = 40


def ocr_contradicts(raw: str, read: str) -> bool:
    """Does an OCR reading actually contradict the value, or did OCR just fail to read it
    cleanly? A reading contradicts only if the value's digit groups can't all be found in
    it, in order ('23.53' vs '23.93', '5' vs '9'). Extra characters around or between the
    groups — a table rule read as '1' ('129' -> '1129'), '/' read as '1', 'Oct' read as
    '0' — and readings with no digits at all are OCR noise, not evidence of a different value."""
    got = re.sub(r"\D", "", read or "")
    if not got:
        return False
    pos = 0
    for group in re.findall(r"\d+", raw):
        k = got.find(group, pos)
        if k < 0:
            return True
        pos = k + len(group)
    return False


def _ocr_workers() -> int:
    """Tesseract runs as one process per call, so OCR scales with this job's CPU share."""
    from app.runtime import process_workers
    return max(2, process_workers(cap=16))


def reextraction_reads(figs: list[dict], doc: pymupdf.Document, workers: int) -> dict[str, list[str]]:
    """Every OCR reading of each figure's region. Pass 1 reads crops in batches (one
    Tesseract call per ~40 figures); anything not confirmed is read again, batched at a
    second resolution, then individually in several configurations. Figures go in page
    order, so a batch's neighbours — which Tesseract's reading depends on — are always
    the same for the same document."""
    figs = sorted(figs, key=lambda f: (f["source"]["page"], f["id"]))

    def crop(f: dict, dpi: int, loose: bool = False):
        """Tight crop around the figure's own text line. Extra vertical padding pulls in
        the neighbouring line or a table rule, which OCR then reads as part of the
        figure; the white border added before OCR provides the margin instead.
        Wrapped figures are read line by line and stitched together."""
        page = doc[f["source"]["page"] - 1]
        boxes = f["source"].get("parts") or [f["source"]["bbox"]]
        ims = []
        for x0, y0, x1, y1 in boxes:
            x0 += _prefix_offset(f["raw"], x0, x1) if len(boxes) == 1 else 0
            h = y1 - y0
            vpad = 0.4 * h if loose else min(1.2, 0.12 * h)
            clip = pymupdf.Rect(x0 - 3, y0 - vpad, x1 + 3, y1 + vpad) & page.rect
            im = ocr.render(page, dpi, clip)
            # Only the figure's own glyphs: a neighbour's edge inside the side padding
            # ("480 001" -> "1001") is painted out. The exact-match rule is unchanged.
            k = dpi / 72
            keep0, keep1 = int(max(0.0, x0 - EDGE_KEEP - clip.x0) * k), int(min(clip.width, x1 + EDGE_KEEP - clip.x0) * k)
            if 0 < keep0 or keep1 < im.width:
                from PIL import ImageDraw
                draw = ImageDraw.Draw(im)
                if keep0 > 0:
                    draw.rectangle([0, 0, keep0 - 1, im.height], fill=255)
                if keep1 < im.width:
                    draw.rectangle([keep1, 0, im.width, im.height], fill=255)
            ims.append(im)
        if len(ims) == 1:
            return ims[0]
        from PIL import Image
        W = sum(i.width for i in ims) + 40 * len(ims)
        H = max(i.height for i in ims)
        sheet = Image.new("L", (W, H), 255)
        x = 0
        for i in ims:
            sheet.paste(i, (x, 0))
            x += i.width + 40
        return sheet

    reads: dict[str, list[str]] = {f["id"]: [] for f in figs}
    ok = lambda f: any(reextraction_matches(f["raw"], r, f["kind"]) for r in reads[f["id"]])  # noqa: E731

    def batched(group: list[dict], dpi: int) -> None:
        """One Tesseract call per BATCH crops. Crops are cut on this thread (the PDF
        document isn't thread-safe) and each batch is read as soon as it's cut, so
        cutting and reading overlap."""
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = []
            for i in range(0, len(group), BATCH):
                b = group[i:i + BATCH]
                futures.append((b, pool.submit(ocr.read_batch, [crop(f, dpi) for f in b])))
            for b, fut in futures:
                for f, t in zip(b, fut.result()):
                    reads[f["id"]].append(t)

    # Pass 1: every figure, batched.
    batched(figs, ocr.CROP_DPI)
    # Pass 1b: what pass 1 didn't confirm, batched again at a different resolution — one
    # more independent reading configuration, far cheaper than a Tesseract start per figure.
    batched([f for f in figs if not ok(f)], REREAD_DPIS[0])
    # Pass 2+: individual reads for anything not yet confirmed. Each attempt is a
    # different reading configuration; a figure passes only if one agrees exactly.
    attempts = [
        (ocr.CROP_DPI, 7, 1, False),   # one line
        (ocr.CROP_DPI, 8, 2, False),   # one word, upscaled — short/isolated numbers
        (ocr.CROP_DPI, 10, 3, False),  # one character — lone digits (page numbers)
        (REREAD_DPIS[0], 7, 1, False),
        (REREAD_DPIS[1], 7, 1, False),
        (ocr.CROP_DPI, 7, 1, True),    # looser crop, in case the text box is clipped
    ]
    for dpi, psm, up, loose in attempts:
        pending = [(f, crop(f, dpi, loose)) for f in figs if not ok(f)
                   and (psm != 10 or len(re.sub(r"\D", "", f["raw"])) == 1)]
        if not pending:
            continue
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for f, (text, _c) in zip([p[0] for p in pending],
                                     pool.map(lambda fc, psm=psm, up=up: ocr.ocr_image(fc[1], psm=psm, upscale=up), pending)):
                reads[f["id"]].append(text)
    return reads


def check_reextraction(schema: dict, doc: pymupdf.Document | None, workers: int | None = None,
                       verified: set[str] | None = None, text_confirmed: set[str] | None = None,
                       reads: dict[str, list[str]] | None = None) -> list[Issue]:
    """Render each figure's region and read it with OCR (a different method from the
    text layer). A figure passes only if a reading agrees exactly on digits, sign and kind.
    `reads` can come from reextraction_reads_parallel (run alongside other checks)."""
    figs = [f for f in _active(schema) if f["kind"] in REEXTRACT_KINDS]
    issues = []
    if reads is None:
        reads = reextraction_reads(figs, doc, workers or _ocr_workers())
    ok = lambda f: any(reextraction_matches(f["raw"], r, f["kind"]) for r in reads.get(f["id"], []))  # noqa: E731
    for f in figs:
        if ok(f):
            if verified is not None:
                verified.add(f["id"])
            continue
        rs = [r for r in reads.get(f["id"], []) if r]
        # Text-layer values that two independent PDF parsers already read identically: when
        # OCR merely failed to read them cleanly, that's a warning. It still blocks when
        # most OCR readings show different digits — the sign of a text layer that doesn't
        # match what's printed (e.g. hidden text saying 46.20 over a printed 48.20).
        digit_reads = [r for r in rs if re.search(r"\d", r)]
        against = sum(1 for r in digit_reads if ocr_contradicts(f["raw"], r))
        if f["method"] == "text_layer" and text_confirmed and f["id"] in text_confirmed \
                and against * 2 < max(1, len(digit_reads)):
            issues.append(_fig_issue("re_extraction", "warning", f,
                                     "The image re-read couldn't read this cleanly, but two independent PDF "
                                     "parsers read it identically and no reading shows different digits.",
                                     expected=f["raw"], actual=rs[-1] if rs else "(unreadable)"))
            continue
        issues.append(_fig_issue("re_extraction", "blocking", f,
                                 "A second, independent reading of the PDF page disagrees with this value.",
                                 expected=f["raw"], actual=rs[-1] if rs else "(unreadable)"))
    return issues


PARALLEL_REREAD_MIN = 800      # figures; below this one process is faster than starting several
REREAD_CHUNKS = 4


def _reads_chunk(pdf_bytes: bytes, figs: list[dict], workers: int) -> dict[str, list[str]]:
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        return reextraction_reads(figs, doc, workers)
    finally:
        doc.close()


def reextraction_reads_parallel(schema: dict, pdf_bytes: bytes, pool=None) -> dict[str, list[str]]:
    """The OCR readings for every figure, split by page range across processes (cutting
    crops is single-threaded per PDF document). Same readings as one process, sooner."""
    import os
    figs = [f for f in _active(schema) if f["kind"] in REEXTRACT_KINDS]
    from app.runtime import process_workers
    cores = process_workers(cap=16)
    # Always the same split for the same document, whatever the machine: results must not
    # depend on hardware. Chunks are whole batches, so pass 1 reads identical batches.
    n = 1 if len(figs) < PARALLEL_REREAD_MIN else REREAD_CHUNKS
    figs.sort(key=lambda f: (f["source"]["page"], f["id"]))
    size = max(BATCH, -(-(-(-len(figs) // n)) // BATCH) * BATCH) if figs else 1
    chunks = [figs[i:i + size] for i in range(0, len(figs), size)] or [[]]
    per = max(2, -(-cores // len(chunks)) + 1)
    if pool is None or len(chunks) == 1:
        out: dict[str, list[str]] = {}
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        try:
            out.update(reextraction_reads(figs, doc, _ocr_workers()))
        finally:
            doc.close()
        return out
    futures = [pool.submit(_reads_chunk, pdf_bytes, c, per) for c in chunks]
    merged: dict[str, list[str]] = {}
    for fut in futures:
        merged.update(fut.result())
    return merged


# ------------------------------------------------------------------ 2b. independent parser (pdfminer)


def _pdfminer_words(pdf_bytes: bytes, page_numbers: list[int]) -> dict[int, list[tuple[str, list[float]]]]:
    """Words with boxes from pdfminer.six — a separate PDF parser implementation from
    PyMuPDF, so a bug or quirk in one is caught by the other. Top-left origin, points."""
    import io as _io

    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LAParams, LTChar, LTTextContainer, LTTextLine

    out: dict[int, list[tuple[str, list[float]]]] = {}
    wanted = sorted(set(page_numbers))
    for pno, layout in zip(wanted, extract_pages(_io.BytesIO(pdf_bytes), page_numbers=[p - 1 for p in wanted],
                                                   laparams=LAParams(char_margin=1.0, word_margin=0.1, all_texts=True))):
        h = layout.height
        words: list[tuple[str, list[float]]] = []

        def lines(obj):
            if isinstance(obj, LTTextLine):
                yield obj
            elif isinstance(obj, LTTextContainer) or hasattr(obj, "__iter__"):
                for child in obj:
                    yield from lines(child)

        for ln in lines(layout):
            cur: list = []

            def flush():
                if cur:
                    x0 = min(c.x0 for c in cur)
                    x1 = max(c.x1 for c in cur)
                    y0 = h - max(c.y1 for c in cur)
                    y1 = h - min(c.y0 for c in cur)
                    words.append(("".join(c.get_text() for c in cur), [x0, y0, x1, y1]))
                cur.clear()

            for ch in ln:
                if isinstance(ch, LTChar) and not ch.get_text().isspace():
                    cur.append(ch)
                else:
                    flush()
            flush()
        out[pno] = words
    return out


_HYPHENS = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "–", "\u00ad": ""})


def _same_glyphs(s: str) -> str:
    """Two parsers can report the same printed glyph as different code points: a
    non-breaking hyphen vs a hyphen, or an invisible control byte. Neither changes a digit."""
    return re.sub(r"[\x00-\x1f\x7f]", "", s.translate(_HYPHENS))


def check_second_parser(schema: dict, pdf_bytes: bytes, artifact: dict, verified: set[str] | None = None,
                        pool=None) -> list[Issue]:
    text_pages = {p["page"] for p in artifact["pages"] if p["method"] == "text_layer"}
    figs = [f for f in _active(schema) if f["source"]["page"] in text_pages and not f.get("edited")]
    if not figs:
        return []
    try:
        pages = sorted({f["source"]["page"] for f in figs})
        if pool is not None and len(pages) > 40:
            # pdfminer is pure Python: page ranges in separate processes.
            n = 4
            size = -(-len(pages) // n)
            words = {}
            for part in [pool.submit(_pdfminer_words, pdf_bytes, pages[i:i + size]) for i in range(0, len(pages), size)]:
                words.update(part.result())
        else:
            words = _pdfminer_words(pdf_bytes, pages)
    except Exception as e:  # noqa: BLE001 - a parser crash is itself worth a warning, not a silent pass
        return [Issue("second_parser", "warning", f"The second PDF parser couldn't read this file ({type(e).__name__}).")]
    issues = []
    for f in figs:
        found = [_same_glyphs(w) for w in _words_in(words.get(f["source"]["page"], []), f["source"]["bbox"], pad=2.5)]
        joined = " ".join(found)
        cores = {joined, _split_punct(joined)[1]} | {_split_punct(w)[1] for w in found}
        raw = _same_glyphs(f["raw"])
        if raw in cores or raw in joined:
            if verified is not None:
                verified.add(f["id"])
            continue
        issues.append(_fig_issue("second_parser", "blocking", f,
                                 "A second, independent PDF parser reads something different at this position.",
                                 expected=f["raw"], actual=joined or "(nothing at that location)"))
    return issues


# ------------------------------------------------------------------ 3b. cross-table consistency


def _norm_label(s: str | None) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def check_cross_tables(schema: dict, verified: set[str] | None = None) -> list[Issue]:
    """The same line item for the same period and unit should have one value wherever it
    appears (e.g. revenue in the highlights table and in the P&L). Differences are
    warnings, since standalone and consolidated statements legitimately differ."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for f in _active(schema):
        if f.get("role") != "cell" or f["kind"] != "number" or not f.get("period") or not f.get("row_label"):
            continue
        key = (_norm_label(f["row_label"]), f["period"].get("key"), f.get("unit"), f.get("currency"))
        if key[0] and key[1]:
            groups[key].append(f)
    issues = []
    for key, fs in groups.items():
        tables = {f.get("table_id") for f in fs}
        if len(tables) < 2:
            continue
        values = {Decimal(f["value"]) for f in fs if f.get("value") is not None}
        if len(values) == 1:
            if verified is not None:
                verified.update(f["id"] for f in fs)
            continue
        first = fs[0]
        for other in fs[1:]:
            if other.get("value") != first.get("value"):
                issues.append(_fig_issue(
                    "cross_table", "warning", other,
                    f"“{other['row_label']}” for {other['period'].get('raw')} is {other['raw']} here but "
                    f"{first['raw']} in another table (page {first['source']['page']}).",
                    expected=first["raw"], actual=other["raw"]))
    return issues


# ------------------------------------------------------------------ 3. arithmetic


def _val(schema: dict, runs: list[dict]) -> tuple[Decimal | None, dict | None]:
    for r in runs:
        if "f" in r:
            f = schema["figures"][r["f"]]
            if f.get("status") == "active" and f["kind"] == "number" and f.get("value") is not None:
                return Decimal(f["value"]), f
            if f.get("status") == "active" and f["kind"] == "nil":
                return Decimal(0), f
    return None, None


def _dp(raw: str) -> int:
    m = re.search(r"\.(\d+)", raw)
    return len(m.group(1)) if m else 0


_TOTAL = re.compile(r"^\s*(total|net total|grand total)\b|\btotal\b", re.I)
_ASSETS = re.compile(r"^total assets$", re.I)
_EQ_LIAB = re.compile(r"^total (equity and liabilities|liabilities and (shareholders'? )?equity|equity & liabilities)$", re.I)


def check_arithmetic(schema: dict, verified: set[str] | None = None) -> list[Issue]:
    issues: list[Issue] = []
    for sec in schema["sections"]:
        for blk in sec["blocks"]:
            if blk["type"] != "table":
                continue
            rows = blk["rows"]
            ncols = len(blk["columns"])
            matrix = [[_val(schema, r["cells"][c]["runs"]) if c < len(r["cells"]) else (None, None)
                       for c in range(ncols)] for r in rows]
            numeric_cols = [c for c in range(ncols) if not blk["columns"][c].get("change")]
            # --- totals = sum of the rows since the previous total
            start = 0
            for i, r in enumerate(rows):
                if not _TOTAL.search(r.get("label_text", "")):
                    continue
                cands = [k for k in range(start, i) if any(matrix[k][c][0] is not None for c in numeric_cols)
                         and not _TOTAL.search(rows[k].get("label_text", ""))]
                start = i + 1
                if len(cands) < 2:
                    continue
                results = []
                for c in numeric_cols:
                    total, tf = matrix[i][c]
                    parts = [matrix[k][c][0] for k in cands]
                    if total is None or any(p is None for p in parts):
                        continue
                    s = sum(parts, Decimal(0))
                    tol = Decimal(5) * Decimal(10) ** (-_dp(tf["raw"]) - 1) * len(parts)
                    results.append((c, abs(s - total) <= tol, s, total, tf))
                ok = [x for x in results if x[1]]
                bad = [x for x in results if not x[1]]
                if verified is not None:
                    for c, good, _s, _t, tf in ok:
                        verified.add(tf["id"])
                        verified.update(matrix[k][c][1]["id"] for k in cands if matrix[k][c][1])
                if not ok:
                    continue            # not a simple sum in this table (e.g. derived line) — don't guess
                for c, _, s, total, tf in bad:
                    sev = "blocking" if len(ok) >= len(bad) else "warning"
                    issues.append(_fig_issue(
                        "arithmetic", sev, tf,
                        f"“{r.get('label_text')}” doesn't equal the sum of the {len(cands)} rows above it "
                        f"in this column (it does in {len(ok)} other column(s)).",
                        expected=_fmt(s), actual=tf["raw"]))
            # --- balance sheet balances
            a_row = next((i for i, r in enumerate(rows) if _ASSETS.match(r.get("label_text", "").strip())), None)
            l_row = next((i for i, r in enumerate(rows) if _EQ_LIAB.match(r.get("label_text", "").strip())), None)
            if a_row is not None and l_row is not None:
                for c in numeric_cols:
                    (a, af), (lv, lf) = matrix[a_row][c], matrix[l_row][c]
                    if a is not None and lv is not None and a != lv:
                        issues.append(_fig_issue("arithmetic", "blocking", lf,
                                                 "Total assets don't equal total equity and liabilities.",
                                                 expected=af["raw"], actual=lf["raw"]))
            # --- stated % changes
            issues.extend(_check_changes(schema, blk, matrix))
    return issues


def _fmt(d: Decimal) -> str:
    return format(d, "f")


def _prev_key(key: str, kind: str) -> str | None:
    m = re.match(r"^(quarter|half|nine_months|year)(\d?):FY(\d{4})$", key)
    if not m:
        return None
    t, n, fy = m.group(1), m.group(2), int(m.group(3))
    if kind in ("yoy", "change"):
        return f"{t}{n}:FY{fy - 1}"
    if kind == "qoq" and t == "quarter":
        q = int(n)
        return f"quarter{q - 1}:FY{fy}" if q > 1 else f"quarter4:FY{fy - 1}"
    return None


def _check_changes(schema: dict, blk: dict, matrix) -> list[Issue]:
    issues = []
    cols = blk["columns"]
    for c, col in enumerate(cols):
        kind = col.get("change")
        if not kind:
            continue
        # current = the nearest period column to the left whose comparison period
        # (prior year for YoY, prior quarter for QoQ) is also in the table
        keyed = {cc["period"]["key"]: k for k, cc in enumerate(cols) if cc.get("period")}
        cur_c = prev_c = None
        for k in range(c - 1, -1, -1):
            if cols[k].get("period"):
                pk = _prev_key(cols[k]["period"]["key"], kind)
                if pk in keyed:
                    cur_c, prev_c = k, keyed[pk]
                    break
        if cur_c is None or prev_c is None:
            continue
        for i, r in enumerate(blk["rows"]):
            stated_f = None
            for run in r["cells"][c]["runs"] if c < len(r["cells"]) else []:
                if "f" in run:
                    f = schema["figures"][run["f"]]
                    if f["kind"] == "percent" and f.get("status") == "active":
                        stated_f = f
            (cur, cf), (prev, pf) = matrix[i][cur_c], matrix[i][prev_c]
            if stated_f is None or cur is None or prev is None or prev == 0:
                continue
            stated = Decimal(stated_f["value"])
            dp = _dp(stated_f["raw"])
            computed = (cur - prev) / abs(prev) * 100
            # rounding in the stated % plus rounding of the two inputs
            in_err = Decimal(5) * Decimal(10) ** (-max(_dp(cf["raw"]), 0) - 1)
            tol = Decimal(5) * Decimal(10) ** (-dp - 1) + (in_err * 2 / abs(prev) * 100)
            if abs(computed - stated) > tol:
                issues.append(_fig_issue(
                    "arithmetic", "warning", stated_f,
                    f"Stated change for “{r.get('label_text')}” doesn't match {cols[cur_c]['label']} vs {cols[prev_c]['label']}.",
                    expected=f"{computed.quantize(Decimal(10) ** -dp)}%", actual=stated_f["raw"]))
    return issues


# ------------------------------------------------------------------ 4. period & unit consistency


def check_period_unit(schema: dict) -> list[Issue]:
    issues = []
    meta = schema["metadata"]
    mentioned = set(schema.get("units_mentioned", []))
    mixed = {"crore", "lakh"} <= mentioned
    for sec in schema["sections"]:
        for blk in sec["blocks"]:
            if blk["type"] != "table":
                continue
            src = blk["source"]
            has_numbers = any(schema["figures"][r["f"]]["kind"] == "number"
                              for row in blk["rows"] for cell in row["cells"] for r in cell["runs"] if "f" in r)
            if not has_numbers:
                continue
            if blk.get("unit_source") != "caption":
                sev = "blocking" if mixed else "warning"
                msg = ("This document uses both crore and lakh, and this table has no unit label."
                       if mixed else f"This table has no unit label; assumed the report unit ({meta['reporting_unit']}).")
                issues.append(Issue("period_unit", sev, msg, None, src["page"], sec["id"], src["bbox"],
                                    expected="a unit label such as “₹ in crore”", actual="none"))
            elif blk["unit"] != meta.get("reporting_unit_canonical"):
                issues.append(Issue("period_unit", "warning",
                                    f"This table is in {blk['unit']}, but the report unit is {meta['reporting_unit']}.",
                                    None, src["page"], sec["id"], src["bbox"],
                                    expected=meta["reporting_unit"], actual=blk["unit"]))
            for c, col in enumerate(blk["columns"]):
                if col.get("period") or col.get("change"):
                    continue
                if any(c < len(r["cells"]) and r["cells"][c]["runs"] for r in blk["rows"]):
                    issues.append(Issue("period_unit", "warning",
                                        f"Column {c + 1} (“{col['label'] or 'no header'}”) has no identifiable period.",
                                        None, src["page"], sec["id"], src["bbox"]))
            for row in blk["rows"]:
                for c, cell in enumerate(row["cells"]):
                    for r in cell["runs"]:
                        if "f" not in r:
                            continue
                        f = schema["figures"][r["f"]]
                        colp = blk["columns"][c].get("period") if c < len(blk["columns"]) else None
                        if colp and f.get("period") and f["period"].get("key") != colp.get("key"):
                            issues.append(_fig_issue("period_unit", "blocking", f,
                                                     "This value's period doesn't match its column header.",
                                                     expected=colp["raw"], actual=f["period"]["raw"]))
    return issues


# ------------------------------------------------------------------ 6. completeness


def check_completeness(schema: dict, doc: pymupdf.Document, artifact: dict) -> list[Issue]:
    issues = []
    covered: dict[int, int] = defaultdict(int)
    for f in schema["figures"].values():
        covered[f["source"]["page"]] += sum(1 for part in f["raw"].split(" ") if has_digit(part))
    excluded = {p["page"]: p.get("excluded_header_footer", []) for p in schema["pages"]}
    for p in artifact["pages"]:
        n = p["page"]
        if p["method"] == "ocr":
            words = [(w["t"], w["b"]) for w in p["words"]]
        else:
            from app.extraction.pdf import text_layer_words
            words = [(w.text, list(w.bbox)) for w in text_layer_words(doc[n - 1])[0]]
        ex_boxes = [e["bbox"] for e in excluded.get(n, [])]
        if p.get("hidden_numbers"):
            issues.append(Issue("completeness", "warning",
                                f"Page {n} has {p['hidden_numbers']} number(s) printed too small to read (a hidden text layer, "
                                "usually behind a chart image). They were left out, since no reader can see them.",
                                None, n, None, None))
        images = p.get("image_regions", [])

        def inside(b, boxes, pad=2.0):
            cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            return any(x0 - pad <= cx <= x1 + pad and y0 - pad <= cy <= y1 + pad for x0, y0, x1, y1 in boxes)

        total = sum(1 for t, b in words if has_digit(t) and not inside(b, ex_boxes))
        in_images = sum(1 for t, b in words if has_digit(t) and inside(b, images))
        gap = total - covered[n]
        if gap > 0:
            issues.append(Issue("completeness", "warning",
                                f"Page {n}: {gap} of {total} numbers in the PDF text weren't captured.",
                                None, n, None, None, expected=str(total), actual=str(covered[n])))
        del in_images
    for sec in schema["sections"]:
        for blk in sec["blocks"]:
            if blk["type"] == "chart" and not blk.get("extracted"):
                issues.append(Issue("completeness", "warning",
                                    "An image on this page (a chart, photo or logo) isn't reproduced on the web page; if it "
                                    "contains figures, they aren't included. The page links to the PDF for it.", None, blk["source"]["page"], sec["id"],
                                    blk["source"]["bbox"]))
    return issues


# ------------------------------------------------------------------ 7. low confidence


# A low-confidence OCR value still needs a person unless an independent re-read of the
# same spot agreed with it exactly — and even then not when the first read was a guess.
AUTO_CLEAR_MIN_CONFIDENCE = 0.5


def check_low_confidence(schema: dict, threshold: float, reread_ok: set[str] | None = None) -> list[Issue]:
    from app.extraction.tokens import YEAR_RANGE
    issues = []
    reread_ok = reread_ok or set()
    for f in _active(schema):
        if f.get("unparsed_numeric") and YEAR_RANGE.match(f["raw"].strip()):
            continue          # a span of years (tenure, financial year) — flagged by older extractions
        if f.get("unparsed_numeric"):
            issues.append(_fig_issue("low_confidence", "blocking", f,
                                     "This looks like a number but couldn't be read as one. Check and correct it.",
                                     actual=f["raw"]))
        elif f["method"] == "ocr" and f["confidence"] < threshold and f["kind"] in REEXTRACT_KINDS \
                and f["id"] in reread_ok and f["confidence"] >= AUTO_CLEAR_MIN_CONFIDENCE:
            issues.append(_fig_issue("low_confidence", "warning", f,
                                     f"Read from a scanned page with low confidence ({f['confidence']:.0%}), but an "
                                     "independent re-read of the same spot agrees exactly.", actual=f["raw"]))
        elif f["method"] == "ocr" and f["confidence"] < threshold and f["kind"] in REEXTRACT_KINDS:
            issues.append(_fig_issue("low_confidence", "blocking", f,
                                     f"Read from a scanned page with low confidence ({f['confidence']:.0%}). "
                                     "A person must confirm it.", actual=f["raw"]))
        elif f["kind"] == "date" and f.get("ambiguous"):
            issues.append(_fig_issue("low_confidence", "warning", f,
                                     "This date could be read day-first or month-first; confirm it.",
                                     expected=f.get("iso"), actual=f["raw"]))
    return issues


# ------------------------------------------------------------------ 5. rendered page


class _PageScan(html.parser.HTMLParser):
    """Collects every digit-bearing piece of visible text with the figure/meta context
    it sits in, the exact text of every data-fig element, display attributes, and
    JSON-LD blocks."""

    VOID = {"br", "img", "meta", "link", "hr", "input", "source", "col", "wbr", "area", "base"}
    ATTRS = ("title", "alt", "aria-label", "placeholder")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[dict] = []
        self.found: list[tuple[str, str | None, str | None, bool]] = []   # (text, fid, meta, nocheck)
        self.fig_elems: list[dict] = []                                    # {"fid", "texts"}
        self.attr_texts: list[str] = []
        self.jsonld: list[str] = []
        self._script: str | None = None           # tag name while inside script/style
        self._script_buf: list[str] | None = None  # collecting JSON-LD

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        if tag == "meta" and (a.get("name") == "description" or a.get("property") in ("og:title", "og:description")):
            self.attr_texts.append(a.get("content", ""))
        for k in self.ATTRS:
            if a.get(k):
                self.attr_texts.append(a[k])
        if tag in ("script", "style"):
            self._script = tag
            self._script_buf = [] if a.get("type") == "application/ld+json" else None
            return
        if tag in self.VOID:
            return
        entry = {"tag": tag, "attrs": a}
        if "data-fig" in a:
            entry["texts"] = []
            self.fig_elems.append({"fid": a["data-fig"], "texts": entry["texts"]})
        self.stack.append(entry)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            if self._script_buf is not None:
                self.jsonld.append("".join(self._script_buf))
            self._script, self._script_buf = None, None
            return
        if tag in self.VOID:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self._script:
            if self._script_buf is not None:
                self._script_buf.append(data)
            return
        if self.stack and self.stack[-1]["tag"] == "title":
            self.attr_texts.append(data)          # <title> may only carry report labels
            return
        fid = meta = None
        nocheck = False
        for entry in reversed(self.stack):
            a = entry["attrs"]
            if fid is None and "data-fig" in a:
                fid = a["data-fig"]
                entry["texts"].append(data)
            if meta is None and "data-meta" in a:
                meta = a["data-meta"]
            if "data-nocheck" in a:
                nocheck = True
        if has_digit(data):
            self.found.append((data, fid, meta, nocheck))


def check_rendered_html(schema: dict, html_text: str, page_name: str) -> list[Issue]:
    issues = []
    meta = schema["metadata"]
    meta_values = {k: str(v) for k, v in meta.items() if isinstance(v, (str, int))}
    allowed_meta_tokens = {t for v in meta_values.values() for t in re.split(r"\s+", v) if has_digit(t)}
    scan = _PageScan()
    scan.feed(html_text)
    figs = schema["figures"]

    def bad(msg, actual, fid=None):
        f = figs.get(fid) if fid else None
        issues.append(Issue("rendered_page", "blocking", f"{page_name}: {msg}", fid,
                            f["source"]["page"] if f else None, f.get("section_id") if f else None,
                            f["source"]["bbox"] if f else None,
                            expected=f["raw"] if f else None, actual=actual))

    for text, fid, mkey, nocheck in scan.found:
        if nocheck:
            continue
        if fid:
            if fid not in figs:
                bad("a value on the page references a figure that doesn't exist.", text.strip())
            continue          # exact-text check below
        if mkey:
            if mkey not in meta_values or text.strip() != meta_values[mkey]:
                bad("a report label on the page doesn't match the report metadata.", text.strip())
            continue
        for tok in re.findall(r"\S*\d\S*", text):
            bad("a number on the page isn't in the schema.", tok)
    # exact text of every figure element
    for el in scan.fig_elems:
        fid = el["fid"]
        shown = "".join(el["texts"]).strip()
        f = figs.get(fid)
        if f is None:
            continue
        if f.get("status") != "active":
            if has_digit(shown) and shown != f["raw"]:
                bad("an excluded value is shown differently from the PDF.", shown, fid)
            continue
        if shown != f["raw"].strip():
            bad("a value on the page differs from the PDF.", shown, fid)
    for t in scan.attr_texts:
        for tok in re.findall(r"\S*\d\S*", t):
            if tok.strip(",.;:()") not in allowed_meta_tokens:
                bad("a number in page metadata/attributes isn't a report label.", tok)
    for block in scan.jsonld:
        issues.extend(_check_jsonld(block, allowed_meta_tokens, page_name))
    return issues


_URLISH = re.compile(r"^(https?:)?//|^/")


def _check_jsonld(text: str, allowed: set[str], page_name: str) -> list[Issue]:
    import json

    issues = []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [Issue("rendered_page", "blocking", f"{page_name}: structured data is not valid JSON.")]

    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                if k in ("@context", "@type", "url", "@id", "contentUrl", "sameAs", "encodingFormat", "isBasedOn"):
                    continue
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, (int, float)):
            if str(v) not in allowed:
                issues.append(Issue("rendered_page", "blocking", f"{page_name}: structured data has a number not in the report labels.", actual=str(v)))
        elif isinstance(v, str) and has_digit(v) and not _URLISH.match(v):
            for tok in re.findall(r"\S*\d\S*", v):
                if tok.strip(",.;:()") not in allowed:
                    issues.append(Issue("rendered_page", "blocking", f"{page_name}: structured data has a number not in the report labels.", actual=tok))
    walk(data)
    return issues


def check_markdown(schema: dict, md: str) -> list[Issue]:
    raws = {f["raw"] for f in schema["figures"].values()}
    raw_parts = {part for r in raws for part in r.split(" ")}
    meta_tokens = {t for v in schema["metadata"].values() if isinstance(v, (str, int)) for t in str(v).split() if has_digit(t)}
    issues = []
    for line in md.splitlines():
        if line.startswith("<!--") or "](" in line and line.strip().startswith("["):
            line = re.sub(r"\]\([^)]*\)", "]", line)
        line = re.sub(r"\]\([^)]*\)", "]", line)
        for tok in re.findall(r"[^\s|]*\d[^\s|]*", line):
            core = tok.strip("*_`,;:")
            stripped = _split_punct(core)[1]
            if {tok, tok.rstrip(",;:"), core, stripped} & (raws | raw_parts | meta_tokens):
                continue
            issues.append(Issue("rendered_page", "blocking", "report.md: a number isn't in the schema.", actual=tok))
    return issues


def check_bundle(schema: dict, files: dict[str, bytes]) -> list[Issue]:
    issues = []
    for name, data in sorted(files.items()):
        if name.endswith(".html"):
            issues.extend(check_rendered_html(schema, data.decode("utf-8"), name))
        elif name.endswith(".md"):
            issues.extend(check_markdown(schema, data.decode("utf-8")))
    return issues


# ------------------------------------------------------------------ orchestration


AGENTS = [
    ("traceability", "Source trace", "Finds each value at its recorded position in the PDF's text layer (or OCR record)."),
    ("second_parser", "Second PDF parser", "Re-reads every position with a different PDF engine (pdfminer) than the extractor."),
    ("re_extraction", "Visual re-read", "Renders the page as an image and reads each figure again with OCR."),
    ("arithmetic", "Arithmetic", "Totals add up, the balance sheet balances, stated % changes match."),
    ("cross_table", "Cross-table consistency", "The same line and period has the same value in every table."),
    ("period_unit", "Periods & units", "Each value's period and unit match its table; no crore/lakh mix-ups."),
    ("rendered_page", "Web page check", "Every number on the generated page exists in the data, character for character."),
    ("completeness", "Completeness", "Numbers on each PDF page vs numbers captured."),
    ("low_confidence", "Human review", "Low-confidence OCR values need a person, unless an independent re-read agrees exactly."),
    ("schema", "Schema", "The data document matches the published JSON Schema."),
]


def _check_pool(schema: dict):
    """A process pool for the large-report checks; None (run in-process) for small reports
    or where processes can't be started (sandbox, serverless)."""
    if sum(1 for f in _active(schema) if f["kind"] in REEXTRACT_KINDS) < PARALLEL_REREAD_MIN:
        return None
    try:
        import multiprocessing as mp
        import os
        from concurrent.futures import ProcessPoolExecutor
        from app.runtime import process_workers
        return ProcessPoolExecutor(max_workers=max(2, process_workers(cap=8)),
                                   mp_context=mp.get_context("spawn"))
    except Exception:  # noqa: BLE001
        return None


def _safe_reads(schema: dict, pdf_bytes: bytes, pool) -> dict[str, list[str]] | None:
    """OCR readings from the pool; None (so the check reads in-process) if the pool fails."""
    try:
        return reextraction_reads_parallel(schema, pdf_bytes, pool)
    except Exception:  # noqa: BLE001
        return None


def run_all(schema: dict, pdf_bytes: bytes, artifact: dict, bundle_files: dict[str, bytes] | None,
            *, threshold: float, reextract: bool = True, progress=None) -> tuple[list[Issue], dict[str, Any]]:
    """Runs every validation agent. Each positive confirmation is tallied per figure, so
    the summary can say how many independent agents agreed on each value."""
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    verified: dict[str, set[str]] = {k: set() for k, *_ in AGENTS}
    steps = []

    def step(name, fn):
        if progress:
            progress(name, len(steps) + 1)
        steps.append(name)
        return fn()

    issues: list[Issue] = [Issue("schema", "blocking", f"Schema document invalid: {e}") for e in schema_errors(schema)]
    # The slow checks — the second parser and the OCR readings — don't depend on each other,
    # so on a large report they run side by side in other processes while the rest goes on.
    pool = _check_pool(schema) if reextract else None
    try:
        sp_future = None
        if pool:
            import threading
            sp_box: dict = {}
            sp_ids: set[str] = set()
            sp_th = threading.Thread(target=lambda: sp_box.update(r=check_second_parser(schema, pdf_bytes, artifact, sp_ids, pool=pool)),
                                     daemon=True)
            sp_th.start()
            sp_future = (sp_th, sp_box, sp_ids)
        reads_future = None
        if pool:
            import threading
            box: dict = {}
            th = threading.Thread(target=lambda: box.update(r=_safe_reads(schema, pdf_bytes, pool)), daemon=True)
            th.start()
            reads_future = (th, box)
        issues += step("Source trace", lambda: check_traceability(schema, doc, artifact, verified["traceability"]))

        def second_parser():
            if sp_future is None:
                return check_second_parser(schema, pdf_bytes, artifact, verified["second_parser"])
            sp_future[0].join()
            verified["second_parser"].update(sp_future[2])
            return sp_future[1].get("r", [])
        issues += step("Second PDF parser", second_parser)
        if reextract:
            def reread():
                reads = None
                if reads_future:
                    reads_future[0].join()
                    reads = reads_future[1].get("r")
                return check_reextraction(schema, doc, verified=verified["re_extraction"],
                                          text_confirmed=verified["traceability"] & verified["second_parser"], reads=reads)
            issues += step("Visual re-read", reread)
    finally:
        if pool:
            pool.shutdown(wait=False, cancel_futures=True)
    issues += step("Arithmetic", lambda: check_arithmetic(schema, verified["arithmetic"]))
    issues += step("Cross-table consistency", lambda: check_cross_tables(schema, verified["cross_table"]))
    issues += step("Periods & units", lambda: check_period_unit(schema))
    if bundle_files is not None:
        issues += step("Web page check", lambda: check_bundle(schema, bundle_files))
    issues += step("Completeness", lambda: check_completeness(schema, doc, artifact))
    issues += step("Human review", lambda: check_low_confidence(
        schema, threshold, reread_ok=verified["re_extraction"] if reextract else None))
    summary = summarize(schema, issues)
    has_text_layer = any(p["method"] == "text_layer" for p in artifact["pages"])
    summary["agents"] = _agent_summary(schema, issues, verified, ran={
        "re_extraction": reextract, "rendered_page": bundle_files is not None,
        "second_parser": has_text_layer})
    summary["consensus"] = _consensus(schema, verified)
    return issues, summary


def _agent_summary(schema: dict, issues: list[Issue], verified: dict[str, set[str]], ran: dict[str, bool]) -> list[dict]:
    out = []
    for key, name, desc in AGENTS:
        mine = [i for i in issues if i.check == key]
        out.append({"key": key, "name": name, "description": desc, "ran": ran.get(key, True),
                    "confirmed": len(verified.get(key, ())),
                    "blocking": sum(1 for i in mine if i.severity == "blocking"),
                    "warnings": sum(1 for i in mine if i.severity == "warning")})
    return out


def _consensus(schema: dict, verified: dict[str, set[str]]) -> dict:
    """How many independent agents positively confirmed each numeric figure."""
    counts = {"3+": 0, "2": 0, "1": 0, "0": 0}
    for f in _active(schema):
        if f["kind"] not in NUMERIC_KINDS:
            continue
        n = sum(1 for k in ("traceability", "second_parser", "re_extraction", "arithmetic", "cross_table")
                if f["id"] in verified.get(k, ()))
        counts["3+" if n >= 3 else str(n)] += 1
    return counts


def issue_key(check: str, page: int | None, bbox: list | None, message: str) -> str:
    import hashlib
    import json
    return hashlib.sha256(json.dumps([check, page, bbox, message]).encode()).hexdigest()[:24]


REVIEW_ONLY_KEYS = ("review", "ai_check")      # per-figure: people's and the AI's decisions


def checked_content_sha(schema: dict) -> str:
    """Hash of everything the checks read: the schema minus review decisions (a confirmed
    figure, an acknowledged note). Same hash + same rendered bundle = the checks would
    find exactly the same things again; only which of them are resolved can differ."""
    import hashlib
    import json
    figs = {k: {fk: fv for fk, fv in f.items() if fk not in REVIEW_ONLY_KEYS} for k, f in schema["figures"].items()}
    core = {**{k: v for k, v in schema.items() if k not in ("figures", "acknowledged")}, "figures": figs}
    return hashlib.sha256(json.dumps(core, sort_keys=True, default=str).encode()).hexdigest()


def apply_reviews(issues: list[Issue], schema: dict) -> None:
    """A figure a person confirmed (at its current raw value) resolves that figure's
    issues from checks that compare against the PDF. Rendered-page and schema issues
    are platform faults and can never be waived by a reviewer."""
    acknowledged = schema.get("acknowledged", {})
    for i in issues:
        if i.check in ("rendered_page", "schema"):
            continue
        if not i.fid:
            if issue_key(i.check, i.page, i.bbox, i.message) in acknowledged:
                i.status, i.resolution = "resolved", "acknowledged"
            continue
        f = schema["figures"].get(i.fid)
        rv = f.get("review") if f else None
        if rv and rv.get("action") == "confirm" and rv.get("raw") == f["raw"]:
            i.status, i.resolution = "resolved", "confirmed"


def summarize(schema: dict, issues: list[Issue]) -> dict[str, Any]:
    active = _active(schema)
    open_ = [i for i in issues if i.status == "open"]
    bad_fids = {i.fid for i in open_ if i.fid}
    return {
        "figures_checked": len(active),
        "passed": sum(1 for f in active if f["id"] not in bad_fids),
        "warnings": sum(1 for i in open_ if i.severity == "warning"),
        "blocking": sum(1 for i in open_ if i.severity == "blocking"),
        "resolved": sum(1 for i in issues if i.status == "resolved"),
        "by_check": {c: sum(1 for i in open_ if i.check == c) for c in sorted({i.check for i in issues})},
    }


def issue_dict(i: Issue) -> dict:
    return asdict(i)
