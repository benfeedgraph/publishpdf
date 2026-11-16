"""Guess report metadata from the PDF so the upload form comes pre-filled.

These are suggestions for the person uploading to confirm, never silent facts: the
confirmed values are what the report is filed under. Every value comes with a confidence;
only "high" is marked as detected on the form. A field with no evidence is left out (the
form shows it blank) rather than filled with a default.

There are no word lists. Whether a word is a name or an ordinary word, and which name is
the company's, is decided from how this PDF itself uses it: whether it is ever written in
lower case, how many pages use it, whether it is the running header or footer, whether the
body text uses it outside that header, possessives, initials, and the PDF's own metadata
(believed only when the body agrees). The only fixed vocabulary is the formats being read:
legal forms (Limited, Inc.), report-type phrases, period labels (Q2 FY26), month names,
currency signs and units.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import date
from functools import lru_cache

import pymupdf

from app.extraction.dates import MONTHS, parse_date
from app.extraction.tokens import CURRENCY_CODES, UNIT_CANON

# --- formats ---------------------------------------------------------------------------------
# Every word of a legal name is capitalised (or a joiner), so a match can't swallow the sentence
# before it: "Contents are hyper-linked ... Click 'ITC Limited" -> "ITC Limited". A curly
# apostrophe is part of a name: "Dr. Reddy’s Laboratories Limited".
_NAME_CHARS = r"[A-Za-z0-9&.,'’\-]"
_JOINERS = ("&", "and", "of", "the")
_NAME_WORD = r"(?:[A-Z]" + _NAME_CHARS + r"*|&|and|of|the)"
_LEGAL = r"(?:Limited|Ltd\.?|Inc\.?|Corporation|Corp\.?|plc|PLC|LLP|AG|SA|N\.V\.|Holdings)"
_COMPANY = re.compile(r"(?<![A-Za-z])([A-Z]" + _NAME_CHARS + r"*(?:\s" + _NAME_WORD + r"){0,6}?\s" + _LEGAL
                      + r"(?:\s(?:Limited|Ltd\.?))?)\b")                    # "Indian Oil Corporation Limited"
_LEGAL_SUFFIX = re.compile(r"\s" + _LEGAL + r"$")
_LEGAL_WORD = re.compile(_LEGAL + r"$")

_CUR = r"(?<![A-Za-z])(?P<cur>₹|Rs\.?|INR|US\$|USD|\$|€|EUR|£|GBP)"
_UNIT = r"(?P<unit>crores?|cr\.?|lakhs?|lacs?|millions?|mn|billions?|bn|thousands?)\b"
# A unit is read where it is about money: next to a currency ("₹ in crore", "₹K crores",
# "US$ bn") or after an amount in one ("$4.7 billion", "Rs. 37,000 cr"). A bare "billion" is
# never a reporting unit ("17 billion gallons of freshwater").
_TIED_UNIT = re.compile(_CUR + r"\s*(?:K|'000)?\s*(?:in\s+)?" + _UNIT, re.I)
_AMOUNT_UNIT = re.compile(_CUR + r"\s?\d[\d,.]*\s?" + _UNIT, re.I)
# Crore and lakh are rupee amounts even where the ₹ sign is drawn with a font of its own and
# comes out as another character: "J3,71,085 Crore", "·1,234.56 crore", "(` cr.)". Used only
# when no amount next to a readable currency names a unit ("40 lakh tonnes" counts tonnes).
_INR_WORD = r"(?P<unit>crores?|cr\.?|lakhs?|lacs?)\b"
_INR_AFTER_AMOUNT = re.compile(r"\d[\d,.]*\s?" + _INR_WORD, re.I)
_INR_CAPTION = re.compile(r"\(\s*[^\w\s()]?\s*(?:in\s+)?" + _INR_WORD + r"\.?\s*\)", re.I)

_ANNUAL = re.compile(r"annual report|integrated report|report\s(?:and|&)\saccounts|annual accounts", re.I)
_ANNUAL_YEAR = re.compile(r"(?:annual report|integrated report|report\s(?:and|&)\saccounts|annual accounts)"
                          r"\s*(?:for\s)?(?:FY\s?)?((?:19|20)\d{2})(?:\s?[-–/]\s?(\d{2,4}))?", re.I)
_DOC_TITLE = re.compile(r"investor presentation|earnings presentation|\bresults\b|" + _ANNUAL.pattern, re.I)
# "Q2 FY26", "Q2FY27", "Q2 F26" (FY written F), "H1 FY2026", "Q3 2025" (a calendar-year label)
_PERIOD_TOKEN = re.compile(r"\b(Q[1-4]|H[12]|9M)\s?[-'’]?\s?((?:FY|F)\s?'?\d{2,4}(?:\s?[-/]\s?\d{2,4})?|(?:19|20)\d{2})\b",
                           re.I)
_SPAN = r"quarter|three months|half\s?-?\s?year|six months|nine months|year"
# "quarter ended 30 September 2025", "Quarter and Half - year ended September 30, 2025"
_ENDED = re.compile(rf"({_SPAN})(?:\s+and\s+(?:{_SPAN}))?\s+ended\s+(?:on\s+)?"
                    r"([0-9]{1,2}(?:st|nd|rd|th)?[\s./-][A-Za-z0-9]{2,9}[\s.,/-]+[0-9]{2,4}"
                    r"|[A-Za-z]{3,9}\.?\s[0-9]{1,2},?\s*[0-9]{4})", re.I)
# A fiscal year stated outright: "Covering fiscal year 2025", "Fiscal Results 2025-26", "FY 2025-26"
_FY_PHRASE = re.compile(r"\b(?:fiscal|financial)\s+year\s+(?:(?:ended|ending)\s+)?(?P<a>(?:19|20)\d{2})"
                        r"(?:\s?[-–/]\s?(?P<b>\d{2,4}))?\b"
                        r"|\b(?:fiscal|financial)\s+[A-Za-z]+\s+(?P<c>(?:19|20)\d{2})\s?[-–/]\s?(?P<d>\d{2,4})\b"
                        r"|\bFY\s?(?P<e>(?:19|20)\d{2})(?:\s?[-–/]\s?(?P<f>\d{2,4}))?\b", re.I)
_FY_LABEL = re.compile(r"\bFY\s?'?((?:19|20)\d{2}\s?[-–/]\s?\d{2,4}|(?:19|20)\d{2}|\d{2})\b", re.I)
_MON = "(?:" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?"
# "21st September 2020", "12 December 2023", "February 17th, 2026", "October 17, 2026"
_COVER_DATE = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?[\s-]+({_MON})[\s,-]+((?:19|20)\d{{2}})\b"
                         rf"|\b({_MON})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+((?:19|20)\d{{2}})\b", re.I)
_YEAR = re.compile(r"(?<![\d,.])((?:19|20)\d{2})(?![\d%])")

NAME_PAGES = 25          # pages read (text layer only, so cheap) for company-name evidence
OCR_PAGES = 3            # most pages read with OCR (in parallel) when there is no text layer

Line = tuple[float, tuple[float, float, float, float], str]       # font size, bbox, text


# --- small helpers -----------------------------------------------------------------------------

def _fiscal(d: date, start_month: int) -> tuple[int, int]:
    """(fiscal year it ends in, quarter). April start: Sep 2025 -> (2026, 2); January: (2025, 3)."""
    if start_month == 1:
        return d.year, (d.month - 1) // 3 + 1
    return (d.year + 1 if d.month >= start_month else d.year), ((d.month - start_month) % 12) // 3 + 1


def _end_year(start: str, end: str | None) -> int:
    """'2025', '26' -> 2026; '2024', '2025' -> 2025; '2025', None -> 2025."""
    s = int(start)
    if not end:
        return s
    return int(end) if len(end) == 4 else s // 100 * 100 + int(end)


def _fy_label(s: str) -> int:
    """'26' -> 2026, '2025' -> 2025, '2025-26' -> 2026."""
    s = re.sub(r"\s", "", s).lstrip("'’")
    if m := re.fullmatch(r"(\d{4})[-–/](\d{2,4})", s):
        return _end_year(m.group(1), m.group(2))
    return int(s) if len(s) == 4 else 2000 + int(s)


@lru_cache(maxsize=1024)
def _word_re(phrase: str, flags: int = 0) -> re.Pattern[str]:
    # Not inside another word, a URL or an e-mail address ("www.infosys.com" isn't a use).
    return re.compile(r"(?<![A-Za-z0-9./@])" + re.escape(phrase).replace(r"\ ", r"\s+")
                      + r"(?![A-Za-z0-9]|\.[a-z])", flags)


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip()


def _format_token(w: str) -> bool:
    """A number, page or period label, month or punctuation: never part of a name."""
    lw = w.lower().strip(".,:;()[]")
    return (not lw or lw in MONTHS or not re.search(r"[a-z]", lw)
            or bool(re.fullmatch(r"(?:q[#\d]|h[12#]|9m)?\W*(?:fy?\s?'?[#\d\-/]*)?|[a-z]{1,9}['’][#\d]+", lw)))


class _Corpus:
    """Page texts (and the body text outside running headers/footers) with word statistics."""

    def __init__(self, page_texts: list[str], body_texts: list[str]):
        self.pages = [_norm(t) for t in page_texts]
        self.all = " ".join(self.pages)
        self.body = " ".join(_norm(t) for t in body_texts)
        self.text_pages = sum(1 for p in self.pages if p)
        self._memo: dict[tuple[str, str], object] = {}   # the same questions come up for many candidates

    def _cached(self, kind: str, key: str, compute):
        if (kind, key) not in self._memo:
            self._memo[(kind, key)] = compute()
        return self._memo[(kind, key)]

    def pages_with(self, phrase: str) -> int:
        rx = _word_re(phrase)
        return self._cached("pages", phrase, lambda: sum(1 for p in self.pages if rx.search(p)))

    def count(self, phrase: str) -> int:
        return self._cached("count", phrase, lambda: len(_word_re(phrase).findall(self.all)))

    def body_count(self, phrase: str) -> int:
        return self._cached("body", phrase, lambda: len(_word_re(phrase).findall(self.body)))

    def proper(self, word: str) -> bool:
        return self._cached("proper", word, lambda: self._proper(word))

    def _proper(self, word: str) -> bool:
        """Used as a name, not an ordinary word: it (almost) never appears in lower case.
        'Focus', 'Creating' and 'Engagement' do; 'Infosys', 'ITC' and 'Apple' don't. An
        all-caps word says nothing either way ('ENGAGEMENT' in a section tab), so only a
        short acronym that is never written in lower case counts in that form."""
        title = caps = low = 0
        for m in _word_re(word, re.I).finditer(self.all):
            s = m.group(0)
            if s.islower():
                low += 1
            elif s.isupper():
                caps += 1
            else:
                title += 1
        if word.isupper():
            return len(word) <= 5 and caps > 0 and low == 0
        # one stray lower-case use (an address OCR read without its dot: "mahindra com") is
        # forgiven a name used capitalised often; an ordinary word is lower case again and again
        return title > 0 and low <= max(1 if title + caps >= 5 else 0, 0.05 * (title + caps))

    def brand_word(self, w: str) -> bool:
        if len(w) < 2 or not w[0].isupper() or any(ch.isdigit() for ch in w) or sum(ch.isalpha() for ch in w) < 2:
            return False
        return w.lower().strip(".") not in MONTHS and not _LEGAL_WORD.match(w) and self.proper(w)

    def extend(self, phrase: str) -> str:
        """'HDFC' -> 'HDFC Bank', 'Tata' -> 'Tata Motors': add the next word while it follows
        nearly every use of the name in the body text and is itself used as a name."""
        while len(phrase.split()) < 4:
            total = len(_word_re(phrase).findall(self.body))
            follow = Counter(m.group(1) for m in re.finditer(
                _word_re(phrase).pattern + r" ([A-Z][A-Za-z&\-]+)\b", self.body))
            if not total or not follow:
                break
            nxt, n = follow.most_common(1)[0]
            # a capitalised word, not an acronym: 'HDFC Bank', never 'HDFC Bank PAT'
            if n < 0.6 * total or nxt.isupper() or not self.brand_word(nxt):
                break
            phrase = f"{phrase} {nxt}"
        return phrase


# --- page reading --------------------------------------------------------------------------

def _lines(layout: dict) -> list[Line]:
    """Every text line of a page's get_text('dict') layout."""
    out = []
    for b in layout["blocks"]:
        for ln in b.get("lines", []):
            t = _norm("".join(s["text"] for s in ln["spans"]))
            if t:
                out.append((max(s["size"] for s in ln["spans"]), tuple(ln["bbox"]), t))
    return out


