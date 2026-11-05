"""Page numbers, chart axis ticks, OCR read off logos/icons and garbled OCR headings must
not become figures/sections — while real table values, years and body numbers stay."""

from __future__ import annotations

import pymupdf
import pytest

from app.extraction import layout, noise
from app.extraction.pdf import PageData, Word
from app.extraction.schema_builder import extract

W, H = 595.0, 842.0


def word(text, x, y, *, w=None, h=10.0, conf=1.0, font=""):
    w = w if w is not None else 6.0 * len(text)
    return Word(text, (x, y, x + w, y + h), conf, h, font=font)


def page(n, words, *, method="text_layer", **kw):
    return PageData(n, W, H, method, words, "test", **kw)


# ------------------------------------------------------------------ page numbers


def _report_pages(count=6, offset=10, y=760.0):
    """Footer at ~90% of the page height (outside the old 7.5% band), next to a running
    title that changes per chapter, so neither old rule catches it."""
    pages = []
    for n in range(1, count + 1):
        title = ["Strategic", "Report"] if n <= 3 else ["Governance"]
        ws = [word(t, 60 + 70 * i, y) for i, t in enumerate(title)]
        ws.append(word(str(n + offset), 520, y))
        ws += [word("Revenue", 60, 300), word("1,234", 400, 300), word("2025", 480, 300)]
        pages.append(page(n, ws))
    return pages


def test_running_page_numbers_are_dropped_and_old_remover_missed_them():
    pages = _report_pages()
    old = layout.header_footer_words(pages)
    new = noise.page_number_words(pages)
    for p in pages:
        num = next(w for w in p.words if w.text == str(p.number + 10))
        assert id(num) not in old[p.number]           # why users saw '27' flagged
        assert id(num) in new[p.number]
        body = {w.text: id(w) for w in p.words if w.bbox[1] < 400}
        assert not set(body.values()) & new[p.number]   # body figures untouched


def test_page_numbers_in_side_margin_and_two_up_spreads():
    pages = [page(n, [word(str(2 * n + 4), 10, 400), word(str(2 * n + 5), 570, 400, w=15)]) for n in range(1, 6)]
    new = noise.page_number_words(pages)
    assert all(len(new[p.number]) == 2 for p in pages)


def test_margin_number_that_does_not_follow_the_page_sequence_is_kept():
    pages = [page(n, [word(str(v), 520, 790)]) for n, v in enumerate([7, 42, 3, 19, 88], start=1)]
    assert not any(noise.page_number_words(pages).values())


def test_single_page_is_left_to_the_existing_rule():
    assert noise.page_number_words([page(1, [word("1", 520, 790)])]) == {1: set()}


# ------------------------------------------------------------------ axis ticks


def test_y_axis_ticks_in_chart_region_are_dropped():
    ticks = [word(str(v), 80, 200 + 30 * i, w=12) for i, v in enumerate([80, 60, 40, 20, 0])]
    labels = [word("54.2", 150, 230), word("71.9", 200, 210)]
    p = page(1, ticks + labels, chart_regions=[(70, 180, 400, 360)])
    out = noise.axis_tick_words(p)
    assert out == {id(w) for w in ticks}


def test_x_axis_ticks_without_a_label_on_the_line_are_dropped_but_years_are_kept():
    ticks = [word(v, 100 + 60 * i, 500, w=18) for i, v in enumerate(["0%", "25%", "50%", "75%", "100%"])]
    years = [word(str(y), 100 + 60 * i, 600, w=24) for i, y in enumerate(range(2021, 2026))]
    p = page(1, ticks + years + [word("Revenue", 100, 300)])
    out = noise.axis_tick_words(p)
    assert out == {id(w) for w in ticks}


def test_table_column_that_happens_to_be_an_even_run_is_kept():
    rows = []
    for i, (lab, v) in enumerate([("Plants", "0"), ("Depots", "10"), ("Mills", "20"), ("Farms", "30"), ("Hubs", "40")]):
        rows += [word(lab, 60, 200 + 20 * i), word(v, 300, 200 + 20 * i, w=12)]
    p = page(1, rows)
    assert noise.axis_tick_words(p) == set()


def test_table_row_with_a_label_is_kept():
    ws = [word("Score", 40, 400)] + [word(str(v), 200 + 60 * i, 400, w=12) for i, v in enumerate([0, 5, 10, 15, 20])]
    assert noise.axis_tick_words(page(1, ws)) == set()


def test_uneven_or_non_arithmetic_values_are_kept():
    ws = [word(str(v), 80, 200 + 30 * i, w=12) for i, v in enumerate([80, 61, 40, 20, 0])]
    assert noise.axis_tick_words(page(1, ws, chart_regions=[(70, 180, 400, 360)])) == set()


# ------------------------------------------------------------------ OCR junk


@pytest.mark.parametrize("raw,bad", [
    ("8888", True), ("$8888", True), ("888", True), ("€8", True), ("$4", True), ("#8~%", True),
    ("1,234", False), ("€8.5", False), ("-1%", False), ("(5)", False), ("2025", False), ("12%", False), ("100", False), ("(45)", False),
])
def test_degenerate_number(raw, bad):
    assert noise.degenerate_number(raw) is bad


def _ocr_page(words, **kw):
    return page(1, words, method="ocr", **kw)


