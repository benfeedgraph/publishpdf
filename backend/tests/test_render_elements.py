"""Regression tests for the design-led page heuristics (app/render/elements.py and the page-1
cover rule). These pin the fixes for real artifacts seen on dense infographic reports:

  - wrapped paragraph lines stored as separate blocks must rejoin into one paragraph, even when
    one line is bold-dominant (so a sentence never "ends halfway");
  - a row of product packs, or a small logo pressed against a bigger logo wall, must become ONE
    picture (so an image is never shown in broken pieces);
  - page 1 reflows as a web page when it has real content, and stays a whole cover only when it
    is a bare title/picture page.

They are pure-function tests (no DB, no PDF), so they run fast and localise any regression.
"""

from __future__ import annotations

from app.render import ai_layout, site
from app.render.elements import Element, _join_wrapped, _merge_rows, _panel_at, _panels, is_bullet


def _t(id: str, x0, y0, x1, y1, *, size=8.0, bold=False, color="#111111", text="word word",
       meta=None) -> Element:
    return Element(id, "text", (x0, y0, x1, y1), text=text, html=text, size=size, bold=bold,
                   color=color, safe=True, lines=1, meta=meta or {})


# ------------------------------------------------------------------ _join_wrapped

def test_wrapped_lines_rejoin_into_one_paragraph_even_when_one_is_bold():
    # The FMCG-portfolio case: two stacked lines in one column, the second bold-dominant.
    a = _t("t1", 40, 100, 210, 112, bold=False, text="...extending trusted Mother Brands")
    b = _t("t2", 40, 113, 217, 125, bold=True, text="into Value-added Adjacencies.")
    out = _join_wrapped([a, b])
    assert len(out) == 1
    assert out[0].text == "...extending trusted Mother Brands into Value-added Adjacencies."


def test_separate_columns_do_not_merge():
    # Mission / Vision set side by side must stay separate paragraphs.
    mission = _t("m", 40, 100, 170, 140, text="To be India's most trusted enterprise")
    vision = _t("v", 300, 100, 430, 140, text="Our brands and products will lead")
    out = _join_wrapped([mission, vision])
    assert len(out) == 2


def test_paragraph_gap_is_not_merged():
    # A real paragraph break (big vertical gap) stays two paragraphs.
    a = _t("a", 40, 100, 200, 112)
    b = _t("b", 40, 150, 200, 162)              # ~38pt gap for size-8 text: a new block
    assert len(_join_wrapped([a, b])) == 2


def test_heading_does_not_merge_into_body():
    heading = _t("h", 40, 100, 200, 118, size=16.0, bold=True, text="Key highlights")
    body = _t("b", 40, 119, 200, 131, size=8.0, text="Revenue grew strongly this year")
    assert len(_join_wrapped([heading, body])) == 2


def test_bullets_are_not_merged_into_prose():
    bullet = _t("x", 40, 100, 200, 112, text="first point", meta={"bullet": "#c00"})
    assert is_bullet(bullet)
    body = _t("y", 40, 113, 200, 125, text="a following sentence")
    assert len(_join_wrapped([bullet, body])) == 2


def test_label_bar_is_not_merged_into_body():
    label = _t("l", 40, 100, 120, 114, text="MISSION", meta={"bg": "#123456"})
    body = _t("b", 40, 115, 200, 127, text="To be the most trusted")
    assert len(_join_wrapped([label, body])) == 2


# ------------------------------------------------------------------ _merge_rows

def test_row_of_similar_packs_becomes_one_strip():
    packs = [(10, 10, 60, 60), (62, 10, 112, 60), (114, 10, 164, 60)]
    out = _merge_rows(packs)
    assert len(out) == 1
    assert out[0] == (10, 10, 164, 60)


def test_small_logo_touching_a_bigger_wall_is_absorbed():
    # The FMCG Power Brands case: two small logos pressed against the big logo grid.
    grid = (257, 685, 478, 822)
    logo1 = (481, 708, 522, 736)
    logo2 = (479, 745, 521, 770)
    out = _merge_rows([grid, logo1, logo2])
    assert len(out) == 1
    assert out[0][0] == 257 and out[0][2] == 522


