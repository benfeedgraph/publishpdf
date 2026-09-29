"""Tesseract OCR wrapper (runs locally — no document leaves the server; PLAN D2).

Two entry points:
- `ocr_page`  — full-page OCR for scanned pages, word boxes + confidence.
- `ocr_crop`  — re-read one figure's region for the independent re-extraction check.
"""

from __future__ import annotations

import csv
import io
import math
import shutil
import subprocess
from dataclasses import dataclass

import numpy as np
import pymupdf
from PIL import Image, ImageFilter, ImageOps

from app.jobs import UserFacingError

OCR_DPI = 300
CROP_DPI = 400


@dataclass(frozen=True)
class OcrWord:
    text: str
    bbox: tuple[float, float, float, float]   # deskewed page space (for layout), PDF points
    conf: float                               # 0..1
    src: tuple[float, float, float, float]    # original page space (for tracing to the PDF)


def tesseract_bin() -> str:
    path = shutil.which("tesseract")
    if not path:
        raise UserFacingError("Scanned pages can't be read right now (OCR engine unavailable). "
                              "Our team has been notified.", retryable=True)
    return path


def _run(png: bytes, *args: str, timeout: int = 180) -> str:
    proc = subprocess.run([tesseract_bin(), "stdin", "stdout", *args], input=png,
                          capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"tesseract failed: {proc.stderr.decode(errors='replace')[:500]}")
    return proc.stdout.decode("utf-8", errors="replace")


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render(page: pymupdf.Page, dpi: int, clip: pymupdf.Rect | None = None) -> Image.Image:
    pix = page.get_pixmap(dpi=dpi, clip=clip, colorspace=pymupdf.csGRAY)
    return Image.frombytes("L", (pix.width, pix.height), pix.samples)


def _binary(img: Image.Image) -> np.ndarray:
    a = np.asarray(img, dtype=np.uint8)
    return a < 128


def estimate_skew(img: Image.Image) -> float:
    """Skew angle in degrees (projection-profile method), searched in +/-2 degrees."""
    small = img.copy()
    small.thumbnail((1000, 1000))
    best, best_score = 0.0, -1.0
    for tenth in range(-20, 21):
        ang = tenth / 10
        rot = small.rotate(ang, fillcolor=255, resample=Image.NEAREST)
        prof = _binary(rot).sum(axis=1).astype(np.float64)
        score = float(np.var(np.diff(prof)))
        if score > best_score:
            best, best_score = ang, score
    return best


def preprocess(img: Image.Image) -> tuple[Image.Image, float]:
    """Despeckle, deskew, binarise. Returns (image, angle applied in degrees)."""
    img = img.filter(ImageFilter.MedianFilter(3))
    img = ImageOps.autocontrast(img)
    angle = estimate_skew(img)
    if abs(angle) >= 0.1:
        img = img.rotate(angle, fillcolor=255, resample=Image.BICUBIC)
    img = img.point(lambda v: 255 if v > 160 else 0)
    return img, angle


# Characters OCR invents from specks at word edges; never part of a printed figure.
_EDGE_NOISE = "\"'`“”‘’~_|:;"
_NOISE_ONLY = set(".,:;_~`'\"‘’“”|^*")


def _clean(text: str) -> str:
    return text.strip(_EDGE_NOISE)


@dataclass(frozen=True)
class Prepared:
    png: bytes
    angle: float
    width: int
    height: int


def prepare_page(page: pymupdf.Page) -> Prepared:
    """Render + clean up a page for OCR. Uses PyMuPDF, so call it from one thread."""
    img, angle = preprocess(render(page, OCR_DPI))
    return Prepared(_png(img), angle, img.width, img.height)