def _ocr_lines(words: list) -> list[tuple[Line, float]]:
    """OCR words as lines, each with its lowest word confidence. Words OCR barely believes are
    dropped first (a logo read as 'EC)' beside 'LARSEN & TOUBRO'); a row splits where a gap
    wider than three letter-heights opens."""
    rows: list[list] = []
    for w in sorted((w for w in words if w.conf >= 0.5), key=lambda w: (w.src[1] + w.src[3]) / 2):
        yc, h = (w.src[1] + w.src[3]) / 2, w.src[3] - w.src[1]
        if rows and abs(rows[-1][0] - yc) <= 0.5 * max(h, rows[-1][1]):
            rows[-1][2].append(w)
        else:
            rows.append([yc, h, [w]])
    out = []
    for _, h, ws in rows:
        ws.sort(key=lambda w: w.src[0])
        segs, seg = [], [ws[0]]
        for a, b in zip(ws, ws[1:]):
            if b.src[0] - a.src[2] > 3 * max(h, 1):
                segs.append(seg)
                seg = []
            seg.append(b)
        segs.append(seg)
        for s in segs:
            box = (min(x.src[0] for x in s), min(x.src[1] for x in s), max(x.src[2] for x in s), max(x.src[3] for x in s))
            out.append(((box[3] - box[1], box, " ".join(x.text for x in s)), min(x.conf for x in s)))
    return out


