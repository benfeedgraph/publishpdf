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
                    "report": _report_label(r.company_name, r.period, r.fiscal_year),
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


# ------------------------------------------------------------------ credit history


def _month_start(month: str | None):
    """'YYYY-MM' → that month's first instant (UTC); None → the current month."""
    from datetime import datetime, timezone
    if month:
        y, m = (int(x) for x in month.split("-"))
        return datetime(y, m, 1, tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def _next_month(d):
    return d.replace(year=d.year + (d.month == 12), month=1 if d.month == 12 else d.month + 1)


def _report_label(company, period, fy) -> str | None:
    if not company:
        return None
    return f"{company} · FY{fy}" if period == "fy" else f"{company} · {period.upper()} FY{fy}"


def history(ctx: Context, *, month: str | None = None, page: int = 1, page_size: int = 50) -> dict[str, Any]:
    """Credit history for one workspace: the last 12 months, and for one month every
    report's spend and every request (paged). Read straight from the ledger."""
    tid = ctx.require_tenant()
    start = _month_start(month)
    end = _next_month(start)
    page, page_size = max(1, page), min(200, max(1, page_size))
    with db.session(ctx) as s:
        months = s.execute(text("""
            SELECT to_char(date_trunc('month', created_at AT TIME ZONE 'UTC'), 'YYYY-MM') AS m,
                   count(*) AS requests, coalesce(sum(credits), 0) AS credits, coalesce(sum(usd), 0) AS usd
            FROM ai_usage WHERE tenant_id = :tid
              AND created_at >= (date_trunc('month', now() AT TIME ZONE 'UTC') - interval '11 months') AT TIME ZONE 'UTC'
            GROUP BY 1 ORDER BY 1 DESC"""), {"tid": tid}).all()
        reports = s.execute(text("""
            SELECT v.report_id, r.company_name, r.period, r.fiscal_year, u.feature,
                   count(*) AS requests, sum(u.credits) AS credits, sum(u.usd) AS usd,
                   array_agg(DISTINCT v.version_no) AS versions
            FROM ai_usage u
            LEFT JOIN report_versions v ON v.id = u.version_id
            LEFT JOIN reports r ON r.id = v.report_id
            WHERE u.tenant_id = :tid AND u.created_at >= :a AND u.created_at < :b
            GROUP BY 1, 2, 3, 4, 5"""), {"tid": tid, "a": start, "b": end}).all()
        total = s.scalar(text("SELECT count(*) FROM ai_usage WHERE tenant_id = :tid AND created_at >= :a AND created_at < :b"),
                         {"tid": tid, "a": start, "b": end}) or 0
        rows = s.execute(text("""
            SELECT u.created_at, u.feature, u.provider, u.model, u.input_tokens, u.output_tokens, u.usd, u.credits, u.ok,
                   v.version_no, v.report_id, r.company_name, r.period, r.fiscal_year
            FROM ai_usage u
            LEFT JOIN report_versions v ON v.id = u.version_id
            LEFT JOIN reports r ON r.id = v.report_id
            WHERE u.tenant_id = :tid AND u.created_at >= :a AND u.created_at < :b
            ORDER BY u.created_at DESC, u.id DESC LIMIT :n OFFSET :o"""),
            {"tid": tid, "a": start, "b": end, "n": page_size, "o": (page - 1) * page_size}).all()
    by_report: dict[str, dict[str, Any]] = {}
    for r in reports:
        key = str(r.report_id) if r.report_id else "-"
        e = by_report.setdefault(key, {"report_id": str(r.report_id) if r.report_id else None,
                                       "report": _report_label(r.company_name, r.period, r.fiscal_year),
                                       "requests": 0, "credits_exact": 0.0, "usd": 0.0, "features": {}, "versions": set()})
        e["requests"] += r.requests
        e["credits_exact"] += float(r.credits)
        e["usd"] += float(r.usd)
        e["features"][r.feature] = e["features"].get(r.feature, 0.0) + float(r.credits)
        e["versions"].update(x for x in (r.versions or []) if x is not None)
    report_list = sorted(by_report.values(), key=lambda e: -e["credits_exact"])
    for e in report_list:
        e["credits"] = shown(e.pop("credits_exact"))
        e["usd"] = round(e["usd"], 4)
        e["features"] = [{"feature": f, "label": FEATURES.get(f, f), "credits": shown(c)} for f, c in e["features"].items()]
        e["versions"] = sorted(e["versions"])
    return {
        "month": start.strftime("%Y-%m"),
        "credit_usd": get_settings().ai_credit_usd,
        "months": [{"month": m.m, "requests": m.requests, "credits": shown(float(m.credits)),
                    "usd": round(float(m.usd), 4)} for m in months],
        "reports": report_list,
        "total": total, "page": page, "page_size": page_size,
        "rows": [{"at": r.created_at.isoformat(), "feature": r.feature, "label": FEATURES.get(r.feature, r.feature),
                  "provider": r.provider, "model": r.model, "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
                  "usd": round(float(r.usd), 6), "credits": round(float(r.credits), 4), "ok": r.ok,
                  "report": _report_label(r.company_name, r.period, r.fiscal_year),
                  "report_id": str(r.report_id) if r.report_id else None, "version_no": r.version_no} for r in rows],
    }


def history_csv(ctx: Context, month: str | None = None) -> str:
    """Every request of one month, for a spreadsheet."""
    import csv
    import io
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["time_utc", "report", "version", "feature", "provider", "model", "input_tokens",
                "output_tokens", "usd", "credits", "succeeded"])
    page = 1
    while True:
        h = history(ctx, month=month, page=page, page_size=200)
        for r in h["rows"]:
            w.writerow([r["at"], r["report"] or "", r["version_no"] or "", r["label"], r["provider"], r["model"],
                        r["input_tokens"], r["output_tokens"], f'{r["usd"]:.6f}', f'{r["credits"]:.4f}', r["ok"]])
        if page * 200 >= h["total"]:
            return out.getvalue()
        page += 1
