"""Upload-form metadata suggestions on covers that broke them: a slogan, a sensitivity label,
a section tab or a caption instead of the company; an auditor, an exchange or another
company's metadata; chart labels, last year's figures or today's date as the period; the file
name on a scanned PDF. Pure unit tests (no DB); OCR is replaced by fixed words."""

import pymupdf
import pytest

from app.extraction.detect import detect_metadata

Item = tuple[float, float, str, float]          # x, y (baseline), text, font size


def _deck(*pages: list[Item], landscape: bool = True, metadata: dict | None = None) -> bytes:
    return _pages(*[(landscape, items) for items in pages], metadata=metadata)


def _pages(*pages: tuple[bool, list[Item]], metadata: dict | None = None) -> bytes:
    doc = pymupdf.open()
    for landscape, items in pages:
        page = doc.new_page(width=960, height=540) if landscape else doc.new_page()
        for x, y, text, size in items:
            page.insert_text((x, y), text, fontsize=size)
    if metadata:
        doc.set_metadata(metadata)
    return doc.tobytes()


def _body(*lines: str) -> list[Item]:
    return [(60, 140 + 30 * i, t, 14) for i, t in enumerate(lines)]


def _header(text: str) -> Item:
    return (40, 30, text, 10)


def _ocr_words(line: str, y: float, x: float = 600.0, conf: float = 0.95) -> list:
    from app.extraction.ocr import OcrWord
    out = []
    for t in line.split():
        box = (x, y, x + 8 * len(t), y + 12)
        out.append(OcrWord(t, box, conf, box))
        x += 8 * len(t) + 6
    return out


def _ocr(monkeypatch, pages: dict[int, list]) -> None:
    """OCR that reads the given words on those pages (and nothing elsewhere)."""
    from app.extraction import ocr
    monkeypatch.setattr(ocr, "prepare_page", lambda page: page.number)
    monkeypatch.setattr(ocr, "run_prepared", lambda number: pages.get(number, []))


def test_slogan_on_a_logo_only_cover_is_not_the_company():
    """ITC at the JP Morgan summit: the cover's text is a slogan (the name is in the logo
    image); chart labels FY17..FY20 tie; the cover carries the date; the money is in crore."""
    cover = [(60, 60, "JP Morgan India Investor Summit 2020", 14), (60, 90, "21st September 2020", 12),
             (60, 250, "Creating Multiple Drivers of Growth", 24), (60, 290, "through Responsible Competitiveness", 24)]
    deck = _deck(cover,
                 _body("ITC's Non-Cigarette Business Portfolio", "FMCG Foods Agri", "Segment Revenue (` cr.)"),
                 _body("Strategic directions - ITC FMCG", "Enhance scale in existing categories"),
                 _body("FY17 FY18 FY19 FY20", "Revenue growth"),
                 _body("FY17 FY18 FY19 FY20", "ITC Foods portfolio"),
                 _body("ITC e-Store and digital first brands", "Supply chain"))
    meta = detect_metadata(deck, filename="test.pdf")
    assert (meta["company_name"], meta["confidence"]["company_name"]) == ("ITC", "medium")    # not "detected"
    # 21 Sep 2020 is in FY2020-21 for a rupee report (the year it ends), not the first chart label, FY17
    assert (meta["fiscal_year"], meta["period"], meta["confidence"]["fiscal_year"]) == (2021, "q2", "low")
    # slides that never say what kind of deck they are: suggested as a presentation, never "detected"
    assert (meta["report_type"], meta["confidence"]["report_type"]) == ("investor_presentation", "medium")


def test_running_header_names_the_company_not_a_sensitivity_label():
    """HDFC Bank earnings: 'Classification - Confidential' sits at the top of the cover; every
    page's header reads 'HDFC Bank Presentation Q2 FY2027', and the body uses 'HDFC Bank' but
    never 'Presentation' on its own."""
    cover = [(40, 20, "Classification - Confidential", 11), (60, 250, "Q2FY27 Earnings", 60),
             (60, 330, "Presentation", 60), (60, 380, "October 17, 2026", 20)]
    pages = [[_header(f"HDFC Bank Presentation Q2 FY2027 {n}")] + _body("Key performance metrics for Q2 FY27",
                                                                       f"HDFC Bank customers grew in region {n}")
             for n in range(2, 7)]
    meta = detect_metadata(_deck(cover, *pages))
    assert meta["company_name"] == "HDFC Bank" and meta["confidence"]["company_name"] == "medium"
    assert (meta["fiscal_year"], meta["period"], meta["confidence"]["period"]) == (2027, "q2", "high")


