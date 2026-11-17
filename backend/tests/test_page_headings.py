"""Issue #9: labels, half-lines and numbers taken for headings.

  - a slide template's full-page background no longer hides the slide's pictures, so the labels
    and data labels printed on a diagram or chart stay with it;
  - a picture holding running text is a text box: its text stays text;
  - a heading set on two lines is one heading; a heading that stops mid-phrase, a number, a short
    heading printed twice, and a group of short labels with nothing under them are not headings —
    a page's title is never taken for a label;
  - on a slide shown as designed, the template's background is not a picture (its words stay
    real text), and the headings there follow the same rules.
"""

from __future__ import annotations

import pymupdf

from app.extraction import layout
from app.extraction.pdf import PageData, Word, _classify
from app.render import site, web_layout
from app.render.elements import Element, page_elements

W, H = 960.0, 540.0
BODY = 16.0


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


def headings(els) -> list[str]:
    return [" ".join(w.text for w in e.words) for e in els if isinstance(e, layout.TextEl) and e.kind == "heading"]


def paragraphs(els) -> list[str]:
    return [" ".join(w.text for w in e.words) for e in els if isinstance(e, layout.TextEl) and e.kind == "paragraph"]


def charts(els) -> list[layout.ChartEl]:
    return [e for e in els if isinstance(e, layout.ChartEl)]


def _pix(rgb=(200, 210, 240)):
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 16, 16), 0)
    pix.set_rect(pix.irect, rgb)
    return pix


# ------------------------------------------------------------------ pictures on a slide background


def _slide(with_pictures: bool) -> pymupdf.Page:
    doc = pymupdf.open()
    p = doc.new_page(width=W, height=H)
    p.insert_image(pymupdf.Rect(-20, 0, W + 20, H), pixmap=_pix((240, 244, 250)))   # the template
    if with_pictures:
        p.insert_image(pymupdf.Rect(300, 120, 480, 300), pixmap=_pix())              # a diagram
        p.insert_image(pymupdf.Rect(520, 120, 700, 300), pixmap=_pix((180, 220, 180)))
    p.insert_text((40, 60), "Strategic directions for the consumer goods business", fontname="helv", fontsize=24)
    p.insert_text((330, 200), "Consumer", fontname="hebo", fontsize=18)
    return p


def test_a_slide_background_does_not_hide_the_pictures_on_the_slide():
    _w, method, _r, regions, _h = _classify(_slide(with_pictures=True))
    assert method == "text_layer"
    assert sorted(round(r[0]) for r in regions) == [300, 520]          # the background isn't one


def test_a_page_sized_image_alone_is_still_a_scan_with_a_text_layer():
    _w, method, reason, regions, _h = _classify(_slide(with_pictures=False))
    assert method == "text_layer" and regions == [] and "previously OCR'd scan" in reason


# ------------------------------------------------------------------ headings


def test_diagram_labels_on_a_picture_stay_with_the_picture():
    ws = line("Strategic directions – ITC FMCG", 25, 17, size=28, bold=True)
    for i, t in enumerate(("Consumer", "Connect &", "Brand", "Affinity")):
        ws += line(t, 450, 162 + 20 * i, size=18, bold=True)
    for i, t in enumerate(("Agile &", "Distributed", "Supply", "Chain")):
        ws += line(t, 450, 300 + 20 * i, size=18, bold=True)
    els = analyse(page(ws, image_regions=[(425, 105, 584, 262), (427, 264, 586, 420)]))
    assert headings(els) == ["Strategic directions – ITC FMCG"]
    assert [" ".join(w.text for w in c.axis) for c in charts(els)] == ["Consumer Connect & Brand Affinity",
                                                                      "Agile & Distributed Supply Chain"]


def test_a_text_box_drawn_as_a_picture_keeps_its_text():
    ws = line("Invest in cutting-edge product", 31, 93) + line("development & R&D capabilities", 31, 112) \
        + line("Leverage LSTC for innovation", 31, 150)
    els = analyse(page(ws, image_regions=[(14, 74, 306, 237)]))
    assert not charts(els)
    assert "Leverage LSTC for innovation" in " ".join(paragraphs(els))


