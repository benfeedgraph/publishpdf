"""Issue #11: a "Contents" tab printed on every page doesn't make pages contents pages.
Issue #9: text that isn't a title never becomes a heading — a number or a mark, invisible text,
a line shown only as a picture of itself, or the labels of a diagram with nothing under them.

The PDFs here are built in the test."""
from __future__ import annotations

import re

import pymupdf

from app import validation
from app.extraction import layout, page_titles, schema_builder
from app.extraction.pdf import PageData, text_layer_words
from app.render import ai_layout, site, web_layout
from app.render import theme as theming
from app.render.elements import Element

META = {"company_name": "Acme Industries", "report_type": "annual_report", "fiscal_year": 2026,
        "period": "fy", "currency": "INR", "reporting_unit": "₹ crore"}
TEXT = "Our business grew across every region this year, led by strong demand from our customers."
TOPICS = ["Reflections", "Report highlights", "Goals and progress", "Climate change", "Resources"]
WORDS = ["rural distribution and the reach of our brands", "capital expenditure on new plants",
         "working capital and the cash conversion cycle", "water energy and packaging targets",
         "recycled materials in every product line"]


def _report(*, nav_tab: bool, contents: bool) -> bytes:
    """A cover, page 2 (the report's contents page, or a page of its own), then a page per topic.
    With `nav_tab`, every page carries a small "CONTENTS" link in its top corner."""
    doc = pymupdf.open()
    for n in range(1, 8):
        p = doc.new_page(width=612, height=792)
        if nav_tab:
            p.insert_text((28, 32), "CONTENTS", fontname="helv", fontsize=7)
        if n == 1:
            p.insert_text((72, 300), "Environmental Progress Report", fontname="hebo", fontsize=30)
        elif n == 2 and contents:
            p.insert_text((72, 100), "Contents", fontname="hebo", fontsize=36)
            for k, t in enumerate(TOPICS):
                p.insert_text((72, 160 + 30 * k), f"{t}  {k + 3}", fontname="helv", fontsize=14)
        else:
            k = (n - 3) % len(TOPICS)
            p.insert_text((72, 100), TOPICS[k], fontname="hebo", fontsize=28)
            p.insert_textbox(pymupdf.Rect(72, 130, 540, 500), f"This page is about {WORDS[k]}. {TEXT} " * 3,
                             fontname="helv", fontsize=11)
    return doc.tobytes()


def test_a_contents_tab_on_every_page_doesnt_make_pages_contents_pages():
    assert site._contents_pages(_report(nav_tab=True, contents=True)) == {2}      # the real one, set large
    assert site._contents_pages(_report(nav_tab=True, contents=False)) == set()
    assert site._contents_pages(_report(nav_tab=False, contents=True)) == {2}


def test_pages_behind_a_contents_tab_are_on_the_web_page():
    pdf = _report(nav_tab=True, contents=True)
    schema, _ = schema_builder.extract(pdf, META)
    assert site.left_out_pages(schema, pdf) == {2}
    files = site.render_report(schema, theme=theming.validate({}), disclaimer=None, pdf_bytes=pdf)
    text = " ".join(d.decode() for n, d in files.items() if n.endswith(".html"))
    for words in WORDS[:3]:                       # pages 3-5 were left out as "contents pages"
        assert words in text
    assert validation.check_text_on_site(schema, pdf, files) == []


# ------------------------------------------------------------------ invisible text


def _stamped_page() -> bytes:
    """A page with a title, its text, and a stamp's invisible text layer ("Jy~", set large)."""
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    p.insert_text((72, 80), "Independent Auditor's Report", fontname="hebo", fontsize=16)
    p.insert_textbox(pymupdf.Rect(72, 100, 520, 400), f"{TEXT} " * 6, fontname="helv", fontsize=10)
    p.insert_text((350, 540), "Jy~", fontname="helv", fontsize=56, render_mode=3)
    p.insert_text((72, 620), "Basis for Opinion", fontname="hebo", fontsize=16)
    p.insert_textbox(pymupdf.Rect(72, 640, 520, 800), f"{TEXT} " * 3, fontname="helv", fontsize=10)
    return doc.tobytes()


