"""Visuals carried over from the PDF: the cover page and every chart / infographic region,
cropped from the source as PNG images.

An image is a picture of the PDF, not a re-typed copy of it: no figure is read, rounded
or restated here, so the zero-discrepancy rule for numbers is untouched. Where a chart's
data labels were extracted, the page still shows them as figures next to the picture.
"""

from __future__ import annotations

import pymupdf

CROP_DPI = 150
COVER_DPI = 110
MIN_SIDE = 40          # points: skip slivers (rules, tiny icons)
PAD = 4                # points around a crop


def _area(b: list[float]) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _overlap(a: list[float], b: list[float]) -> float:
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    return _area([x0, y0, x1, y1])


def visual_blocks(schema: dict) -> list[tuple[dict, dict]]:
    """(section, chart block) pairs worth showing as an image. A region that sits mostly
    inside a larger one on the same page is dropped — the larger crop already shows it."""
    out = [(s, b) for s in schema["sections"] for b in s["blocks"]
           if b["type"] == "chart" and b.get("source", {}).get("bbox")]
    keep = []
    for s, b in out:
        bb, pg = b["source"]["bbox"], b["source"]["page"]
        if bb[2] - bb[0] < MIN_SIDE or bb[3] - bb[1] < MIN_SIDE:
            continue
        inside = any(o is not b and o["source"]["page"] == pg and _area(o["source"]["bbox"]) > _area(bb)
                     and _overlap(bb, o["source"]["bbox"]) > 0.8 * _area(bb) for _, o in out)
        if not inside:
            keep.append((s, b))
    return keep


def render_media(schema: dict, pdf_bytes: bytes | None) -> tuple[dict[str, bytes], dict]:
    """Returns (files keyed media/<name>.png, info) where info = {"cover": {...} | None,
    "blocks": {block_id: {"src", "w", "h", "page"}}}. Empty when no PDF is at hand
    (e.g. the theme editor's live preview)."""
    info: dict = {"cover": None, "blocks": {}}
    if not pdf_bytes:
        return {}, info
    files: dict[str, bytes] = {}
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        if doc.page_count:
            pix = doc[0].get_pixmap(dpi=COVER_DPI)
            files["media/cover.png"] = pix.tobytes("png")
            info["cover"] = {"src": "media/cover.png", "w": pix.width, "h": pix.height}
        for _s, b in visual_blocks(schema):
            pg = b["source"]["page"]
            if not 1 <= pg <= doc.page_count:
                continue
            page = doc[pg - 1]
            x0, y0, x1, y1 = b["source"]["bbox"]
            clip = pymupdf.Rect(x0 - PAD, y0 - PAD, x1 + PAD, y1 + PAD) & page.rect
            if clip.is_empty:
                continue
            pix = page.get_pixmap(dpi=CROP_DPI, clip=clip)
            name = f"media/{b['id']}.png"
            files[name] = pix.tobytes("png")
            info["blocks"][b["id"]] = {"src": name, "w": pix.width, "h": pix.height, "page": pg}
    finally:
        doc.close()
    return files, info
