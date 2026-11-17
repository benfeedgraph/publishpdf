"""Issue #10: charts read as broken tables.

  - a chart drawn as a picture gathers its own name, value labels and axis, so each value is
    paired with its axis label; small charts that share an axis become one table of values;
  - every table is checked before it goes out; one whose numbers don't line up (most cells
    empty, values scattered down a staircase, many numbers with no cell) is shown as it looks
    in the PDF, and Checks & issues says so;
  - a table's caption says "Periods" only when its columns are periods, and the first column
    carries the PDF's own label or nothing (no "Line item" placeholder).
"""

from __future__ import annotations

import pymupdf

from app import validation
from app.extraction import layout
from app.extraction.pdf import PageData, Word, _classify
from app.extraction.schema_builder import _combine_small_charts, extract
from app.render import site
from app.render import theme as theming

W, H = 960.0, 540.0
BODY = 16.0
META = {"company_name": "Acme Ltd", "report_type": "investor_presentation", "fiscal_year": 2026,
        "period": "q2", "currency": "INR", "reporting_unit": "₹ crore"}


def word(text, x, y, *, size=BODY, bold=False, w=None):
    w = w if w is not None else 0.55 * size * len(text)
    return Word(text, (x, y, x + w, y + size), 1.0, size, bold)


def line(text, x, y, **kw) -> list[Word]:
    out, cx = [], x
    for t in text.split():
        wd = word(t, cx, y, **kw)
        out.append(wd)
        cx = wd.bbox[2] + 0.3 * kw.get("size", BODY)
    return out


def page(words, **kw) -> PageData:
    return PageData(1, W, H, "text_layer", words, "test", **kw)


def analyse(p: PageData):
    return layout.analyse_page(p, set(), BODY)


def charts(els) -> list[layout.ChartEl]:
    return [e for e in els if isinstance(e, layout.ChartEl)]


def test_a_chart_drawn_as_a_picture_takes_its_name_values_and_axis():
    ws = line("Snacks", 22, 112, size=14.4, bold=True) + line("143000", 188, 128, size=9.8, bold=True)
    for x, v in ((35, "32000"), (87, "56000"), (140, "100000")):
        ws += line(v, x, 200, size=9.8, bold=True)
    for x, y in ((35, "2020"), (87, "2025"), (140, "2030"), (192, "2035")):
        ws += line(y, x, 265, size=10.6, bold=True)
    (c,) = charts(analyse(page(ws, image_regions=[(29, 143, 217, 255)])))
    assert [w.text for w in c.title] == ["Snacks"]
    assert sorted(w.text for w in c.labels) == ["100000", "143000", "32000", "56000"]
    assert sorted(w.text for w in c.axis) == ["2020", "2025", "2030", "2035"]


def test_small_charts_sharing_an_axis_become_one_table():
    def chart(cid, title, values):
        pts = [{"label": [{"t": y}], "label_text": y, "value": [{"t": v}]} for y, v in zip(("2020", "2025", "2030"), values)]
        return {"id": cid, "type": "chart", "extracted": True, "title": [{"t": title}], "points": pts}
    a, b, lone = chart("b1", "Snacks", ("32", "56", "100")), chart("b2", "Spices", ("22", "39", "70")), \
        {"id": "b3", "type": "chart", "extracted": True, "points": [{"label": [], "label_text": "", "value": []}]}
    _combine_small_charts([a, b, lone])
    st = a["series_table"]
    assert [c[0]["t"] for c in st["columns"]] == ["2020", "2025", "2030"]
    assert [(r["label"][0]["t"], [c[0]["t"] for c in r["cells"]]) for r in st["rows"]] == \
        [("Snacks", ["32", "56", "100"]), ("Spices", ["22", "39", "70"])]
    assert b["series_of"] == "b1" and "series_table" not in lone


def test_a_staircase_of_chart_labels_is_not_published_as_a_table():
    ws = []
    for i, (x, v) in enumerate(((300, "143000"), (200, "100000"), (100, "56000"), (40, "32000"))):
        ws += line(v, x, 100 + 20 * i)
    els = analyse(page(ws))
    assert not [e for e in els if isinstance(e, layout.TableEl)]
    (c,) = charts(els)
    assert c.from_table and len(c.labels) == 4


