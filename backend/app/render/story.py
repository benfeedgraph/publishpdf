"""Presentation hints for the web edition of a PDF: question headings, bullets, links,
where visuals sit and how wide they are, and the PDF's own index as navigation.

Nothing here creates, rounds or recomputes a number that is shown. Hints come from block
positions and text structure; figures are only ever referenced by id.
"""

from __future__ import annotations

import re
from decimal import Decimal

def _fig_of(schema: dict, runs: list[dict]) -> dict | None:
    for r in runs:
        if "f" in r:
            f = schema["figures"][r["f"]]
            if f.get("status") == "active":
                return f
    return None


# ------------------------------------------------------------------ FAQ-style reports

_QMARK_TEXT = re.compile(r"^\s*(Q\s?\d{1,3}|\([a-z]\))")
BULLETS = "•▪◦●‣–-·"


def question_parts(schema: dict, sec: dict) -> tuple[list[dict], list[dict]] | None:
    """A numbered-question heading split into (marker runs, question runs), e.g.
    [Q1] + [What is …?]. Nothing is re-typed: the marker is the heading's own first run
    (a figure when it carries a digit), and only the separating punctuation is trimmed."""
    runs = sec.get("heading") or []
    if not runs:
        return None
    first = runs[0]
    first_text = schema["figures"][first["f"]]["raw"] if "f" in first else first["t"]
    m = _QMARK_TEXT.match(first_text)
    if not m:
        return None
    if "f" in first:
        marker, rest = [first], [dict(r) for r in runs[1:]]
    else:
        marker, tail = [{"t": m.group(1)}], first["t"][m.end():]
        rest = ([{"t": tail}] if tail else []) + [dict(r) for r in runs[1:]]
    if rest and "t" in rest[0]:
        rest[0]["t"] = rest[0]["t"].lstrip(" .:)–-")
    rest = [r for r in rest if "f" in r or r["t"]]
    return (marker, rest) if rest else None


def is_faq(schema: dict) -> bool:
    return sum(1 for s in schema["sections"] if question_parts(schema, s)) >= 3


def bullet_runs(b: dict) -> list[dict] | None:
    """A paragraph that starts with a list glyph, returned without the glyph (so the
    page can draw its own marker); None for ordinary paragraphs."""
    runs = b.get("runs") or []
    if not runs or "t" not in runs[0]:
        return None
    t = runs[0]["t"].lstrip()
    if not t or t[0] not in BULLETS or (t[0] in "–-" and len(t) > 1 and not t[1].isspace()):
        return None
    head = t[1:].lstrip()
    return ([{"t": head}] if head else []) + runs[1:]


# ------------------------------------------------------------------ document layout
# The "document" layout keeps the PDF's own order and arrangement. These helpers derive
# presentation hints from block positions only — never from, or into, figure values.