def test_auditors_legal_name_loses_to_the_name_used_throughout():
    """Apple's environmental report: the only legal name in the first pages is the assurance
    provider's; the cover states the fiscal year; its title is a report, not a presentation."""
    cover = [(60, 200, "Environmental", 54), (60, 260, "Progress", 54), (60, 320, "Report", 54),
             (60, 380, "COVERING FISCAL YEAR 2025", 7)]
    deck = _deck(cover,
                 _body("Contents", "Ernst & Young LLP Use of Proceeds Examination", "Apple 2030"),
                 _body("At Apple, we feel a deep sense of responsibility", "Apple's goal is carbon neutrality"),
                 _body("Apple's progress toward Apple 2030", "Recycled content"),
                 _body("Certified all Apple-owned data centers", "Renewable electricity"))
    meta = detect_metadata(deck)
    assert meta["company_name"] == "Apple"
    assert (meta["fiscal_year"], meta["period"]) == (2025, "fy")
    assert meta["report_type"] == "other"


def test_safe_harbour_mention_of_an_annual_report_is_not_the_report_type():
    """Infosys AI Day: the name is the first word of the cover title; 'annual report' in the
    Form 20-F safe-harbour note is about another filing (these are slides)."""
    cover = [(60, 250, "Infosys AI Day", 80), (60, 320, "February 17th, 2026", 18)]
    deck = _deck(cover,
                 _body("Agenda", "Convention Center, Infosys Campus, Bengaluru"),
                 _body("Infosys Topaz Fabric - AI platform suite"),
                 _body("Safe harbor", "Infosys may, from time to time, make additional written statements.",
                       "See our Annual Report on Form 20-F for the fiscal year ended March 31, 2025."),
                 _body("Infosys Living Labs walkthrough"))
    meta = detect_metadata(deck)
    assert meta["company_name"] == "Infosys"
    assert meta.get("report_type") != "annual_report"
    # its money is in dollars, but the deck says its fiscal year ends on March 31: 17 Feb 2026 is Q4 FY2026
    assert (meta["fiscal_year"], meta["period"]) == (2026, "q4")


def test_a_date_alone_gives_no_period_for_a_report_that_isnt_one():
    """A broker's initiation note dated 17 April 2025 is not a 'Q1' report."""
    deck = _deck(_body("Initiating Coverage 17 April 2025", "Equity Research report", "BUY Target Price: Rs. 16,617"),
                 _body("Investment argument"), landscape=False)
    meta = detect_metadata(deck)
    assert meta["report_type"] == "other" and "period" not in meta


@pytest.mark.parametrize("mention,expected", [
    ("PB Balaji Group CFO Tata Motors Limited", "Tata Motors Limited"),    # a caption before the name
    ("Consolidated Indian Oil Corporation Limited", "Indian Oil Corporation Limited"),   # a word of the name
])
def test_legal_name_keeps_the_name_and_drops_the_words_before_it(mention, expected):
    short = expected.rsplit(" ", 1)[0]
    deck = _deck(_body(mention), *[_body(f"{short} at a glance", "Revenue") for _ in range(3)], landscape=False)
    meta = detect_metadata(deck)
    assert meta["company_name"] == expected and meta["confidence"]["company_name"] == "high"


def test_name_with_an_ampersand_and_and_is_kept_whole():
    """Procter & Gamble Hygiene and Health Care Limited: 'Health Care' alone is used more often."""
    letter = _body("Procter & Gamble Hygiene and Health Care Limited", "To, The BSE Limited", "June 16, 2026")
    deck = _pages((False, letter),
                  (True, _body("Procter & Gamble Hygiene and Health Care Ltd. Investor and Analyst Presentation")),
                  *[(True, _body("Health Care segment", "Fiscal Results 2025-26")) for _ in range(3)])
    meta = detect_metadata(deck)
    assert meta["company_name"] == "Procter & Gamble Hygiene and Health Care Limited"
    assert (meta["fiscal_year"], meta["period"]) == (2026, "fy")      # stated, not read off the June date


