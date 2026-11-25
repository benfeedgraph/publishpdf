"""Issue #11: no PDF page is folded into a neighbour or lost without a word.

Section boundaries come from the PDF itself: a page break ends a section, and a page's own title
(its top line in its largest type, clearly bigger than the body text) opens the page's section.
The page build records where every page went (the page map), and the page check reports any page
with content that isn't under a heading of its own, or isn't on the web page at all.

The PDFs here are built in the test: slides (960 x 540) with a 12pt body, 24pt titles.
"""
from __future__ import annotations

import re

import pymupdf
import pytest

from app import validation
from app.extraction import schema_builder
from app.extraction.page_titles import is_wordy
from app.render import site
from app.render import theme as theming

META = {"company_name": "Acme Industries", "report_type": "investor_presentation", "fiscal_year": 2026,
        "period": "q2", "currency": "INR", "reporting_unit": "₹ crore"}
BODY = ["Our business grew across every region this quarter, led by strong demand.",
        "Margins improved as input costs eased and efficiency programmes delivered.",
        "We continued to invest in brands, distribution and new product development."]


def picture(page, rect):
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 40), 0)
    pix.clear_with(170)
    page.insert_image(pymupdf.Rect(*rect), pixmap=pix, keep_proportion=False)


def slide(doc, title=None, *, size=24, body=(), caption=None, pictures=0, table=None, top_text=None):
    p = doc.new_page(width=960, height=540)
    y = 50
    if top_text:                       # something set even larger above the title
        p.insert_text((40, y), top_text[0], fontname="hebo", fontsize=top_text[1])
        y += top_text[1] + 12
    if title:
        p.insert_text((40, y), title, fontname="hebo", fontsize=size)
        y += size + 24
    for line in body:
        p.insert_text((40, y), line, fontname="helv", fontsize=12)
        y += 18
    if table:
        for r, row in enumerate(table):
            for c, cell in enumerate(row):
                p.insert_text((40 + c * 160, y + r * 18), cell, fontname="helv", fontsize=12)
        y += len(table) * 18 + 10
    for k in range(pictures):
        picture(p, (40 + k * 220, 260, 240 + k * 220, 420))
    if caption:                        # a heading-like line at the foot of the page, nothing under it
        p.insert_text((300, 480), caption, fontname="hebo", fontsize=20)
    return p


def pdf_of(*slides_) -> bytes:
    doc = pymupdf.open()
    for kw in slides_:
        slide(doc, **kw)
    data = doc.tobytes()
    doc.close()
    return data


def extract(pdf: bytes) -> dict:
    schema, _ = schema_builder.extract(pdf, META, llm=None)
    return schema


def opening(schema: dict) -> dict[int, str]:
    """{page: heading of the first section that starts on it}"""
    out: dict[int, str] = {}
    for s in schema["sections"]:
        if s.get("heading_text") and (s.get("source") or {}).get("page"):
            out.setdefault(s["source"]["page"], s["heading_text"])
    return out


def titles(schema: dict) -> dict[int, str | None]:
    return {p["page"]: p.get("title") for p in schema["pages"]}


# ------------------------------------------------------------------ section boundaries
def test_next_pages_title_is_not_folded_into_a_caption_at_the_foot_of_the_page():
    """Issue #11's page 30: page 1 ends on a heading-like caption with nothing under it; page 2
    is its title and pictures. Page 2 opens its own section; the caption doesn't take its title."""
    s = extract(pdf_of(dict(title="Strategic sourcing support", body=BODY, caption="Agri Business capability"),
                       dict(title="Scaling up presence in Value Added Agriculture", pictures=3)))
    assert opening(s)[2] == "Scaling up presence in Value Added Agriculture"
    assert titles(s)[2] == "Scaling up presence in Value Added Agriculture"
    caption = next(x for x in s["sections"] if x["heading_text"] == "Agri Business capability")
    assert not caption.get("subheading_text")


def test_a_short_last_page_gets_its_own_section():
    s = extract(pdf_of(dict(title="Results overview", body=BODY), dict(title="Segment review", body=BODY),
                       dict(title="Thank you", body=["Investor relations: ir@example.com"])))
    assert opening(s)[3] == "Thank you"


def test_a_page_with_one_heading_and_a_few_lines_gets_its_own_section():
    s = extract(pdf_of(dict(title="Highlights", body=BODY), dict(title="Outlook", body=BODY[:1])))
    assert opening(s)[2] == "Outlook"
    outlook = next(x for x in s["sections"] if x["heading_text"] == "Outlook")
    assert {b["source"]["page"] for b in outlook["blocks"]} == {2}