def _edge_lines(lines: list[Line], height: float) -> set[str]:
    """Lines in the top or bottom tenth of the page (running headers and footers), digits normalised."""
    return {re.sub(r"\d+", "#", t) for _, bb, t in lines
            if len(t) >= 3 and not height * 0.10 <= (bb[1] + bb[3]) / 2 <= height * 0.90}


def _big(lines: list[Line]) -> list[Line]:
    """The page's big type: lines at least 60% the size of its largest."""
    top = max((sz for sz, _, _ in lines), default=0)
    return [ln for ln in lines if top and ln[0] >= 0.6 * top]


# --- company -------------------------------------------------------------------------------

def _legal_candidates(c: _Corpus, text: str, page_lines: list[list[Line]]) -> list[tuple[str, int, int]]:
    """Legal names ('Tata Motors Limited') as (name, pages its short name is used on, mentions).
    A match can start mid-sentence ('PB Balaji Group CFO Tata Motors Limited'): keep the longest
    tail of the name that more than one page uses. A name used on one page only counts when a
    distinctive word of it is big type on another page: an exchange a cover letter is addressed
    to ('National Stock Exchange of India Limited') isn't."""
    found: Counter[str] = Counter()
    for m in _COMPANY.finditer(text):
        name = re.sub(r"\s+", " ", m.group(1)).strip(" ,.-'")
        suffix = _LEGAL_SUFFIX.search(name)
        if not suffix:
            continue
        head = name[:suffix.start()].split()
        if not head or sum(any(ch.isdigit() for ch in w) for w in head) > 1 or re.search(r"['’]\d", " ".join(head)):
            continue                       # "Jun'25 Sep'25 CA SA": a table row, not a name
        tails = [" ".join(head[k:]) for k in range(len(head)) if len(head) - k >= min(2, len(head))
                 and head[k][:1].isupper() and head[k][-1] not in "'’\""]
        if not tails:
            continue
        uses = {t: c.pages_with(t) for t in tails}
        repeated = [t for t in tails if uses[t] >= 2]
        best = max(uses.values())
        head_s = repeated[0] if repeated else next(t for t in tails if uses[t] >= 0.8 * best)
        found[head_s + name[suffix.start():]] += 1
    for n in sorted(found, key=len):       # "Indian Oil Corporation" mentions count for "... Corporation Limited"
        longer = [m for m in found if m != n and m.startswith(n + " ") and _LEGAL_SUFFIX.search(m[len(n):])]
        if longer:
            found[max(longer, key=lambda m: found[m])] += found.pop(n)
    by_head: dict[str, Counter[str]] = {}
    for n, k in found.items():                                        # "… Ltd" and "… Limited": one company
        by_head.setdefault(_LEGAL_SUFFIX.sub("", n), Counter())[n] += k
    out = []
    for head, forms in by_head.items():
        pages = c.pages_with(head)
        if pages < min(2, c.text_pages):
            # a letterhead name the deck keeps using ("Mahindra" on other pages), or big type
            # on another page ("FINANCE" in a scanned logo); not an addressee
            words = [w for w in (re.sub(r"['’]s$", "", x) for x in head.split()) if len(w) >= 3 and c.brand_word(w)]
            elsewhere = [" ".join(t for _, _, t in _big(lines)) for lines in page_lines
                         if not _word_re(head).search(" ".join(t for _, _, t in lines))]
            first = words[0] if words and head.split()[0].startswith(words[0]) else None
            if not ((first and c.pages_with(first) >= 3)
                    or any(_word_re(w, re.I).search(b) for w in words for b in elsewhere)):
                continue
        name = max(forms, key=lambda f: (forms[f], len(f)))
        out.append((name, pages, sum(forms.values())))
    return out