def test_exchanges_a_covering_letter_is_addressed_to_are_not_the_company():
    """Bajaj Finance: a portrait letter to BSE and NSE comes before the landscape deck."""
    letter = _body("Bajaj Finance Limited", "BSE Limited", "National Stock Exchange of India Limited",
                   "Sub: Q2 FY26 investor presentation")
    deck = _pages((False, letter),
                  (True, [(60, 250, "Q2 FY26 Investor Presentation", 40), (60, 320, "10 November 2025", 18)]),
                  *[(True, _body("Bajaj Finance Limited overview", "AUM grew Rs. 1,000 crore")) for _ in range(4)])
    meta = detect_metadata(deck)
    assert (meta["company_name"], meta["fiscal_year"], meta["period"]) == ("Bajaj Finance Limited", 2026, "q2")
    assert meta["report_type"] == "investor_presentation"


def test_metadata_naming_another_company_is_ignored():
    """Novartis, saved from a file whose metadata still says Roche's 2019 annual report."""
    pages = [[_header(f"Novartis Q3 Results | October 28, 2025 {n}")] + _body("Novartis AG net sales grew",
                                                                               "Q3 2024 Q3 2025 USD 5.1 billion")
             for n in range(1, 6)]
    deck = _deck([(60, 250, "Q3 2025 Results", 40)], *pages,
                 metadata={"title": "Roche Holding AG - Annual Report 2019", "author": "F. Hoffmann-La Roche Ltd"})
    meta = detect_metadata(deck)
    assert (meta["company_name"], meta["fiscal_year"], meta["period"]) == ("Novartis AG", 2025, "q3")
    assert meta.get("report_type") != "annual_report"


def test_section_tabs_and_last_years_figures_are_not_the_answer():
    """BASF's German quarterly statement: capitalised section tabs on every page, the name only
    in the metadata and the body, and last year's quarter quoted more often than this one."""
    pages = [[_header("Geschäftsverlauf"), (40, 520, "BASF-Quartalsmitteilung 3. Quartal 2025", 9)]
             + _body("Die BASF-Gruppe steigerte den Umsatz", "BASF Umsatz Q3 2024 und Q3 2024 im Vergleich")
             for _ in range(5)]
    pages[0] += _body("", "", "", "Ausblick Q3 2025")
    deck = _deck([(60, 200, "Q3 Quartalsmitteilung", 40), (60, 260, "3. Quartal 2025", 20)], *pages,
                 landscape=False, metadata={"title": "BASF 2025 Q3 DE"})
    meta = detect_metadata(deck)
    assert meta["company_name"] == "BASF"
    # the cover says "3. Quartal 2025" itself, so the period is the cover's (last year's Q3 2024,
    # quoted more often, isn't it)
    assert (meta["fiscal_year"], meta["period"], meta["confidence"]["period"]) == (2025, "q3", "high")


def test_a_running_caption_sentence_is_not_a_name():
    """Dr. Reddy's: every slide's footer says 'As per consolidated financial statements under
    IFRS'; the legal name has a curly apostrophe."""
    doc = pymupdf.open()                    # HTML boxes: insert_text's font has no '’'

    def page(landscape: bool, *boxes: tuple[float, str]) -> None:
        pg = doc.new_page(width=960, height=540) if landscape else doc.new_page()
        for y, text in boxes:
            pg.insert_htmlbox(pymupdf.Rect(40, y, pg.rect.width - 40, y + 30), text)
    page(False, (120, "Dr. Reddy’s Laboratories Limited"), (160, "BSE Limited"), (200, "New York Stock Exchange Inc."))
    page(True, (120, "Investor Presentation"), (160, "Q2FY26 RESULTS UPDATE Dr. Reddy’s"))
    for _ in range(4):
        page(True, (510, "As per consolidated financial statements under IFRS"),
             (120, "Dr. Reddy’s Laboratories Limited Q2FY26 Results Update"), (160, "Dr. Reddy’s revenue grew"))
    meta = detect_metadata(doc.tobytes())
    assert meta["company_name"] == "Dr. Reddy’s Laboratories Limited"
    assert (meta["fiscal_year"], meta["period"], meta["report_type"]) == (2026, "q2", "investor_presentation")


