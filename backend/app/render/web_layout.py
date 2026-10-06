"""Built-in web layout of design-led pages: no AI, no credits.

A design-led PDF page (a cover spread, a strategy page with photos and coloured panels)
is rebuilt as web sections — headings, paragraphs, bullet lists, cards, picture rows,
picture-beside-text — from its own elements (app/render/elements.py). The arrangement
comes from the page's geometry: rows and columns found by cutting along the white space
between elements (XY-cut), headings from type size and weight.

The sections use the same shapes as the AI layout (app/render/ai_layout.py), so one
renderer draws both; an AI layout, when one has been paid for, replaces this one. Every
element is placed exactly once — nothing is dropped except page numbers and running
headers, and no text is written here.
"""

from __future__ import annotations

import re
from statistics import median

from app.render.elements import Element, is_bullet

ROW_GAP = 5.0            # points of white space that separate two rows
COL_GAP = 10.0           # points of white space that separate two columns
CUT_SLACK = 8.0          # failing a clean cut, a picture may overlap its neighbour this much
CAPTION_GAP = 24.0       # a short line this close under a picture is its caption
PROSE_COLUMN_WORDS = 70  # columns this wordy, with no titles, are one text in columns, not cards


def _words(e: Element) -> int:
    return len(e.text.split())


