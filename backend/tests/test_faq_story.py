"""FAQ-style reports: bold numbered questions become sections, wrapped links never become
KPI tiles, and the PDF's charts/cover are carried into the site as images."""

from __future__ import annotations

import re

import pymupdf

from app import validation
from app.extraction.schema_builder import extract
from app.render import site, story
from app.render import theme as theming

META = {"company_name": "Acme Ltd", "report_type": "investor_presentation", "fiscal_year": 2027,
        "period": "q1", "currency": "INR", "reporting_unit": "crore", "version": 1, "filename": "faq.pdf"}
BODY = ("The Company continued to invest behind its brands and distribution network during the "
        "quarter, with demand remaining resilient across rural and urban markets.")


def _faq_pdf() -> bytes:
    doc = pymupdf.open()
    cover = doc.new_page()
    cover.insert_text((72, 300), "RESULTS UPDATE & FAQ", fontname="hebo", fontsize=22)
    p = doc.new_page()
    y = 72
    for q, extra in (("Q1. What is the Company's vision?", None),
                     ("Q2. How does the Company manage a diversified portfolio? What is the",
                      "governance structure?"),
                     ("Q3. Please provide a brief overview of the results.", None)):
        p.insert_text((72, y), q, fontname="hebo", fontsize=11)
        if extra:
            y += 14
            p.insert_text((72, y), extra, fontname="hebo", fontsize=11)
        y += 20
        assert p.insert_textbox(pymupdf.Rect(72, y - 10, 520, y + 60), BODY, fontname="helv", fontsize=11) >= 0
        y += 76
    p.insert_text((72, y), "Please refer to the presentation:", fontname="helv", fontsize=11)
    p.insert_text((72, y + 14), "https://example.com/content/dam/pdfs/financial-result/quarterly-results-",
                  fontname="helv", fontsize=11)
    p.insert_text((72, y + 28), "2026-2027/june-2026/Presentation-Q1-FY2027.pdf", fontname="helv", fontsize=11)
    # A small bar chart: coloured filled shapes.
    for i, h in enumerate((40, 60, 80, 100)):
        p.draw_rect(pymupdf.Rect(120 + i * 50, 700 - h, 150 + i * 50, 700), color=None, fill=(0.1, 0.3, 0.7))
    return doc.tobytes()


def test_bold_numbered_questions_become_sections():
    schema, _ = extract(_faq_pdf(), META)
    qs = [s for s in schema["sections"] if story.question_parts(schema, s)]
    assert len(qs) == 3 and story.is_faq(schema)
    # A question that wraps onto a second bold line stays one heading.
    assert qs[1]["heading_text"].endswith("governance structure?")
    marker, question = story.question_parts(schema, qs[0])
    assert "f" in marker[0] and question[0]["t"].startswith("What is")


def test_a_wrapped_link_is_not_a_kpi_tile():
    schema, _ = extract(_faq_pdf(), META)
    stats = [b for s in schema["sections"] for b in s["blocks"] if b["type"] == "stat"]
    assert not stats


def test_the_report_is_one_page_that_looks_like_the_pdf():
    pdf = _faq_pdf()
    schema, _ = extract(pdf, META)
    files = site.render_report(schema, theme=theming.validate({}), disclaimer="d", pdf_bytes=pdf)
    base = site.report_base_path(schema["metadata"]).lstrip("/")
    # Exactly one HTML page per report; every PDF page is on it, with its artwork.
    assert [k for k in files if k.endswith(".html")] == [base + "index.html"]
    html_ = files[base + "index.html"].decode()
    assert html_.count('<section class="part') == 2 and 'id="p1"' in html_ and 'id="p2"' in html_
    assert 'class="pg' not in html_ and 'class="flow"' in html_          # one continuous page, no page frames
    assert {base + "pages/p0001.webp", base + "pages/p0002.webp"} <= set(files)
    assert all(files[k][:4] == b"RIFF" for k in files if k.endswith(".webp"))
    assert any(k.endswith(".woff2") for k in files) and any(k.endswith("-OFL.txt") for k in files)
    # The words are real text on the page (search and answer engines read them)…
    assert "diversified portfolio" in html_ and "What is the Company" in html_
    # …and every number reaches the page only as a figure with its exact PDF string.
    assert validation.check_bundle(schema, files) == []
    q1 = next(f for f in schema["figures"].values() if f["raw"] == "Q1" and f["source"]["page"] == 2)
    assert f'data-fig="{q1["id"]}">Q1<' in html_
    # The page scales to any screen: positions and sizes are shares of the page width.
    assert "--x:" in html_ and "cqw" in html_