@pytest.mark.parametrize("line,expected", [
    ("Consolidated M&M results: Q2 F26 PAT Revenue Q2 F25 Q2 F26 Rs. 37,924 crore", (2026, "q2")),   # FY written F
    ("Earnings Presentation for the Quarter and Half - year ended September 30, 2025 Rs. 1,000 crore", (2026, "q2")),
])
def test_period_labels(line, expected):
    meta = detect_metadata(_deck(_body(line), _body("Revenue")))
    assert (meta["fiscal_year"], meta["period"]) == expected


def test_nothing_found_is_left_blank_not_guessed(monkeypatch):
    """A scanned PDF named 'test.pdf' with no readable cover: no company from the file name,
    no year from today's date, no period."""
    _ocr(monkeypatch, {})
    meta = detect_metadata(_deck([], []), filename="test.pdf")
    assert "company_name" not in meta and "fiscal_year" not in meta and "period" not in meta


def test_scanned_cover_uses_the_file_name_only_where_the_pdf_backs_it_up(monkeypatch):
    """ITC Investor Day 2023, scanned: OCR reads 'ITC', the date and a crore figure on the cover."""
    cover = (_ocr_words("ITC Next Creating Enduring Value Responsible Competitiveness", 100)
             + _ocr_words("Investor Day 2023 12 December 2023", 300) + _ocr_words("Revenue 69,481 cr.", 400))
    _ocr(monkeypatch, {0: cover})
    deck = _deck([], [])
    meta = detect_metadata(deck, filename="ITC_30_pages_test.pdf")
    assert (meta["company_name"], meta["confidence"]["company_name"]) == ("ITC", "low")     # not "ITC Pages Test"
    assert (meta["fiscal_year"], meta["period"]) == (2024, "q3")                          # 12 Dec 2023 -> Q3 FY2024
    assert "company_name" not in detect_metadata(deck, filename="test.pdf")


@pytest.mark.parametrize("name,uses_initials,expected", [
    ("LARSEN & TOUBRO", True, "Larsen & Toubro"),     # the deck calls itself "L&T"
    ("Enduring Value", False, None),                  # a slogan repeats the same way: not a name
])
def test_scanned_deck_name_from_its_cover_and_running_footer(monkeypatch, name, uses_initials, expected):
    """Larsen & Toubro's scanned earnings deck: no text layer, so OCR is all there is. The name
    is on the cover and in every page's footer (beside the logo, which OCR reads as 'EC)')."""
    cover = _ocr_words(name, 200) + _ocr_words("Earnings Call Presentation", 260) + _ocr_words("Q2 / H1 FY27", 300)
    disclaimer = "concerning L&T's future business prospects" if uses_initials else "concerning future business prospects"
    _ocr(monkeypatch, {0: cover,
                       1: _ocr_words(disclaimer, 120, x=60) + _ocr_words("EC)", 516, x=560, conf=0.16) + _ocr_words(name, 516),
                       2: _ocr_words("Key Highlights", 60, x=60) + _ocr_words("EC)", 516, x=560, conf=0.16) + _ocr_words(name, 516)})
    meta = detect_metadata(_deck([], [], []), filename="test.pdf")
    assert meta.get("company_name") == expected
    if expected:
        assert meta["confidence"]["company_name"] == "medium"
    assert (meta["fiscal_year"], meta["period"]) == (2027, "h1")


def test_unit_comes_from_money_not_from_a_count_of_something_else():
    """Apple: the only 'billion' in the first pages is water; the one dollar amount is on page 8.
    With no money anywhere, currency and unit are left out (the form no longer shows INR)."""
    cover = [(60, 200, "Environmental Progress Report", 54), (60, 380, "COVERING FISCAL YEAR 2025", 7)]
    water = _body("Last year we worked with suppliers to save 17 billion gallons of freshwater.")
    deck = _deck(cover, water, *[_body("Renewable electricity") for _ in range(5)],
                 _body("We have issued $4.7 billion in green bonds."))
    meta = detect_metadata(deck)
    assert (meta["currency"], meta["reporting_unit"]) == ("USD", "USD billion")
    assert meta["confidence"]["currency"] == meta["confidence"]["reporting_unit"] == "medium"
    meta = detect_metadata(_deck(cover, water))
    assert "currency" not in meta and "reporting_unit" not in meta