def _header_names(c: _Corpus, running: Counter[str]) -> list[str]:
    """Names in the running header or footer ('HDFC Bank Presentation Q2 FY2027' on every
    page -> 'HDFC Bank'): the leading words the body text also uses, outside the header. A
    section tab ('Geschäftsverlauf', 'APPENDIX') or a report title the body never repeats
    ('Earnings Presentation') isn't a name; a caption with lower-case words is a sentence."""
    need = max(2, math.ceil(0.4 * c.text_pages))
    out = []
    for line, k in running.items():
        if k < need:
            continue
        segs = re.split(r"\s+[|·•–—-]\s+|:\s", line)
        # "Acme Industries | Investor Presentation": a segment beside the report's own title
        # is the name, even where the body never repeats it
        titled = [bool(_DOC_TITLE.search(s)) for s in segs]
        for seg, is_title in zip(segs, titled):
            words = seg.split()
            while words and _format_token(words[-1]):
                words.pop()
            while words and _format_token(words[0]):
                words.pop(0)
            if not words or len(words) > 6 or not all(w[:1].isupper() or w in _JOINERS for w in words):
                continue
            beside_title = any(titled) and not is_title
            run = []
            for w in words:
                if w not in _JOINERS and c.body_count(w) < 2 and not (beside_title and c.brand_word(w)):
                    break
                run.append(w)
            while run and run[-1] in _JOINERS:
                run.pop()
            if run and c.brand_word(run[0]):
                out.append(" ".join(run))
    return out


def _metadata_names(c: _Corpus, doc: pymupdf.Document) -> list[str]:
    """The PDF's Title/Author/Subject, only as far as the body uses it on several pages: a file
    saved from another company's template still says 'Roche' while every page says 'Novartis'."""
    out = []
    for key in ("title", "author", "subject"):
        for run in re.findall(r"[A-Z][\w&.’'\-]*(?:\s+(?:[A-Z][\w&.’'\-]*|&|and|of|the))*", (doc.metadata or {}).get(key) or ""):
            kept = []
            for w in run.split():
                if w not in _JOINERS and c.pages_with(w) < min(3, c.text_pages):
                    break
                kept.append(w)
            while kept and kept[-1] in _JOINERS:
                kept.pop()
            if kept and c.brand_word(kept[0]):
                out.append(" ".join(kept))
    return out


