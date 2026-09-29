"""FAQ-style reports: bold numbered questions become sections, wrapped links never become
KPI tiles, and the PDF's charts/cover are carried into the site as images."""

from __future__ import annotations

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


def test_one_report_is_one_page_in_the_pdf_order():
    pdf = _faq_pdf()
    schema, _ = extract(pdf, META)
    files = site.render_report(schema, theme=theming.validate({}), disclaimer="d", pdf_bytes=pdf)
    base = site.report_base_path(schema["metadata"]).lstrip("/")
    # Exactly one HTML page per report: no section pages, no separate "full" page.
    assert [k for k in files if k.endswith(".html")] == [base + "index.html"]
    landing = files[base + "index.html"].decode()
    assert validation.check_bundle(schema, files) == []
    # Every question, in the PDF's order, as real text on that page (sections are anchors).
    qs = [s for s in schema["sections"] if story.question_parts(schema, s)]
    pos = [landing.index(f'id="{s["slug"]}"') for s in qs]
    assert pos == sorted(pos) and "diversified portfolio" in landing
    # Search/answer engines: FAQ microdata around the visible answers.
    assert landing.count('itemtype="https://schema.org/Question"') == 3 and 'itemprop="acceptedAnswer"' in landing
    # Printed web addresses are links, re-joined where the PDF wrapped them.
    assert 'href="https://example.com/content/dam/pdfs/financial-result/quarterly-results-2026-2027/june-2026/Presentation-Q1-FY2027.pdf"' in landing
    # The cover and the chart are carried over as images from the PDF.
    pngs = [k for k in files if k.endswith(".png")]
    assert base + "media/cover.png" in pngs and len(pngs) >= 2 and 'class="dv' in landing
    # Without the PDF (theme editor preview) the page still renders, just without images.
    bare = site.render_report(schema, theme=theming.validate({}), disclaimer="d")
    assert not any(k.endswith(".png") for k in bare) and 'class="doc"' in bare[base + "index.html"].decode()
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