def test_without_the_pdf_the_text_is_laid_out_as_one_flowing_page():
    schema, _ = extract(_faq_pdf(), META)
    base = site.report_base_path(schema["metadata"]).lstrip("/")
    files = site.render_report(schema, theme=theming.validate({}), disclaimer="d")
    landing = files[base + "index.html"].decode()
    assert 'class="doc"' in landing and validation.check_bundle(schema, files) == []
    qs = [s for s in schema["sections"] if story.question_parts(schema, s)]
    pos = [landing.index(f'id="{s["slug"]}"') for s in qs]
    assert pos == sorted(pos)
    assert landing.count('itemtype="https://schema.org/Question"') == 3
    assert 'href="https://example.com/content/dam/pdfs/financial-result/quarterly-results-2026-2027/june-2026/Presentation-Q1-FY2027.pdf"' in landing
    # The retired "story" layout in a saved theme is ignored, not an error.
    assert "layout" not in theming.validate({"layout": "story"})


def test_a_sentence_split_by_a_page_break_is_one_paragraph():
    a = {"id": "p1", "type": "paragraph", "runs": [{"t": "Gross Revenue grew"}], "source": {"page": 4}}
    b = {"id": "p2", "type": "paragraph", "runs": [{"t": "while Net Revenue declined."}], "source": {"page": 5}}
    c = {"id": "p3", "type": "paragraph", "runs": [{"t": "A new point."}], "source": {"page": 6}}
    joined = story._join_across_pages([a, b, c])
    assert [x["id"] for x in joined] == ["p1", "p3"]
    assert joined[0]["runs"] == [{"t": "Gross Revenue grew"}, {"t": " "}, {"t": "while Net Revenue declined."}]


def test_visual_floats_beside_text_the_way_the_pdf_wraps_it():
    schema = {"sections": [{"id": "s1", "blocks": [
        {"id": "p1", "type": "paragraph", "runs": [], "source": {"page": 1, "bbox": [70, 100, 525, 300]}},
        {"id": "c1", "type": "chart", "source": {"page": 1, "bbox": [300, 100, 525, 200]}},
        {"id": "p2", "type": "paragraph", "runs": [], "source": {"page": 1, "bbox": [70, 320, 525, 400]}},
        {"id": "c2", "type": "chart", "source": {"page": 1, "bbox": [180, 420, 420, 520]}},
    ]}]}
    lay = story.document_layout(schema, {"c1": {}, "c2": {}})
    assert lay["c1"]["float"] == "right" and lay["c1"]["w"] == 49
    assert lay["c2"]["float"] is None                    # centred, nothing beside it
    order = [b["id"] for b in story.document_order(schema, lay)["s1"]]
    assert order == ["c1", "p1", "p2", "c2"]             # a float precedes the text around it


def test_index_table_links_entries_to_their_sections():
    figs = {f"f{i}": {"id": f"f{i}", "raw": str(p), "value": str(p), "kind": "number", "status": "active"}
            for i, p in enumerate((2, 2, 3, 3))}
    rows = [{"label": [{"t": t}], "label_text": t, "cells": [{"runs": [{"f": f"f{i}"}]}]}
            for i, t in enumerate(("Vision & Mission", "Core Values", "Strategy elements", "Dividend policy"))]
    schema = {"figures": figs, "sections": [
        {"id": "s0", "slug": "other", "heading": [], "heading_text": "", "source": {"page": 1},
         "blocks": [{"id": "t", "type": "table", "columns": [{}], "rows": rows}]},
        {"id": "s1", "slug": "q1", "heading": [{"t": "x"}], "heading_text": "Q1. What is the Vision and Mission?", "source": {"page": 2}, "blocks": []},
        {"id": "s2", "slug": "q2", "heading": [{"t": "x"}], "heading_text": "Q2. What are the Core Values?", "source": {"page": 2}, "blocks": []},
        {"id": "s3", "slug": "q3", "heading": [{"t": "x"}], "heading_text": "Q3. Key elements of strategy?", "source": {"page": 3}, "blocks": []},
        {"id": "s4", "slug": "q4", "heading": [{"t": "x"}], "heading_text": "Q4. What is the dividend policy?", "source": {"page": 3}, "blocks": []},
    ]}
    assert story.index_links(schema, 5) == {"t": {0: "q1", 1: "q2", 2: "q3", 3: "q4"}}


def test_links_and_wrapped_hyphens():
    figs = {"x": {"raw": "2026/report.pdf"}}
    out = story.linkify([{"t": "See https://a.com/files- "}, {"f": "x"}, {"t": " for best- in-class detail."}], figs)
    assert out[1] == {"a": "https://a.com/files-2026/report.pdf", "runs": [{"t": "https://a.com/files-"}, {"f": "x"}]}
    assert out[2] == {"t": " for best-in-class detail."}
    assert story.lead_label([{"t": "Vision: Our brands"}]) == ("Vision:", [{"t": "Our brands"}])
    assert story.lead_label([{"t": "The Company said: yes"}]) is None


