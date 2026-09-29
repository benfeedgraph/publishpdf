"""Domain verification/monitoring jobs and the periodic scheduler."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app import audit, db, domains, emailer, jobs
from app.models import Domain, Membership, Tenant, User
from app.reports import rebuild_site
from app.tenancy import Context, system_context, worker_context

log = logging.getLogger(__name__)

# Hooks for tests.
RESOLVER: domains.Resolver = domains.live_resolver
TLS_PROBE: domains.TlsProbe | None = None
SSL_GRACE = timedelta(hours=2)


def run_check(ctx: Context, domain_id: uuid.UUID, *, actor: uuid.UUID | None = None) -> Domain:
    now = datetime.now(timezone.utc)
    with db.session(ctx) as s:
        d = s.get(Domain, domain_id)
        if d is None or d.status == "removed":
            raise ValueError("domain not found")
        hostname, token, before = d.hostname, d.verify_token, d.status
    view = RESOLVER(hostname)
    res = domains.evaluate(hostname, token, before, view, TLS_PROBE, now=now)
    with db.session(ctx) as s:
        d = s.get(Domain, domain_id)
        assert d is not None
        d.last_checked_at = now
        prev = d.status
        if res.status == "ssl_issuing" and prev == "ssl_issuing" and d.verified_at and now - d.verified_at > SSL_GRACE:
            res = domains.CheckResult("failed", "cert_failed", domains.FAILURES["cert_failed"].format(
                detail="no valid certificate after two hours"))
        if res.status in ("verified", "ssl_issuing", "live") and d.verified_at is None:
            d.verified_at = now
        d.status = res.status
        d.failure_code, d.failure_reason = res.failure_code, res.failure_reason
        if res.cert_expires_at:
            d.cert_expires_at = res.cert_expires_at
        went_live = res.status == "live" and prev != "live"
        if went_live:
            d.live_at = now
        if prev != d.status:
            audit.record(s, ctx, "domain.status_changed", target_type="domain", target_id=d.id,
                         actor_user_id=actor, before={"status": prev},
                         after={"status": d.status, "reason": d.failure_reason})
        broke = prev == "live" and d.status == "failed"
        if broke and (d.last_alert_at is None or now - d.last_alert_at > timedelta(hours=6)):
            d.last_alert_at = now
            _alert(s, ctx, d)
        if went_live or broke:
            rebuild_site(s, ctx)          # canonical origin / sitemap switch to (or away from) the subdomain
        s.flush()
        s.expunge(d)
        return d


def _alert(s, ctx: Context, d: Domain) -> None:
    tenant = s.get(Tenant, ctx.tenant_id)
    admins = s.scalars(select(User.email).join(Membership, Membership.user_id == User.id)
                       .where(Membership.tenant_id == ctx.tenant_id, Membership.role == "client_admin")).all()
    with db.session(system_context()) as ss:
        platform = ss.scalars(select(User.email).where(User.is_platform_admin.is_(True))).all()
    body = (f"Your report site {d.hostname} has a problem and may not be reachable:\n\n{d.failure_reason}\n\n"
            "Fix the DNS record(s) shown in Domain settings, then press “Check now”.")
    for to in sorted(set(admins) | set(platform)):
        try:
            emailer.send(to, f"Action needed: {d.hostname} ({tenant.name if tenant else ''})", body)
        except Exception:  # noqa: BLE001 - an alert failure must not break monitoring
            log.exception("failed to send domain alert")


@jobs.handler("domain.check")
def domain_check_job(ctx: Context, payload: dict[str, Any]) -> dict[str, Any]:
    d = run_check(ctx, uuid.UUID(payload["domain_id"]))
    return {"status": d.status, "reason": d.failure_reason}


@jobs.periodic(60)
def schedule_due_checks(now: datetime | None = None) -> int:
    """Called by the worker loop: enqueue checks for domains that are due."""
    now = now or datetime.now(timezone.utc)
    with db.session(system_context()) as s:
        rows = s.scalars(select(Domain).where(Domain.status != "removed")).all()
        due = [(d.id, d.tenant_id) for d in rows
               if d.last_checked_at is None or now - d.last_checked_at >= domains.check_interval(d.status, d.created_at, now)]
    n = 0
    bucket = now.strftime("%Y%m%d%H%M")
    for did, tid in due:
        ctx = worker_context(tid)
        with db.session(ctx) as s:
            jobs.enqueue(s, ctx, "domain.check", {"domain_id": str(did)}, idempotency_key=f"domain:{did}:{bucket}",
                         max_attempts=1)
        n += 1
    return n