def _brand_candidates(c: _Corpus, running: Counter[str], cover_lines: list[list[Line]],
                      doc: pymupdf.Document, text_layer: bool) -> list[tuple[str, str]]:
    out = [(n, "header") for n in _header_names(c, running)]
    out += [(n, "metadata") for n in _metadata_names(c, doc)]
    if not text_layer:
        # A few OCR'd pages are too little text to tell a name from a slogan by how it is
        # written or repeated ('Enduring Value' starts the cover and every page): a scan's
        # name comes from a legal name, its initials or corroborated metadata only.
        return out
    for lines in cover_lines:              # the cover title's first word ("Infosys AI Day"), read as one title
        title = " ".join(t for _, _, t in sorted(_big(lines), key=lambda ln: (round(ln[1][1]), ln[1][0])))
        w = re.split(r"[\s,:|]", title.strip(), maxsplit=1)[0] if title.strip() else ""
        if c.brand_word(w) and c.pages_with(w) >= min(3, c.text_pages) and c.body_count(w) >= 2:
            out.append((w, "cover"))
    # a possessive the report uses on more than one page: "ITC's", "Apple's" (not "the Company's" twice on one)
    poss = Counter(m.group(1) for m in re.finditer(r"(?<![A-Za-z])([A-Z][A-Za-z&]+)[’']s\b", c.all))
    out += [(w, "possessive") for w, k in poss.items()
            if c.pages_with(w) >= 2 and (k >= 2 or c.pages_with(w) >= 3) and c.brand_word(w)]
    return out


def _pick_brand(c: _Corpus, cands: list[tuple[str, str]]) -> str | None:
    """The name most sources agree on; a source's rank breaks ties (a running header over the
    metadata or the cover title, those over a possessive), then page spread. A name inside a
    longer candidate ('India' in 'Hitachi Energy India') is the longer one's."""
    names = {n for n, _ in cands}
    names = {n for n in names if not any(o != n and _word_re(n).search(o) for o in names)}
    if not names:
        return None
    rank = {"header": 3, "metadata": 2, "cover": 2, "possessive": 1}
    def support(n: str) -> tuple[int, int, int]:
        first = n.split()[0]
        srcs = {s for m, s in cands if m == n or m.split()[0] == first or _word_re(m).search(n)}
        return len(srcs), max(rank[s] for s in srcs), c.pages_with(n)
    return max(sorted(names), key=support)


def _consistent(legal: str, brand: str) -> bool:
    """'Tata Motors' and 'Tata Motors Limited', 'Reddy' and 'Dr. Reddy’s Laboratories Limited'."""
    head = {re.sub(r"['’]s$", "", w) for w in _LEGAL_SUFFIX.sub("", legal).split()}
    return all(w in head for w in brand.split() if w not in _JOINERS)


def _initials_name(c: _Corpus, ocr_lines: dict[int, list[tuple[Line, float]]], heights: dict[int, float]) -> str | None:
    """A scanned deck's name: OCR reads it on the cover and again in the header or footer band
    of the next pages ('LARSEN & TOUBRO'). A slogan repeats the same way ('Enduring Value'),
    so the report must also use it as a name: by its initials ('L&T's future business') or,
    for an acronym ('ITC'), as is."""
    pages = sorted(ocr_lines)
    if len(pages) < 2:
        return None
    cover, later = pages[0], pages[1:]
    cover_text = " ".join(t for (_, _, t), _ in ocr_lines[cover])
    seen: Counter[str] = Counter()
    for i in later:
        phrases = set()
        for (_, bb, line), conf in ocr_lines[i]:
            if conf < 0.8 or 0.15 < (bb[1] + bb[3]) / 2 / heights[i] < 0.85:
                continue
            ws = line.split()
            while ws and _format_token(ws[-1]):
                ws.pop()
            while ws and _format_token(ws[0]):
                ws.pop(0)
            if 1 <= len(ws) <= 4:
                phrases.add(" ".join(ws))
        seen.update(phrases)
    for phrase, k in seen.most_common():
        if k < len(later) or not _word_re(phrase, re.I).search(cover_text):
            continue
        words = phrase.split()
        if len(words) == 1:
            ok = words[0].isupper() and 2 <= len(words[0]) <= 5
        else:
            initials = "".join(w if w == "&" else w[0] for w in words if w == "&" or w[:1].isupper())
            ok = (sum(w[:1].isupper() for w in words) >= 2
                  and bool(re.search(r"(?<![A-Za-z])" + re.escape(initials) + r"(?![A-Za-z])", c.all)))
        if ok:
            return " ".join(w.capitalize() if w.isupper() and len(w) >= 5 else w for w in words)
    return None


def company_from_filename(filename: str | None) -> str | None:
    """'ITC_Annual_Report_2025.pdf' -> 'ITC'. detect_metadata offers it only where the PDF's
    own text contains it (see _filename_in_doc)."""
    if not filename:
        return None
    stem = re.sub(r"\.pdf$", "", filename.strip(), flags=re.I)
    stem = re.sub(r"[_\-.+]+", " ", stem)
    stem = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", stem)              # "AcmeIndustries" -> "Acme Industries"
    stem = _FILENAME_NOISE.sub(" ", stem)
    stem = re.sub(r"\s{2,}", " ", stem).strip(" ,&")
    if len(stem) < 2 or sum(ch.isalpha() for ch in stem) < 2:
        return None
    return " ".join(w if w.isupper() else w[:1].upper() + w[1:] for w in stem.split())[:120]


_FILENAME_NOISE = re.compile(
    r"\b(?:annual|integrated|quarterly|investor|earnings|financial|results?|reports?|presentation|accounts|"
    r"statements?|press|release|final|draft|web|print|version|v\d+|q[1-4]|h[12]|9m|fy\s?\d{2,4}|"
    r"(?:19|20)\d{2}(?:\s?[-–]\s?\d{2,4})?|\d+)\b", re.I)


