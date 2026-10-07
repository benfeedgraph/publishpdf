"""Number/date/period parsing — every format in brief §4.2. Pure unit tests (no DB)."""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.extraction.dates import is_change_header, parse_date, parse_period
from app.extraction.numbers import digits_signature, parse_number, try_parse_number


@pytest.mark.parametrize("raw,value,kind,grouping", [
    ("1,23,456.78", "123456.78", "number", "indian"),
    ("12,34,56,789", "123456789", "number", "indian"),
    ("123,456.78", "123456.78", "number", "international"),
    ("1,234,567", "1234567", "number", "international"),
    ("12,345", "12345", "number", "both"),
    ("1234.50", "1234.50", "number", "none"),
    ("(1,234)", "-1234", "number", "both"),
    ("(1,23,456.7)", "-123456.7", "number", "indian"),
    ("-1,234", "-1234", "number", "both"),
    ("−1,234", "-1234", "number", "both"),
    ("12.5%", "12.5", "percent", "none"),
    ("(12.5)%", "-12.5", "percent", "none"),
    ("(12.5%)", "-12.5", "percent", "none"),
    ("-3.2 %", "-3.2", "percent", "none"),
    ("45 bps", "45", "bps", "none"),
    ("2.5x", "2.5", "multiple", "none"),
    ("₹1,234", "1234", "number", "both"),
    ("Rs. 12,34,567", "1234567", "number", "indian"),
    ("$(1,200)", "-1200", "number", "both"),
    ("0.00", "0.00", "number", "none"),
])
def test_number_formats(raw, value, kind, grouping):
    p = parse_number(raw)
    assert p.value == Decimal(value) and p.kind == kind and p.grouping == grouping
    assert p.raw == raw


@pytest.mark.parametrize("raw", ["-", "–", "—", "nil", "Nil", "NIL"])
def test_nil_tokens(raw):
    p = parse_number(raw)
    assert p.kind == "nil" and p.value is None


@pytest.mark.parametrize("raw", [
    "1,2345", "12,34", "1,23,4567", "1,,234", ",123", "1.2.3", "(1,234", "1,234)", "(-12)",
    "12a", "abc", "", "1 234", ".5", "12%%", "₹12%",
])
def test_rejects_malformed(raw):
    assert try_parse_number(raw) is None


def test_values_are_decimal_not_float():
    assert parse_number("0.1").value + parse_number("0.2").value == Decimal("0.3")
    assert parse_number("1234.50").value_str == "1234.50"


def test_digit_signature_ignores_style_but_not_digits():
    assert digits_signature("(1,23,456)") == digits_signature("-123456")
    assert digits_signature("1,234.5") == digits_signature("1234.5")
    assert digits_signature("1,234.5") != digits_signature("1,234.50")   # decimal places are data
    assert digits_signature("1,234") != digits_signature("1,284")
    assert digits_signature("12%") != digits_signature("12")


def _indian(n: int) -> str:
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    return ",".join([head] + groups + [tail])


@given(st.integers(min_value=0, max_value=10**15), st.integers(min_value=0, max_value=99),
       st.booleans())
def test_property_indian_and_international_round_trip(n, frac, bracket):
    for text in (_indian(n), f"{n:,}"):
        raw = f"{text}.{frac:02d}"
        raw = f"({raw})" if bracket else raw
        p = parse_number(raw)
        expected = Decimal(f"{n}.{frac:02d}")
        assert p.value == (-expected if bracket else expected)


@pytest.mark.parametrize("raw,iso,amb", [
    ("30.09.2025", "2025-09-30", False),
    ("30/09/2025", "2025-09-30", False),
    ("05/09/2025", "2025-09-05", True),
    ("30-Sep-2025", "2025-09-30", False),
    ("30 September 2025", "2025-09-30", False),
    ("30th September, 2025", "2025-09-30", False),
    ("September 30, 2025", "2025-09-30", False),
    ("Sept 30, 2025", "2025-09-30", False),
    ("2025-09-30", "2025-09-30", False),
    ("Sep-25", "2025-09", False),
    ("March 2025", "2025-03", False),
])
def test_dates(raw, iso, amb):
    d = parse_date(raw)
    assert d is not None and d.iso == iso and d.ambiguous == amb


@pytest.mark.parametrize("raw", ["31.02.2025", "32/01/2025", "hello", "2025"])
def test_invalid_dates(raw):
    assert parse_date(raw) is None


@pytest.mark.parametrize("raw,key", [
    ("Q2 FY26", "quarter2:FY2026"),
    ("Q2FY26", "quarter2:FY2026"),
    ("Q1 FY2025-26", "quarter1:FY2026"),
    ("H1 FY26", "half1:FY2026"),
    ("9M FY26", "nine_months:FY2026"),
    ("FY25", "year:FY2025"),
    ("FY 2024-25", "year:FY2025"),
    ("Quarter ended 30.09.2025", "quarter@2025-09-30"),
    ("Year ended 31 March 2025", "year@2025-03-31"),
    ("Six months ended 30.09.2025", "half@2025-09-30"),
])
def test_periods(raw, key):
    p = parse_period(raw)
    assert p is not None and p.key == key


def test_change_headers():
    assert is_change_header("YoY %") == "yoy"
    assert is_change_header("QoQ") == "qoq"
    assert is_change_header("% Change") == "change"
    assert is_change_header("Q2 FY26") is None


def _pdf(*pages: str) -> bytes:
    import pymupdf
    doc = pymupdf.open()
    for text in pages:
        doc.new_page().insert_textbox(pymupdf.Rect(40, 40, 560, 800), text, fontsize=10)
    return doc.tobytes()


def test_detect_annual_report_and_accounts():
    """ITC-style cover: the legal name mustn't swallow the sentence before it, 'Report and
    Accounts' is an annual report, and its year is the fiscal year."""
    from app.extraction.detect import detect_metadata
    meta = detect_metadata(_pdf(
        "Contents are hyper-linked to the relevant pages of the report Click 'ITC Limited",
        "Contents REPORT AND ACCOUNTS 2026 Board of Directors",
        "ITC has always lived by its vision. Nearly 40 lakh tonnes sourced. Over Rs. 37,000 cr consumer spends. "
        "Luxury' & Sustainability ITC Hotels Limited",
    ))
    assert meta["reporting_unit"] == "₹ crore"          # the money amount, not "40 lakh tonnes"
    assert meta["company_name"] == "ITC Limited"
    assert meta["report_type"] == "annual_report"
    assert (meta["period"], meta["fiscal_year"]) == ("fy", 2026)


def test_detect_annual_report_year_range():
    from app.extraction.detect import detect_metadata
    meta = detect_metadata(_pdf("Borealis Holdings Annual Report 2024-25"))
    assert meta["fiscal_year"] == 2025 and meta["report_type"] == "annual_report"


def test_the_report_schema_ships_inside_the_backend():
    """The worker's image is built from backend/ alone: a schema outside it was missing in
    production and every report's checks failed (FileNotFoundError)."""
    from pathlib import Path

    from app.extraction.jsonschema_check import SCHEMA_PATH, schema_errors
    backend = Path(__file__).resolve().parents[1]
    assert SCHEMA_PATH.is_file() and backend in SCHEMA_PATH.parents
    assert schema_errors({}) != []                       # it loads and validates
