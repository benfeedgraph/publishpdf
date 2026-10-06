"""AI usage ledger: what each workspace spent on model calls, and its optional monthly limit.

Every paid model request records one row as soon as it returns — the provider's own token
counts, priced at the configured rates — in its own transaction, so a run that fails
halfway still shows what it spent. Credits are kept exact per row and rounded UP only when
shown or compared with a limit, so nothing is ever under-counted.

A limit (tenants.ai_monthly_credit_limit, set by a platform admin; NULL = none) applies per
calendar month, UTC. User-started AI (double-check) is refused up front when its estimate
would cross the limit; automatic AI (section labelling) is simply skipped once it is reached.
"""

from __future__ import annotations

import logging
import math
import uuid
from typing import Any

from sqlalchemy import text

from app import db
from app.config import get_settings
from app.tenancy import Context

log = logging.getLogger(__name__)

FEATURES = {"ai_check": "AI double-check of flagged figures", "section_labels": "AI section labelling",
            "page_layout": "AI layout of design pages"}


class LimitReached(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def usd_for(provider: str, input_tokens: int, output_tokens: int) -> float:
    cfg = get_settings()
    if provider == "gemini":
        pin, pout = cfg.gemini_price_in_per_m, cfg.gemini_price_out_per_m
    elif provider == "anthropic":
        pin, pout = cfg.anthropic_price_in_per_m, cfg.anthropic_price_out_per_m
    else:
        raise ValueError(provider)
    return input_tokens * pin / 1e6 + output_tokens * pout / 1e6


def _credits_exact(usd: float) -> float:
    return usd / get_settings().ai_credit_usd if usd > 0 else 0.0


def shown(credits: float) -> int:
    """Credits as displayed and as compared with a limit: always rounded up."""
    return math.ceil(credits - 1e-9) if credits > 0 else 0


def record(ctx: Context, *, feature: str, provider: str, model: str, input_tokens: int,
           output_tokens: int, version_id: uuid.UUID | None = None, ok: bool = True) -> dict[str, Any]:
    """Write one ledger row now, in its own transaction. Never raises: a failed write is
    logged loudly, but must not turn a model call that already happened into a failed job."""
    usd = usd_for(provider, input_tokens, output_tokens)
    row = {"tenant_id": ctx.require_tenant(), "version_id": version_id, "feature": feature,
           "provider": provider, "model": model, "input_tokens": int(input_tokens),
           "output_tokens": int(output_tokens), "usd": round(usd, 6),
           "credits": round(_credits_exact(usd), 4), "ok": ok}
    try:
        with db.session(ctx) as s:
            s.execute(text("""INSERT INTO ai_usage (tenant_id, version_id, feature, provider, model,
                                  input_tokens, output_tokens, usd, credits, ok)
                              VALUES (:tenant_id, :version_id, :feature, :provider, :model,
                                  :input_tokens, :output_tokens, :usd, :credits, :ok)"""), row)
    except Exception:  # noqa: BLE001
        log.exception("AI usage NOT recorded: %s", row)
    return row


_ALLOWANCE = text("""
    SELECT t.ai_monthly_credit_limit AS lim,
           (SELECT coalesce(sum(credits), 0) FROM ai_usage u
             WHERE u.tenant_id = t.id AND u.created_at >= date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') AS used
    FROM tenants t WHERE t.id = :tid
""")


def allowance(ctx: Context) -> dict[str, Any]:
    """This month's use against the limit. `remaining` is None when there is no limit."""
    with db.session(ctx) as s:
        row = s.execute(_ALLOWANCE, {"tid": ctx.require_tenant()}).one()
    used = shown(float(row.used))
    remaining = None if row.lim is None else max(0, row.lim - used)
    return {"limit": row.lim, "used": used, "remaining": remaining}


def check(ctx: Context, credits_needed: int) -> None:
    a = allowance(ctx)
    if a["limit"] is not None and a["used"] + credits_needed > a["limit"]:
        raise LimitReached(
            f"This needs {credits_needed} credits, but your workspace has {a['remaining']} of its "
            f"{a['limit']} monthly AI credits left. Ask your PublishPDF contact to raise the limit.")


def can_run_automatic(ctx: Context) -> bool:
    """For automatic AI (no estimate step): allowed until the monthly limit is used up."""
    a = allowance(ctx)
    return a["limit"] is None or a["used"] < a["limit"]


_TOTALS = text("""
    SELECT feature, count(*) AS requests, coalesce(sum(input_tokens), 0) AS tin,
           coalesce(sum(output_tokens), 0) AS tout, coalesce(sum(usd), 0) AS usd,
           coalesce(sum(credits), 0) AS credits
    FROM ai_usage
    WHERE tenant_id = :tid AND created_at >= :since
    GROUP BY feature
""")


def summary(ctx: Context, *, recent: int = 20) -> dict[str, Any]:
    """This month (UTC) by feature, plus the latest requests. Counts and money only."""
    tid = ctx.require_tenant()
    with db.session(ctx) as s:
        since = s.scalar(text("SELECT date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"))
        rows = s.execute(_TOTALS, {"tid": tid, "since": since}).all()
        last = s.execute(text("""
            SELECT u.created_at, u.feature, u.model, u.input_tokens, u.output_tokens, u.usd, u.credits, u.ok,
                   u.version_id, r.company_name, r.period, r.fiscal_year, v.report_id
            FROM ai_usage u
            LEFT JOIN report_versions v ON v.id = u.version_id
            LEFT JOIN reports r ON r.id = v.report_id
            WHERE u.tenant_id = :tid ORDER BY u.created_at DESC, u.id DESC LIMIT :n"""),
            {"tid": tid, "n": recent}).all()
        lim = s.scalar(text("SELECT ai_monthly_credit_limit FROM tenants WHERE id = :tid"), {"tid": tid})
    by_feature = [{"feature": r.feature, "label": FEATURES.get(r.feature, r.feature), "requests": r.requests,
                   "input_tokens": int(r.tin), "output_tokens": int(r.tout), "usd": round(float(r.usd), 4),
                   "credits": shown(float(r.credits))} for r in rows]
    used_exact = sum(float(r.credits) for r in rows)
    used = shown(used_exact)
    return {
        "month_start": since.isoformat(),
        "credit_usd": get_settings().ai_credit_usd,
        "month": {"credits": used, "usd": round(sum(float(r.usd) for r in rows), 4),
                  "requests": sum(r.requests for r in rows),
                  "input_tokens": sum(int(r.tin) for r in rows), "output_tokens": sum(int(r.tout) for r in rows)},
        "by_feature": by_feature,
        "limit": lim, "remaining": None if lim is None else max(0, lim - used),
        "recent": [{"at": r.created_at.isoformat(), "feature": r.feature, "label": FEATURES.get(r.feature, r.feature),
                    "model": r.model, "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
                    "usd": round(float(r.usd), 4), "credits": round(float(r.credits), 4), "ok": r.ok,
                    "report": (f"{r.company_name} · {r.period.upper()} FY{r.fiscal_year}" if r.company_name else None),
                    "report_id": str(r.report_id) if r.report_id else None} for r in last],
    }


def month_credits_by_tenant(admin_ctx: Context) -> dict[str, int]:
    """Platform admin: this month's credits per workspace (for the tenants list)."""
    with db.session(admin_ctx) as s:
        rows = s.execute(text("""
            SELECT tenant_id, sum(credits) AS c FROM ai_usage
            WHERE created_at >= date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
            GROUP BY tenant_id""")).all()
    return {str(r.tenant_id): shown(float(r.c)) for r in rows}