def _filename_in_doc(c: _Corpus, filename: str | None) -> str | None:
    """The leading words of the file name that the PDF's own text uses as a name:
    'ITC_30_pages_test.pdf' + a cover reading 'ITC Next ...' -> 'ITC'; 'test.pdf' -> None."""
    stem = re.sub(r"\.pdf$", "", (filename or "").strip(), flags=re.I)
    words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", re.sub(r"[_\-.+]+", " ", stem)).split()
    for k in range(len(words), 0, -1):
        m = _word_re(" ".join(words[:k]), re.I).search(c.all)
        if m and c.brand_word(m.group(0).split()[0]):
            return re.sub(r"\s+", " ", m.group(0))
    return None


def _company(c: _Corpus, doc: pymupdf.Document, running: Counter[str], cover_lines: list[list[Line]],
             page_lines: list[list[Line]], head_text: str, filename: str | None,
             ocr_lines: dict[int, list[tuple[Line, float]]], heights: dict[int, float]) -> tuple[str, str] | None:
    """(name, confidence), from the document only: never a slogan, a section heading, a
    sensitivity label ('Classification - Confidential'), an auditor or an exchange."""
    legal = sorted(_legal_candidates(c, head_text, page_lines), key=lambda x: (x[1], x[2]), reverse=True)
    brand = _pick_brand(c, _brand_candidates(c, running, cover_lines, doc, text_layer=len(ocr_lines) < c.text_pages))
    brand = c.extend(brand) if brand else None
    if legal:
        if brand and (same := [x for x in legal if _consistent(x[0], brand)]):
            name, pages, _ = same[0]
            return name, "high" if pages >= 2 else "medium"
        name, pages, _ = legal[0]
        # A legal name the report hardly uses loses to the name it uses throughout
        # ("Ernst & Young LLP" in an assurance note vs "Apple" on every page).
        if not brand or 2 * pages >= c.pages_with(brand):
            return name, "high" if pages >= 2 else "medium"
    if brand:
        full = re.search(_word_re(brand).pattern + r"\s(Limited|Ltd\.?|Inc\.?|Corporation|plc|PLC|N\.V\.)(?![A-Za-z])", c.all)
        return (f"{brand} {full.group(1)}", "high") if full else (brand, "medium")
    if name := _initials_name(c, ocr_lines, heights):
        return name, "medium"
    if guess := _filename_in_doc(c, filename):
        return guess, "low"
    return None


# --- money -----------------------------------------------------------------------------------

def _currency(text: str) -> tuple[str, str] | None:
    """(currency, confidence) by how often each one's marks appear; "Crore" counts in any case."""
    counts = {"INR": len(re.findall(r"₹|\bRs\.?\s|\bINR\b|(?i:\b(?:crores?|lakhs?|lacs?)\b)", text)),
              "USD": len(re.findall(r"US\$|\bUSD\b|\$\s?\d", text)),
              "EUR": len(re.findall(r"€|\bEUR\b", text)),
              "GBP": len(re.findall(r"£|\bGBP\b", text))}
    (cur, n), (_, runner_up) = sorted(counts.items(), key=lambda kv: -kv[1])[:2]
    if not n:
        return None
    return cur, "high" if n >= 3 and n >= 2 * runner_up else "medium"


def _unit(text: str, currency: str | None) -> tuple[str, str] | None:
    """(unit, confidence) from money mentions in the report's own currency: the 'US$ 39.1
    Billion' conversions in a rupee report don't make it a dollar-billion report."""
    units: Counter[str] = Counter()
    for m in [*_TIED_UNIT.finditer(text), *_AMOUNT_UNIT.finditer(text)]:
        if CURRENCY_CODES.get(m["cur"].lower()) == currency:
            units[UNIT_CANON[m["unit"].lower()]] += 1
    if not units and currency in (None, "INR"):
        for m in [*_INR_AFTER_AMOUNT.finditer(text), *_INR_CAPTION.finditer(text)]:
            units[UNIT_CANON[m["unit"].lower()]] += 1
    if not units:
        return None
    (unit, n), *rest = units.most_common()
    return unit, "high" if n >= 2 and n >= 2 * (rest[0][1] if rest else 0) else "medium"


# --- period ------------------------------------------------------------------------------------

def _fy_start(text: str, currency: str | None) -> int:
    """The month the report's fiscal year starts: as the report itself states its year-end
    ('fiscal year ended March 31, 2025' -> April), else April for a rupee report (India's
    statutory year), else January."""
    ends: Counter[int] = Counter()
    for m in _ENDED.finditer(text):
        if m.group(1).lower() == "year" and (d := parse_date(m.group(2))) and d.precision == "day":
            ends[int(d.iso[5:7])] += 1
    if ends:
        return ends.most_common(1)[0][0] % 12 + 1
    return 4 if currency == "INR" else 1