def test_a_heading_set_on_two_lines_is_one_heading():
    ws = line("Staples, Convenience Foods and", 148, 137, size=20.7, bold=True) \
        + line("Health & Hygiene Products", 173, 164, size=20.6, bold=True) + line("c. 75% of the portfolio", 195, 208)
    assert headings(analyse(page(ws))) == ["Staples, Convenience Foods and Health & Hygiene Products"]


def test_a_title_and_its_subtitle_stay_two_headings():
    ws = line("Cutting-edge R&D capability", 20, 20, size=24, bold=True) \
        + line("Life Sciences & Technology Centre", 20, 48, size=24, bold=True) + line("Over 350 scientists at work", 20, 90)
    assert headings(analyse(page(ws))) == ["Cutting-edge R&D capability", "Life Sciences & Technology Centre"]


def test_a_heading_that_stops_mid_phrase_is_not_a_heading():
    ws = line("Market share gains in", 40, 40, size=24, bold=True) + line("Atta and noodles grew fastest", 40, 120)
    els = analyse(page(ws))
    assert headings(els) == [] and "Market share gains in" in paragraphs(els)


def test_short_labels_with_nothing_under_them_are_not_sections_but_the_title_is():
    ws = line("Manufacturing Excellence", 35, 22, size=24, bold=True)
    for x, y, t in ((289, 138, "Right to Win"), (509, 178, "State-of-the-Art"), (514, 230, "Manufacturing"),
                    (100, 300, "Domain Expertise")):
        ws += line(t, x, y, size=24)
    els = analyse(page(ws))
    assert headings(els) == ["Manufacturing Excellence"]
    assert {"Right to Win", "State-of-the-Art", "Manufacturing", "Domain Expertise"} <= set(paragraphs(els))


def test_a_column_heading_over_its_own_list_stays_a_heading():
    ws = line("Benefits to Farmers", 235, 291, size=20, bold=True) + line("Value to ITC", 611, 292, size=20, bold=True)
    for i, (a, b) in enumerate((("Productivity Improvement", "Cost-Quality Optimisation"),
                                ("Cost Reduction", "Traceability in Sourcing"),
                                ("Efficient Price Discovery", "New Platform Businesses"))):
        ws += line(a, 209, 339 + 48 * i, size=20) + line(b, 555, 340 + 48 * i, size=20)
    hs = headings(analyse(page(ws)))
    assert "Benefits to Farmers" in hs and "Value to ITC" in hs
    assert "Cost Reduction" not in hs


def test_a_title_printed_again_as_a_label_keeps_the_title():
    ws = line("Net interest income", 38, 65, size=31) + line("Net interest income", 176, 117, size=18, bold=True) \
        + line("Net interest margin (NIM)", 605, 117, size=18, bold=True) + line("Up 4% on the year", 176, 145)
    hs = headings(analyse(page(ws)))
    assert hs.count("Net interest income") == 1
    assert hs[0] == "Net interest income"


def test_short_labels_printed_twice_are_not_headings():
    ws = line("Revenue bridge", 20, 20, size=28, bold=True) + line("Adjusted", 300, 120, size=20, bold=True) \
        + line("Adjusted", 600, 120, size=20, bold=True) + line("Sales grew in every region", 300, 160)
    assert headings(analyse(page(ws))) == ["Revenue bridge"]


def test_numbers_and_half_lines_are_never_headings_on_picture_led_pages():
    def el(text, size=10.0, bold=True):
        return Element("t", "text", (0, 0, 100, 12), text=text, html=text, size=size, bold=bold, safe=True, lines=1)
    assert web_layout.heading_level(el("315.5"), 10.0) is None
    assert web_layout.heading_level(el("(997)"), 10.0) is None
    assert web_layout.heading_level(el("R&D and"), 10.0) is None
    assert web_layout.heading_level(el("Key highlights"), 10.0) == 3
    assert site._is_junk_heading("21.97%") and site._is_junk_heading("Connect &")
    assert not site._is_junk_heading("Key highlights")


