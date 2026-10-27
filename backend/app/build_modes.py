"""How a report's web page is built — chosen per report at upload, with its credits shown first.

  none    Build without AI. The built-in layout and all nine checks; never any AI credits.
  layout  AI just for layout. The AI arranges the design-heavy pages (pictures, cards, stat
          tiles) into web sections. It only sees ids, positions and short text, never
          writes words or numbers. Nothing else uses AI.
  full    Use AI to build the page. AI layout, plus AI section labels, plus the automatic
          AI double-check of figures our checks flag (read from a crop; it can only confirm).

Every estimate is an upper bound worked out from the page count alone, so the server can
check that the number a person saw is the number it will hold the build to. Real use is
usually much lower: only design-heavy pages are laid out by the AI, a cached layout is
reused for free, and the double-check only runs when figures are flagged.
"""

from __future__ import annotations

from typing import Any

from app.ai_usage import shown, usd_for
from app.config import get_settings
from app.render import ai_layout

# The two ways a page can be built. Both use AI; "layout" is the cheaper of the two.
# ("none" — build with no AI at all — was retired: every report now gets at least AI layout,
#  so the page reads as a real web page rather than a reflowed PDF. Legacy versions stored as
#  "none" or NULL still render through `caps` below.)
MODES = ("layout", "full")
DEFAULT_MODE = "full"

# The extractive "overview" landing page has been retired: a long report is now split into one
# web page per section with a sidebar navigator (app.render.site.report_chapters), which serves the
# same purpose. The opt-in is no longer offered and the page is no longer built. The flag/column and
# the option shape are kept so existing versions and API clients don't break.
LANDING_MIN_PAGES = 20


def landing_available(page_count: int | None) -> bool:
    return False


def landing_option(page_count: int | None) -> dict[str, Any]:
    """What the upload screen needs to show (or hide) the landing-page opt-in."""
    return {"available": landing_available(page_count), "min_pages": LANDING_MIN_PAGES}

# Upper bound used for estimates: elements on a busy page (text blocks + pictures). Set high
# enough that a design-heavy page (a product spread with many labels) still fits the budget,
# so the AI layout reflows the whole report instead of stopping part-way through.
ELEMENTS_PER_PAGE = 100
# Section labelling: one request for the sections rules can't name (≈2 per page, at most 400).
LABEL_PROMPT_TOKENS, LABEL_TOKENS_PER_SECTION, LABEL_OUTPUT_TOKENS = 300, 150, 1024


def _credits(provider: str, tin: int, tout: int) -> int:
    return shown(usd_for(provider, tin, tout) / get_settings().ai_credit_usd)


def layout_credits(page_count: int) -> int:
    """At most what AI layout can spend on this PDF (page 1 stays the cover)."""
    pages = max(0, page_count - 1)
    if not pages:
        return 0
    tin = pages * (ai_layout.PROMPT_TOKENS + ai_layout.TOKENS_PER_ELEMENT * ELEMENTS_PER_PAGE)
    tout = pages * (40 + ai_layout.OUTPUT_TOKENS_PER_ELEMENT * ELEMENTS_PER_PAGE)
    return _credits("gemini", tin, tout)


def label_credits(page_count: int) -> int:
    if not get_settings().anthropic_api_key:
        return 0
    sections = min(400, 2 * max(1, page_count))
    return _credits("anthropic", LABEL_PROMPT_TOKENS + LABEL_TOKENS_PER_SECTION * sections, LABEL_OUTPUT_TOKENS)


def check_credits() -> int:
    return max(0, get_settings().ai_auto_check_max_credits)


def caps(mode: str | None, page_count: int) -> dict[str, int]:
    """What each AI feature may spend for one build of a PDF this long."""
    if mode == "none":
        return {"layout": 0, "labels": 0, "check": 0}
    if mode == "layout":
        return {"layout": layout_credits(page_count), "labels": 0, "check": 0}
    if mode == "full":
        return {"layout": layout_credits(page_count), "labels": label_credits(page_count), "check": check_credits()}
    # NULL: a version made before build modes — the old platform-wide defaults.
    cfg = get_settings()
    return {"layout": max(0, cfg.ai_layout_max_credits), "labels": label_credits(page_count), "check": check_credits()}


def max_credits(mode: str, page_count: int) -> int:
    return sum(caps(mode, page_count).values())


def options(page_count: int) -> list[dict[str, Any]]:
    """The three choices as the upload screen shows them."""
    cfg = get_settings()
    gemini = bool(cfg.gemini_api_key)
    labels = bool(cfg.anthropic_api_key)
    out = []
    for mode in MODES:
        c = caps(mode, page_count)
        out.append({
            "mode": mode,
            "max_credits": sum(c.values()),
            "max_usd": round(sum(c.values()) * cfg.ai_credit_usd, 2),
            "parts": c,
            "available": gemini,
            "unavailable_reason": None if gemini else "AI isn't set up on this platform yet.",
            "labels_available": labels,
        })
    return out


def validate(mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"Unknown build option {mode!r}.")
    return mode
