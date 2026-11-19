"""Issue #11 on real PDFs (opt-in): every PDF in the folder(s) named by COVERAGE_PDF_DIR goes
through extraction, the page build and the page checks. The test fails for a PDF if any page with
content isn't on the web page, or if a page with a title of its own sits under another page's
heading without the page check saying so. (Pages the check reports are on the web page, under a
neighbour's heading; they are printed, not failed.) Slow — a few minutes per PDF — so it runs only
when asked:

    COVERAGE_PDF_DIR=/pdfs uv run pytest tests/test_page_coverage_pdfs.py -rA

The folder should hold only the PDFs meant for the test (every *.pdf in it is read).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app import validation
from app.extraction import schema_builder
from app.render import site
from app.render import theme as theming

DIRS = [Path(d) for d in os.environ.get("COVERAGE_PDF_DIR", "").split(":") if d]
PDFS = sorted({p for d in DIRS if d.is_dir() for p in d.glob("*.pdf")}, key=lambda p: p.name)
META = {"company_name": "Test Company", "report_type": "investor_presentation", "fiscal_year": 2026,
        "period": "q2", "currency": "INR", "reporting_unit": "₹ crore"}

pytestmark = pytest.mark.skipif(not PDFS, reason="set COVERAGE_PDF_DIR to a folder of test PDFs")


@pytest.mark.parametrize("pdf_path", PDFS, ids=[p.name for p in PDFS])
def test_every_page_with_content_is_on_the_web_page_and_no_merge_goes_unreported(pdf_path: Path):
    pdf = pdf_path.read_bytes()
    schema, artifact = schema_builder.extract(pdf, META, llm=None)
    theme, _ = theming.enforce(theming.merge(theming.DEFAULT_THEME, {}))
    page_map: list[dict] = []
    files = site.render_report(schema, theme=theme, disclaimer=None, pdf_bytes=pdf, layouts={}, page_map_sink=page_map)
    assert [e["page"] for e in page_map] == list(range(1, schema["metadata"]["source_pdf"]["page_count"] + 1))
    found = validation.check_text_on_site(schema, pdf, files) + validation.check_page_coverage(schema, artifact, page_map)
    for i in found:
        print(f"{pdf_path.name} page {i.page}: {i.message}")
    missing = [f"page {i.page}: {i.message}" for i in found if "isn't on the web page" in i.message]
    assert not missing, "pages with content that aren't on the web page:\n" + "\n".join(missing)
    titles = {p["page"]: p.get("title") for p in schema["pages"]}
    reported = {i.page for i in found}
    silent = [e["page"] for e in page_map if titles.get(e["page"]) and not e["own_section"]
              and e["shown"] not in ("left_out", "cover", "none") and e["page"] not in reported]
    assert not silent, f"pages under another page's heading that the page check doesn't report: {silent}"
