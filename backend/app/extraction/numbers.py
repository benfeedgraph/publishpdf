"""The one number parser. Every figure in the system is parsed here and nowhere else.

Rules (zero tolerance — anything not matching exactly is NOT a number):
- Indian grouping      1,23,456.78     (first group 1–2 digits... then pairs, last group 3)
- International        123,456.78
- Ungrouped            123456.78, 0.5, .5 is rejected (must have a leading digit)
- Negative             (1,234)  -1,234  −1,234 (U+2212)  –1,234 (en dash as minus, only when
                       immediately followed by a digit)
- Percent              12.5%   (12.5)%   (12.5%)   -12.5 %
- Basis points         45 bps  45bps
- Multiples            2.5x    2.5X
- Nil                  -  –  —  nil  Nil  NIL  (alone)
- Currency prefixes    ₹ Rs Rs. INR $ US$ USD € £   (kept in the raw string, reported as `currency_symbol`)

Values are `Decimal`, never float, and keep the source's decimal places.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

Kind = Literal["number", "percent", "bps", "multiple", "nil"]
Grouping = Literal["indian", "international", "both", "none"]

NIL_TOKENS = {"-", "–", "—", "nil", "Nil", "NIL", "‐"}
_MINUS = "-−–"
_CURRENCY = r"(?:₹|Rs\.?|INR|US\$|USD|\$|€|EUR|£|GBP)"

_INDIAN = re.compile(r"^\d{1,2}(?:,\d{2})*,\d{3}(?:\.\d+)?$")
_INTL = re.compile(r"^\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
_PLAIN = re.compile(r"^\d+(?:\.\d+)?$")

_FULL = re.compile(
    rf"""^
    (?P<cur>{_CURRENCY})?\s?
    (?P<open>\()?
    (?P<cur2>{_CURRENCY})?\s?
    (?P<sign>[{_MINUS}])?
    (?P<num>\d[\d,]*(?:\.\d+)?)
    (?P<pct_in>\s?%)?
    (?P<close>\))?
    (?P<suffix>\s?(?:%|bps|x|X))?
    $""",
    re.VERBOSE,
)


@dataclass(frozen=True)
class ParsedNumber:
    raw: str
    kind: Kind
    value: Decimal | None      # None only for nil
    grouping: Grouping
    currency_symbol: str | None
    negative: bool

    @property
    def value_str(self) -> str | None:
        return None if self.value is None else format(self.value, "f")


class NotANumber(ValueError):
    pass


def grouping_of(digits: str) -> Grouping | None:
    """Classify the comma grouping of an unsigned numeric string, or None if invalid."""
    intpart = digits.split(".")[0]
    if "," not in intpart:
        return "none" if _PLAIN.match(digits) else None
    indian = bool(_INDIAN.match(digits))
    intl = bool(_INTL.match(digits))
    if indian and intl:
        return "both"          # e.g. 12,345 — same value either way
    if indian:
        return "indian"
    if intl:
        return "international"
    return None


def parse_number(raw: str) -> ParsedNumber:
    s = raw.strip()
    if s in NIL_TOKENS:
        return ParsedNumber(raw, "nil", None, "none", None, False)
    m = _FULL.match(s)
    if not m:
        raise NotANumber(raw)
    if bool(m["open"]) != bool(m["close"]):
        raise NotANumber(raw)          # unbalanced bracket
    if m["cur"] and m["cur2"]:
        raise NotANumber(raw)
    if m["open"] and m["sign"]:
        raise NotANumber(raw)          # "(-1)" is ambiguous — refuse rather than guess
    num = m["num"]
    grouping = grouping_of(num)
    if grouping is None:
        raise NotANumber(raw)
    pct = bool(m["pct_in"]) or (m["suffix"] or "").strip() == "%"
    if m["pct_in"] and m["suffix"]:
        raise NotANumber(raw)
    suffix = (m["suffix"] or "").strip()
    kind: Kind = "percent" if pct else "bps" if suffix == "bps" else "multiple" if suffix in ("x", "X") else "number"
    if kind != "number" and (m["cur"] or m["cur2"]):
        raise NotANumber(raw)
    negative = bool(m["open"]) or bool(m["sign"])
    value = Decimal(num.replace(",", ""))
    if negative:
        value = -value
    return ParsedNumber(raw, kind, value, grouping, m["cur"] or m["cur2"], negative)


def try_parse_number(raw: str) -> ParsedNumber | None:
    try:
        return parse_number(raw)
    except NotANumber:
        return None


def digits_signature(raw: str) -> str:
    """Canonical comparison key used by the independent re-extraction check:
    sign + digits + decimal point, ignoring grouping commas, spacing, currency and
    bracket-vs-minus style. Two readings of the same printed figure must match."""
    p = try_parse_number(raw)
    if p is None:
        return re.sub(r"[^0-9.]", "", raw)
    if p.kind == "nil":
        return "nil"
    assert p.value is not None
    unit = {"percent": "%", "bps": "bps", "multiple": "x"}.get(p.kind, "")
    return f"{'-' if p.negative else ''}{format(abs(p.value), 'f')}{unit}"


_DIGIT = re.compile(r"\d")


def has_digit(s: str) -> bool:
    return bool(_DIGIT.search(s))
