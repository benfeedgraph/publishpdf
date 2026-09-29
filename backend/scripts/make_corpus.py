"""Generate the SYNTHETIC test corpus with ground truth.

    uv run python scripts/make_corpus.py            # writes ../corpus/synthetic/

These are NOT real company filings. Company names and figures are invented. They
exist so extraction accuracy can be measured against exact ground truth until real
sample PDFs are supplied (drop those in corpus/real/ with a *.truth.json alongside).

Every digit-bearing word drawn is recorded with page, bbox (PDF points, top-left
origin) and — for table cells — row and column labels.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parents[2] / "corpus" / "synthetic"
FONT_DIR = Path("/System/Library/Fonts/Supplemental")
pdfmetrics.registerFont(TTFont("Body", str(FONT_DIR / "Georgia.ttf")))
pdfmetrics.registerFont(TTFont("Bold", str(FONT_DIR / "Georgia Bold.ttf")))


@dataclass
class Doc:
    path: Path
    pagesize: tuple[float, float] = A4
    truth: list[dict] = field(default_factory=list)
    page: int = 1

    def __post_init__(self) -> None:
        self.c = canvas.Canvas(str(self.path), pagesize=self.pagesize)
        self.w, self.h = self.pagesize

    # All y coordinates here are top-left origin (like PDF readers / our extractor).
    def text(self, x: float, y: float, s: str, size: float = 9, font: str = "Body",
             align: str = "left", record: bool = True, **meta) -> float:
        width = pdfmetrics.stringWidth(s, font, size)
        if align == "right":
            x -= width
        elif align == "center":
            x -= width / 2
        self.c.setFont(font, size)
        self.c.drawString(x, self.h - y, s)
        if record:
            cursor = x
            for word in s.split(" "):
                ww = pdfmetrics.stringWidth(word, font, size)
                if any(ch.isdigit() for ch in word):
                    self.truth.append({"page": self.page, "raw": word,
                                       "bbox": [round(cursor, 1), round(y - size * 0.9, 1),
                                                round(cursor + ww, 1), round(y + size * 0.25, 1)],
                                       **meta})
                cursor += ww + pdfmetrics.stringWidth(" ", font, size)
        return width

    def para(self, x: float, y: float, width: float, s: str, size: float = 9.5, leading: float = 13) -> float:
        words, line = s.split(" "), ""
        for word in words:
            trial = f"{line} {word}".strip()
            if pdfmetrics.stringWidth(trial, "Body", size) > width and line:
                self.text(x, y, line, size)
                y += leading
                line = word
            else:
                line = trial
        if line:
            self.text(x, y, line, size)
            y += leading
        return y

    def header_footer(self, header: str, total_pages: int) -> None:
        self.c.setFillGray(0.4)
        self.text(40, 28, header, 8, record=False)
        self.text(self.w - 40, self.h - 20, f"Page {self.page} of {total_pages}", 8, align="right", record=False)
        self.c.setFillGray(0)
        self.c.setLineWidth(0.3)
        self.c.line(40, self.h - 34, self.w - 40, self.h - 34)

    def table(self, x: float, y: float, label_w: float, col_w: float, header_rows: list[list[tuple[str, int]]],
              rows: list[tuple[str, list[str]]], col_labels: list[str], size: float = 8.5,
              row_h: float = 14, bold_rows: set[str] = frozenset()) -> float:
        n = len(col_labels)
        self.c.setLineWidth(0.6)
        self.c.line(x, self.h - (y - 10), x + label_w + n * col_w, self.h - (y - 10))
        for hr in header_rows:
            cx = x + label_w
            for text, span in hr:
                if text and span > 1:      # merged header: centred across its columns
                    self.text(cx + span * col_w / 2, y, text, size, "Bold", align="center", record=True,
                              role="header")
                elif text:
                    self.text(cx + span * col_w - 4, y, text, size, "Bold", align="right", record=True,
                              role="header")
                cx += span * col_w
            y += row_h
        self.c.line(x, self.h - (y - 10), x + label_w + n * col_w, self.h - (y - 10))
        for label, cells in rows:
            font = "Bold" if label in bold_rows else "Body"
            self.text(x, y, label, size, font, role="row_label")
            for i, v in enumerate(cells):
                self.text(x + label_w + (i + 1) * col_w - 4, y, v, size, font, align="right",
                          role="cell", row_label=label, col_label=col_labels[i])
            y += row_h
        self.c.line(x, self.h - (y - 10), x + label_w + n * col_w, self.h - (y - 10))
        return y

    def next_page(self) -> None:
        self.c.showPage()
        self.page += 1

    def save(self) -> None:
        self.c.save()
        self.path.with_suffix(".truth.json").write_text(json.dumps(
            {"file": self.path.name, "synthetic": True, "figures": self.truth}, indent=1, ensure_ascii=False))


def ind(v: float, dp: int = 2) -> str:
    """Format in Indian grouping with brackets for negatives."""
    neg = v < 0
    s = f"{abs(v):.{dp}f}"
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join([head] + parts + [tail])
    out = f"{whole}.{frac}" if dp else whole
    return f"({out})" if neg else out


def intl(v: float, dp: int = 1) -> str:
    s = f"{abs(v):,.{dp}f}"
    return f"({s})" if v < 0 else s


def pct(a: float, b: float) -> str:
    return f"{(a - b) / abs(b) * 100:.1f}%"


# ------------------------------------------------------------------------------ samples


def results_pdf(path: Path) -> None:
    """Text-layer quarterly results: highlights (2 columns), P&L, balance sheet, segments."""
    d = Doc(path)
    hdr = "Acme Industries Limited (synthetic sample) | Q2 FY26 Results"
    total = 4

    d.header_footer(hdr, total)
    d.text(40, 70, "Q2 FY26 Results", 20, "Bold")
    d.text(40, 92, "Quarter ended 30.09.2025", 10)
    d.text(40, 125, "Key highlights", 13, "Bold")
    col_w = (d.w - 80 - 20) / 2
    left = ("Revenue from operations grew 12.4% year on year to ₹1,234.56 crore in Q2 FY26, "
            "driven by volume growth of 9.8% in the domestic business. EBITDA rose to ₹245.10 crore "
            "with a margin of 19.9%, up 110 bps compared with the same quarter last year.")
    right = ("Net profit for the quarter was ₹142.35 crore against ₹118.20 crore in Q2 FY25. "
             "The Board declared an interim dividend of ₹3.50 per share on 24.10.2025. "
             "Net debt reduced to ₹410.00 crore as at 30.09.2025 from ₹502.75 crore on 31.03.2025.")
    d.para(40, 150, col_w, left)
    d.para(40 + col_w + 20, 150, col_w, right)
    d.text(40, 260, "Outlook", 13, "Bold")
    d.para(40, 285, d.w - 80, "Management expects demand to remain steady in H2 FY26 and maintains "
           "its capital expenditure plan of ₹600 crore for FY26.")
    d.next_page()

    # --- P&L -----------------------------------------------------------------------
    d.header_footer(hdr, total)
    d.text(40, 70, "Statement of Profit and Loss", 14, "Bold")
    d.text(40, 88, "(₹ in crore, except per share data)", 9)
    cur = [1234.56, 48.20]
    q1 = [1180.40, 41.75]
    ly = [1098.30, 39.10]
    h1 = [cur[0] + q1[0], cur[1] + q1[1]]
    h1ly = [2150.25, 76.80]
    exp_cur, exp_q1, exp_ly = [612.40, 145.80, 22.15, 48.90, 218.26], [590.10, 142.30, 23.40, 47.70, 214.85], [560.20, 131.50, 25.60, 45.30, 199.70]
    exp_h1 = [a + b for a, b in zip(exp_cur, exp_q1)]
    exp_h1ly = [1101.40, 260.10, 50.20, 89.90, 390.35]

    def col(rev, exp, exceptional, tax):
        ti = sum(rev)
        te = sum(exp)
        pbt = ti - te + exceptional
        return rev + [ti] + exp + [te, exceptional, pbt, tax, pbt - tax]

    cols = [col(cur, exp_cur, -12.30, 55.00), col(q1, exp_q1, 0.0, 51.20), col(ly, exp_ly, 0.0, 45.60),
            col(h1, exp_h1, -12.30, 106.20), col(h1ly, exp_h1ly, 0.0, 88.10)]
    labels = ["Revenue from operations", "Other income", "Total income", "Cost of materials consumed",
              "Employee benefits expense", "Finance costs", "Depreciation and amortisation",
              "Other expenses", "Total expenses", "Exceptional items", "Profit before tax",
              "Tax expense", "Profit for the period"]
    rows = []
    for i, lab in enumerate(labels):
        vals = [c[i] for c in cols]
        cells = ["-" if v == 0 else ind(v) for v in vals]
        yoy = "-" if vals[2] == 0 or vals[0] == 0 else pct(vals[0], vals[2])
        rows.append((lab, cells[:3] + [yoy] + cells[3:]))
    eps = [cols[i][-1] / 40.67 for i in range(5)]
    rows.append(("Basic EPS (₹)", [f"{eps[0]:.2f}", f"{eps[1]:.2f}", f"{eps[2]:.2f}", pct(eps[0], eps[2]),
                                   f"{eps[3]:.2f}", f"{eps[4]:.2f}"]))
    col_labels = ["Q2 FY26", "Q1 FY26", "Q2 FY25", "YoY", "H1 FY26", "H1 FY25"]
    d.table(40, 118, 170, 62, [[(c, 1) for c in col_labels]], rows, col_labels,
            bold_rows={"Total income", "Total expenses", "Profit before tax", "Profit for the period"})
    d.next_page()

    # --- Balance sheet ----------------------------------------------------------------
    d.header_footer(hdr, total)
    d.text(40, 70, "Statement of Assets and Liabilities", 14, "Bold")
    d.text(40, 88, "(₹ in crore)", 9)
    assets = [("Property, plant and equipment", 1850.40, 1790.10), ("Capital work-in-progress", 210.35, 185.00),
              ("Investments", 420.00, 402.50), ("Inventories", 612.75, 580.20),
              ("Trade receivables", 498.60, 455.35), ("Cash and cash equivalents", 188.90, 160.45)]
    ta = (sum(a[1] for a in assets), sum(a[2] for a in assets))
    eq = [("Equity share capital", 40.67, 40.67), ("Other equity", 2588.63, 2433.13),
          ("Borrowings", 598.90, 663.20), ("Trade payables", 402.80, 380.60)]
    other = (ta[0] - sum(e[1] for e in eq), ta[1] - sum(e[2] for e in eq))
    eq.append(("Other liabilities", *other))
    te = (sum(e[1] for e in eq), sum(e[2] for e in eq))
    rows = [(a, [ind(v1), ind(v2)]) for a, v1, v2 in assets] + [("Total assets", [ind(ta[0]), ind(ta[1])])]
    rows += [(e, [ind(v1), ind(v2)]) for e, v1, v2 in eq] + [("Total equity and liabilities", [ind(te[0]), ind(te[1])])]
    d.table(40, 110, 200, 80, [[("As at", 2)], [("30.09.2025", 1), ("31.03.2025", 1)]], rows,
            ["As at 30.09.2025", "As at 31.03.2025"],
            bold_rows={"Total assets", "Total equity and liabilities"})
    d.next_page()

    # --- Segments -----------------------------------------------------------------------
    d.header_footer(hdr, total)
    d.text(40, 70, "Segment Results", 14, "Bold")
    d.text(40, 88, "(₹ in crore)", 9)
    seg = [("Chemicals", 812.30, 790.15, 720.40), ("Materials", 356.16, 331.10, 318.20),
           ("Services", 66.10, 59.15, 59.70)]
    tot = [sum(s[i] for s in seg) for i in (1, 2, 3)]
    rows = [(s, [ind(a), ind(b), ind(c)]) for s, a, b, c in seg] + [("Total segment revenue", [ind(x) for x in tot])]
    d.table(40, 118, 170, 80, [[("Q2 FY26", 1), ("Q1 FY26", 1), ("Q2 FY25", 1)]], rows, ["Q2 FY26", "Q1 FY26", "Q2 FY25"],
            bold_rows={"Total segment revenue"})
    d.save()


def annual_pdf(path: Path) -> None:
    """International format, USD million, merged header spanning two year columns."""
    d = Doc(path)
    total = 1
    d.header_footer("Borealis Holdings Inc. (synthetic sample) | Annual Report", total)
    d.text(40, 70, "Consolidated Income Statement", 14, "Bold")
    d.text(40, 88, "(USD million)", 9)
    rev, cogs = (45210.4, 41876.9), (27120.8, 25310.2)
    gp = (rev[0] - cogs[0], rev[1] - cogs[1])
    opex = (9820.5, 9410.3)
    oi = (gp[0] - opex[0], gp[1] - opex[1])
    rows = [("Net revenue", [intl(rev[0]), intl(rev[1])]), ("Cost of revenue", [intl(-cogs[0]), intl(-cogs[1])]),
            ("Gross profit", [intl(gp[0]), intl(gp[1])]), ("Operating expenses", [intl(-opex[0]), intl(-opex[1])]),
            ("Operating income", [intl(oi[0]), intl(oi[1])]), ("Operating margin", [f"{oi[0]/rev[0]*100:.1f}%", f"{oi[1]/rev[1]*100:.1f}%"])]
    d.table(40, 132, 200, 90, [[("Year ended March 31,", 2)], [("2025", 1), ("2024", 1)]], rows,
            ["Year ended March 31, 2025", "Year ended March 31, 2024"])
    d.save()


def deck_pdf(path: Path) -> None:
    """Landscape investor presentation: KPI tiles, vector bar chart with labels, image-only chart."""
    d = Doc(path, landscape(A4))
    total = 2
    d.header_footer("Acme Industries | Investor Presentation (synthetic sample)", total)
    d.text(40, 80, "Q2 FY26 performance at a glance", 18, "Bold")
    for i, (label, val) in enumerate([("Revenue", "₹1,234.56 Cr"), ("EBITDA margin", "19.9%"), ("Net profit", "₹142.35 Cr")]):
        x = 40 + i * 250
        d.c.setStrokeGray(0.7)
        d.c.rect(x, d.h - 200, 220, 80)
        d.text(x + 12, 150, label, 11)
        d.text(x + 12, 180, val, 18, "Bold")
    # vector bar chart with data labels
    d.text(40, 250, "Quarterly revenue (₹ crore)", 12, "Bold")
    vals = [("Q2 FY25", 1098.30), ("Q3 FY25", 1120.85), ("Q4 FY25", 1162.40), ("Q1 FY26", 1180.40), ("Q2 FY26", 1234.56)]
    base = 500
    for i, (q, v) in enumerate(vals):
        x = 60 + i * 90
        hbar = v / 1300 * 200
        d.c.setFillColorRGB(0.2, 0.35, 0.7)
        d.c.rect(x, d.h - base, 50, hbar, fill=1, stroke=0)
        d.c.setFillGray(0)
        d.text(x + 25, base - hbar - 6, ind(v), 8, align="center", role="chart_label", chart="quarterly_revenue")
        d.text(x + 25, base + 14, q, 8, align="center", role="chart_axis", chart="quarterly_revenue")
    d.next_page()
    d.header_footer("Acme Industries | Investor Presentation (synthetic sample)", total)
    d.text(40, 80, "Revenue mix", 18, "Bold")
    # raster chart: numbers are pixels only -> must be marked image_only_not_extracted
    img = pymupdf.open()
    pg = img.new_page(width=300, height=200)
    pg.draw_rect(pymupdf.Rect(20, 60, 90, 180), color=(0.2, 0.4, 0.7), fill=(0.2, 0.4, 0.7))
    pg.draw_rect(pymupdf.Rect(120, 110, 190, 180), color=(0.9, 0.5, 0.2), fill=(0.9, 0.5, 0.2))
    pg.insert_text((30, 50), "66%", fontsize=16)
    pg.insert_text((130, 100), "29%", fontsize=16)
    png = OUT / "_chart.png"
    pg.get_pixmap(dpi=150).save(str(png))
    d.c.drawImage(str(png), 60, d.h - 420, width=300, height=200)
    png.unlink()
    d.text(420, 260, "Chemicals remains the largest segment.", 12, record=False)
    d.save()


def scanned_from(src: Path, dst: Path, dpi: int = 200, seed: int = 7) -> None:
    """Image-only copy of `src` (no text layer), with mild noise and skew like a scan."""
    from PIL import Image, ImageFilter

    rnd = random.Random(seed)
    doc, out = pymupdf.open(src), pymupdf.open()
    for page in doc:
        pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
        im = Image.frombytes("L", (pix.width, pix.height), pix.samples)
        im = im.rotate(rnd.uniform(-0.25, 0.25), fillcolor=255, resample=Image.BICUBIC)
        im = im.filter(ImageFilter.GaussianBlur(0.4))
        px = im.load()
        for _ in range(im.width * im.height // 400):
            px[rnd.randrange(im.width), rnd.randrange(im.height)] = rnd.choice((0, 255))
        np_ = out.new_page(width=page.rect.width, height=page.rect.height)
        import io
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        np_.insert_image(np_.rect, stream=buf.getvalue())
    out.save(dst)
    truth = json.loads(src.with_suffix(".truth.json").read_text())
    truth["file"] = dst.name
    truth["scanned"] = True
    dst.with_suffix(".truth.json").write_text(json.dumps(truth, indent=1, ensure_ascii=False))


def broken_text_layer_from(src: Path, dst: Path, page_no: int, true_raw: str, fake_raw: str) -> None:
    """Seeded error: the page LOOKS like `src` (image) but its invisible text layer says
    `fake_raw` where the printed figure is `true_raw`. Only an independent visual
    re-extraction can catch this."""
    doc, out = pymupdf.open(src), pymupdf.open()
    truth = json.loads(src.with_suffix(".truth.json").read_text())
    replaced = False
    for page in doc:
        np_ = out.new_page(width=page.rect.width, height=page.rect.height)
        np_.insert_image(np_.rect, pixmap=page.get_pixmap(dpi=200))
        for (x0, y0, x1, y1, word, *_rest) in page.get_text("words"):
            text = word
            if page.number + 1 == page_no and word == true_raw and not replaced:
                text, replaced = fake_raw, True
            fs = (y1 - y0) * 0.8
            np_.insert_text((x0, y1 - (y1 - y0) * 0.22), text, fontsize=fs, fontname="helv", render_mode=3)
    assert replaced, "seed target not found"
    out.save(dst)
    truth["file"] = dst.name
    truth["seeded_error"] = {"page": page_no, "printed": true_raw, "text_layer": fake_raw}
    dst.with_suffix(".truth.json").write_text(json.dumps(truth, indent=1, ensure_ascii=False))


def edge_cases_pdf(path: Path) -> None:
    """Real-world layouts that tripped the visual re-read on an actual report:
    an index table of short page numbers with dotted rules, a microscopic hidden text
    layer behind a chart image, 'Rs.' prefixes, 'x' multiples and a wrapped date."""
    d = Doc(path)
    d.header_footer("Edge cases (synthetic sample)", 2)
    d.text(260, 80, "Index", 12, "Bold", record=False)
    x0, x1, col = 60, 540, 470
    items = [("Vision & Mission", "3"), ("Core Values", "3"), ("Strategy", "4"), ("Overview of Q1 FY27 results", "4"),
             ("Impact of taxes", "5"), ("Track record", "6"), ("Distribution channel", "7"), ("Brands", "9"),
             ("New categories", "10"), ("Margins", "11"), ("Segments", "12"), ("Fresh food", "13"),
             ("Capex plan", "14"), ("Dividend policy", "15"), ("Triple Bottom Line", "16"), ("Digital journey", "17")]
    y = 110
    d.c.setDash(1, 2)
    for label, pg in items:
        d.text(x0 + 6, y, label, 11)
        d.text((col + x1) / 2, y, pg, 11, align="center", role="cell", row_label=label, col_label="Page No.")
        d.c.line(x0, d.h - (y + 8), x1, d.h - (y + 8))
        y += 22
    d.c.line(col, d.h - 96, col, d.h - (y - 14))
    d.c.setDash()
    d.next_page()
    d.header_footer("Edge cases (synthetic sample)", 2)
    d.text(40, 70, "Dividend and valuation", 14, "Bold")
    d.para(40, 95, 500, "The interim dividend was Rs.8.0 per share and the final dividend Rs.14.35 per share. "
           "Net cash is 2.1x of debt. The Minimum Import Price on paperboard was extended till 30th September 2026 by notification.")
    # chart as an image with a microscopic (0.66pt) hidden text layer of its axis labels
    img = pymupdf.open()
    pg_ = img.new_page(width=300, height=160)
    for i, hgt in enumerate((60, 90, 120)):
        pg_.draw_rect(pymupdf.Rect(30 + i * 90, 150 - hgt, 90 + i * 90, 150), color=(0.2, 0.4, 0.7), fill=(0.2, 0.4, 0.7))
    png = OUT / "_edge_chart.png"
    pg_.get_pixmap(dpi=120).save(str(png))
    d.c.drawImage(str(png), 60, d.h - 360, width=300, height=160)
    png.unlink()
    d.c.setFont("Body", 0.66)
    for i, lab in enumerate(("15,000", "35,000", "55,000")):
        d.c.drawString(62, d.h - (220 + i * 12), lab)
    d.save()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    results = OUT / "acme_q2fy26_results.pdf"
    results_pdf(results)
    annual_pdf(OUT / "borealis_annual_usd.pdf")
    deck_pdf(OUT / "acme_q2fy26_presentation.pdf")
    scanned_from(results, OUT / "acme_q2fy26_results_scanned.pdf")
    broken_text_layer_from(results, OUT / "acme_q2fy26_broken_text_layer.pdf", 2, "48.20", "46.20")
    edge_cases_pdf(OUT / "edge_cases_index_microtext.pdf")
    for p in sorted(OUT.glob("*.pdf")):
        print(p.name)


if __name__ == "__main__":
    main()
