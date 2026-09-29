"""Validate generated report documents against schema/report.schema.json."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import jsonschema

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schema" / "report.schema.json"


@lru_cache
def _validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def schema_errors(doc: dict, limit: int = 20) -> list[str]:
    errs = []
    for e in sorted(_validator().iter_errors(doc), key=lambda e: list(e.path))[:limit]:
        errs.append(f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message[:200]}")
    return errs
