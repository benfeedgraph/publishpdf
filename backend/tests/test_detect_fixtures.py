"""The upload auto-fill on real investor PDFs, against their expected values.

The expected values are data (fixtures/detect_expected.json), never part of the detection
logic. The PDFs are testers' own files and aren't in the repository: set DETECT_PDF_DIR to
one or more folders holding them (separated by ':'); a PDF that isn't there is skipped.

  DETECT_PDF_DIR=~/Downloads uv run pytest tests/test_detect_fixtures.py
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from app.extraction.detect import detect_metadata

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "detect_expected.json").read_text())["files"]
DIRS = [Path(d).expanduser() for d in os.environ.get("DETECT_PDF_DIR", "").split(":") if d]
FIELDS = {"company": "company_name", "year": "fiscal_year", "period": "period", "type": "report_type",
          "currency": "currency", "unit": "reporting_unit"}


def _norm(name: str | None) -> str:
    s = (name or "").lower().replace("’", "'")
    s = re.sub(r"\bltd\b\.?", "limited", s)
    return re.sub(r"[^\w&']+", " ", s).strip()


def _find(name: str) -> Path | None:
    return next((d / name for d in DIRS if (d / name).exists()), None)


@pytest.mark.skipif(not DIRS, reason="set DETECT_PDF_DIR to the folder(s) holding the test PDFs")
@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_auto_fill_matches_the_expected_values(name):
    path = _find(name)
    if path is None:
        pytest.skip(f"{name} isn't in DETECT_PDF_DIR")
    meta = detect_metadata(path.read_bytes(), filename=name)
    want = FIXTURES[name]
    wrong = []
    for field, key in FIELDS.items():
        got = meta.get(key)
        if field == "company":
            # the full legal name, or the name as printed where the PDF never writes the legal one
            ok = _norm(got) in {_norm(x) for x in want["company"] + want.get("company_as_printed", [])}
        else:
            ok = want[field] == "*" or got in want[field]
        if not ok:
            wrong.append(f"{field}: got {got!r} ({meta['confidence'].get(key)}), expected {want[field]!r}")
    assert not wrong, f"{name}: " + "; ".join(wrong)