def test_bullet_paragraphs_lose_only_the_glyph():
    assert story.bullet_runs({"runs": [{"t": "• The Company continues"}, {"f": "p1f1"}]}) == \
        [{"t": "The Company continues"}, {"f": "p1f1"}]
    assert story.bullet_runs({"runs": [{"t": "-based pricing"}]}) is None
    assert story.bullet_runs({"runs": [{"t": "Plain text"}]}) is None


def test_continuous_page_drops_page_furniture_contents_and_blank_pages():
    doc = pymupdf.open()
    for _ in range(6):
        doc.new_page()
    for n in range(1, 7):
        p = doc[n - 1]
        p.insert_text((60, 40), "ACME LIMITED ANNUAL REPORT", fontname="helv", fontsize=8)     # running header
        p.insert_text((290, 820), str(n), fontname="helv", fontsize=8)                          # page number
        if n == 2:                                             # contents page, linked
            for k, (label, target) in enumerate([("Cover", 1), ("Chairman's message", 3), ("Our businesses", 4),
                                                 ("Strategy", 5), ("Governance", 6)]):
                y = 150 + 30 * k
                p.insert_text((72, y), label, fontname="helv", fontsize=12)
                p.insert_link({"kind": pymupdf.LINK_GOTO, "page": target - 1,
                               "from": pymupdf.Rect(70, y - 12, 400, y + 4)})
        elif n == 6:
            pass                                               # blank apart from furniture
        else:
            assert p.insert_textbox(pymupdf.Rect(72, 120, 520, 300), BODY, fontname="helv", fontsize=11) >= 0
    pdf = doc.tobytes()
    schema, _ = extract(pdf, META)
    files = site.render_report(schema, theme=theming.validate({}), disclaimer="d", pdf_bytes=pdf)
    base = site.report_base_path(schema["metadata"]).lstrip("/")
    html_ = files[base + "index.html"].decode()
    parts = re.findall(r'<section class="part[^"]*" id="p(\d+)"', html_)
    assert parts == ["1", "3", "4", "5"]                        # contents (2) and blank (6) pages left out
    assert "ACME LIMITED ANNUAL REPORT" not in html_           # running header trimmed away
    nav = html_[html_.index('class="edition-rail"'):html_.index("</nav>", html_.index('class="edition-rail"'))]
    assert 'href="#p3"' in nav and "Chairman" in nav and "Governance" not in nav   # its target page was blank
    assert validation.check_bundle(schema, files) == []


def test_parallel_and_serial_builds_are_identical(monkeypatch):
    doc = pymupdf.open()
    for n in range(12):
        p = doc.new_page()
        assert p.insert_textbox(pymupdf.Rect(72, 120, 520, 300), f"Q{n + 1}. " + BODY, fontname="helv", fontsize=11) >= 0
    pdf = doc.tobytes()
    schema, _ = extract(pdf, META)
    theme = theming.validate({})
    par = site.render_report(schema, theme=theme, disclaimer="d", pdf_bytes=pdf)
    from app.render import pages as pages_mod
    monkeypatch.setattr(pages_mod, "_workers", lambda: 1)
    ser = site.render_report(schema, theme=theme, disclaimer="d", pdf_bytes=pdf)
    assert par.keys() == ser.keys() and all(par[k] == ser[k] for k in par)
    assert validation.check_bundle(schema, par) == []


def test_phone_view_reads_a_three_column_panel_column_by_column():
    def para(bid, x0, y0, text):
        return {"kind": "paragraph", "sec": None,
                "b": {"id": bid, "type": "paragraph", "runs": [{"t": text}], "source": {"page": 1, "bbox": [x0, y0, x0 + 150, y0 + 10]}}}
    cols = [para(f"{c}{r}", 50 + c * 170, 200 + r * 12, f"col{c} line{r}") for r in range(3) for c in range(3)]
    title = {**para("t", 50, 150, "Title"), "b": {**para("t", 50, 150, "Title")["b"], "source": {"page": 1, "bbox": [50, 150, 540, 170]}}}
    out = story.reflow_order([title] + cols)
    texts = ["".join(r["t"] for r in o["b"]["runs"]) for o in out]
    assert texts == ["Title", "col0 line0 col0 line1 col0 line2", "col1 line0 col1 line1 col1 line2",
                     "col2 line0 col2 line1 col2 line2"]