def test_pictures_far_apart_stay_separate():
    out = _merge_rows([(10, 10, 60, 60), (400, 10, 460, 60)])
    assert len(out) == 2


def test_pictures_in_different_bands_stay_separate():
    out = _merge_rows([(10, 10, 60, 60), (12, 200, 62, 250)])
    assert len(out) == 2


# ------------------------------------------------------------------ _panels / _panel_at

W, H = 600.0, 800.0


def test_coloured_section_behind_a_group_is_a_panel():
    # A navy section fill behind two text blocks: a panel the web page should paint.
    fills = [((40, 100, 560, 300), "#17153B")]
    blocks = [{"bbox": (60, 120, 520, 160)}, {"bbox": (60, 180, 520, 260)}]
    panels = _panels(fills, blocks, W, H)
    assert len(panels) == 1
    assert panels[0][2] == "#17153B"
    assert _panel_at((60, 120, 520, 160), panels) == (0, "#17153B")
    assert _panel_at((60, 500, 300, 520), panels) is None   # outside the fill


def test_near_white_fill_is_not_a_panel():
    fills = [((40, 100, 560, 300), "#FFFFFF")]
    blocks = [{"bbox": (60, 120, 520, 160)}, {"bbox": (60, 180, 520, 260)}]
    assert _panels(fills, blocks, W, H) == []


def test_tight_one_line_bar_is_not_a_panel():
    # A label bar (one short line, tiny area) is handled per-block, not as a section panel.
    fills = [((40, 100, 160, 116), "#17153B")]
    blocks = [{"bbox": (46, 101, 150, 115)}]
    assert _panels(fills, blocks, W, H) == []


def test_nested_panels_pick_the_smallest():
    # An inner card inside an outer section: an element in the card gets the card's colour.
    fills = [((20, 80, 580, 400), "#102A54"), ((60, 120, 300, 300), "#E8F0FF")]
    blocks = [{"bbox": (80, 140, 280, 180)}, {"bbox": (80, 200, 280, 280)}]
    panels = _panels(fills, blocks, W, H)
    assert len(panels) == 2
    inner = _panel_at((80, 140, 280, 180), panels)
    assert inner is not None and inner[1] == "#E8F0FF"      # the tighter fill wins


# ------------------------------------------------------------------ ai_layout.render crops


def test_each_picture_and_unverified_line_becomes_its_own_crop_file():
    # Every picture and every kept (unverified-number) line asks for its own crop URL, and the
    # img has no full-page windowing offsets — so each loads as its own small file.
    pic = Element("p1", "picture", (40, 40, 560, 200))
    num = _t("t1", 40, 220, 300, 236, text="Rs 1,234")
    num.safe = False
    secs = [{"type": "picture", "picture": "p1", "caption": []}, {"type": "paragraph", "ids": ["t1"]}]
    asked: list[str] = []

    def crop_url(e):
        asked.append(e.id)
        return f"/r/pages/p0005-x{e.id}.webp"

    html = ai_layout.render(ai_layout.validate({"sections": secs}, [pic, num]), [pic, num],
                            crop_url=crop_url, W=600, H=800)
    assert asked == ["p1", "t1"]
    assert "/r/pages/p0005-xp1.webp" in html and "/r/pages/p0005-xt1.webp" in html
    assert "left:" not in html and "%;top:" not in html      # no CSS-windowing of a full-page image


# ------------------------------------------------------------------ page1_is_cover

def _schema(page1_blocks: list[dict]) -> dict:
    return {"sections": [{"id": "s1", "blocks": page1_blocks}]}


def _para(page: int, words: int) -> dict:
    return {"type": "paragraph", "source": {"page": page}, "runs": [{"t": " ".join(["w"] * words)}]}


def test_page1_bare_cover_is_kept_whole():
    assert site.page1_is_cover(_schema([_para(1, 6)])) is True


def test_page1_with_real_content_reflows():
    assert site.page1_is_cover(_schema([_para(1, 80)])) is False


def test_page1_with_a_table_reflows():
    assert site.page1_is_cover(_schema([{"type": "table", "source": {"page": 1}}])) is False
