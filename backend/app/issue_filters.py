"""Step 1's review list and its filters, worked out together from one set of rows.

The list shows the items that match every filter. Each filter's options are the values that
have at least one item under the *other* filters, with how many, so picking an option never
leads to an empty list ("Page 5" isn't offered under "Blocking" when page 5 only has notes).
A row is what the list shows: one per open figure (a figure flagged by several checks is one
decision) and one per other item. Rows that can't be decided side by side (a page note such
as "3 of 17 numbers here weren't captured", a page-build fault, a colleague's flag) are
counted as notes, so the list's count adds up to what the headline counts.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable

# Items that aren't a value at a spot in the PDF, so they can't be confirmed side by side.
NOT_FIGURE_CHECKS = frozenset({"rendered_page", "schema", "reviewer_flag", "completeness"})
FACETS = ("severity", "status", "check", "page", "section")
_FIELD = {"severity": "severity", "status": "status", "check": "check", "page": "page", "section": "section_id"}


def row_key(i: dict) -> str:
    return f"fig:{i['fid']}" if i.get("fid") and i.get("status") == "open" else f"issue:{i['id']}"


def is_figure_row(i: dict) -> bool:
    """Decidable side by side: an open value tied to a spot in the PDF."""
    return bool(i.get("status") == "open" and i.get("page") and i.get("bbox") and i.get("check") not in NOT_FIGURE_CHECKS)


def _value(i: dict, facet: str) -> str:
    v = i.get(_FIELD[facet])
    return "" if v is None else str(v)


def _matches(i: dict, filters: dict[str, str], skip: str | None = None) -> bool:
    return all(not want or facet == skip or _value(i, facet) == want for facet, want in filters.items())


# ------------------------------------------------------------------ section names

_LEAD = re.compile(r"^[\s•·▪►*‐‑‒–—―\-‘’“”'\"…]+")
_ENUM = re.compile(r"^\(?(?:[0-9]{1,3}|[a-zA-Z]|[ivxIVX]{1,4})[.)]\s+")
_CONSONANT_START = re.compile(r"^[bcdfghjklmnpqrstvwxz]{2}")
_CLUSTERS = re.compile(r"^(bl|br|ch|cl|cr|dr|fl|fr|gl|gr|kn|ph|pl|pr|sc|sh|sk|sl|sm|sn|sp|st|sw|th|tr|tw|wh|wr|sch|scr|shr|spl|spr|str|thr)")


def _shape_ok(t: str) -> bool:
    """Reads like words, not OCR noise ("gtaaliyy", "MM ENA"): the dashboard's former test."""
    if len(t) < 4 or len(re.findall(r"[A-Za-z]", t)) / len(t) < 0.6:
        return False
    words = [w for w in t.split() if re.search(r"[A-Za-z]", w)]
    if not words or all(len(w) <= 3 and w == w.upper() for w in words):
        return False

    def ok(w: str) -> bool:
        if w.isupper():                     # an acronym: "FMCG", "EBITDA" (vowels say nothing)
            return True
        x = re.sub(r"[^a-z]", "", w.lower())
        if len(x) < 2:
            return True
        vowels = len(re.findall(r"[aeiou]", x))
        if len(x) >= 4 and not 0.1 <= vowels / len(x) <= 0.75:     # "growth", "strengths" are words
            return False
        if re.search(r"([a-z])\1\1", x) or re.search(r"(yy|ii|uu|qq|jj|vv|ww|xx|hh)$", x):
            return False
        if re.search(r"[bcdfghjklmnpqrstvwxz]{5,}", x):
            return False
        return not (_CONSONANT_START.match(x) and not _CLUSTERS.match(x))
    return sum(ok(w) for w in words) / len(words) >= 0.75