def test_ocr_junk_from_logos_and_icons_is_dropped():
    junk = [word("$8888", 450, 60, conf=0.31), word("€8", 50, 700, conf=0.42), word("7", 300, 780, conf=0.6)]
    body, x = [], 60.0
    for t in ["Revenue", "grew", "to", "€8", "billion"]:
        body.append(word(t, x, 300, conf=0.93))
        x += 6 * len(t) + 3
    big = word("27%", 300, 450, h=40, conf=0.95)             # a confident hero figure stays
    p = _ocr_page(junk + body + [big])
    out = noise.ocr_junk_words(p)
    assert out == {id(w) for w in junk}


def test_low_confidence_table_cells_are_kept_for_review():
    ws = []
    for i, (lab, a, b) in enumerate([("Revenue", "1,234.5", "1,111"), ("EBITDA", "245.1", "230.0"), ("PAT", "142.3", "138.9")]):
        y = 200 + 18 * i
        ws += [word(lab, 60, y, conf=0.9), word(a, 330, y, conf=0.45), word(b, 430, y, conf=0.45)]
    assert noise.ocr_junk_words(_ocr_page(ws)) == set()


def test_ocr_on_a_small_logo_image_is_dropped():
    on_logo = [word("88", 505, 30, conf=0.7), word("MMENA", 470, 45, conf=0.6)]
    p = _ocr_page(on_logo + [word("Annual", 60, 300, conf=0.95), word("report", 110, 300, conf=0.95)],
                  logo_regions=[(460, 20, 560, 70)])
    assert noise.ocr_junk_words(p) == {id(w) for w in on_logo}


def test_text_layer_pages_are_not_touched_by_ocr_rules():
    assert noise.ocr_junk_words(page(1, [word("$8888", 450, 60, conf=1.0)])) == set()


def test_digits_in_an_icon_font_are_dropped():
    p = page(1, [word("8", 50, 100, font="Wingdings-Regular"), word("8", 80, 100, font="Helvetica")])
    assert noise.icon_font_words(p) == {id(p.words[0])}


# ------------------------------------------------------------------ OCR headings


def _hw(text, conf=0.9):
    return [word(t, 60 + 80 * i, 100, conf=conf) for i, t in enumerate(text.split())]


@pytest.mark.parametrize("text,conf,ok", [
    ("gtaaliyy", 0.9, False),
    ("MM ENA", 0.7, False),
    ("xQzRtv KjhGfd", 0.9, False),
    ("Statement of Profit and Loss", 0.9, True),
    ("Nation First Sab Saath Badhein", 0.93, True),
    ("Chairman's Message", 0.8, True),
    ("ESG", 0.95, True),
    ("CSR", 0.6, False),
    ("Q2 FY26 Results", 0.9, True),
    ("Statement of Profit and Loss", 0.3, False),
])
def test_plausible_ocr_heading(text, conf, ok):
    assert noise.plausible_ocr_heading(_hw(text, conf)) is ok


# ------------------------------------------------------------------ end to end


META = {"company_name": "Sample", "report_type": "annual_report", "fiscal_year": 2026, "period": "fy",
        "currency": "INR", "reporting_unit": "₹ crore", "filename": "t.pdf"}


def _pdf() -> bytes:
    doc = pymupdf.open()
    for n in range(1, 5):
        pg = doc.new_page(width=W, height=H)
        pg.insert_text((60, 80), "Financial Review" if n % 2 else "Strategic Report", fontsize=18)
        pg.insert_text((60, 120), f"Revenue rose to {n}2.5 crore in the year.", fontsize=10)
        # table with years in the header
        pg.insert_text((300, 160), "2026", fontsize=10)
        pg.insert_text((400, 160), "2025", fontsize=10)
        for i, (lab, a, b) in enumerate([("Revenue", "1,234.50", "1,100.25"), ("EBITDA", "245.10", "230.00"),
                                          ("Profit", "142.35", "138.90")]):
            y = 180 + 16 * i
            pg.insert_text((60, y), lab, fontsize=10)
            pg.insert_text((300, y), a, fontsize=10)
            pg.insert_text((400, y), b, fontsize=10)
        # a line chart: axis ticks + lines only (no filled shapes, so no chart region)
        for i, v in enumerate([100, 75, 50, 25, 0]):
            pg.insert_text((60, 320 + 30 * i), str(v), fontsize=8)
        pg.draw_polyline([(90, 440), (200, 400), (310, 360), (420, 330)], color=(0, 0, 1))
        # running footer at ~91% height, the page number beside a per-chapter title
        pg.insert_text((60, 770), "ITC Report" if n <= 2 else "Governance Report", fontsize=8)
        pg.insert_text((520, 770), str(n + 26), fontsize=8)
    return doc.tobytes()


def test_extract_drops_page_numbers_and_ticks_but_keeps_real_figures():
    schema, _ = extract(_pdf(), META)
    raws = [f["raw"] for f in schema["figures"].values()]
    for v in ("27", "28", "29", "30"):
        assert v not in raws
    for v in ("100", "75", "50", "25", "0"):
        assert v not in raws
    for v in ("1,234.50", "1,100.25", "245.10", "230.00", "142.35", "138.90", "2026", "2025", "12.5", "42.5"):
        assert v in raws, v
    reasons = {e.get("reason") for p in schema["pages"] for e in p["excluded_header_footer"]}
    assert {"page_number", "axis_tick"} <= reasons