def test_invisible_text_is_known_as_invisible():
    doc = pymupdf.open(stream=_stamped_page(), filetype="pdf")
    words, _, _ = text_layer_words(doc[0])
    assert {w.text: w.invisible for w in words if w.text in ("Jy~", "Independent", "Opinion")} == \
        {"Jy~": True, "Independent": False, "Opinion": False}


def test_invisible_text_never_becomes_a_heading_or_a_section():
    schema, _ = schema_builder.extract(_stamped_page(), META)
    heads = [s["heading_text"] for s in schema["sections"]]
    assert heads == ["Independent Auditor's Report", "Basis for Opinion"]
    assert not any("jy" in s["slug"] for s in schema["sections"])


def test_invisible_text_is_never_a_pages_title():
    doc = pymupdf.open(stream=_stamped_page(), filetype="pdf")
    words, _, _ = text_layer_words(doc[0])
    p = PageData(1, 595, 842, "text_layer", words, "test")
    els = layout.analyse_page(p, set(), 10.0)
    # moved to the top of the page, the stamp would be the page's largest type
    for e in els:
        if isinstance(e, layout.TextEl) and e.words[0].text == "Jy~":
            for w in e.words:
                w.bbox = (w.bbox[0], 20, w.bbox[2], 76)
    t = page_titles.title_element(els, 10.0, 842)
    assert t is not None and " ".join(w.text for w in t.words) == "Independent Auditor's Report"
    assert page_titles.is_wordy("Jy~ Report") and not page_titles.is_wordy("Jy~ Report", [w for w in words if w.invisible])


# ------------------------------------------------------------------ headings on rebuilt web pages


def el(i: str, text: str, *, y: float = 0, size: float = 14.0, safe: bool = True, **meta) -> Element:
    return Element(i, "text", (0, y, 300, y + size), text=text, html=text, size=size, safe=safe, meta=meta)


def H(level: int, *ids: str) -> dict:
    return {"type": "heading", "level": level, "ids": list(ids)}


def P(*ids: str) -> dict:
    return {"type": "paragraph", "ids": list(ids)}


def test_a_number_or_a_mark_is_never_a_heading_or_a_card_title():
    els = [el("t1", "74.08%"), el("t2", TEXT), el("t3", "₹"), el("t4", "Net interest income"), el("t5", "’")]
    secs = [H(2, "t1"), P("t2"), {"type": "cards", "cards": [{"title": ["t3"], "body": ["t4"], "picture": None},
                                                             {"title": ["t5"], "body": [], "picture": None}]}]
    out = web_layout.checked_headings(secs, els)
    assert out[0] == P("t1")
    assert [(c["title"], c["body"]) for c in out[2]["cards"]] == [([], ["t3", "t4"]), ([], ["t5"])]


def test_invisible_text_is_never_a_heading_on_the_web_page():
    els = [el("t1", "Jy~ Stamp", unseen=True), el("t2", TEXT)]
    assert web_layout.checked_headings([H(1, "t1"), P("t2")], els) == [P("t1"), P("t2")]


def test_a_heading_shown_as_a_picture_says_its_words_or_isnt_a_heading():
    els = [el("t1", "Q2 FY26 Q1 FY27 Q2 FY27", safe=False), el("t2", "Our franchise", safe=False), el("t3", TEXT)]
    out = web_layout.checked_headings([H(3, "t1"), H(2, "t2"), P("t3")], els)
    assert out == [P("t1"), H(2, "t2"), P("t3")]     # its unverified numbers can't be given as text
    html = ai_layout.render(out, els, crop_url=lambda e: f"/c/{e.id}.webp", W=600, H=800)
    assert re.search(r'<h3[^>]*><span class="ai-pic ai-textcrop"[^>]*><img [^>]*alt="Our franchise"', html)
    assert 'alt="Q2' not in html and 'alt=""' in html