def usable_heading(text: str | None, *, ocr: bool = False) -> str | None:
    """The heading as a filter label, or None when it reads as a fragment rather than a
    heading. A heading starts like one: with a capital letter or a digit once bullets, dashes,
    quotes and an enumerator ("(c)", "1.") are set aside, so "drivers", "or Bandra-Kurla
    Complex", "*for Gestated" and "= 51% of ..." are not headings. On a scanned page a lone
    all-caps word ("FYSOE") can't be told apart from an OCR fragment, so it isn't used."""
    t = re.sub(r"\s+", " ", text or "").strip()
    core = _ENUM.sub("", _LEAD.sub("", t)).strip()
    if not core or not (core[0].isupper() or core[0].isdigit()) or not _shape_ok(core):
        return None
    words = core.split()
    if ocr and len(words) == 1 and words[0].isupper():
        return None
    return _LEAD.sub("", t).strip()


def section_labels(sections: Iterable[dict], pages: Iterable[dict] = ()) -> dict[str, str]:
    """{section id: label}: "p12 · Financial highlights", else "Section on page 12" (numbered
    when a page has several unnamed ones)."""
    ocr_pages = {p.get("page") for p in pages if p.get("method") == "ocr"}
    out, unnamed = {}, defaultdict(int)
    for s in sections:
        page = (s.get("source") or {}).get("page") or s.get("page")
        h = usable_heading(s.get("heading_text"), ocr=page in ocr_pages)
        if h:
            out[s["id"]] = f"p{page} · {h[:57] + '…' if len(h) > 60 else h}" if page else h
        else:
            unnamed[page] += 1
            n = unnamed[page]
            out[s["id"]] = f"Section on page {page or '?'}" + (f" · {n}" if n > 1 else "")
    return out


# ------------------------------------------------------------------ the list and its filters

def build(issues: list[dict], filters: dict[str, str | None], *, sections: Iterable[dict] = (),
          pages: Iterable[dict] = (), check_labels: dict[str, str] | None = None) -> dict[str, Any]:
    """{"items": issues matching every filter, each marked `decidable` (a value to confirm side by
    side, see is_figure_row: the screen uses this rather than its own copy of the rule),
    "options": {facet: [{value, label, count}]}, "totals": {"rows", "figures", "notes",
    "all_rows"}}. One pass per facet over the rows the request already loaded: no extra queries."""
    want = {f: (filters.get(f) or "") for f in FACETS}
    items = [{**i, "decidable": is_figure_row(i)} for i in issues if _matches(i, want)]
    sections = list(sections)
    labels = section_labels(sections, pages)
    sec_page = {s["id"]: (s.get("source") or {}).get("page") or s.get("page") or 0 for s in sections}
    names = check_labels or {}

    options: dict[str, list[dict]] = {}
    for facet in FACETS:
        rows: dict[str, set[str]] = defaultdict(set)
        for i in issues:
            if _matches(i, want, skip=facet):
                rows[_value(i, facet)].add(row_key(i))
        rows.pop("", None)
        if want[facet] and want[facet] not in rows:
            rows[want[facet]] = set()           # keep the current choice (it has 0 now), so the screen can reset it
        if facet == "section":
            rows = {k: v for k, v in rows.items() if k in labels or k == want[facet]}
        opts = [{"value": k, "count": len(v),
                 "label": (f"Page {k}" if facet == "page" else labels.get(k, k) if facet == "section"
                           else names.get(k, k) if facet == "check" else k)} for k, v in rows.items()]
        order = {"page": lambda o: int(o["value"]) if o["value"].isdigit() else 0,
                 "section": lambda o: (sec_page.get(o["value"], 0), o["label"]),
                 "check": lambda o: o["label"].lower()}.get(facet, lambda o: o["value"])
        options[facet] = sorted(opts, key=order)

    figure_rows = {row_key(i) for i in items if is_figure_row(i)}
    all_rows = {row_key(i) for i in items}
    return {"items": items, "options": options,
            "totals": {"rows": len(all_rows), "figures": len(figure_rows), "notes": len(all_rows - figure_rows),
                       "all_rows": len({row_key(i) for i in issues})}}