def test_a_table_whose_numbers_line_up_stays_a_table():
    ws = line("FY2026", 300, 60, bold=True) + line("FY2025", 400, 60, bold=True)
    for i, (label, a, b) in enumerate((("Revenue", "1,234", "1,100"), ("EBITDA", "456", "400"), ("PAT", "210", "190"))):
        ws += line(label, 40, 90 + 22 * i) + line(a, 300, 90 + 22 * i) + line(b, 400, 90 + 22 * i)
    (t,) = [e for e in analyse(page(ws)) if isinstance(e, layout.TableEl)]
    assert layout.table_problems(t) == [] and len(t.rows) == 3


def test_a_table_shown_as_in_the_pdf_is_reported_in_checks_and_issues():
    schema = {"metadata": {"reporting_unit": "₹ crore", "reporting_unit_canonical": "crore"}, "figures": {},
              "sections": [{"id": "s1", "blocks": [{"id": "b1", "type": "chart", "extracted": True, "from_table": True,
                                                    "points": [], "source": {"page": 11, "bbox": [0, 0, 10, 10]}}]}]}
    (issue,) = validation.check_period_unit(schema)
    assert issue.severity == "warning" and issue.page == 11 and "as it looks in the PDF" in issue.message


# ------------------------------------------------------------------ table captions and headers on the page


def _table_pdf(headers: tuple[str, str], corner: str | None) -> bytes:
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    p.insert_text((72, 80), "Market size by category", fontname="hebo", fontsize=20)
    p.insert_text((72, 120), "(Rs in crore)", fontname="helv", fontsize=10)
    if corner:
        p.insert_text((72, 140), corner, fontname="hebo", fontsize=10)
    p.insert_text((330, 140), headers[0], fontname="hebo", fontsize=10)
    p.insert_text((430, 140), headers[1], fontname="hebo", fontsize=10)
    for i, (label, a, b) in enumerate((("Snacks", "32,000", "56,000"), ("Spices", "22,000", "39,000"),
                                       ("Biscuits", "38,000", "61,000"))):
        y = 160 + 18 * i
        p.insert_text((72, y), label, fontname="helv", fontsize=10)
        p.insert_text((330, y), a, fontname="helv", fontsize=10)
        p.insert_text((430, y), b, fontname="helv", fontsize=10)
    return doc.tobytes()


def _table_html(pdf: bytes) -> str:
    schema, _ = extract(pdf, META)
    files = site.render_report(schema, theme=theming.validate({}), disclaimer="d", pdf_bytes=pdf)
    return next(v.decode() for k, v in files.items() if k.endswith("index.html"))


def test_the_caption_says_periods_only_when_the_columns_are_periods():
    assert "Periods:" in _table_html(_table_pdf(("FY2026", "FY2025"), None))
    html = _table_html(_table_pdf(("Retail", "Wholesale"), None))
    assert "Periods:" not in html and "Retail" in html


def test_the_first_column_carries_the_pdfs_own_label_or_nothing():
    html = _table_html(_table_pdf(("FY2026", "FY2025"), "Particulars"))
    assert "Line item" not in html and "Particulars" in html.split("<thead>")[1].split("</thead>")[0]
    assert "Line item" not in _table_html(_table_pdf(("FY2026", "FY2025"), None))


def test_a_chart_read_from_a_picture_is_one_picture_on_a_slide_with_its_labels():
    from app.render.elements import page_elements
    doc = pymupdf.open()
    p = doc.new_page(width=W, height=H)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 16, 16), 0)
    pix.set_rect(pix.irect, (200, 210, 240))
    p.insert_image(pymupdf.Rect(29, 143, 217, 255), pixmap=pix)                  # the bars
    p.insert_text((22, 125), "Snacks", fontname="hebo", fontsize=14)              # its name, over it
    p.insert_text((188, 139), "143000", fontname="hebo", fontsize=10)             # over its tallest bar
    for x, y in ((35, "2020"), (87, "2025"), (140, "2030"), (192, "2035")):
        p.insert_text((x, 276), y, fontname="hebo", fontsize=10)                  # its axis, under it
    p.insert_text((40, 40), "Market size by category", fontname="hebo", fontsize=26)
    schema = {"figures": {}, "pages": [], "sections": [{"blocks": [
        {"id": "b1", "type": "chart", "extracted": True, "from_image": True, "points": [{}],
         "source": {"page": 1, "bbox": [19.0, 113.0, 227.0, 285.0]}}]}]}
    els = page_elements(p, schema, 1, [[19.0, 113.0, 227.0, 285.0]])
    texts = [e.text for e in els if e.kind == "text"]
    assert [e.kind for e in els].count("picture") == 1
    assert not any(t in " ".join(texts) for t in ("Snacks", "143000", "2020", "2035"))
    assert "Market size by category" in " ".join(texts)
