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


def test_the_report_is_one_web_page_not_a_copy_of_the_pdf():
    pdf = _faq_pdf()
    schema, _ = extract(pdf, META)
    files = site.render_report(schema, theme=theming.validate({}), disclaimer="d", pdf_bytes=pdf)
    base = site.report_base_path(schema["metadata"]).lstrip("/")
    # Exactly one HTML page per report.
    assert [k for k in files if k.endswith(".html")] == [base + "index.html"]
    html_ = files[base + "index.html"].decode()
    # A web page: hero, the questions as real headings with FAQ markup — no contents list
    # ("In this report" was removed on request), no page-by-page artwork, no side rail.
    assert 'class="web"' in html_ and 'class="web-hero"' in html_ and 'class="web-toc"' not in html_
    assert 'class="edition-rail"' not in html_ and 'class="doc-rail"' not in html_ and 'class="leaf"' not in html_
    assert html_.count('itemtype="https://schema.org/Question"') == 3
    qs = [s for s in schema["sections"] if story.question_parts(schema, s)]
    pos = [html_.index(f'id="{s["slug"]}"') for s in qs]
    assert pos == sorted(pos)                                        # the PDF's order
    # The chart is shown inline, cut from the page artwork, not as "see the PDF".
    assert 'class="web-vis"' in html_ and "This chart is an image in the PDF" not in html_
    assert all(files[k][:4] == b"RIFF" for k in files if k.endswith(".webp"))
    assert "diversified portfolio" in html_ and "What is the Company" in html_
    # …and every number reaches the page only as a figure with its exact PDF string.
    assert validation.check_bundle(schema, files) == []
    q1 = next(f for f in schema["figures"].values() if f["raw"] == "Q1" and f["source"]["page"] == 2)
    assert re.search(rf'data-fig="{q1["id"]}"[^>]*>Q1<', html_)
    # Links into the PDF's own pages ("#p2") still land somewhere.
    assert 'id="p2"' in html_


def test_bullets_are_mended_not_retyped():
    a = {"id": "a", "type": "paragraph", "runs": [{"t": "• FMCG-Others: revenue grew at a CAGR of "}, {"f": "x"}, {"t": " and Segment"}]}
    b = {"id": "b", "type": "paragraph", "runs": [{"t": "Results grew at CAGR of 27%"}]}
    c = {"id": "c", "type": "paragraph", "runs": [{"t": "Sales grew fast. • E-Commerce sales rose • Quick commerce too."}]}
    out = story.mend_lists([a, b, c])
    assert [x["id"] for x in out] == ["a", "c", "c~1", "c~2"]
    assert out[0]["runs"][-2:] == [{"t": " "}, {"t": "Results grew at CAGR of 27%"}]     # wrapped line rejoined
    assert story.bullet_runs(out[2]) == [{"t": "E-Commerce sales rose"}]               # split, glyph only
    assert "".join(r.get("t", "") for x in out[1:] for r in x["runs"]).replace("• ", "") == \
        "Sales grew fast.E-Commerce sales roseQuick commerce too."                       # no words lost or added


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
    # Contents (2) and blank (6) pages leave no artwork; the contents become "In this report".
    assert "pages/p0002.webp" not in " ".join(files) and "pages/p0006.webp" not in " ".join(files)
    assert "ACME LIMITED ANNUAL REPORT" not in html_           # running header trimmed away
    assert 'class="edition-rail"' not in html_
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


def test_a_design_led_page_is_rebuilt_as_web_sections_not_a_picture_of_the_page():
    """A product spread (pictures with short captions) becomes a web picture grid with no AI
    (built-in layout, no credits); a page of prose becomes web text. Each caption appears once."""
    doc = pymupdf.open()
    prose = doc.new_page()
    assert prose.insert_textbox(pymupdf.Rect(72, 90, 520, 700), (BODY + " ") * 4, fontname="helv", fontsize=11) >= 0
    spread = doc.new_page()
    for i, (x, y) in enumerate(((60, 120), (320, 120), (60, 420), (320, 420))):
        spread.draw_rect(pymupdf.Rect(x, y, x + 220, y + 220), color=None, fill=(0.2 + 0.15 * i, 0.4, 0.7))
        spread.insert_text((x, y + 240), f"Caption for brand range {chr(65 + i)}", fontname="helv", fontsize=9)
    pdf = doc.tobytes()
    schema, _ = extract(pdf, META)
    files = site.render_report(schema, theme=theming.validate({}), disclaimer="d", pdf_bytes=pdf)
    html_ = [v for k, v in files.items() if k.endswith(".html")][0].decode()
    assert 'class="web-page"' not in html_                              # never the page as a picture
    assert 'class="ai-page"' in html_ and "ai-gallery" in html_          # a grid of pictures + captions
    assert html_.count("Caption for brand range A") == 1                 # once, as a caption
    assert "<p>The Company continued to invest behind its brands" in html_   # the prose page is web text
    assert validation.check_bundle(schema, files) == []
