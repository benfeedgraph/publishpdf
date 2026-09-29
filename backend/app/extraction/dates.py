"""Date and reporting-period parsing.

Dates keep the raw string and get an ISO form. Numeric dates are read day-first
(Indian/UK convention) unless the report says otherwise; a numeric date where both
readings are valid and differ is flagged `ambiguous` rather than guessed silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"], start=1)}
MONTHS |= {k[:3]: v for k, v in list(MONTHS.items())}
MONTHS["sept"] = 9

_MON = r"(?P<mon>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
_DAY = r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
_YEAR = r"(?P<year>\d{4}|\d{2})"

_PATTERNS = [
    ("numeric", re.compile(r"^(?P<a>\d{1,2})[./-](?P<b>\d{1,2})[./-](?P<year>\d{4}|\d{2})$")),
    ("iso", re.compile(r"^(?P<year>\d{4})-(?P<mon>\d{2})-(?P<day>\d{2})$")),
    ("dmy", re.compile(rf"^{_DAY}[\s-]{_MON}[\s,-]*{_YEAR}$", re.I)),
    ("mdy", re.compile(rf"^{_MON}\s{_DAY},?\s{_YEAR}$", re.I)),
    ("my", re.compile(rf"^{_MON}[\s'-]*{_YEAR}$", re.I)),
]


@dataclass(frozen=True)
class ParsedDate:
    raw: str
    iso: str                          # YYYY-MM-DD, or YYYY-MM for month-only
    precision: Literal["day", "month"]
    ambiguous: bool = False


def _year(y: str) -> int:
    n = int(y)
    return n if len(y) == 4 else 2000 + n


def parse_date(raw: str, *, day_first: bool = True) -> ParsedDate | None:
    s = raw.strip().rstrip(",")
    for kind, pat in _PATTERNS:
        m = pat.match(s)
        if not m:
            continue
        try:
            if kind == "numeric":
                a, b, y = int(m["a"]), int(m["b"]), _year(m["year"])
                d1 = _mk(y, b, a) if day_first else _mk(y, a, b)
                d2 = _mk(y, a, b) if day_first else _mk(y, b, a)
                if d1 is None:
                    if d2 is None:
                        return None
                    return ParsedDate(raw, d2.isoformat(), "day", ambiguous=True)
                return ParsedDate(raw, d1.isoformat(), "day", ambiguous=d2 is not None and d2 != d1)
            if kind == "iso":
                d = _mk(int(m["year"]), int(m["mon"]), int(m["day"]))
                return ParsedDate(raw, d.isoformat(), "day") if d else None
            key = m["mon"].lower().rstrip(".")
            mon = MONTHS.get(key) or MONTHS[key[:3]]
            if kind == "my":
                return ParsedDate(raw, f"{_year(m['year']):04d}-{mon:02d}", "month")
            d = _mk(_year(m["year"]), mon, int(m["day"]))
            return ParsedDate(raw, d.isoformat(), "day") if d else None
        except (KeyError, ValueError):
            return None
    return None


def _mk(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


# --------------------------------------------------------------------------- periods

PeriodType = Literal["quarter", "half", "nine_months", "year", "as_at"]   # as_at = balance-sheet date


@dataclass(frozen=True)
class Period:
    raw: str
    type: PeriodType
    fiscal_year: int | None           # FY ending year, e.g. FY26 / FY2025-26 -> 2026
    number: int | None = None         # quarter or half number
    end_date: str | None = None       # ISO, when the header states it

    @property
    def key(self) -> str:
        if self.end_date and self.fiscal_year is None:
            return f"{self.type}@{self.end_date}"
        n = f"{self.number}" if self.number else ""
        return f"{self.type}{n}:FY{self.fiscal_year}"

    def as_json(self) -> dict:
        return {"raw": self.raw, "type": self.type, "fiscal_year": self.fiscal_year,
                "number": self.number, "end_date": self.end_date, "key": self.key}


def _fy(s: str) -> int:
    s = s.strip().replace("’", "'").lstrip("'")
    if "-" in s or "/" in s:                       # 2025-26, 2024-2025
        a, b = re.split(r"[-/]", s)[:2]
        b = b.strip()
        return int(b) if len(b) == 4 else int(a[:2] + b) if len(a) == 4 else 2000 + int(b)
    return int(s) if len(s) == 4 else 2000 + int(s)


_FYNUM = r"(?:FY|F\.Y\.?)\s?'?(?P<fy>\d{4}\s?[-/]\s?\d{2,4}|\d{2,4})"
_PERIOD_PATTERNS: list[tuple[PeriodType, re.Pattern[str]]] = [
    ("quarter", re.compile(rf"^Q(?P<n>[1-4])\s?[-']?\s?{_FYNUM}$", re.I)),
    ("half", re.compile(rf"^H(?P<n>[12])\s?[-']?\s?{_FYNUM}$", re.I)),
    ("nine_months", re.compile(rf"^9\s?M\s?[-']?\s?{_FYNUM}$", re.I)),
    ("year", re.compile(rf"^{_FYNUM}$", re.I)),
]
_ENDED = re.compile(
    r"^(?P<what>quarter|three months|3 months|half[- ]year|six months|6 months|nine months|9 months|year|twelve months|12 months)"
    r"\s+ended\s+(?:on\s+)?(?P<date>.+)$", re.I)
_WHAT = {"quarter": "quarter", "three months": "quarter", "3 months": "quarter",
         "half-year": "half", "half year": "half", "six months": "half", "6 months": "half",
         "nine months": "nine_months", "9 months": "nine_months", "year": "year",
         "twelve months": "year", "12 months": "year"}


def parse_period(raw: str) -> Period | None:
    s = " ".join(raw.split())
    for ptype, pat in _PERIOD_PATTERNS:
        m = pat.match(s)
        if m:
            n = m.groupdict().get("n")
            return Period(raw, ptype, _fy(m["fy"]), int(n) if n else None)
    m = _ENDED.match(s)
    if m:
        d = parse_date(m["date"])
        if d and d.precision == "day":
            return Period(raw, _WHAT[m["what"].lower()], None, None, d.iso)  # type: ignore[arg-type]
    return None


def parse_as_at(label: str) -> Period | None:
    """'As at 30.09.2025' or a bare date column header -> point-in-time period."""
    core = re.sub(r"^as\s+(at|on)\s+", "", label.strip(), flags=re.I)
    d = parse_date(core)
    if d and d.precision == "day":
        return Period(label, "as_at", None, None, d.iso)
    return None


def is_change_header(label: str) -> str | None:
    """Return 'yoy' | 'qoq' | 'change' for growth columns, else None."""
    s = label.lower()
    if "yoy" in s or "y-o-y" in s or "y-o-y" in s or "year on year" in s:
        return "yoy"
    if "qoq" in s or "q-o-q" in s or "sequential" in s or "quarter on quarter" in s:
        return "qoq"
    if "% change" in s or "growth" in s or s.strip() in ("change", "% chg", "chg %", "var %", "% var"):
        return "change"
    return None