def run_prepared(prep: Prepared) -> list[OcrWord]:
    """Run Tesseract on a prepared page. Thread-safe (subprocess), so pages OCR in parallel."""
    tsv = _run(prep.png, "--psm", "3", "-c", "preserve_interword_spaces=1", "tsv")
    scale = 72 / OCR_DPI
    cx, cy = prep.width / 2, prep.height / 2
    # PIL rotated the image counter-clockwise by `angle`; map boxes back to the original page.
    t = math.radians(prep.angle)
    cos, sin = math.cos(t), math.sin(t)

    def unrotate(x: float, y: float) -> tuple[float, float]:
        dx, dy = x - cx, y - cy
        return cx + dx * cos - dy * sin, cy + dx * sin + dy * cos

    words: list[OcrWord] = []
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        text = (row.get("text") or "").strip()
        if not text or row.get("level") != "5":
            continue
        conf = max(0.0, float(row["conf"])) / 100
        text = _clean(text)
        if not text or set(text) <= _NOISE_ONLY or (len(text) == 1 and not text.isalnum() and conf < 0.5):
            continue
        x, y, w, h = (int(row[k]) for k in ("left", "top", "width", "height"))
        pts = [unrotate(x, y), unrotate(x + w, y), unrotate(x, y + h), unrotate(x + w, y + h)]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        straight = (x * scale, y * scale, (x + w) * scale, (y + h) * scale)
        src = (min(xs) * scale, min(ys) * scale, max(xs) * scale, max(ys) * scale)
        words.append(OcrWord(text, straight, conf, src))
    return words


def ocr_page(page: pymupdf.Page) -> list[OcrWord]:
    return run_prepared(prepare_page(page))


def read_batch(images: list[Image.Image]) -> list[str]:
    """Read many figure crops with ONE Tesseract call: crops are stacked vertically with
    wide white gaps and each output line is mapped back to its crop by position.
    Tesseract's start-up dominates per-crop cost, so this is several times faster on
    reports with thousands of figures. Crops it can't place come back as ''."""
    if not images:
        return []
    gap = 60
    prepped = [ImageOps.expand(ImageOps.autocontrast(im), border=12, fill=255) for im in images]
    width = max(im.width for im in prepped) + 40
    height = sum(im.height for im in prepped) + gap * (len(prepped) + 1)
    sheet = Image.new("L", (width, height), 255)
    spans = []
    y = gap
    for im in prepped:
        sheet.paste(im, (20, y))
        spans.append((y, y + im.height))
        y += im.height + gap
    tsv = _run(_png(sheet), "--psm", "6", "-c", f"tessedit_char_whitelist={CROP_WHITELIST}", "tsv", timeout=300)
    out: list[list[tuple[int, str]]] = [[] for _ in images]
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        t = (row.get("text") or "").strip()
        if not t or row.get("level") != "5":
            continue
        yc = int(row["top"]) + int(row["height"]) / 2
        for i, (a, b) in enumerate(spans):
            if a - 8 <= yc <= b + 8:
                out[i].append((int(row["left"]), t))
                break
    return [" ".join(t for _, t in sorted(parts)) for parts in out]


CROP_WHITELIST = "0123456789.,()%-–—₹$€£xXbpsRs "


def ocr_image(img: Image.Image, *, psm: int = 7, upscale: int = 1) -> tuple[str, float]:
    """Read one figure crop. psm 7 = one line, 8 = one word, 10 = one character.
    Short, isolated numbers (page numbers, single digits) read far better upscaled
    in single-word mode. Returns (text, min word confidence)."""
    img = ImageOps.autocontrast(img)
    if upscale > 1:
        img = img.resize((img.width * upscale, img.height * upscale), Image.LANCZOS)
    img = ImageOps.expand(img, border=40 if upscale > 1 else 20, fill=255)   # tesseract reads badly at the edge
    tsv = _run(_png(img), "--psm", str(psm), "-c", f"tessedit_char_whitelist={CROP_WHITELIST}", "tsv", timeout=30)
    parts, confs = [], []
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        t = (row.get("text") or "").strip()
        if t and row.get("level") == "5":
            parts.append(t)
            confs.append(max(0.0, float(row["conf"])) / 100)
    return " ".join(parts), (min(confs) if confs else 0.0)


def ocr_crop(page: pymupdf.Page, bbox: tuple[float, float, float, float], pad: float = 2.0) -> tuple[str, float]:
    """Read a single figure from the rendered page image."""
    x0, y0, x1, y1 = bbox
    clip = pymupdf.Rect(x0 - pad, y0 - pad, x1 + pad, y1 + pad) & page.rect
    return ocr_image(render(page, CROP_DPI, clip))
