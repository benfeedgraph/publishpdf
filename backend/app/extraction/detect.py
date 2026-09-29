"""Guess report metadata from the PDF so the upload form comes pre-filled.

These are suggestions for the person uploading to confirm, never silent facts: the
confirmed values are what the report is filed under.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date

import pymupdf

from app.extraction.dates import parse_date, parse_period
from app.extraction.tokens import detect_unit

_COMPANY = re.compile(r"\b([A-Z][A-Za-z0-9&.,'\- ]{2,80}?\s(?:Limited|Ltd\.?|Inc\.?|Corporation|Corp\.?|plc|PLC|LLP|AG|SA|N\.V\.|Holdings))\b")
_PERIOD_TOKEN = re.compile(r"\b(Q[1-4]|H[12]|9M)\s?[-']?\s?(FY\s?'?\d{2,4}(?:\s?[-/]\s?\d{2,4})?)\b", re.I)
_FY_TOKEN = re.compile(r"\bFY\s?'?(\d{4}\s?[-/]\s?\d{2,4}|\d{2,4})\b", re.I)
_ENDED = re.compile(r"(quarter|three months|half[- ]year|six months|nine months|year)\s+ended\s+(?:on\s+)?"
                    r"([0-9]{1,2}(?:st|nd|rd|th)?[\s./-][A-Za-z0-9]{2,9}[\s.,/-]+[0-9]{2,4}"
                    r"|[A-Za-z]{3,9}\.?\s[0-9]{1,2},?\s*[0-9]{4})", re.I)


def _fy_of(d: date, fy_start_month: int = 4) -> int:
    """Indian-style fiscal year (April–March) by default: Sep 2025 -> FY2026."""
    return d.year + 1 if d.month >= fy_start_month else d.year


def _quarter_of(d: date, fy_start_month: int = 4) -> int:
    return ((d.month - fy_start_month) % 12) // 3 + 1


def _top_line(page: pymupdf.Page) -> str | None:
    """Running header on page 1, e.g. 'Acme Industries | Investor Presentation' -> 'Acme Industries'."""
    lines = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            t = "".join(s["text"] for s in ln["spans"]).strip()
            if t and ln["bbox"][1] < page.rect.height * 0.08:
                lines.append((ln["bbox"][1], t))
    for _, t in sorted(lines):
        part = re.split(r"\s[|·•–-]\s", t)[0].strip()
        part = re.sub(r"\s*\(.*?\)\s*", " ", part).strip()
        if len(part) >= 3 and sum(ch.isalpha() for ch in part) > len(part) / 2:
            return part[:120]
    return None


def detect_metadata(pdf_bytes: bytes, max_pages: int = 6) -> dict:
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    pages = [doc[i] for i in range(min(max_pages, doc.page_count))]
    text = "\n".join(p.get_text() for p in pages)
    first = pages[0] if pages else None
    if len(text.strip()) < 40 and first is not None:          # scanned: read page 1 with OCR
        from app.extraction import ocr
        text = " ".join(w.text for w in ocr.ocr_page(first))
    text = re.sub(r"[ \t]*\n[ \t]*", " ", text)                    # line breaks inside names
    text = re.sub(r"\s{2,}", " ", text)
    out: dict = {"page_count": doc.page_count, "confidence": {}}

    # --- company: explicit legal name, else the biggest text on page 1
    m = _COMPANY.search(text)
    if m:
        out["company_name"] = re.sub(r"\s+", " ", m.group(1)).strip(" ,.-")
        out["confidence"]["company_name"] = "high"
    elif first is not None and (top := _top_line(first)):
        out["company_name"] = top
        out["confidence"]["company_name"] = "medium"
    elif first is not None:
        spans = [(s["size"], s["text"].strip()) for b in first.get_text("dict")["blocks"] if b.get("type") == 0
                 for ln in b["lines"] for s in ln["spans"] if len(s["text"].strip()) > 2]
        spans = [s for s in spans if not any(ch.isdigit() for ch in s[1])]
        if spans:
            out["company_name"] = max(spans)[1][:120]
            out["confidence"]["company_name"] = "low"

    # --- report type
    low = text.lower()
    landscape = first is not None and first.rect.width > first.rect.height
    if "annual report" in low or "integrated report" in low:
        out["report_type"] = "annual_report"
    elif "investor presentation" in low or "earnings presentation" in low or (landscape and doc.page_count < 80):
        out["report_type"] = "investor_presentation"
    elif re.search(r"results|quarter ended|financial results|profit and loss", low):
        out["report_type"] = "quarterly_results"
    else:
        out["report_type"] = "other"

    # --- period and fiscal year
    periods = Counter()
    for pm in _PERIOD_TOKEN.finditer(text):
        p = parse_period(f"{pm.group(1)} {pm.group(2)}")
        if p and p.fiscal_year:
            periods[(p.type, p.number, p.fiscal_year)] += 1
    ended = []
    for em in _ENDED.finditer(text):
        d = parse_date(em.group(2))
        if d and d.precision == "day":
            ended.append((em.group(1).lower(), date.fromisoformat(d.iso)))
    if periods:
        (ptype, num, fy), _ = max(periods.items(), key=lambda kv: (kv[1], kv[0][2]))
        out["fiscal_year"] = fy
        out["period"] = {"quarter": f"q{num}", "half": f"h{num}", "nine_months": "9m", "year": "fy"}[ptype]
        out["confidence"]["period"] = "high"
    elif ended:
        what, d = max(ended, key=lambda e: e[1])
        out["fiscal_year"] = _fy_of(d)
        out["period"] = ("fy" if what == "year" else "9m" if "nine" in what else
                         f"h{1 if _quarter_of(d) <= 2 else 2}" if ("half" in what or "six" in what) else f"q{_quarter_of(d)}")
        out["confidence"]["period"] = "medium"
    else:
        fys = [int(x) for x in re.findall(r"\bFY\s?'?(\d{2})\b", text, re.I)]
        if fys:
            out["fiscal_year"] = 2000 + Counter(fys).most_common(1)[0][0]
            out["period"] = "fy"
            out["confidence"]["period"] = "low"
    if out.get("report_type") == "annual_report":
        out["period"] = "fy"

    # --- currency and unit
    inr = len(re.findall(r"₹|\bRs\.?\s|\bINR\b|crore|lakh", text))
    usd = len(re.findall(r"US\$|\bUSD\b|\$\s?\d", text))
    eur = len(re.findall(r"€|\bEUR\b", text))
    gbp = len(re.findall(r"£|\bGBP\b", text))
    cur = max((("INR", inr), ("USD", usd), ("EUR", eur), ("GBP", gbp)), key=lambda c: c[1])
    if cur[1]:
        out["currency"] = cur[0]
    units = Counter(u for line in text.splitlines() for u in [detect_unit(line)[0]] if u)
    if units:
        unit = units.most_common(1)[0][0]
        symbol = {"INR": "₹", "USD": "USD", "EUR": "EUR", "GBP": "GBP"}.get(out.get("currency", ""), "")
        out["reporting_unit"] = f"{symbol} {unit}".strip()
    return out