@pytest.mark.parametrize("line", [
    # Reliance: the ₹ sign comes out as 'H'/'J'; the US$ figures are conversions
    "Revenue at H 11,75,919 crore (US$ 124.0 billion), EBITDA at H 2,07,911 crore (US$ 21.9 billion), "
    "segment revenue J3,71,085 Crore",
    "Segment Revenue Segment EBITDA (` cr.)",        # ITC: the ₹ sign comes out as '`'
    "Revenue ₹K crores 93 263 261 440",               # Tata Motors: thousand crore
])
def test_rupee_amounts_whose_rupee_sign_is_unreadable(line):
    doc = pymupdf.open()
    for _ in range(2):          # an HTML box falls back to a font that has '₹' (insert_text's doesn't)
        doc.new_page(width=960, height=540).insert_htmlbox(pymupdf.Rect(60, 120, 900, 300), line)
    meta = detect_metadata(doc.tobytes())
    assert (meta["currency"], meta["reporting_unit"]) == ("INR", "₹ crore")


def test_chart_labels_tie_to_the_latest_year():
    meta = detect_metadata(_deck(_body("Growth story"), _body("FY17 FY18 FY19 FY20", "FY17 FY18 FY19 FY20")))
    assert (meta["fiscal_year"], meta["confidence"]["fiscal_year"]) == (2020, "low")
    assert "period" not in meta                 # chart labels don't say it is a full-year report


# ------------------------------------------------------------------ the year as the PDF writes it (issue #6)

@pytest.mark.parametrize("cover,later,expected", [
    # a results deck: the year part of its period label
    (["UltraTech Cement Limited", "Q2 FY26 Result Presentation"], ["Revenue Q2 FY26 Q2 FY25"], (2026, "q2", "FY26")),
    # US style, number first; the cover names the quarter even where last quarter is mentioned more
    (["Bank of America", "3Q25 Financial Results", "$ 1,000 million"], ["2Q25 2Q25 2Q25 3Q24 results"], (2025, "q3", None)),
    # German
    (["Q3", "Quartalsmitteilung", "3. Quartal 2025", "EUR 1.000 Millionen"], ["Umsatz 3. Quartal 2025"], (2025, "q3", None)),
    # only a date on the cover: the fiscal year it falls in, written so it says which one
    (["JP Morgan India Investor Summit 2020", "21st September 2020", "₹ 1,000 crore"], ["Growth"], (2021, "q2", "FY2021")),
])
def test_the_year_is_filled_in_as_the_pdf_writes_it(cover, later, expected):
    meta = detect_metadata(_deck(_body(*cover), _body(*later)))
    assert (meta["fiscal_year"], meta.get("period"), meta.get("fiscal_year_label")) == expected


def test_an_annual_report_keeps_its_year_range():
    meta = detect_metadata(_pages((False, _body("Powering a Nation", "Integrated Annual Report 2025-26")),
                                  (False, _body("Chairman's letter"))))
    assert (meta["report_type"], meta["fiscal_year"], meta["period"], meta["fiscal_year_label"]) == ("annual_report", 2026, "fy", "2025-26")


def test_a_japanese_fiscal_year_and_quarter():
    from app.extraction.detect import _period
    t = "2026年３月期 第２四半期 決算説明会"
    assert _period([t], t, t, 4) == (2026, "q2", "high", "FY2026")
    t = "2026年3月期 決算の概要"                                  # the fiscal year ending March 2026, no quarter
    assert _period([t], t, t, 4) == (2026, None, "medium", "FY2026")


# ------------------------------------------------------------------ evidence from the whole PDF