def _body_size(els: list[Element]) -> float:
    sized = [(e.size, len(e.text)) for e in els if e.kind == "text" and e.size]
    if not sized:
        return 10.0
    pool = [s for s, n in sized for _ in range(max(1, min(n // 20, 50)))]
    return median(pool)


def heading_level(e: Element, body: float) -> int | None:
    """1-3 for a heading, None for running text."""
    if e.kind != "text" or not e.text.strip() or is_bullet(e):
        return None
    w = _words(e)
    if w > 16 or e.lines > 3:
        return None
    if e.size >= body * 1.9:
        return 1
    if e.size >= body * 1.3:
        return 2
    if (e.bold and e.size >= body * 0.98 and w <= 10) or (e.text.isupper() and w <= 6 and e.size >= body * 0.9):
        return 3
    return None


# ------------------------------------------------------------------ geometry


def _spans(els: list[Element], axis: int, gap: float, slack: float = 0.0) -> list[list[Element]]:
    """Groups of elements separated by at least `gap` of white space along an axis. With
    `slack`, a picture may also overlap its neighbour by that much and still be cut from
    it (a photo's edge reaching into the next row) — text never is."""
    lo, hi = (0, 2) if axis == 0 else (1, 3)
    order = sorted(els, key=lambda e: e.bbox[lo])
    groups: list[list[Element]] = []
    edge, edge_el = 0.0, None
    for e in order:
        join = bool(groups) and e.bbox[lo] < edge + gap
        if join and slack and (e.kind == "picture" or edge_el.kind == "picture") and e.bbox[lo] >= edge - slack:
            join = False
        if join:
            groups[-1].append(e)
            if e.bbox[hi] > edge:
                edge, edge_el = e.bbox[hi], e
        else:
            groups.append([e])
            edge, edge_el = e.bbox[hi], e
    return groups


def xy_cut(els: list[Element], depth: int = 0) -> dict:
    """{"rows": [...]} | {"cols": [...]} | {"leaf": [elements]} — rows first, the way a
    page is read; a leaf is one column of elements top to bottom."""
    if len(els) <= 1 or depth > 12:
        return {"leaf": sorted(els, key=lambda e: (e.bbox[1], e.bbox[0]))}
    # a clean cut first; then one that lets a picture's edge overlap a neighbour slightly
    for slack in (0.0, CUT_SLACK):
        rows = _spans(els, 1, ROW_GAP, slack)
        if len(rows) > 1:
            return {"rows": [xy_cut(r, depth + 1) for r in rows]}
        cols = _spans(els, 0, COL_GAP, slack)
        if len(cols) > 1:
            return {"cols": [xy_cut(c, depth + 1) for c in cols]}
    return {"leaf": sorted(els, key=lambda e: (e.bbox[1], e.bbox[0]))}


def _overlaps(a: tuple, b: tuple) -> bool:
    return min(a[2], b[2]) - max(a[0], b[0]) > 1 and min(a[3], b[3]) - max(a[1], b[1]) > 1


def _flat(node: dict) -> list[Element]:
    if "leaf" in node:
        return node["leaf"]
    return [e for k in node.get("rows") or node.get("cols") for e in _flat(k)]


# ------------------------------------------------------------------ sections


class _Builder:
    def __init__(self, els: list[Element]):
        self.body = _body_size(els)
        self.extra: list[Element] = []          # picture groups made here

    def group(self, pics: list[Element]) -> str:
        """Several pictures set together (a logo beside a product strip): one picture."""
        if len(pics) == 1:
            return pics[0].id
        box = (min(p.bbox[0] for p in pics), min(p.bbox[1] for p in pics),
               max(p.bbox[2] for p in pics), max(p.bbox[3] for p in pics))
        g = Element(f"g{len(self.extra) + 1}", "picture", box)
        self.extra.append(g)
        return g.id

    def runs(self, els: list[Element]) -> list[list[Element]]:
        """Lines of one paragraph come out of many PDFs as one block per line: blocks of the
        same size that follow on directly (no gap, overlapping across) are one run."""
        out: list[list[Element]] = []
        for e in els:
            prev = out[-1][-1] if out else None
            if (prev is not None and e.kind == "text" and prev.kind == "text" and not is_bullet(e)
                    and abs(e.size - prev.size) < 0.6 and -prev.size <= e.bbox[1] - prev.bbox[3] < max(4.0, prev.size * 0.9)
                    and min(e.bbox[2], prev.bbox[2]) - max(e.bbox[0], prev.bbox[0]) > 0):
                out[-1].append(e)
            else:
                out.append([e])
        return out

    def run_level(self, run: list[Element]) -> int | None:
        if len(run) > 2:
            return None
        if len(run) == 2 and run[0].size < self.body * 1.3:
            return None
        lv = [heading_level(e, self.body) for e in run]
        return lv[0] if lv[0] is not None and all(v == lv[0] for v in lv) else None

    # one column, top to bottom
    def flow(self, els: list[Element]) -> list[dict]:
        out: list[dict] = []
        rs = self.runs(els)
        i = 0
        while i < len(rs):
            run = rs[i]
            e = run[0]
            if e.kind == "picture":
                pics = [e]
                while i + 1 < len(rs) and rs[i + 1][0].kind == "picture" and rs[i + 1][0].bbox[1] < e.bbox[3]:
                    i += 1
                    pics.append(rs[i][0])
                cap = []
                nxt = rs[i + 1] if i + 1 < len(rs) else None
                bottom = max(p.bbox[3] for p in pics)
                if (nxt is not None and nxt[0].kind == "text" and self.run_level(nxt) is None
                        and sum(_words(x) for x in nxt) <= 40 and nxt[0].size <= self.body * 1.05
                        and nxt[0].bbox[1] - bottom <= CAPTION_GAP):
                    cap = [x.id for x in nxt]
                    i += 1
                out.append({"type": "picture", "picture": self.group(pics), "caption": cap})
            elif is_bullet(e):
                items = [[x.id for x in run]]
                while i + 1 < len(rs) and rs[i + 1][0].kind == "text" and is_bullet(rs[i + 1][0]):
                    i += 1
                    items.append([x.id for x in rs[i]])
                out.append({"type": "list", "items": items})
            else:
                lvl = self.run_level(run)
                if lvl:
                    out.append({"type": "heading", "level": lvl, "ids": [x.id for x in run]})
                else:
                    out.append({"type": "paragraph", "ids": [x.id for x in run]})
            i += 1
        return out

    def node(self, n: dict) -> list[dict]:
        if "leaf" in n:
            return self.flow(n["leaf"])
        if "rows" in n:
            out: list[dict] = []
            for k in n["rows"]:
                secs = self.node(k)
                # a row of labels over a row of untitled cards, one each: the cards' titles
                if (out and secs and out[-1]["type"] == "chips" and secs[0]["type"] == "cards"
                        and len(out[-1]["items"]) == len(secs[0]["cards"])
                        and not any(c["title"] for c in secs[0]["cards"])):
                    for c, it in zip(secs[0]["cards"], out.pop()["items"]):
                        c["title"] = it
                out.extend(secs)
            return out
        cols = [_flat(c) for c in n["cols"]]
        return self.columns(cols, n["cols"])

    def columns(self, cols: list[list[Element]], nodes: list[dict]) -> list[dict]:
        texts = [[e for e in c if e.kind == "text"] for c in cols]
        pics = [[e for e in c if e.kind == "picture"] for c in cols]
        # pictures only, side by side: one picture (they were designed as a set)
        if not any(texts):
            return [{"type": "picture", "picture": self.group([p for ps in pics for p in ps]), "caption": []}]
        # bullet points set in two or three columns: one list (after any title over them)
        flat = sorted((e for ts in texts for e in ts), key=lambda e: (e.bbox[1], e.bbox[0]))
        lead = []
        for e in flat:
            if is_bullet(e) or heading_level(e, self.body) is None:
                break
            lead.append(e)
        rest = [e for ts in texts for e in sorted(ts, key=lambda e: e.bbox[1]) if e not in lead]
        if not any(pics) and rest and all(is_bullet(e) for e in rest):
            out = self.flow(lead) if lead else []
            return out + [{"type": "list", "items": [[x.id for x in run] for run in self.runs(rest)]}]
        # a row of short labels ("Future Tech | Consumer Centric | Inclusive")
        if not any(pics) and all(len(t) == 1 and _words(t[0]) <= 6 for t in texts):
            return [{"type": "chips", "items": [[t[0].id] for t in texts]}]
        # each column a picture with a caption: a picture row
        if all(len(ps) == 1 and sum(_words(t) for t in ts) <= 40 for ps, ts in zip(pics, texts)):
            items = []
            for ps, ts in zip(pics, texts):
                items.append({"picture": ps[0].id, "caption": [t.id for t in sorted(ts, key=lambda e: e.bbox[1])]})
            return [{"type": "gallery", "items": items}]
        # a picture beside text: the two side by side on the web too
        if len(cols) == 2:
            only_pic = [bool(ps) and not ts for ps, ts in zip(pics, texts)]
            if any(only_pic):
                k = only_pic.index(True)
                other = nodes[1 - k]
                return [{"type": "media", "picture": self.group(pics[k]), "side": "left" if k == 0 else "right",
                         "body": self.node(other)}]
        # text set in columns with no titles: one text, column after column
        titled = [bool(ts) and heading_level(sorted(ts, key=lambda e: e.bbox[1])[0], self.body) is not None for ts in texts]
        avg_words = sum(sum(_words(t) for t in ts) for ts in texts) / len(cols)
        if not any(titled) and avg_words >= PROSE_COLUMN_WORDS:
            return [s for k in nodes for s in self.node(k)]
        # otherwise each column is a card: its title, its picture, its text
        cards = []
        for c in cols:
            c = sorted(c, key=lambda e: (e.bbox[1], e.bbox[0]))
            # the card's pictures as one, when they sit together (a grid of food photos)
            ps = [e for e in c if e.kind == "picture"]
            together = None
            if len(ps) > 1:
                box = (min(p.bbox[0] for p in ps), min(p.bbox[1] for p in ps), max(p.bbox[2] for p in ps), max(p.bbox[3] for p in ps))
                if not any(e.kind == "text" and _overlaps(e.bbox, box) for e in c):
                    together = self.group(ps)
                    c = [e for e in c if e.kind != "picture"] + [ps[0]]
            rs = self.runs(c)
            title: list[str] = []
            pic = None
            rest: list[Element] = []
            for k, run in enumerate(rs):
                if run[0].kind == "picture" and pic is None:
                    pic = together or run[0].id
                elif (k <= 1 and not title and not rest and (lv := self.run_level(run)) is not None
                      and (lv <= 2 or run[0].text.isupper())):
                    title = [x.id for x in run]
                else:
                    rest.extend(run)
            cards.append({"title": title, "body": [e.id for e in rest], "picture": pic, "flow": self.flow(rest)})
        return [{"type": "cards", "cards": cards}]


def auto_layout(elements: list[Element], band: tuple[float, float] | None = None) -> tuple[list[dict], list[Element]]:
    """(sections, elements to render with). Page numbers, stray characters and anything
    in the page's running header/footer band are left out; everything else is placed."""
    keep = []
    for e in elements:
        if e.kind == "text" and len(e.text.strip()) <= 3:
            continue
        if band and (e.bbox[3] <= band[0] + 0.5 or e.bbox[1] >= band[1] - 0.5):
            continue
        keep.append(e)
    b = _Builder(keep)
    sections = b.node(xy_cut(keep)) if keep else []
    return sections, elements + b.extra