def test_a_page_after_a_table_gets_its_own_section():
    table = [["Revenue", "1,234", "1,120"], ["EBITDA", "456", "401"], ["Net profit", "210", "188"]]
    s = extract(pdf_of(dict(title="Financial summary", table=table), dict(title="Business segments", body=BODY)))
    assert opening(s)[2] == "Business segments"
    assert all(b["source"]["page"] == 1 for x in s["sections"] if x["heading_text"] == "Financial summary" for b in x["blocks"])


def test_the_first_page_title_opens_the_first_section():
    s = extract(pdf_of(dict(title="Investor presentation", body=BODY[:1]), dict(title="Agenda", body=BODY)))
    assert opening(s)[1] == "Investor presentation"
    assert titles(s)[1] == "Investor presentation"


def test_a_full_sentence_title_still_opens_its_page():
    """Slide titles are often sentences, even ones ending in a full stop: longer than the
    general heading rules allow. In the page's largest type, at its top, it is still the title."""
    long = "Tech innovations have continuously redefined how enterprises operate and grow."
    s = extract(pdf_of(dict(title="Why now", body=BODY), dict(title=long, body=BODY)))
    # (a lone full stop is set aside as a stray mark by the noise filter, as before)
    assert opening(s)[2].rstrip(".") == long.rstrip(".")
    assert titles(s)[2].rstrip(".") == long.rstrip(".")


def test_a_page_without_a_title_continues_the_section_before():
    s = extract(pdf_of(dict(title="Management commentary", body=BODY), dict(body=BODY + BODY)))
    assert 2 not in opening(s)
    assert titles(s)[2] is None
    commentary = next(x for x in s["sections"] if x["heading_text"] == "Management commentary")
    assert {b["source"]["page"] for b in commentary["blocks"]} == {1, 2}


def test_a_running_title_on_every_page_is_no_pages_own_title():
    s = extract(pdf_of(*[dict(title="Acme Industries Quarterly Update", body=BODY) for _ in range(4)]))
    assert all(t is None for t in titles(s).values())


def test_a_big_figure_at_the_top_is_not_the_pages_title():
    s = extract(pdf_of(dict(title="Overview", body=BODY),
                       dict(title="Quarter highlights", size=20, top_text=("₹627bn", 36), body=BODY)))
    assert titles(s)[2] == "Quarter highlights"


def test_a_title_and_its_subtitle_on_the_same_page_still_join():
    """The join of a heading-only line with the next heading stays within a page. (The subtitle
    starts with a capital: a line starting in lower case carries on the text above it, and is
    never a heading of its own.)"""
    doc = pymupdf.open()
    p = doc.new_page(width=960, height=540)
    p.insert_text((40, 60), "Creating Multiple Drivers of Growth", fontname="hebo", fontsize=24)
    p.insert_text((40, 96), "Through Responsible Competitiveness", fontname="hebo", fontsize=20)
    for i, line in enumerate(BODY):
        p.insert_text((40, 150 + 18 * i), line, fontname="helv", fontsize=12)
    s = extract(doc.tobytes())
    first = s["sections"][0]
    assert first["heading_text"] == "Creating Multiple Drivers of Growth"
    assert first["subheading_text"] == "Through Responsible Competitiveness"


@pytest.mark.parametrize("text,ok", [("Scaling up presence in Value Added Agriculture", True), ("₹627bn", False),
                                     ("15,2", False), ("India's fastest-growing FMCG company,", False), ("Q2 FY26 Results", True),
                                     ("3 Million+", False), ("Investor Day 2023", True),
                                     ("(ご参考)所在地別営業利益（３ヶ月）", True), ("（ご参考）中国事業/⾦融セグメント（３ヶ月）", True)])
def test_a_title_is_words(text, ok):
    assert is_wordy(text) is ok


# ------------------------------------------------------------------ the page build and the page check
def render(pdf: bytes):
    schema, artifact = schema_builder.extract(pdf, META, llm=None)
    theme, _ = theming.enforce(theming.merge(theming.DEFAULT_THEME, {}))
    pm: list = []
    files = site.render_report(schema, theme=theme, disclaimer=None, pdf_bytes=pdf, layouts={}, page_map_sink=pm)
    html = " ".join(d.decode() for n, d in files.items() if n.endswith(".html"))
    return schema, artifact, files, pm, html


