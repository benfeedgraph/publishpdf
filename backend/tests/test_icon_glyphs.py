"""Icon-font glyphs (Unicode private-use code points) set right against a number in the
text layer: they are not part of the figure, and the second parser must not call the
figure misread because it keeps them."""

from __future__ import annotations

from app import validation
from app.extraction.pdf import text_layer_words
from app.extraction.tokens import classify

ENVELOPE, TICK, PLANE_15 = "", "", "\U000f0001"


class _Page:
    """Just what text_layer_words reads: one line of characters, 5 pt apart."""

    def __init__(self, text: str):
        self.text = text

    def get_text(self, kind, flags=0):
        chars = [{"c": c, "bbox": (10 + 5 * i, 100, 15 + 5 * i, 110)} for i, c in enumerate(self.text)]
        return {"blocks": [{"type": 0, "lines": [{"dir": (1.0, 0.0), "spans": [
            {"size": 10.0, "flags": 0, "font": "Arial", "chars": chars}]}]}]}


def test_an_icon_glyph_ends_a_word_like_a_space():
    words, total, bad = text_layer_words(_Page(f"{ENVELOPE}19700 outlets {TICK}85%+ reach a{PLANE_15}b"))
    assert [w.text for w in words] == ["19700", "outlets", "85%+", "reach", "a", "b"]
    # the word's box is the digits' own box, not the icon's
    assert words[0].bbox[0] == 15
    # still counted as unreadable for the scanned / unusable-text-layer decision, as before
    assert total == 30 and bad == 2
    assert classify(words[0].text)[0] == "number"


def _check(raw: str, pdfminer_reads: list[str]) -> list:
    bbox = [10.0, 100.0, 40.0, 110.0]
    schema = {"figures": {"f1": {"id": "f1", "raw": raw, "kind": "number", "status": "active",
                                 "source": {"page": 1, "bbox": bbox}}}}
    artifact = {"pages": [{"page": 1, "method": "text_layer"}]}
    words = [(w, [10.0 + 6 * i, 100.0, 15.0 + 6 * i, 110.0]) for i, w in enumerate(pdfminer_reads)]
    orig = validation._pdfminer_words
    validation._pdfminer_words = lambda _pdf, pages: {1: words}
    try:
        return validation.check_second_parser(schema, b"", artifact)
    finally:
        validation._pdfminer_words = orig


def test_second_parser_ignores_icon_glyphs_on_either_side():
    assert _check("19700", [ENVELOPE, "19700"]) == []
    assert _check("19700", [ENVELOPE + "19700"]) == []
    # a figure extracted before the fix still carries the icon: the same digits, so no finding
    assert _check(ENVELOPE + "19700", [ENVELOPE, "19700"]) == []


def test_second_parser_still_flags_different_digits_next_to_an_icon():
    found = _check("19700", [ENVELOPE, "19800"])
    assert [(i.check, i.severity) for i in found] == [("second_parser", "blocking")]
    assert found[0].actual == "19800"


def test_the_second_parser_compares_what_a_reader_sees_not_where_words_break():
    # pdfminer guesses word breaks from the gaps between letters; a zero-width space is invisible
    for raw, other in (("Q3FY25", "Q 3 F Y2 5"), ("25%", "25 %"), ("c.65%", "c.65 %"), ("2)​", "2)")):
        assert validation._visible(raw) == validation._visible(other)
    # a different character is still a different reading
    assert validation._visible("46.20") != validation._visible("48.20")
    assert validation._visible("Q3FY25") != validation._visible("Q 3 F Y2 6")