def _period(texts: list[str], text: str, cover: str, fy_start: int,
            dated_period: bool = True) -> tuple[int | None, str | None, str]:
    """(fiscal year, period, confidence), strongest evidence first; nothing the PDF doesn't say
    or plainly imply. A cover date is read in the report's own fiscal calendar, and gives a
    quarter only for a results deck or presentation (`dated_period`): a broker note or an ESG
    report dated in April isn't a Q1 report."""
    def token(pm: re.Match) -> tuple[str, int, int]:
        kind = pm.group(1).upper()
        return kind[0], int(kind[1]) if kind[0] in "QH" else 0, _fy_label(re.sub(r"^F[Yy]?", "", pm.group(2)))

    tokens = Counter(token(pm) for pm in _PERIOD_TOKEN.finditer(text + " " + cover))
    if tokens:                             # "Q2 FY27": the latest year shown (a deck compares with last year's)
        latest = max(fy for _, _, fy in tokens)
        (kind, num, fy), _ = max(((k, n) for k, n in tokens.items() if k[2] == latest), key=lambda kv: kv[1])
        label = {"Q": f"q{num}", "H": f"h{num}", "9": "9m"}[kind]
        sure = tokens.most_common(1)[0][0] == (kind, num, fy) or (kind, num, fy) in {token(pm) for pm in _PERIOD_TOKEN.finditer(cover)}
        return fy, label, "high" if sure else "medium"

    def ended(page_texts: list[str]) -> tuple[int, str] | None:
        found = []
        for t in page_texts:
            for em in _ENDED.finditer(t):
                d = parse_date(em.group(2))
                if d and d.precision == "day":
                    found.append((em.group(1).lower(), date.fromisoformat(d.iso)))
        if not found:
            return None
        what, d = max(found, key=lambda e: e[1])
        fy, q = _fiscal(d, fy_start)
        return fy, ("fy" if what == "year" else "9m" if "nine" in what else
                    f"h{1 if q <= 2 else 2}" if ("half" in what or "six" in what) else f"q{q}")

    if e := ended([cover]):                                           # "quarter ended 30 September 2026" on the cover
        return e[0], e[1], "medium"
    if m := _ANNUAL_YEAR.search(texts[0] if texts else ""):           # "Integrated Annual Report 2025-26" on page 1
        return _end_year(m.group(1), m.group(2)), "fy", "medium"
    for scope in (cover, text):                                       # "Covering fiscal year 2025", "Fiscal Results 2025-26"
        stated = []
        for m in _FY_PHRASE.finditer(scope):
            a, b = next((m.group(x), m.group(y)) for x, y in (("a", "b"), ("c", "d"), ("e", "f")) if m.group(x))
            # "fiscal year 2025" is the full year; a bare "FY2024" only gives the year
            stated.append((_end_year(a, b), None if m.group("e") else "fy"))
        if stated:                                                    # the latest: older ones are history
            fy, label = max(stated, key=lambda s: s[0])
            return fy, label, "medium"
    if m := _ANNUAL_YEAR.search(cover):
        return _end_year(m.group(1), m.group(2)), "fy", "medium"
    for m in _COVER_DATE.finditer(cover):                            # the date on the cover: 12 Dec 2023 -> Q3 FY2024
        day, mon, year = (m.group(1), m.group(2), m.group(3)) if m.group(1) else (m.group(5), m.group(4), m.group(6))
        d = parse_date(f"{day} {mon.rstrip('.')} {year}")
        if d and d.precision == "day":
            fy, q = _fiscal(date.fromisoformat(d.iso), fy_start)
            return fy, f"q{q}" if dated_period else None, "low"
    if e := ended(texts):                                             # may be boilerplate about another filing
        return e[0], e[1], "medium"
    fys = Counter(_fy_label(x) for x in _FY_LABEL.findall(text))
    if fys:                                                           # chart labels: most used, ties to the latest;
        return max(fys.items(), key=lambda kv: (kv[1], kv[0]))[0], None, "low"   # they don't say which period
    years = Counter(int(y) for y in _YEAR.findall(cover))
    if years:                                                         # "Investor Day 2025"
        return max(years.items(), key=lambda kv: (kv[1], kv[0]))[0], None, "low"
    return None, None, "low"


# --- the whole thing -------------------------------------------------------------------------