def test_every_page_is_mapped_anchored_and_under_its_own_heading():
    pdf = pdf_of(dict(title="Strategic sourcing support", body=BODY, caption="Agri Business capability"),
                 dict(title="Scaling up presence in Value Added Agriculture", pictures=3),
                 dict(title="Outlook", body=BODY))
    schema, artifact, files, pm, html = render(pdf)
    assert [e["page"] for e in pm] == [1, 2, 3]
    assert all(e["own_section"] for e in pm), pm
    assert pm[1]["heading"] == "Scaling up presence in Value Added Agriculture"
    assert set(re.findall(r'class="pa" id="p(\d+)"', html)) == {"1", "2", "3"}
    assert validation.check_page_coverage(schema, artifact, pm) == []


def test_a_title_printed_over_a_picture_still_opens_its_page():
    """A slide whose title sits on a picture (a banner across its top): the title's words lie on
    the picture, yet the page still opens its own section with that title, and nothing is left
    under the previous page's heading."""
    doc = pymupdf.open()
    slide(doc, title="Foods business overview", body=BODY)
    p = doc.new_page(width=960, height=540)
    picture(p, (0, 0, 960, 110))
    p.insert_text((40, 70), "Cloud Kitchens: better scalability", fontname="hebo", fontsize=24)
    for i, line in enumerate(BODY):
        p.insert_text((40, 160 + 18 * i), line, fontname="helv", fontsize=12)
    schema, artifact, files, pm, html = render(doc.tobytes())
    page2 = next(x for x in schema["pages"] if x["page"] == 2)
    assert page2["title"] == "Cloud Kitchens: better scalability" and page2["title_on_picture"]
    assert opening(schema)[2] == "Cloud Kitchens: better scalability"
    assert pm[1]["own_section"] and pm[1]["heading"] == "Cloud Kitchens: better scalability"
    assert validation.check_page_coverage(schema, artifact, pm) == []


def test_the_check_reports_a_titled_page_left_under_another_heading():
    """Should a page with its own title still end up under another page's heading (here: the
    page build is told so), the check says which page and where its content went."""
    schema = schema_with({1: "Foods business overview", 2: "Cloud Kitchens: better scalability"},
                         {1: "growth margins brands", 2: "delivery kitchens scale"})
    pm = [entry(1), entry(2, own_section=False, heading="Foods business overview", heading_page=1)]
    issues = validation.check_page_coverage(schema, {"pages": []}, pm)
    assert [i.page for i in issues] == [2]
    assert "\u201cCloud Kitchens: better scalability\u201d) is under \u201cFoods business overview\u201d from page 1" in issues[0].message


def test_captions_side_by_side_on_a_picture_are_not_one_title():
    doc = pymupdf.open()
    slide(doc, title="Overview", body=BODY)
    p = doc.new_page(width=960, height=540)
    picture(p, (0, 0, 960, 140))
    p.insert_text((40, 70), "Creating desirable brands", fontname="hebo", fontsize=24)
    p.insert_text((560, 70), "Future-proof portfolio", fontname="hebo", fontsize=24)
    for i, line in enumerate(BODY):
        p.insert_text((40, 190 + 18 * i), line, fontname="helv", fontsize=12)
    s = extract(doc.tobytes())
    assert titles(s)[2] == "Creating desirable brands"


def test_two_big_divider_slides_dont_swallow_the_report_into_two_chapters():
    """A 14-slide deck: every slide has its own title (24pt), and two are dividers in much bigger
    type. Chapters made of only the two dividers would put 12 slides in one; so every titled
    slide stays a chapter of the web edition."""
    slides = [dict(title=f"Business review part {chr(65 + i)}", body=BODY) for i in range(14)]
    slides[6] = dict(title="Section two", size=44)
    slides[13] = dict(title="Section three", size=44)
    *_, pm, _html = render(pdf_of(*slides))
    chapters = {e["chapter"] for e in pm if e["chapter"]}
    assert len(chapters) > 2, chapters


def test_a_picture_led_page_keeps_its_title_as_a_heading_on_the_web_page():
    pdf = pdf_of(dict(title="Overview", body=BODY), dict(title="Our brands", pictures=3))
    *_, pm, html = render(pdf)
    assert pm[1]["own_section"]
    assert re.search(r"<h[1-6][^>]*>\s*(<[^>]+>\s*)*Our brands", html), "the page's title is a heading"


