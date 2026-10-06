"""AI layouts for a report's design-led pages: cached per PDF, capped per version, metered.

For each design-led page: a cached layout made for exactly the same elements is reused (a
re-render or a new version of the same PDF costs nothing). Missing ones are asked of the
model, in page order, while the estimate stays within AI_LAYOUT_MAX_CREDITS for this render
and the workspace's monthly AI limit. Each call is recorded in the AI usage ledger as it
returns. The first rejected key stops all calls for an hour (no hammering a bad key).
"""

from __future__ import annotations

import json
import logging
import time
import uuid

import pymupdf

from app import ai_check, ai_usage, storage
from app.config import get_settings
from app.render import ai_layout, elements, site
from app.tenancy import Context

log = logging.getLogger(__name__)

_REJECTED_UNTIL = [0.0]          # monotonic time: a rejected key pauses calls in this process
REJECT_PAUSE_SECONDS = 3600


def cache_key(sha: str) -> str:
    return f"layouts/{sha}-v{ai_layout.LAYOUT_VERSION}.json"


def layouts_for(ctx: Context, version_id: uuid.UUID | None, sha: str, schema: dict, pdf: bytes) -> dict[int, dict]:
    """{page: {"fingerprint", "sections"}} for design-led pages that have a layout."""
    cfg = get_settings()
    pages = sorted(site.whole_pages(schema) - {1})            # page 1 is the hero's cover
    if not pages:
        return {}
    try:
        cache = json.loads(storage.get(ctx, cache_key(sha)))
    except Exception:  # noqa: BLE001 - nothing cached yet
        cache = {}
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    out: dict[int, dict] = {}
    todo: list[tuple[int, list, str, float, float]] = []
    try:
        for n in pages:
            if n > doc.page_count:
                continue
            page = doc[n - 1]
            els = elements.page_elements(page, schema, n, site.chart_boxes(schema, n))
            if not any(e.kind == "text" for e in els):
                continue
            fp = ai_layout.fingerprint(els)
            hit = cache.get(str(n))
            if hit and hit.get("fingerprint") == fp:
                out[n] = hit
            else:
                todo.append((n, els, fp, page.rect.width, page.rect.height))
    finally:
        doc.close()

    can_call = (cfg.ai_layout_max_credits > 0 and cfg.gemini_api_key and todo
                and time.monotonic() >= _REJECTED_UNTIL[0])
    if can_call:
        budget = cfg.ai_layout_max_credits
        try:
            allowance = ai_usage.allowance(ctx)
            if allowance["remaining"] is not None:
                budget = min(budget, allowance["remaining"])
        except Exception:  # noqa: BLE001
            pass
        transport = ai_check.transport()
        spent_exact = 0.0
        changed = False
        for n, els, fp, W, H in todo:
            tin, tout = ai_layout.estimate_tokens(els)
            est = ai_usage.usd_for("gemini", tin, tout) / cfg.ai_credit_usd
            if spent_exact + est > budget:
                log.info("AI layout: credit cap reached at page %s", n)
                break
            try:
                resp = transport(ai_layout.request_body(els, W, H))
            except ai_check.ProviderRejected:
                _REJECTED_UNTIL[0] = time.monotonic() + REJECT_PAUSE_SECONDS
                log.warning("AI layout: the provider rejected the key; pausing layout calls")
                break
            except Exception:  # noqa: BLE001 - a failed call leaves the page as designed
                log.exception("AI layout call failed for page %s", n)
                break
            usage = resp.get("usageMetadata") or {}
            used_in, used_out = int(usage.get("promptTokenCount") or 0), int(usage.get("candidatesTokenCount") or 0)
            ai_usage.record(ctx, feature="page_layout", provider="gemini", model=cfg.gemini_model,
                            input_tokens=used_in, output_tokens=used_out, version_id=version_id)
            spent_exact += ai_usage.usd_for("gemini", used_in, used_out) / cfg.ai_credit_usd
            layout = ai_layout.parse(resp)
            if layout is None:
                continue
            entry = {"fingerprint": fp, "sections": layout["sections"]}
            cache[str(n)] = entry
            out[n] = entry
            changed = True
        if changed:
            storage.put(ctx, cache_key(sha), json.dumps(cache).encode(), "application/json")
    return out