def detect_metadata(pdf_bytes: bytes, max_pages: int = 6, filename: str | None = None) -> dict:
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    n = min(NAME_PAGES, doc.page_count)
    texts: list[str] = []
    page_lines: list[list[Line]] = []
    for i in range(n):
        # One text pass per page, without images: the "dict" layout's default embeds every
        # image's bytes, which cost seconds per page on image-heavy decks.
        page = doc[i]
        tp = page.get_textpage(flags=pymupdf.TEXTFLAGS_TEXT)
        texts.append(page.get_text("text", textpage=tp))
        page_lines.append(_lines(page.get_text("dict", textpage=tp)))
    text_layer = [len(t.strip()) >= 10 for t in texts]
    # The deck's own cover: page 1, or, when page 1 is a covering letter in another orientation
    # (portrait letter, landscape slides), the first page shaped like most of the document.
    landscape = [doc[i].rect.width > doc[i].rect.height for i in range(n)]
    majority = Counter(landscape).most_common(1)[0][0] if n else False
    deck_cover = next((i for i in range(n) if landscape[i] == majority), 0)
    covers = sorted({0, deck_cover}) if n else []
    scanned = n > 1 and len("".join(texts[1:max_pages]).strip()) < 40    # no text layer after page 1

    # --- OCR where there is no text layer: the cover(s), and for a scanned PDF the next pages
    ocr_lines: dict[int, list[tuple[Line, float]]] = {}
    want = [i for i in covers if not text_layer[i]] + ([i for i in range(1, n) if not text_layer[i]] if scanned else [])
    want = sorted(dict.fromkeys(want))[:OCR_PAGES]
    if want:
        try:
            from concurrent.futures import ThreadPoolExecutor

            from app.extraction import ocr
            prepared = [ocr.prepare_page(doc[i]) for i in want]       # PyMuPDF: one thread
            with ThreadPoolExecutor(len(prepared)) as pool:           # Tesseract: side by side
                for i, words in zip(want, pool.map(ocr.run_prepared, prepared)):
                    ocr_lines[i] = _ocr_lines(words)
                    page_lines[i] = [ln for ln, _ in ocr_lines[i]]
                    texts[i] = " ".join(t for _, _, t in page_lines[i]) + " " + texts[i]
        except Exception:  # noqa: BLE001 - suggestions only; OCR may be absent
            pass

    # Running headers and footers come from the text layer only: OCR'd pages are too few to
    # tell a name from a slogan by repetition (that is what _initials_name is for).
    running: Counter[str] = Counter()
    for i in range(n):
        if text_layer[i]:
            running.update(_edge_lines(page_lines[i], doc[i].rect.height))
    need = max(2, math.ceil(0.4 * sum(text_layer)))
    furniture = {ln for ln, k in running.items() if k >= need}
    body = [" ".join(t for _, _, t in page_lines[i] if re.sub(r"\d+", "#", t) not in furniture) for i in range(n)]
    corpus = _Corpus(texts, body)
    flat = [_norm(t) for t in texts]
    text = " ".join(flat[:max_pages])
    cover_text = " ".join(flat[i] for i in covers)
    out: dict = {"page_count": doc.page_count, "confidence": {}}

    # --- company
    heights = {i: doc[i].rect.height for i in ocr_lines}
    found = _company(corpus, doc, running, [page_lines[i] for i in covers], page_lines, text, filename, ocr_lines, heights)
    if found:
        out["company_name"], out["confidence"]["company_name"] = found[0][:120], found[1]

    # --- report type: offered only where the PDF's wording says what it is. Slide-shaped pages
    # alone, or nothing at all, are not shown (the form starts blank), but slides still count
    # as a dated deck for reading a quarter off its cover date below.
    low = text.lower()
    title = " ".join(t for i in covers for _, _, t in _big(page_lines[i])) or cover_text
    if _ANNUAL.search(flat[0] if flat else ""):
        kind, conf = "annual_report", "high"
    elif _ANNUAL.search(text) and not majority:                       # slides quoting "our annual report" aren't one
        kind, conf = "annual_report", "medium"
    elif "investor presentation" in low or "earnings presentation" in low:
        kind, conf = "investor_presentation", "medium"
    elif re.search(r"\bpresentation\b", " ".join([title, *furniture]), re.I):
        # the deck calls itself one in its title or on every page: "Earnings Call Presentation",
        # "Investor and Analyst Presentation", "HDFC Bank Presentation Q2 FY2027"
        kind, conf = "investor_presentation", "medium"
    elif re.search(r"\breport\b", title, re.I):                       # a report that isn't annual: ESG, progress, impact
        kind, conf = "other", "medium"
    elif majority and doc.page_count < 80:
        kind, conf = "investor_presentation", None                    # the pages' shape: not shown
    elif re.search(r"results|quarter ended|financial results|profit and loss", low):
        kind, conf = "quarterly_results", "medium"
    else:
        kind, conf = None, None
    if conf:
        out["report_type"], out["confidence"]["report_type"] = kind, conf

    # --- currency and unit: the first pages, else the rest of what was read (then only a
    # guess); nothing found -> left out, never a default.
    wide = " ".join(flat)
    cur = _currency(text) or ((x[0], "medium") if (x := _currency(wide)) else None)
    unit = _unit(text, cur and cur[0]) or ((u[0], "medium") if (u := _unit(wide, cur and cur[0])) else None)
    if cur is None and unit and unit[0] in ("crore", "lakh"):           # "(` cr.)" and no readable ₹ sign
        cur = ("INR", "medium")
    if cur:
        out["currency"], out["confidence"]["currency"] = cur
    if unit:
        symbol = {"INR": "₹", "USD": "USD", "EUR": "EUR", "GBP": "GBP"}.get(cur[0] if cur else "", "")
        out["reporting_unit"], out["confidence"]["reporting_unit"] = f"{symbol} {unit[0]}".strip(), unit[1]

    # --- period and fiscal year
    fy, period, conf = _period(flat[:max_pages], text, cover_text, _fy_start(wide, cur and cur[0]),
                               dated_period=kind in ("investor_presentation", "quarterly_results"))
    if out.get("report_type") == "annual_report":
        period = "fy"
        if fy is None and (ym := _ANNUAL_YEAR.search(text)):          # "Report and Accounts 2026"
            fy, conf = _end_year(ym.group(1), ym.group(2)), "medium"
    if fy is not None:
        out["fiscal_year"] = fy
        out["confidence"]["fiscal_year"] = conf
    if period is not None:
        out["period"] = period
        out["confidence"]["period"] = conf
    return out