def schema_with(titles_: dict[int, str | None], words: dict[int, str]) -> dict:
    return {"pages": [{"page": n, "title": t} for n, t in titles_.items()],
            "sections": [{"heading_text": "", "source": {"page": n}, "blocks": [{"source": {"page": n}, "runs": [{"t": w}]}]}
                         for n, w in words.items()]}


def entry(n, **kw):
    return {"page": n, "title": None, "shown": "text", "own_section": True, "section": f"s{n}", "heading": None,
            "heading_page": None, "hidden_heading": False, "chapter": None, **kw}


def test_the_check_names_a_page_that_sits_under_another_pages_heading():
    schema = schema_with({29: "Strategic sourcing", 30: "Scaling up presence in Value Added Agriculture"},
                         {29: "wheat potato milk", 30: "farmland frozen vegetables"})
    pm = [entry(29), entry(30, shown="rearranged", own_section=False, heading="Agri Business capability", heading_page=29)]
    issues = validation.check_page_coverage(schema, {"pages": []}, pm)
    assert [(i.check, i.severity, i.page) for i in issues] == [("completeness", "warning", 30)]
    assert issues[0].message == (
        "Page 30 has no section of its own on the web page: its content (“Scaling up presence in Value Added "
        "Agriculture”) is under “Agri Business capability” from page 29. Compare it with the PDF before publishing.")


def test_the_check_names_a_picture_page_that_is_missing():
    schema = schema_with({1: "Overview", 2: None}, {1: "growth margins brands"})
    pm = [entry(1), entry(2, shown="none", own_section=False)]
    issues = validation.check_page_coverage(schema, {"pages": [{"page": 2, "image_regions": [[0, 0, 10, 10]]}]}, pm)
    assert [i.message for i in issues] == [
        "Page 2 of the PDF isn't on the web page: neither its text nor its pictures are there. Compare it with the PDF before publishing."]


def test_the_check_leaves_out_contents_pages_covers_and_repeated_pages():
    agenda = "about company story journey brands"
    schema = schema_with({1: None, 2: None, 3: None, 4: None}, {2: agenda, 3: "results " + agenda, 4: agenda})
    pm = [entry(1, shown="cover", own_section=False), entry(2, shown="left_out", own_section=False),
          entry(3), entry(4, shown="none", own_section=False)]
    art = {"pages": [{"page": 4, "image_regions": [[0, 0, 10, 10]]}]}
    assert validation.check_page_coverage(schema, art, pm) == []


def test_a_page_that_continues_the_section_before_is_fine():
    schema = schema_with({1: "Commentary", 2: None}, {1: "growth", 2: "margins improved"})
    pm = [entry(1), entry(2, own_section=False, heading="Commentary", heading_page=1)]
    assert validation.check_page_coverage(schema, {"pages": []}, pm) == []


# ------------------------------------------------------------------ the title as a re-arranged page's heading
def El(id_, text, kind="text"):
    from app.render.elements import Element
    return Element(id_, kind, (0, 0, 100, 12), text=text, html=text)


def test_a_title_laid_out_as_a_label_becomes_the_heading_where_it_stood():
    els = [El("t1", "Net interest income"), El("t2", "Net interest income, quarterly"), El("p1", "", "picture")]
    secs = [{"type": "chips", "items": [["t1"], ["t2"]]}, {"type": "picture", "picture": "p1", "caption": []}]
    out = site.title_first(secs, els, "Net interest income")
    assert out[0] == {"type": "heading", "level": 1, "ids": ["t1"]}
    assert out[1] == {"type": "chips", "items": [["t2"]]}
    assert out[2]["type"] == "picture"


def test_a_title_inside_a_picture_beside_text_block_is_lifted_out():
    els = [El("t1", "Our brands"), El("t2", "Twenty five brands built in fifteen years"), El("p1", "", "picture")]
    secs = [{"type": "media", "picture": "p1", "side": "left",
             "body": [{"type": "paragraph", "ids": ["t1"]}, {"type": "paragraph", "ids": ["t2"]}]}]
    out = site.title_first(secs, els, "Our brands")
    assert out[0] == {"type": "heading", "level": 1, "ids": ["t1"]}
    assert out[1]["body"] == [{"type": "paragraph", "ids": ["t2"]}]


def test_a_title_already_a_heading_is_left_alone():
    els = [El("t1", "Outlook"), El("t2", "We expect growth to continue")]
    secs = [{"type": "heading", "level": 2, "ids": ["t1"]}, {"type": "paragraph", "ids": ["t2"]}]
    assert site.title_first(secs, els, "Outlook") == secs
    assert site.title_first(secs, els, None) == secs
