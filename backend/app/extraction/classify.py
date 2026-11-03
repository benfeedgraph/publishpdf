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


LlmCall = Callable[..., str]      # llm(prompt, system=None, feature=None) -> response text


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


# ------------------------------------------------------------------ chapters

# Same hard rule as labelling: the model is shown ids, digit-masked headings and pages, and may
# only answer with ids it was given. Chapter titles are always the PDF's own heading text.
CHAPTER_SYSTEM = (
    "You split a long company report into the chapters a reader would see in a website's sidebar. "
    "You are given every section in reading order: its id, its heading and the PDF page it starts on. "
    "Pick the sections that START a chapter — the report's main parts (for example a letter to "
    "shareholders, a business overview, a management discussion, the financial statements, notes). "
    "Sub-headings, captions, running titles, quotes and repeated labels never start a chapter. Aim for "
    "between 4 and 15 chapters for the whole report, in reading order. Reply with JSON only, exactly "
    "{\"chapters\": [\"<id>\", ...]} using only the ids provided — no titles, no commentary."
)
CHAPTER_MIN_PAGES = 12        # shorter reports read fine as one page
CHAPTER_MAX_SECTIONS = 600    # keeps the request small on very long reports


def build_chapter_request(sections: list[dict]) -> str:
    items = [{"id": s["id"], "heading": mask_digits(s.get("heading_text", ""))[:120],
              "page": (s.get("source") or {}).get("page")}
             for s in sections if (s.get("heading_text") or "").strip()][:CHAPTER_MAX_SECTIONS]
    return json.dumps({"sections": items})


def parse_chapter_starts(response_text: str, known_ids: list[str]) -> list[str]:
    """Accept ONLY {"chapters": [<known id>, ...]}; kept in the report's reading order,
    duplicates and unknown ids dropped. Fewer than two starts is no split."""
    text = response_text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    ids = data.get("chapters") if isinstance(data, dict) else None
    if not isinstance(ids, list):
        return []
    picked = {i for i in ids if isinstance(i, str)}
    out = [i for i in known_ids if i in picked]
    return out if len(out) >= 2 else []


def pick_chapters(sections: list[dict], llm: LlmCall | None) -> list[str]:
    """Ids of the sections that start a chapter, chosen by the LLM; [] when it isn't enabled
    or gives nothing usable (the renderer then falls back to the report's own headings)."""
    if llm is None:
        return []
    headed = [s for s in sections if (s.get("heading_text") or "").strip()]
    if len(headed) < 3:
        return []
    try:
        reply = llm(build_chapter_request(headed), system=CHAPTER_SYSTEM, feature="chapters")
    except Exception:  # noqa: BLE001 - LLM is optional; never block the pipeline on it
        return []
    return parse_chapter_starts(reply, [s["id"] for s in headed])
