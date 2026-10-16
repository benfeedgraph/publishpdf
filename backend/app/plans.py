"""Workspace plans: the one place their names, prices and monthly AI credit allowances live.

A plan only sets the monthly credit allowance for now — there is no card payment. A
platform admin assigns it (which copies the allowance into tenants.ai_monthly_credit_limit,
still the value every AI feature is checked against); a workspace can ask for an upgrade.
Building pages without AI and all nine checks are free on every plan.
"""

from __future__ import annotations

from typing import Any

DEFAULT_PLAN = "free"          # new workspaces start here

PLANS: list[dict[str, Any]] = [
    {"key": "free", "name": "Free", "price_usd_month": 0, "credits": 50,
     "blurb": "Try AI layout on a few reports a month.",
     "features": ["Unlimited reports built without AI", "All nine figure checks", "50 AI credits a month"]},
    {"key": "starter", "name": "Starter", "price_usd_month": 29, "credits": 500,
     "blurb": "A company publishing its quarterly results.",
     "features": ["Everything in Free", "500 AI credits a month", "AI layout on every quarterly report"]},
    {"key": "pro", "name": "Pro", "price_usd_month": 99, "credits": 2000,
     "blurb": "Annual reports and investor decks, with AI on throughout.",
     "features": ["Everything in Starter", "2,000 AI credits a month", "Full AI on 100+ page annual reports"]},
    {"key": "enterprise", "name": "Enterprise", "price_usd_month": None, "credits": None,
     "blurb": "Many companies or brands, with an allowance set for you.",
     "features": ["Everything in Pro", "Custom monthly allowance", "A named PublishPDF contact"]},
]
BY_KEY = {p["key"]: p for p in PLANS}


def get(key: str | None) -> dict[str, Any] | None:
    return BY_KEY.get(key or "")


def allowance_for(key: str, custom_credits: int | None = None) -> int | None:
    """The monthly limit a plan sets. Enterprise takes the admin's number (None = no limit)."""
    plan = BY_KEY[key]
    return custom_credits if plan["credits"] is None else plan["credits"]


# ------------------------------------------------------------------ a workspace's plan


def state(ctx) -> dict[str, Any]:
    """The workspace's plan, its allowance this month and any upgrade it asked for."""
    from sqlalchemy import text

    from app import ai_usage, db
    with db.session(ctx) as s:
        row = s.execute(text("SELECT plan, plan_requested, plan_requested_at FROM tenants WHERE id = :t"),
                        {"t": ctx.require_tenant()}).one()
    a = ai_usage.allowance(ctx)
    return {"plan": row.plan, "plan_info": get(row.plan), "requested": row.plan_requested,
            "requested_at": row.plan_requested_at.isoformat() if row.plan_requested_at else None,
            "allowance": a, "plans": PLANS}


def request_upgrade(ctx, key: str) -> None:
    """Record the plan a workspace asked for; a platform admin sees it and assigns it.
    Members can't write their own tenants row (RLS), so this one column pair is set in
    the system context — for the caller's own workspace only."""
    from sqlalchemy import text

    from app import audit, db
    from app.tenancy import system_context
    if key not in BY_KEY:
        raise ValueError("Unknown plan.")
    tid = ctx.require_tenant()
    with db.session(system_context()) as s:
        s.execute(text("UPDATE tenants SET plan_requested = :p, plan_requested_at = now() WHERE id = :t"),
                  {"p": key, "t": tid})
    with db.session(ctx) as s:
        audit.record(s, ctx, "tenant.plan_requested", target_type="tenant", target_id=tid, after={"plan": key})


def assign(s, ctx, tenant, key: str, custom_credits: int | None = None) -> dict[str, Any]:
    """Platform admin: put a workspace on a plan. Its allowance becomes the enforced monthly
    limit; an upgrade request it had is cleared."""
    from sqlalchemy import text

    from app import audit
    if key not in BY_KEY:
        raise ValueError("Unknown plan.")
    limit = allowance_for(key, custom_credits)
    before = s.execute(text("SELECT plan, ai_monthly_credit_limit AS lim FROM tenants WHERE id = :t"), {"t": tenant.id}).one()
    s.execute(text("""UPDATE tenants SET plan = :p, ai_monthly_credit_limit = :lim,
                      plan_requested = NULL, plan_requested_at = NULL WHERE id = :t"""),
              {"p": key, "lim": limit, "t": tenant.id})
    s.expire(tenant)
    audit.record(s, ctx, "tenant.plan_changed", tenant_id=tenant.id, target_type="tenant", target_id=tenant.id,
                 before={"plan": before.plan, "monthly_credits": before.lim},
                 after={"plan": key, "monthly_credits": limit})
    return {"plan": key, "ai_monthly_credit_limit": limit}
