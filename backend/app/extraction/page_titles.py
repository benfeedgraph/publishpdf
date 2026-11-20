"""A page's own title, read from the page itself.

A slide or report page usually opens with its title: the top line set in the page's largest
type, clearly bigger than the report's body text. That title is where the page's section starts,
even when the line doesn't pass the general heading rules (a full-sentence slide title, one
ending in a full stop). A page with no such line continues the section before it.

Everything here is decided from the PDF's own type sizes and positions, relative to the same
document: no word lists, no page numbers. Used by section building (schema_builder) and by the
page-coverage check (validation), so both agree on which pages have a title of their own.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter

import statistics

from app.extraction.layout import ChartEl, TextEl, _reads_as_text, group_lines, not_a_title, split_segments

TITLE_SIZE = 1.25        # a title's type is at least this many times the body text (as for headings)
TOP_SHARE = 0.4          # ...and it starts in the top part of the page
MAX_LINES = 3            # a title is a line or a few, not a block of text
MAX_WORDS = 30
RUNNING_PAGES = 3        # the same "title" on this many pages is a running title, not a page's own
_NUMBER = re.compile(r"[\d.,:%+\-–—()/\s]+", re.UNICODE)   # a token that is only a number


def is_wordy(text: str) -> bool:
    """Words, not a figure set large ("₹627bn", "15,2") or a phrase cut off mid-sentence. Letters
    are weighed against digits only: brackets and other marks (common in titles set in Japanese
    or Chinese) don't count either way."""
    chars = [c for c in text if c.isalnum()]
    if not chars:
        return False
    letters = sum(c.isalpha() for c in chars)
    tokens = text.split()
    words = sum(1 for w in tokens if sum(c.isalpha() for c in w) >= 2)
    # a bare number with a word ("3 Million+") is a callout, as the page reader's heading rule has
    # it (a title in a script without spaces is one long token, digits and all: still a title)
    if len(tokens) <= 2 and words < 2 and any(_NUMBER.fullmatch(t) for t in tokens):
        return False
    # nor a line the page reader never takes for a heading: one that stops mid-phrase ("Hygiene
    # and", "… reduced the risk of"), starts in lower case, or is a list item ("• Gentle …")
    return letters >= 0.7 * len(chars) and words >= 1 and not text.rstrip().endswith((",", ":", ";")) \
        and not not_a_title(text)


def key(text: str) -> str:
    """How two titles are compared: case, punctuation and spacing aside."""
    t = unicodedata.normalize("NFKC", text or "").lower()
    return " ".join(re.sub(r"[^\w&%]+", " ", t).split())


def title_element(els: list, body: float, page_height: float) -> TextEl | None:
    """The page's title among its elements: of the wordy text elements in the top part of the
    page whose type is clearly bigger than the body text, the one in the largest type (the
    top-most, of several that size). None when the page has no such line."""
    if not body:
        return None
    cands = [e for e in els if isinstance(e, TextEl) and e.lines
             and e.size >= body * TITLE_SIZE and e.bbox[1] < TOP_SHARE * page_height
             and len(e.lines) <= MAX_LINES and len(e.words) <= MAX_WORDS
             and is_wordy(" ".join(w.text for w in e.words))
             # set over several lines, it must not read as running text (a pull quote in large
             # type), by the same rule the page reader uses for headings; one line may be a sentence
             and not (len(e.lines) > 1 and _reads_as_text(e))]
    if not cands:
        return None
    big = max(e.size for e in cands)
    return min((e for e in cands if e.size >= big - 0.5), key=lambda e: (e.bbox[1], e.bbox[0]))


def title_on_picture(els: list, body: float, page_height: float) -> list | None:
    """A title printed over one of the page's pictures (a banner, a slide drawn as one image):
    of the lines of words lying on its pictures, the same test as `title_element`. Its words,
    left to right; they stay part of the picture as well, and open the page's section."""
    if not body:
        return None
    # one run of words: a line on a picture is split at column-sized gaps (as the page reader
    # splits text into columns), so captions side by side never read as one title
    lines = [seg.words for e in els if isinstance(e, ChartEl) for ln in group_lines(e.axis)
             for seg in split_segments(ln, max(14.0, 2.2 * statistics.median(w.h for w in ln)))]
    cands = [ln for ln in lines if ln and max(w.size for w in ln) >= body * TITLE_SIZE
             and min(w.bbox[1] for w in ln) < TOP_SHARE * page_height and len(ln) <= MAX_WORDS
             and is_wordy(" ".join(w.text for w in ln))]
    if not cands:
        return None
    big = max(max(w.size for w in ln) for ln in cands)
    top = min((ln for ln in cands if max(w.size for w in ln) >= big - 0.5), key=lambda ln: min(w.bbox[1] for w in ln))
    return sorted(top, key=lambda w: w.bbox[0])


def page_titles(per_page: dict[int, tuple[list, float, float]]) -> dict[int, TextEl]:
    """{page: title element} for every page that has a title of its own. `per_page` maps a page
    number to (its elements, the body text size, the page height). A line that is the "title"
    of several pages is the report's running title and is no page's own."""
    found = {n: title_element(els, body, h) for n, (els, body, h) in per_page.items()}
    found = {n: t for n, t in found.items() if t is not None}
    counts = Counter(key(" ".join(w.text for w in t.words)) for t in found.values())
    return {n: t for n, t in found.items() if counts[key(" ".join(w.text for w in t.words))] < RUNNING_PAGES}


def picture_titles(per_page: dict[int, tuple[list, float, float]], text_titles: dict[int, TextEl]) -> dict[int, list]:
    """{page: the title's words} for pages with no title of their own in text, whose title is
    printed over a picture; running titles (the same on several pages) left out, as for text."""
    found = {n: t for n, (els, body, h) in per_page.items() if n not in text_titles
             for t in [title_on_picture(els, body, h)] if t}
    text = lambda ws: key(" ".join(w.text for w in ws))  # noqa: E731
    counts = Counter(text(t) for t in found.values())
    counts.update(text(t.words) for t in text_titles.values())
    return {n: t for n, t in found.items() if counts[text(t)] < RUNNING_PAGES}
