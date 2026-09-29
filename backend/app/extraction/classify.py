"""Section classification: rules by default, optional LLM assist (PLAN D4).

HARD RULE: an LLM may only return section *types* for section *ids* it was given.
It never sees figures (digits are masked before sending) and anything it returns
other than {id: enum} is discarded. See `parse_llm_classification` and its tests.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

SECTION_TYPES = ("highlights", "profit_and_loss", "balance_sheet", "cash_flow", "segment_results",
                 "management_commentary", "outlook", "notes", "other")

_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("profit_and_loss", re.compile(r"profit\s*(and|&)\s*loss|income statement|statement of (standalone |consolidated )?(financial )?results|statement of operations|p\s*&\s*l\b", re.I)),
    ("balance_sheet", re.compile(r"balance sheet|assets and liabilities|financial position", re.I)),
    ("cash_flow", re.compile(r"cash\s*flow", re.I)),
    ("segment_results", re.compile(r"segment", re.I)),
    ("highlights", re.compile(r"highlight|at a glance|key (figures|metrics|numbers)|performance summary|snapshot", re.I)),
    ("outlook", re.compile(r"outlook|guidance|looking ahead|way forward", re.I)),
    ("management_commentary", re.compile(r"management (commentary|discussion)|md\s*&\s*a|chairman|ceo|managing director|message|commentary|review of operations", re.I)),
    ("notes", re.compile(r"^notes?\b|notes to|footnotes", re.I)),
]


def classify_heading(heading: str) -> str:
    for t, rx in _RULES:
        if rx.search(heading):
            return t
    return "other"


def mask_digits(s: str) -> str:
    return re.sub(r"\d", "#", s)


def build_llm_request(sections: list[dict]) -> str:
    """Only ids + digit-masked headings + a short digit-masked text sample are sent."""
    items = [{"id": s["id"], "heading": mask_digits(s.get("heading_text", ""))[:200],
              "sample": mask_digits(s.get("sample_text", ""))[:300]} for s in sections]
    return json.dumps({"allowed_types": SECTION_TYPES, "sections": items})


_DIGIT_OR_DATE = re.compile(r"\d")


def parse_llm_classification(response_text: str, known_ids: set[str]) -> dict[str, str]:
    """Accept ONLY {"<known id>": "<allowed type>"} pairs. Anything containing digits
    outside a known id, unknown ids, unknown types or extra fields is dropped."""
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict) and "classifications" in data:
        data = data["classifications"]
    out: dict[str, str] = {}
    if isinstance(data, dict):
        pairs = data.items()
    elif isinstance(data, list):
        pairs = [(d.get("id"), d.get("type")) for d in data if isinstance(d, dict) and set(d) <= {"id", "type"}]
    else:
        return {}
    for sid, typ in pairs:
        if not isinstance(sid, str) or not isinstance(typ, str):
            continue
        if sid not in known_ids or typ not in SECTION_TYPES:
            continue
        if _DIGIT_OR_DATE.search(typ):
            continue
        out[sid] = typ
    return out


LlmCall = Callable[[str], str]


def classify_sections(sections: list[dict], llm: LlmCall | None = None) -> dict[str, str]:
    """Rules first; if an LLM is enabled it may re-label sections the rules left as 'other'."""
    result = {s["id"]: classify_heading(s.get("heading_text", "")) for s in sections}
    if llm is not None:
        unresolved = [s for s in sections if result[s["id"]] == "other"]
        if unresolved:
            try:
                suggestions = parse_llm_classification(llm(build_llm_request(unresolved)),
                                                       {s["id"] for s in unresolved})
            except Exception:  # noqa: BLE001 - LLM is optional; never block the pipeline on it
                suggestions = {}
            result.update(suggestions)
    return result
