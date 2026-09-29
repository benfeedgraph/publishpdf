"""Turn a sequence of words into text runs + figure tokens.

Invariant (enforced by `assert_no_loose_digits`): after tokenisation, *no* digit
survives in plain text. Every digit-bearing token becomes a figure (number, percent,
date, period or identifier) with its raw string and source bbox, so the rendered page
can only ever show digits that exist in the figure registry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from app.extraction.dates import parse_date, parse_period
from app.extraction.numbers import has_digit, try_parse_number
from app.extraction.pdf import Word

FigKind = Literal["number", "percent", "bps", "multiple", "nil", "date", "period", "identifier"]

_TRAIL = ",;:.!?)"
_LEAD = "("
_CURRENCY_WORDS = {"₹", "Rs", "Rs.", "INR", "USD", "US$", "$", "€", "£", "EUR", "GBP"}
_UNIT_WORDS = {
    "crore": "crore", "crores": "crore", "cr": "crore", "cr.": "crore",
    "lakh": "lakh", "lakhs": "lakh", "lac": "lakh", "lacs": "lakh",
    "million": "million", "mn": "million", "mn.": "million", "billion": "billion", "bn": "billion",
    "thousand": "thousand",
}
_MONTH_WORD = re.compile(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?,?$", re.I)


@dataclass
class Token:
    kind: FigKind
    raw: str                              # exact source characters (may span words)
    words: list[Word]
    parsed: object | None = None          # ParsedNumber | ParsedDate | Period
    unit_hint: str | None = None
    currency_hint: str | None = None

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (min(w.bbox[0] for w in self.words), min(w.bbox[1] for w in self.words),
                max(w.bbox[2] for w in self.words), max(w.bbox[3] for w in self.words))

    @property
    def source_bbox(self) -> tuple[float, float, float, float]:
        bs = [w.source_bbox for w in self.words]
        return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))

    @property
    def conf(self) -> float:
        return min(w.conf for w in self.words)


@dataclass
class Run:
    text: str | None = None
    token: Token | None = None


@dataclass
class Tokenized:
    runs: list[Run] = field(default_factory=list)

    def add_text(self, s: str) -> None:
        if self.runs and self.runs[-1].text is not None:
            self.runs[-1].text += s
        else:
            self.runs.append(Run(text=s))


def _split_punct(text: str) -> tuple[str, str, str]:
    """('(', '12.4%', ')') style split of leading/trailing punctuation that isn't part
    of the figure. Brackets are kept when they wrap a number (negative)."""
    core = text
    lead = trail = ""
    while core and core[-1] in _TRAIL:
        cand = core[:-1]
        if core[-1] == ")" and "(" in core:
            break
        if core[-1] == "." and try_parse_number(core) is not None and try_parse_number(cand) is None:
            break
        trail = core[-1] + trail
        core = cand
    while core and core[0] in _LEAD and ")" not in core:
        lead += core[0]
        core = core[1:]
    return lead, core, trail


def classify(raw: str, *, in_table_cell: bool = False, header: bool = False):
    """Return (kind, parsed) for a single digit-bearing (or nil) token."""
    if header:
        p = parse_period(raw)
        if p:
            return "period", p
    d = parse_date(raw)
    if d and not (try_parse_number(raw) and d.precision == "month"):
        return "date", d
    n = try_parse_number(raw)
    if n is not None:
        if n.kind == "nil" and not in_table_cell:
            return None, None
        return n.kind, n
    p = parse_period(raw)
    if p:
        return "period", p
    if has_digit(raw):
        return "identifier", None
    return None, None


YEAR_RANGE = re.compile(r"^(19|20)\d{2}\s?[-–]\s?((19|20)?\d{2})$")   # 2005-2018, 2024-25


def looks_numeric_unparsed(raw: str) -> bool:
    if YEAR_RANGE.match(raw.strip()):
        return False          # a span of years (a tenure, a financial year), not a garbled number
    return bool(re.match(r"^[(\-−–]?\d[\d,.\-–]*\)?%?$", raw)) and try_parse_number(raw) is None


def tokenize(words: list[Word], *, table_cell: bool = False, header: bool = False) -> Tokenized:
    """Tokenise words (already in reading order) into runs."""
    out = Tokenized()
    i = 0
    n = len(words)
    while i < n:
        w = words[i]
        sep = " " if i > 0 else ""
        # --- multi-word figures (longest match first) -------------------------------
        best = None
        for span in (4, 3, 2):
            if i + span > n:
                continue
            group = words[i:i + span]
            if not any(has_digit(g.text) for g in group):
                continue
            raw_full = " ".join(g.text for g in group)
            lead, core, trail = _split_punct(raw_full)
            kind = None
            if parse_period(core) and not try_parse_number(core):
                kind, parsed = "period", parse_period(core)
            elif (d := parse_date(core)) and not try_parse_number(core):
                kind, parsed = "date", d
            elif (num := try_parse_number(core)) and num.kind != "nil" and (" " in core):
                kind, parsed = num.kind, num
            if kind:
                best = (span, lead, core, trail, kind, parsed)
                break
        if best:
            span, lead, core, trail, kind, parsed = best
            out.add_text(sep + lead)
            tok = Token(kind, core, words[i:i + span], parsed)  # type: ignore[arg-type]
            _hints(tok, words, i, span)
            out.runs.append(Run(token=tok))
            if trail:
                out.add_text(trail)
            i += span
            continue
        # --- single word -----------------------------------------------------------
        lead, core, trail = _split_punct(w.text)
        kind, parsed = classify(core, in_table_cell=table_cell, header=header) if core else (None, None)
        if kind is None:
            out.add_text(sep + w.text)
        else:
            out.add_text(sep + lead)
            tok = Token(kind, core, [w], parsed)
            _hints(tok, words, i, 1)
            out.runs.append(Run(token=tok))
            if trail:
                out.add_text(trail)
        i += 1
    return out


def _hints(tok: Token, words: list[Word], i: int, span: int) -> None:
    if tok.kind not in ("number",):
        return
    if i > 0 and words[i - 1].text.strip("(") in _CURRENCY_WORDS:
        tok.currency_hint = words[i - 1].text.strip("(")
    nxt = words[i + span].text.lower().strip(",.;:)") if i + span < len(words) else ""
    if nxt in _UNIT_WORDS:
        tok.unit_hint = _UNIT_WORDS[nxt]


def assert_no_loose_digits(t: Tokenized) -> None:
    for r in t.runs:
        if r.text is not None and has_digit(r.text):
            raise AssertionError(f"digit leaked into plain text: {r.text!r}")


_UNIT_RE = re.compile(
    r"(?P<cur>₹|Rs\.?|INR|US\$|USD|\$|€|EUR|£|GBP)?\s*(?:in\s+)?(?P<cur2>₹|Rs\.?|INR|US\$|USD|\$|€|EUR|£|GBP)?\s*"
    r"(?P<unit>crores?|cr\.?|lakhs?|lacs?|millions?|mn|billions?|bn|thousands?|'000)\b", re.I)
CURRENCY_CODES = {"₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR", "us$": "USD", "usd": "USD", "$": "USD",
                  "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP"}
UNIT_CANON = {"crore": "crore", "crores": "crore", "cr": "crore", "cr.": "crore", "lakh": "lakh",
              "lakhs": "lakh", "lac": "lakh", "lacs": "lakh", "million": "million", "millions": "million",
              "mn": "million", "billion": "billion", "billions": "billion", "bn": "billion",
              "thousand": "thousand", "thousands": "thousand", "'000": "thousand"}


def detect_unit(text: str) -> tuple[str | None, str | None]:
    """('crore', 'INR') from captions like '(₹ in crore, except per share data)'."""
    m = _UNIT_RE.search(text)
    if not m:
        return None, None
    cur = m["cur"] or m["cur2"]
    return UNIT_CANON[m["unit"].lower()], CURRENCY_CODES.get(cur.lower()) if cur else None


def all_units_mentioned(text: str) -> set[str]:
    return {UNIT_CANON[m["unit"].lower()] for m in _UNIT_RE.finditer(text)}
