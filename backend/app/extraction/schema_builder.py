"""Build the report schema document from a PDF.

Figures flow  extraction -> registry -> schema -> page  unchanged. Blocks reference
figures by id; the value exists in exactly one place (`figures[fid]`).
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
from datetime import datetime, timezone
from typing import Any

import pymupdf

from app.extraction import classify, layout
from app.extraction.dates import ParsedDate, Period, parse_period
from app.extraction.numbers import ParsedNumber, has_digit
from app.extraction.pdf import PageData, Word
from app.extraction.tokens import (
    Token,
    Tokenized,
    all_units_mentioned,
    assert_no_loose_digits,
    detect_unit,
    tokenize,
)

PIPELINE_VERSION = "1.0.0"
SCHEMA_VERSION = "1.0"

REPORT_TYPES = ("quarterly_results", "investor_presentation", "annual_report", "other")


def _r(v: float) -> float:
    return round(v, 2)


def _bbox(b) -> list[float]:
    return [_r(b[0]), _r(b[1]), _r(b[2]), _r(b[3])]


class Builder:
    def __init__(self, metadata: dict[str, Any], pages: list[PageData]):
        self.meta = metadata
        self.pages = {p.number: p for p in pages}
        self.figures: dict[str, dict] = {}
        self._counter: dict[int, int] = {}
        self.section_units: list[str] = []

    # ------------------------------------------------------------------ figures

    def _new_fid(self, page: int) -> str:
        self._counter[page] = self._counter.get(page, 0) + 1
        return f"p{page}f{self._counter[page]}"

    def figure(self, tok: Token, page: int, *, section_id: str, table_id: str | None = None,
               row_label: str | None = None, col_label: str | None = None, period: Period | None = None,
               unit: str | None = None, currency: str | None = None, role: str = "text") -> str:
        fid = self._new_fid(page)
        method = self.pages[page].method
        entry: dict[str, Any] = {
            "id": fid,
            "kind": tok.kind,
            "raw": tok.raw.strip(),
            "value": None,
            "iso": None,
            "unit": None,
            "currency": None,
            "period": period.as_json() if period else None,
            "row_label": row_label,
            "col_label": col_label,
            "table_id": table_id,
            "section_id": section_id,
            "role": role,
            "source": {"page": page, "bbox": _bbox(tok.source_bbox)},
            "method": method,
            "confidence": round(tok.conf, 3),
            "status": "active",
            "review": None,
        }
        p = tok.parsed
        if isinstance(p, ParsedNumber):
            entry["value"] = p.value_str
            entry["negative"] = p.negative
            entry["grouping"] = p.grouping
            if p.kind in ("number", "nil"):
                entry["unit"] = tok.unit_hint or unit
                cur = currency
                if tok.currency_hint or p.currency_symbol:
                    from app.extraction.tokens import CURRENCY_CODES
                    sym = (tok.currency_hint or p.currency_symbol or "").lower()
                    cur = CURRENCY_CODES.get(sym, cur)
                entry["currency"] = cur
            elif p.kind == "percent":
                entry["unit"] = "percent"
            elif p.kind == "bps":
                entry["unit"] = "bps"
            elif p.kind == "multiple":
                entry["unit"] = "x"
        elif isinstance(p, ParsedDate):
            entry["iso"] = p.iso
            entry["precision"] = p.precision
            entry["ambiguous"] = p.ambiguous
        elif isinstance(p, Period):
            entry["iso"] = p.end_date
            entry["period"] = p.as_json()
        elif role == "cell":
            from app.extraction.tokens import looks_numeric_unparsed
            if looks_numeric_unparsed(tok.raw):
                entry["unparsed_numeric"] = True     # e.g. OCR read '55-00' — needs human check
        # A figure whose words wrap onto another line keeps one box per line, so it can
        # be re-read visually line by line.
        if len(tok.words) > 1:
            lines: list[list] = []
            for w in tok.words:
                b = w.source_bbox
                if lines and abs(((b[1] + b[3]) / 2) - ((lines[-1][1] + lines[-1][3]) / 2)) < (b[3] - b[1]) * 0.6:
                    lb = lines[-1]
                    lines[-1] = [min(lb[0], b[0]), min(lb[1], b[1]), max(lb[2], b[2]), max(lb[3], b[3])]
                else:
                    lines.append(list(b))
            if len(lines) > 1:
                entry["source"]["parts"] = [_bbox(b) for b in lines]
        self.figures[fid] = entry
        return fid

    def runs(self, t: Tokenized, page: int, **kw) -> list[dict]:
        assert_no_loose_digits(t)
        out = []
        for r in t.runs:
            if r.token is not None:
                out.append({"f": self.figure(r.token, page, **kw)})
            elif r.text:
                out.append({"t": r.text})
        return out

    # ------------------------------------------------------------------ blocks

    def text_block(self, el: layout.TextEl, page: int, section_id: str, bid: str) -> dict:
        words: list[Word] = []
        for ln in el.lines:
            words.extend(ln)
        runs = self.runs(tokenize(words), page, section_id=section_id)
        return {"id": bid, "type": "paragraph", "runs": runs,
                "source": {"page": page, "bbox": _bbox(el.bbox)}}

    def table_block(self, el: layout.TableEl, page: int, section_id: str, bid: str) -> dict:
        caption_text = " ".join(w.text for w in el.caption_words)
        unit, currency = detect_unit(caption_text)
        unit_source = "caption" if unit else "report_metadata"
        if not unit:
            unit = self.meta["reporting_unit_canonical"]
        if not currency:
            currency = self.meta["currency"]
        if caption_text:
            self.section_units.append(caption_text)
        ncols = len(el.anchors)
        col_labels = [" ".join(w.text for w in el.header_label_words[c]) for c in range(ncols)]
        col_periods = [_period_from_label(lbl) for lbl in col_labels]
        header_rows = []
        for hr in el.header_rows:
            cells = []
            for cell in hr:
                cells.append({"colspan": cell.colspan,
                              "runs": self.runs(tokenize(cell.words, header=True), page,
                                                section_id=section_id, table_id=bid, role="header")})
            header_rows.append(cells)
        caption = self.runs(tokenize(el.caption_words), page, section_id=section_id, table_id=bid,
                            role="caption") if el.caption_words else []
        rows = []
        for label_words, cells in el.rows:
            label_text = " ".join(w.text for w in label_words)
            row_unit, row_cur = _row_unit(label_text, unit, currency)
            label_runs = self.runs(tokenize(label_words), page, section_id=section_id, table_id=bid,
                                   role="row_label") if label_words else []
            out_cells = []
            for c in range(ncols):
                cw = cells[c]
                if not cw:
                    out_cells.append({"runs": []})
                    continue
                change = _change_kind(col_labels[c])
                runs = self.runs(tokenize(cw, table_cell=True), page, section_id=section_id, table_id=bid,
                                 row_label=label_text, col_label=col_labels[c] or None,
                                 period=col_periods[c], unit=row_unit, currency=row_cur, role="cell")
                for r in runs:
                    if "f" in r and change:
                        self.figures[r["f"]]["change"] = change
                out_cells.append({"runs": runs})
            rows.append({"label": label_runs, "label_text": label_text, "cells": out_cells})
        return {
            "id": bid, "type": "table", "caption": caption, "unit": unit, "unit_source": unit_source,
            "currency": currency, "columns": [{"label": col_labels[c], "period": col_periods[c].as_json() if col_periods[c] else None,
                                                "change": _change_kind(col_labels[c])} for c in range(ncols)],
            "header_rows": header_rows, "rows": rows,
            "source": {"page": page, "bbox": _bbox(el.bbox)},
        }

    def stat_block(self, el: layout.StatEl, page: int, section_id: str, bid: str) -> dict:
        label_text = " ".join(w.text for w in el.label)
        return {"id": bid, "type": "stat",
                "label": self.runs(tokenize(el.label), page, section_id=section_id, role="text"),
                "value": self.runs(tokenize(el.value), page, section_id=section_id, row_label=label_text,
                                   unit=self.meta["reporting_unit_canonical"], currency=self.meta["currency"], role="text"),
                "label_text": label_text, "source": {"page": page, "bbox": _bbox(el.bbox)}}

    def chart_block(self, el: layout.ChartEl, page: int, section_id: str, bid: str) -> dict:
        if not el.extracted:
            return {"id": bid, "type": "chart", "extracted": False,
                    "note": "image only, not extracted",
                    "source": {"page": page, "bbox": _bbox(el.bbox)}}
        points = []
        for w in sorted(el.labels, key=lambda w: w.xc):
            axis = [a for a in el.axis if abs(a.xc - w.xc) < 40 and a.yc > w.yc]
            axis_words = sorted(axis, key=lambda a: (round(a.yc), a.bbox[0]))
            axis_label = " ".join(a.text for a in axis_words)
            period = parse_period(axis_label) if axis_label else None
            label_runs = self.runs(tokenize(axis_words, header=True), page, section_id=section_id,
                                   table_id=bid, role="chart_axis") if axis_words else []
            val_runs = self.runs(tokenize([w], table_cell=True), page, section_id=section_id, table_id=bid,
                                 col_label=axis_label or None, period=period,
                                 unit=self.meta["reporting_unit_canonical"], currency=self.meta["currency"],
                                 role="chart_label")
            points.append({"label": label_runs, "value": val_runs})
        return {"id": bid, "type": "chart", "extracted": True, "points": points,
                "note": "values read from the chart's data labels",
                "source": {"page": page, "bbox": _bbox(el.bbox)}}


def _period_from_label(label: str) -> Period | None:
    if not label:
        return None
    from app.extraction.dates import parse_as_at
    p = parse_period(label) or parse_as_at(label)
    if p:
        return p
    m = re.fullmatch(r"(?:.*\s)?((?:19|20)\d\d)", label.strip())
    if m:
        return Period(label, "year", int(m.group(1)))
    return None


def _change_kind(label: str) -> str | None:
    from app.extraction.dates import is_change_header
    return is_change_header(label) if label else None


def _row_unit(label: str, unit: str, currency: str) -> tuple[str, str]:
    lab = label.lower()
    if "(%)" in lab or lab.endswith("%") or "margin" in lab and "(" not in lab:
        return "percent", currency
    if "eps" in lab or "per share" in lab or "(₹)" in lab or "(rs" in lab or "(inr)" in lab or "($)" in lab:
        return "per_share" if ("eps" in lab or "per share" in lab) else "absolute", currency
    return unit, currency


UNIT_ALIASES = {"crore": "crore", "crores": "crore", "cr": "crore", "₹ crore": "crore", "lakh": "lakh",
                "lakhs": "lakh", "million": "million", "mn": "million", "billion": "billion", "thousand": "thousand"}


def canonical_unit(reporting_unit: str) -> str:
    u, _ = detect_unit(reporting_unit)
    return u or reporting_unit.strip().lower()


# ------------------------------------------------------------------ entry point


def extract(pdf_bytes: bytes, metadata: dict[str, Any], *, llm: classify.LlmCall | None = None,
            max_pages: int | None = None, progress=None) -> tuple[dict, dict]:
    """Returns (schema_document, extraction_artifact)."""
    from app.extraction.pdf import PdfError, open_pdf

    doc = open_pdf(pdf_bytes)
    if max_pages and doc.page_count > max_pages:
        raise PdfError(f"This PDF has {doc.page_count} pages; the limit is {max_pages}.")
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    from app.extraction.pdf import read_pages
    pages = read_pages(doc, progress=progress)
    exclude = layout.header_footer_words(pages)

    sizes = [w.size for p in pages if p.method == "text_layer" for w in p.words if w.size]
    body_size = statistics.median(sizes) if sizes else 0.0

    meta = dict(metadata)
    meta["reporting_unit_canonical"] = canonical_unit(metadata["reporting_unit"])
    b = Builder(meta, pages)

    sections: list[dict] = []
    current: dict | None = None
    block_no = 0
    page_info = []

    def new_section(heading_runs: list[dict] | None, heading_text: str, page: int, bbox) -> dict:
        s = {"id": f"s{len(sections) + 1}", "type": "other", "heading": heading_runs or [],
             "heading_text": heading_text, "source": {"page": page, "bbox": _bbox(bbox) if bbox else None},
             "blocks": []}
        sections.append(s)
        return s

    for p in pages:
        page_body = body_size if p.method == "text_layer" and body_size else \
            (statistics.median(w.h for w in p.words) if p.words else 0.0)
        els = layout.analyse_page(p, exclude[p.number], page_body)
        excluded_words = [w for w in p.words if id(w) in exclude[p.number]]
        page_info.append({
            "page": p.number, "width": _r(p.width), "height": _r(p.height), "method": p.method,
            "method_reason": p.text_layer_reason,
            "ocr_mean_confidence": round(statistics.mean(w.conf for w in p.words), 3) if p.method == "ocr" and p.words else None,
            "excluded_header_footer": [{"text": w.text, "bbox": _bbox(w.bbox)} for w in excluded_words],
        })
        for el in els:
            if isinstance(el, layout.TextEl) and el.kind == "heading":
                sid = f"s{len(sections) + 1}"
                runs = b.runs(tokenize(el.words), p.number, section_id=sid, role="heading")
                current = new_section(runs, " ".join(w.text for w in el.words), p.number, el.bbox)
                continue
            if current is None:
                current = new_section(None, "", p.number, None)
            block_no += 1
            bid = f"b{block_no}"
            if isinstance(el, layout.TableEl):
                current["blocks"].append(b.table_block(el, p.number, current["id"], bid))
            elif isinstance(el, layout.StatEl):
                current["blocks"].append(b.stat_block(el, p.number, current["id"], bid))
            elif isinstance(el, layout.ChartEl):
                current["blocks"].append(b.chart_block(el, p.number, current["id"], bid))
            else:
                current["blocks"].append(b.text_block(el, p.number, current["id"], bid))

    # Merge empty heading-only sections into the following one (e.g. a title line).
    merged: list[dict] = []
    for s in sections:
        # Only merge one level deep, so no heading text is ever dropped.
        # A numbered question is never folded into the one before it, even if that one
        # has no answer text of its own.
        if merged and not merged[-1]["blocks"] and merged[-1]["heading"] and s["heading"] \
                and not merged[-1].get("subheading") and not layout._QMARK.match(s["heading_text"]):
            prev = merged[-1]
            prev["subheading"] = s["heading"]
            prev["subheading_text"] = s["heading_text"]
            prev["blocks"] = s["blocks"]
            for fid, f in b.figures.items():
                if f["section_id"] == s["id"]:
                    f["section_id"] = prev["id"]
            continue
        merged.append(s)
    sections = merged

    for s in sections:
        s["sample_text"] = _sample(s)
    labels = classify.classify_sections(sections, llm)
    for i, s in enumerate(sections, start=1):
        s["type"] = labels[s["id"]]
        s["order"] = i
        s["slug"] = _slug(s["heading_text"]) or s["type"].replace("_", "-")
        s.pop("sample_text", None)
    _dedupe_slugs(sections)

    now = datetime.now(timezone.utc).isoformat()
    schema = {
        "schema_version": SCHEMA_VERSION,
        "metadata": {
            "company": metadata["company_name"],
            "report_type": metadata["report_type"],
            "fiscal_year": metadata["fiscal_year"],
            "period": metadata["period"],
            "period_label": period_label(metadata["period"], metadata["fiscal_year"]),
            "fiscal_year_label": f"FY{metadata['fiscal_year']}",
            "period_start": metadata.get("period_start"),
            "period_end": metadata.get("period_end"),
            "currency": metadata["currency"],
            "reporting_unit": metadata["reporting_unit"],
            "reporting_unit_canonical": meta["reporting_unit_canonical"],
            "source_pdf": {"sha256": sha, "filename": metadata.get("filename"), "page_count": doc.page_count},
            "extraction": {"timestamp": now, "pipeline_version": PIPELINE_VERSION,
                           "methods": {str(pi["page"]): pi["method"] for pi in page_info},
                           "llm_assist": llm is not None},
            "version": metadata.get("version", 1),
        },
        "pages": page_info,
        "sections": sections,
        "figures": b.figures,
        "units_mentioned": sorted({u for t in b.section_units for u in all_units_mentioned(t)}),
    }
    artifact = {
        "pipeline_version": PIPELINE_VERSION, "source_sha256": sha,
        "pages": [{"page": p.number, "method": p.method, "width": p.width, "height": p.height,
                   "words": [{"t": w.text, "b": _bbox(w.source_bbox), "c": round(w.conf, 3)} for w in p.words],
                   "image_regions": [_bbox(r) for r in p.image_regions],
                   "hidden_numbers": p.hidden_numbers,
                   "chart_regions": [_bbox(r) for r in p.chart_regions]} for p in pages],
    }
    return schema, artifact


def _sample(s: dict) -> str:
    texts = []
    for blk in s["blocks"][:2]:
        for r in blk.get("runs", [])[:20]:
            if "t" in r:
                texts.append(r["t"])
    return "".join(texts)[:300]


def _slug(text: str) -> str:
    t = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return t[:60].strip("-")


def _dedupe_slugs(sections: list[dict]) -> None:
    seen: dict[str, int] = {}
    for s in sections:
        base = s["slug"] or "section"
        if base in seen:
            seen[base] += 1
            s["slug"] = f"{base}-{seen[base] + 1}"
        else:
            seen[base] = 0


PERIOD_LABELS = {"q1": "Q1", "q2": "Q2", "q3": "Q3", "q4": "Q4", "h1": "H1", "h2": "H2", "9m": "9M", "fy": ""}


def period_label(period: str, fy: int) -> str:
    p = PERIOD_LABELS[period]
    return f"{p} FY{fy}".strip()


def schema_sha256(schema: dict) -> str:
    return hashlib.sha256(json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def figure_digits_in_runs(runs: list[dict]) -> bool:
    return any("t" in r and has_digit(r["t"]) for r in runs)


def render_page_png(pdf_bytes: bytes, page: int, zoom: float = 1.5) -> bytes:
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    return doc[page - 1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png")