def test_the_labels_of_a_diagram_are_text_not_headings():
    names = ["Brand Building Capability", "Agri Sourcing Expertise", "Packaging Knowhow", "Foods", "Our strategy", TEXT]
    els = [el(f"t{k}", t, y=30 * k) for k, t in enumerate(names, start=1)]
    # three labels and a centre node with nothing under them, then a real heading over its text
    secs = [{"type": "cards", "cards": [{"title": [], "body": ["t1", "t2"], "picture": None, "flow": [H(3, "t1"), H(3, "t2")]},
                                        {"title": [], "body": ["t3", "t4"], "picture": None, "flow": [H(3, "t3"), H(1, "t4")]}]},
            H(2, "t5"), P("t6")]
    out = web_layout.checked_headings(secs, els)
    assert [c["flow"] for c in out[0]["cards"]] == [[P("t1"), P("t2")], [P("t3"), P("t4")]]
    assert out[1:] == [H(2, "t5"), P("t6")]


def test_map_pins_set_as_card_titles_are_text():
    els = [el(f"t{k}", t) for k, t in enumerate(["HARIDWAR", "GUWAHATI", "MEDAK"], start=1)]
    secs = [{"type": "cards", "cards": [{"title": [f"t{k}"], "body": [], "picture": None} for k in (1, 2, 3)]}]
    out = web_layout.checked_headings(secs, els)
    assert [(c["title"], c["body"]) for c in out[0]["cards"]] == [([], ["t1"]), ([], ["t2"]), ([], ["t3"])]


def test_real_headings_are_left_alone():
    names = ["Financial performance", "Revenue", TEXT, "Margins", TEXT, "Outlook", "Demand", TEXT, "Costs"]
    els = [el(f"t{k}", t, y=30 * k) for k, t in enumerate(names, start=1)]
    # a heading over sub-headings has them under it; one heading with nothing under it is just that
    secs = [H(1, "t1"), H(2, "t2"), P("t3"), H(2, "t4"), P("t5"), H(1, "t6"), H(2, "t7"), P("t8"), H(2, "t9")]
    assert web_layout.checked_headings(secs, els) == secs
    two = [H(3, "t2"), H(3, "t4"), H(3, "t7"), P("t8")]          # two labels: not enough to call it a diagram
    assert web_layout.checked_headings(two, els) == two


def test_a_heading_beside_its_picture_is_not_a_label():
    names = ["Our plants", "Brand Building Capability", "Agri Sourcing Expertise", "Packaging Knowhow"]
    els = [el(f"t{k}", t, y=30 * k) for k, t in enumerate(names, start=1)] + [Element("p1", "picture", (320, 0, 600, 200))]
    secs = [{"type": "media", "picture": "p1", "side": "right", "body": [H(2, "t1")]}, H(3, "t2"), H(3, "t3"), H(3, "t4")]
    out = web_layout.checked_headings(secs, els)
    assert out[0]["body"] == [H(2, "t1")] and out[1:] == [P("t2"), P("t3"), P("t4")]


def test_a_lone_mark_over_a_table_is_not_its_title():
    from app.render import story
    schema = {"sections": [{"blocks": [
        {"id": "b1", "type": "paragraph", "runs": [{"t": "-"}]}, {"id": "b2", "type": "table", "runs": []},
        {"id": "b3", "type": "paragraph", "runs": [{"t": "Segment results"}]}, {"id": "b4", "type": "table", "runs": []}]}]}
    assert story.heading_like(schema) == {"b3"}


def test_a_pages_title_shown_as_a_picture_still_says_its_words():
    els = [el("t1", "Q2 FY26 results", safe=False), el("t2", TEXT, y=40)]
    secs = site.title_first(web_layout.checked_headings([H(1, "t1"), P("t2")], els), els, "Q2 FY26 results")
    html = ai_layout.render(secs, els, crop_url=lambda e: f"/c/{e.id}.webp", W=600, H=800)
    assert re.search(r'<h2[^>]*><span class="ai-pic ai-textcrop"[^>]*><img [^>]*alt="Q2 FY26 results"', html)
    # a title whose text on the page would read as a lone mark is shown as printed, with its words
    els = [Element("t1", "text", (0, 0, 300, 14), text="Revenue (₹ crores)", html="₹", size=14.0), el("t2", TEXT, y=40)]
    secs = site.title_first([P("t1"), P("t2")], els, "Revenue (₹ crores)")
    html = ai_layout.render(secs, els, crop_url=lambda e: f"/c/{e.id}.webp", W=600, H=800)
    assert re.search(r'<h2[^>]*><span class="ai-pic ai-textcrop"[^>]*><img [^>]*alt="Revenue \(₹ crores\)"', html)