def _html_deck(*pages: tuple[bool, list[tuple[float, str]]], images: tuple[int, ...] = ()) -> bytes:
    """Pages of HTML boxes (a font with é, ’ and ₹), with a small picture on the pages listed."""
    doc = pymupdf.open()
    for i, (landscape, boxes) in enumerate(pages):
        pg = doc.new_page(width=960, height=540) if landscape else doc.new_page()
        for y, text in boxes:
            pg.insert_htmlbox(pymupdf.Rect(40, y, pg.rect.width - 40, y + 40), text)
        if i in images:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 20), False)
            pix.clear_with(200)
            pg.insert_image(pymupdf.Rect(700, 20, 900, 120), pixmap=pix)
    return doc.tobytes()


def test_a_name_with_an_accent_is_kept_whole():
    """'Nestlé India Limited', not 'India Limited': a name's letters are any script's."""
    meta = detect_metadata(_html_deck((False, [(100, "To the Stock Exchange"), (160, "Nestlé India Limited")]),
                                      *[(True, [(100, "Nestlé India at a glance"), (160, "Sales grew")]) for _ in range(3)]))
    assert meta["company_name"] == "Nestlé India Limited"


def test_a_letter_signed_in_capitals_names_the_company(monkeypatch):
    """A scanned covering letter signed 'For ASIAN PAINTS LIMITED'; the logo reads 'asianpaints'."""
    _ocr(monkeypatch, {0: _ocr_words("To the Stock Exchange Sub: Investor Conference", 100)
                       + _ocr_words("Yours truly, For ASIAN PAINTS LIMITED", 400),
                       1: _ocr_words("asianpaints Investor Conference", 100), 2: _ocr_words("Highlights", 100)})
    meta = detect_metadata(_deck([], [], []), filename="IMG_0001.pdf")
    assert meta["company_name"] == "Asian Paints Limited"


def test_a_subsidiary_named_holdings_is_not_the_company(monkeypatch):
    """The cover says 'Bharti Airtel Limited (Incorporated as a public limited company ...)'; a
    subsidiary list mentions 'Bharti Airtel Holdings' more often."""
    _ocr(monkeypatch, {
        0: _ocr_words("Quarterly Report on the results for the second quarter ended September 30, 2026", 60)
        + _ocr_words("Bharti Airtel Limited (Incorporated as a public limited company on July 7, 1995)", 700),
        1: _ocr_words("Bharti Airtel Holdings (Singapore) Pte Ltd and Bharti Airtel Holdings", 100),
        2: _ocr_words("Bharti Airtel customers grew Revenue ₹ 52,145 crore", 100)})
    meta = detect_metadata(_deck([], [], [], landscape=False), filename="scan.pdf")
    assert (meta["company_name"], meta["confidence"]["company_name"]) == ("Bharti Airtel Limited", "high")
    assert (meta["report_type"], meta["fiscal_year"], meta["period"]) == ("quarterly_results", 2027, "q2")


def test_the_quarter_on_the_cover_beats_comparisons_quoted_more_often():
    """A calendar-year bank: '3Q25' on the cover; the slides quote '3Q24' and '2Q25' more often.
    Amounts are US style ('$1.3B')."""
    cover = [(60, 200, "Lakeside Bank 3Q25 Financial Results", 40), (60, 300, "October 15, 2025", 18)]
    slides = [_body("Net income $1.3B vs 3Q24 and 2Q25", "Deposits $2.1B 3Q24 2Q25 3Q24") for _ in range(4)]
    meta = detect_metadata(_deck(cover, *slides))
    assert (meta["fiscal_year"], meta["period"], meta["confidence"]["period"]) == (2025, "q3", "high")
    assert meta["report_type"] == "quarterly_results"
    assert (meta["currency"], meta["reporting_unit"]) == ("USD", "USD billion")


def test_a_product_with_a_two_letter_suffix_is_not_the_company():
    """'Apple Watch SE' is a product, not a company with the legal form SE."""
    pages = [_body("Apple's renewable energy", "Apple Watch SE uses recycled aluminium") for _ in range(4)]
    meta = detect_metadata(_deck([(60, 200, "Environmental Progress Report", 40)], *pages, landscape=False))
    assert meta["company_name"] == "Apple"


