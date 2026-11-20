"""Issue #9 on real investor PDFs (opt-in: they aren't in the repo).

Set HEADTABLES_PDF_DIR to one or more folders (separated by ":") holding the PDFs named in
fixtures/page_titles_expected.json; the ones found are checked, the rest are skipped. Each is
extracted exactly as an upload is, and:

  - no heading on the web page stops mid-phrase ("Connect &", "Staples, Convenience Foods and"),
    or is only a number;
  - no chapter in the sidebar is such a heading;
  - every page title the PDF had on main is still a heading on its page.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from app.extraction import layout
from app.extraction.schema_builder import extract
from app.render import site, story

EXPECTED = json.loads((Path(__file__).parent / "fixtures" / "page_titles_expected.json").read_text())
DIRS = [Path(d) for d in os.environ.get("HEADTABLES_PDF_DIR", "").split(":") if d]
META = {"company_name": "Test Co", "report_type": "investor_presentation", "fiscal_year": 2026,
        "period": "q2", "currency": "INR", "reporting_unit": "₹ crore"}


def _find(name: str) -> Path | None:
    return next((d / name for d in DIRS if (d / name).exists()), None)


def _norm(t: str) -> str:
    return re.sub(r"\W+", " ", (t or "").lower()).strip()


def _bad_heading(t: str) -> bool:
    return bool(layout.DANGLING.search(t.strip())) or not any(sum(c.isalpha() for c in w) >= 2 for w in t.split())


@pytest.fixture(scope="module", params=sorted(EXPECTED["titles"]))
def report(request):
    path = _find(request.param)
    if path is None:
        pytest.skip(f"{request.param} not in HEADTABLES_PDF_DIR")
    pdf = path.read_bytes()
    schema, _ = extract(pdf, META)
    return request.param, pdf, schema


def test_no_heading_stops_mid_phrase_or_is_a_number(report):
    name, _, schema = report
    bad = [(s["source"]["page"], t) for s in schema["sections"]
           for t in (s.get("heading_text") or "", s.get("subheading_text") or "") if t and _bad_heading(t)]
    assert not bad, f"{name}: {bad[:10]}"


def test_the_sidebar_lists_no_label_or_half_line(report):
    name, pdf, schema = report
    pc = schema["metadata"]["source_pdf"]["page_count"]
    chapters = site.report_chapters(schema["sections"], pdf, story.index_links(schema, pc), set(), False, pc,
                                    ai_starts=schema.get("chapters")) or []
    bad = [c["title"] for c in chapters if _bad_heading(c["title"])]
    assert not bad, f"{name}: {bad}"


def test_every_page_title_is_still_a_heading(report):
    name, _, schema = report
    heads: dict[int, list[str]] = {}
    for s in schema["sections"]:
        # an empty title section folds into the next one, its heading becoming that one's
        # sub-heading: a section's headings belong to every page it covers
        # (as on main, a page's title can fold into an empty heading that ends the page before)
        pages = {s["source"]["page"]} | {(b.get("source") or {}).get("page") for b in s["blocks"]}
        for t, extra in ((s.get("heading_text"), set()), (s.get("subheading_text"), {s["source"]["page"] + 1})):
            if t:
                for p in pages | extra:
                    heads.setdefault(p, []).append(_norm(t))
    lost = [(p, t) for p, t in EXPECTED["titles"][name] if not any(_norm(t) in h for h in heads.get(p, []))]
    assert not lost, f"{name}: {lost[:10]}"