def test_a_text_box_image_is_not_a_picture_on_the_web_page():
    doc = pymupdf.open()
    p = doc.new_page(width=W, height=H)
    p.insert_image(pymupdf.Rect(20, 80, 320, 240), pixmap=_pix())              # a text box
    p.insert_image(pymupdf.Rect(420, 100, 600, 260), pixmap=_pix((180, 220, 180)))   # a diagram
    for i, t in enumerate(("Invest in cutting-edge product", "development and R&D capabilities",
                           "Leverage our science centre for innovation")):
        p.insert_text((30, 110 + 22 * i), t, fontname="helv", fontsize=14)
    p.insert_text((450, 180), "Consumer", fontname="hebo", fontsize=16)
    p.insert_text((40, 40), "Strategic directions", fontname="hebo", fontsize=26)
    els = page_elements(p, {"figures": {}, "sections": [], "pages": []}, 1)
    pics = [e for e in els if e.kind == "picture"]
    texts = " ".join(e.text for e in els if e.kind == "text")
    assert len(pics) == 1 and pics[0].bbox[0] >= 400                     # only the diagram
    assert "Leverage our science centre for innovation" in texts and "Consumer" not in texts






def test_half_lines_list_items_and_dashes_are_not_headings():
    for t in ("customers to its franchise in Q2 FY26", "– Net charge-off ratio of 0.47% vs. 0.55%",
              "• CET1 ratio of 11.6%", "Financial performance—", "Revenue, profit,", "315.5"):
        assert layout.not_a_title(t), t
    for t in ("Financial performance", "iPhone sales", "Auf einen Blick", "Q2 FY26 highlights", "トヨタの決算"):
        assert not layout.not_a_title(t), t
    ws = line("customers to its franchise in Q2 FY26", 40, 40, size=24, bold=True) + line("Retail grew 20%", 40, 120)
    assert headings(analyse(page(ws))) == []


def test_a_title_split_on_one_line_and_a_word_wrapped_onto_the_next_stay_one_heading():
    ws = line("Q2 FY26 Highlights (UltraTech existing assets –", 25, 35, size=28) + line("166.76 Mtpa)", 730, 44, size=28) \
        + line("Domestic grey cement sales grew", 25, 120)
    assert headings(analyse(page(ws))) == ["Q2 FY26 Highlights (UltraTech existing assets – 166.76 Mtpa)"]
    ws = line("Build vs Buy: balance moves towards build and re-", 20, 20, size=24, bold=True) \
        + line("invention of services", 20, 47, size=24, bold=True) + line("Clients build more in-house", 20, 100)
    assert headings(analyse(page(ws))) == ["Build vs Buy: balance moves towards build and re- invention of services"]


def test_a_slide_background_is_not_a_picture_on_the_web_page_but_a_full_page_photo_is():
    doc = pymupdf.open()
    p = doc.new_page(width=W, height=H)
    p.insert_image(pymupdf.Rect(-20, 0, W + 20, H), pixmap=_pix((240, 244, 250)))     # the template
    p.insert_image(pymupdf.Rect(420, 100, 600, 260), pixmap=_pix((180, 220, 180)))    # a diagram
    for i, t in enumerate(("Create structural advantage across the chain", "Minimise the total cost of product")):
        p.insert_text((40, 330 + 22 * i), t, fontname="helv", fontsize=14)
    els = page_elements(p, {"figures": {}, "sections": [], "pages": []}, 1)
    pics = [e for e in els if e.kind == "picture"]
    assert len(pics) == 1 and pics[0].bbox[0] >= 400
    assert "Minimise the total cost of product" in " ".join(e.text for e in els if e.kind == "text")
    # a full-page photo with only a title on it stays the page's picture
    doc2 = pymupdf.open()
    p2 = doc2.new_page(width=W, height=H)
    p2.insert_image(pymupdf.Rect(0, 0, W, H), pixmap=_pix((90, 120, 160)))
    p2.insert_text((60, 270), "Agri Business", fontname="hebo", fontsize=40)
    assert [e.kind for e in page_elements(p2, {"figures": {}, "sections": [], "pages": []}, 1)].count("picture") == 1
