"""Issue #10 on real investor PDFs (opt-in: they aren't in the repo).

Set HEADTABLES_PDF_DIR to one or more folders (separated by ":") holding the PDFs named in
fixtures/page_titles_expected.json; the ones found are checked, the rest are skipped. Each is
extracted exactly as an upload is, and every table the page will show passes the table checks:
no more than half its cells empty, and its values lined up rather than scattered down a
staircase. The issue's own example, the ITC deck's page 11, gives one table of its eight small
charts' values, each under its year.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.extraction import layout
from app.extraction.schema_builder import extract

EXPECTED = json.loads((Path(__file__).parent / "fixtures" / "page_titles_expected.json").read_text())
DIRS = [Path(d) for d in os.environ.get("HEADTABLES_PDF_DIR", "").split(":") if d]
META = {"company_name": "Test Co", "report_type": "investor_presentation", "fiscal_year": 2026,
        "period": "q2", "currency": "INR", "reporting_unit": "₹ crore"}


def _find(name: str) -> Path | None:
    return next((d / name for d in DIRS if (d / name).exists()), None)


@pytest.fixture(scope="module", params=sorted(EXPECTED["titles"]))
def report(request):
    path = _find(request.param)
    if path is None:
        pytest.skip(f"{request.param} not in HEADTABLES_PDF_DIR")
    schema, _ = extract(path.read_bytes(), META)
    return request.param, schema


def test_every_table_lines_up(report):
    name, schema = report
    bad = []
    for s in schema["sections"]:
        for b in s["blocks"]:
            if b["type"] != "table":
                continue
            ncols, rows = len(b["columns"]), b["rows"]
            if not ncols or not rows:
                continue
            filled = [sum(1 for c in r["cells"] if c["runs"]) for r in rows]
            empty = 1 - sum(filled) / (ncols * len(rows))
            used = [f for f in filled if f]
            scattered = ncols >= 3 and len(used) >= 3 and \
                sum(1 for f in used if f <= max(1, ncols // 3)) >= 0.6 * len(used) and empty >= 0.5
            if (ncols >= 2 and len(rows) >= 2 and empty > layout.TABLE_MAX_EMPTY) or scattered:
                bad.append((b["source"]["page"], f"{len(rows)}x{ncols}", round(empty, 2)))
    assert not bad, f"{name}: {bad}"


def test_the_itc_market_size_charts_give_one_table_of_values():
    path = _find("ITC_JPMorgan_presentation_30pages.pdf")
    if path is None:
        pytest.skip("ITC_JPMorgan_presentation_30pages.pdf not in HEADTABLES_PDF_DIR")
    schema, _ = extract(path.read_bytes(), META)
    figs = schema["figures"]

    def text(runs):
        return " ".join(r["t"].strip() if "t" in r else figs[r["f"]]["raw"] for r in runs).strip()
    page11 = [b for s in schema["sections"] for b in s["blocks"] if b["source"]["page"] == 11]
    assert not [b for b in page11 if b["type"] == "table"]
    (st,) = [b["series_table"] for b in page11 if b.get("series_table")]
    assert [text(c) for c in st["columns"]] == ["2020", "2025", "2030", "2035"]
    rows = {text(r["label"]): [text(c) for c in r["cells"]] for r in st["rows"]}
    assert rows["Snacks"] == ["32000", "56000", "100000", "143000"]
    assert rows["Branded Atta"] == ["12000", "19000", "31000", "50000"]
    assert rows["Floor Cleaner"] == ["1200", "2000", "4000", "6000"]
    assert len(rows) == 8