def document_layout(schema: dict, visuals: dict) -> dict[str, dict]:
    """Per visual block: its width as a share of the page's text column (so a small chart
    stays small, as in the PDF) and whether body text runs beside it (so it floats on the
    same side, the way the PDF wraps text around it)."""
    paras: dict[int, list[list[float]]] = {}
    for s in schema["sections"]:
        for b in s["blocks"]:
            if b["type"] == "paragraph" and b.get("source", {}).get("bbox"):
                paras.setdefault(b["source"]["page"], []).append(b["source"]["bbox"])
    # The document's usual text column, for pages that have no body text of their own.
    spans = sorted((min(p[0] for p in ps), max(p[2] for p in ps)) for ps in paras.values())
    usual = spans[len(spans) // 2] if spans else None
    out: dict[str, dict] = {}
    for s in schema["sections"]:
        for b in s["blocks"]:
            if b["id"] not in visuals:
                continue
            pg, (x0, y0, x1, y1) = b["source"]["page"], b["source"]["bbox"]
            col = paras.get(pg) or []
            ref = [(p[0], p[2]) for p in col] or ([usual] if usual else [])
            cx0 = min([r[0] for r in ref] + [x0])
            cx1 = max([r[1] for r in ref] + [x1])
            width = max(1.0, cx1 - cx0)
            share = min(100, max(30, round(100 * (x1 - x0) / width)))
            side = "right" if (x0 + x1) / 2 > (cx0 + cx1) / 2 else "left"
            # Text beside the visual: overlaps it vertically and starts clear of it on the
            # other side (it may run full width again below the visual, as wrapped text does).
            beside = [p for p in col if p[1] < y1 - 6 and p[3] > y0 + 6 and
                      ((side == "right" and p[0] <= x0 - 60) or (side == "left" and p[2] >= x1 + 60))
                      and not (p[0] >= x0 - 8 and p[2] <= x1 + 8)]
            centred = abs((x0 + x1) / 2 - (cx0 + cx1) / 2) < 0.1 * width
            out[b["id"]] = {"w": share, "float": side if beside and share <= 62 and not centred else None}
    return out


def document_order(schema: dict, layout: dict[str, dict]) -> dict[str, list[dict]]:
    """Blocks per section in PDF reading order, except that a floated visual moves just
    before the first paragraph that wraps around it (a float must precede its text)."""
    out = {}
    for s in schema["sections"]:
        blocks = list(s["blocks"])
        for b in [x for x in blocks if layout.get(x["id"], {}).get("float")]:
            pg, (x0, y0, x1, y1) = b["source"]["page"], b["source"]["bbox"]
            i = blocks.index(b)
            for j, p in enumerate(blocks[:i]):
                bb = p.get("source", {}).get("bbox")
                if p["type"] == "paragraph" and p["source"]["page"] == pg and bb and bb[1] < y1 - 6 and bb[3] > y0 + 6:
                    blocks.insert(j, blocks.pop(i))
                    break
        out[s["id"]] = _join_across_pages(blocks)
    return out


def _run_text(r: dict) -> str:
    return r.get("t", "")


def _join_across_pages(blocks: list[dict]) -> list[dict]:
    """A sentence the PDF carries over a page break ("…grew 28%" | next page "YoY, while…")
    is one paragraph again: the last paragraph on a page that doesn't end a sentence is
    joined with the first paragraph of the next page. Runs are concatenated, not re-typed."""
    out: list[dict] = []
    for b in blocks:
        prev = out[-1] if out else None
        if prev and prev["type"] == b["type"] == "paragraph" and bullet_runs(b) is None \
                and b.get("source", {}).get("page") == (prev.get("source", {}).get("page") or 0) + 1 + prev.get("_extra_pages", 0):
            tail = _run_text(prev["runs"][-1]).rstrip() if prev["runs"] and "t" in prev["runs"][-1] else "x"
            if tail and tail[-1] not in ".?!:;\"”’)":
                out[-1] = {**prev, "runs": prev["runs"] + [{"t": " "}] + b["runs"],
                           "_extra_pages": prev.get("_extra_pages", 0) + 1}
                continue
        out.append(b)
    return out


def heading_like(schema: dict) -> set[str]:
    """Short title lines the PDF sets above a table or chart ("Index") — shown as headings."""
    ids = set()
    for s in schema["sections"]:
        bl = s["blocks"]
        for i, b in enumerate(bl[:-1]):
            if b["type"] != "paragraph" or bl[i + 1]["type"] not in ("table", "chart"):
                continue
            text = "".join(r.get("t", "") for r in b["runs"]).strip()
            if text and len(text.split()) <= 6 and all("t" in r for r in b["runs"]) and text[-1] not in ".,;:!?":
                ids.add(b["id"])
    return ids


_LEAD = re.compile(r"^(\s*)([A-Z][\w’'&\- ]{1,30}:)(\s+)")


def lead_label(runs: list[dict]) -> tuple[str, list[dict]] | None:
    """'Vision: Our brands…' -> ('Vision:', [rest]) so the label can be set in bold, as in the PDF."""
    if not runs or "t" not in runs[0]:
        return None
    m = _LEAD.match(runs[0]["t"])
    words = m.group(2).rstrip(":").split() if m else []
    # A label reads like a title ("Vision:", "Fresh Food Business:"), not a clause ("The Company said:").
    if not m or len(words) > 3 or not all(w[0].isupper() or w in ("&", "of", "and") for w in words):
        return None
    rest = runs[0]["t"][m.end():]
    return m.group(2), ([{"t": rest}] if rest else []) + runs[1:]


_WRAP_HYPHEN = re.compile(r"(?<=[a-z])- (?=[a-z])")
_URL_START = re.compile(r"^(https?://|www\.)", re.I)
_URL_CONT = re.compile(r"^[A-Za-z0-9\-._~/%#?=&+:]+$")


def linkify(runs: list[dict], figures: dict) -> list[dict]:
    """Web addresses printed in the PDF become links. A link that the PDF wrapped onto the
    next line (…/quarterly-results- ⏎ 2026-2027/…) is re-joined, dropping only the wrap
    space. Figure runs stay whole; their text is untouched."""
    # A compound word the PDF broke at its hyphen ("pay- outs") is re-joined; the hyphen
    # stays, only the line-wrap space goes.
    runs = [{"t": _WRAP_HYPHEN.sub("-", r["t"])} if "t" in r else r for r in runs]
    toks: list[dict] = []
    for r in runs:
        if "f" in r:
            toks.append({"f": r["f"], "s": figures[r["f"]]["raw"]})
        else:
            toks += [{"t": p, "s": p} for p in re.split(r"(\s+)", r["t"]) if p]
    if not any(_URL_START.match(t["s"]) for t in toks):
        return runs
    out: list[dict] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if not _URL_START.match(t["s"]):
            out.append({"f": t["f"]} if "f" in t else {"t": t["s"]})
            i += 1
            continue
        group, href, j = [t], t["s"], i + 1
        while j < len(toks):
            nxt = toks[j]
            if nxt["s"].isspace():
                after = toks[j + 1] if j + 1 < len(toks) else None
                if after and href[-1] in "-/_" and _URL_CONT.match(after["s"]) and re.search(r"[/.\-]", after["s"]):
                    group.append(after)
                    href += after["s"]
                    j += 2
                    continue
                break
            if not _URL_CONT.match(nxt["s"].rstrip(".,;)")):
                break
            group.append(nxt)
            href += nxt["s"]
            j += 1
        trail = re.search(r"[.,;)]+$", href)
        link_runs = [{"f": g["f"]} if "f" in g else {"t": g["s"]} for g in group]
        tail = None
        if trail and "t" in link_runs[-1] and link_runs[-1]["t"].endswith(trail.group()):
            link_runs[-1]["t"] = link_runs[-1]["t"][: -len(trail.group())]
            href, tail = href[: -len(trail.group())], trail.group()
        if href.lower().startswith("www."):
            href = "https://" + href
        out.append({"a": href, "runs": [r for r in link_runs if "f" in r or r["t"]]})
        if tail:
            out.append({"t": tail})
        i = j
    # Merge adjacent text runs back together.
    merged: list[dict] = []
    for r in out:
        if "t" in r and merged and "t" in merged[-1]:
            merged[-1] = {"t": merged[-1]["t"] + r["t"]}
        else:
            merged.append(r)
    return merged


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", s.lower()) if w not in {"the", "and", "what", "please", "provide", "company", "how", "has", "been"}}


def index_links(schema: dict, page_count: int | None) -> dict[str, dict[int, str]]:
    """The PDF's own index/contents table: each entry links to the section it names
    (matched on the entry's words among sections that start on the page it cites)."""
    figs = schema["figures"]
    out: dict[str, dict[int, str]] = {}
    for s in schema["sections"]:
        for b in s["blocks"]:
            if b["type"] != "table" or len(b["columns"]) != 1 or len(b["rows"]) < 4:
                continue
            pages = []
            for row in b["rows"]:
                f = _fig_of(schema, row["cells"][0]["runs"]) if row["cells"] else None
                v = f.get("value") if f else None
                pages.append(int(Decimal(v)) if v is not None and Decimal(v) == Decimal(v).to_integral_value() else None)
            if any(p is None or p < 1 or (page_count and p > page_count) for p in pages) or pages != sorted(pages):
                continue
            links = {}
            for i, (row, pg) in enumerate(zip(b["rows"], pages)):
                want = _words(row.get("label_text") or "".join(figs[r["f"]]["raw"] if "f" in r else r["t"] for r in row["label"]))
                cands = [x for x in schema["sections"] if x.get("source", {}).get("page") in (pg, pg - 1) and x.get("heading")] \
                    or [x for x in schema["sections"] if x.get("heading")]
                best = max(cands, key=lambda x: len(want & _words(x.get("heading_text", ""))), default=None)
                if best and len(want & _words(best.get("heading_text", ""))) >= 1:
                    links[i] = best["slug"]
            if links:
                out[b["id"]] = links
    return out


def plain_excerpt(sec: dict, max_chars: int = 155) -> str:
    """Digit-free summary for meta descriptions: text runs up to the first figure."""
    for b in sec["blocks"]:
        if b["type"] == "paragraph":
            text = ""
            for r in b["runs"]:
                if "f" in r:
                    break
                text += r["t"]
            text = _WRAP_HYPHEN.sub("-", re.sub(r"\s+", " ", text)).strip(" ,;:")
            if len(text) >= 40 and not any(ch.isdigit() for ch in text):
                return text if len(text) <= max_chars else text[:max_chars].rsplit(" ", 1)[0] + "…"
    return ""