def test_a_running_title_is_not_the_company_but_the_listing_code_names_it():
    """Every slide's header is 'Quarterly Results Q3FY26'; the cover gives 'NSE: LOTUSBANK' and
    the slides keep writing 'Lotus Bank'."""
    cover = [(60, 60, "NSE: LOTUSBANK BSE: 532215", 12), (60, 250, "Investor Presentation", 40),
             (60, 320, "Quarterly Results Q3FY26", 30)]
    slides = [[_header("Quarterly Results Q3FY26")] + _body("Lotus Bank at a glance", "Deposits ₹ 1,000 crore")
              for _ in range(5)]
    meta = detect_metadata(_deck(cover, *slides))
    assert meta["company_name"] == "Lotus Bank"
    assert (meta["fiscal_year"], meta["period"]) == (2026, "q3")


def test_a_name_drawn_only_in_the_cover_logo(monkeypatch):
    """The cover's text is 'Q2-2027: Performance review'; the bank's name is in the logo
    picture, which a second look at the cover reads. The slides say 'India's' far more often."""
    from app.extraction import ocr
    monkeypatch.setattr(ocr, "read_pictures", lambda page, rects, dpi=150: "Lotus Bank" if page.number == 0 else "")
    deck = _html_deck((True, [(250, "Q2-2027: Performance review"), (320, "October 17, 2026")]),
                      (True, [(100, "Lotus Bank is a leading private sector bank")]),
                      *[(True, [(100, "India's growth story"), (160, "Loans ₹ 120 billion")]) for _ in range(4)],
                      images=(0,))
    meta = detect_metadata(deck)
    assert meta["company_name"] == "Lotus Bank"
    assert (meta["fiscal_year"], meta["period"], meta["report_type"]) == (2027, "q2", "investor_presentation")


def test_a_subsidiary_named_deep_in_the_report_is_not_the_company():
    """'Acme Fibre Innovations Limited' appears once, on page 9; the report is Acme's."""
    pages = [_body("Acme's growth", "Acme businesses") for _ in range(8)] + [_body("Acme Fibre Innovations Limited")]
    meta = detect_metadata(_deck([(60, 200, "Investor Day 2023", 40)], *pages))
    assert meta["company_name"] == "Acme"


def test_a_legal_name_stated_on_a_contact_page_at_the_end():
    """The deck says 'Hilltop' throughout; 'Hilltop Industries Limited' and its registration
    number are only on the last page."""
    pages = [[_header("Hilltop Q2 FY26 – Earnings Presentation")] + _body("Hilltop revenue grew ₹ 500 crore")
             for _ in range(30)]
    pages[-1] += _body("", "For further queries: Hilltop Industries Limited",
                       "Corporate Identity No.: L27020MH1958PLC011238")
    meta = detect_metadata(_deck([(60, 200, "Q2 FY26 Earnings Presentation", 40)], *pages))
    assert (meta["company_name"], meta["confidence"]["company_name"]) == ("Hilltop Industries Limited", "high")


def test_a_count_in_lakh_is_not_the_money_unit(monkeypatch):
    """A scanned report: '20 lakh acres' and '90 lakh households' are counts; '280,867 cr' is money."""
    _ocr(monkeypatch, {0: _ocr_words("Report and Accounts 2026 Gross Revenue 280,867 cr EBITDA 25,208 cr", 100)
                       + _ocr_words("Covering over 20 lakh acres and 90 lakh households", 300),
                       1: _ocr_words("Contents", 100), 2: _ocr_words("Board of Directors", 100)})
    meta = detect_metadata(_deck([], [], [], landscape=False))
    assert (meta["reporting_unit"], meta["confidence"]["reporting_unit"]) == ("₹ crore", "medium")


def test_a_japanese_legal_name():
    """株式会社 marks the legal name; the name is the run of katakana and kanji before it."""
    from app.extraction.detect import _Corpus, _legal_candidates
    pages = ["2026年3月期第2四半期決算 皆様にサクラ電機株式会社", "サクラの販売台数", "サクラの営業利益", "サクラの見通し"]
    found = _legal_candidates(_Corpus(pages, pages), pages, [[] for _ in pages])
    assert [x[0] for x in found] == ["サクラ電機株式会社"]
