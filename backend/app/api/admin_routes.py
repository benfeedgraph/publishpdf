"""Platform-admin endpoints: /api/admin/..."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import undefer

from app import audit, db, jobs
from app.api.deps import admin_context
from app.api.tenant_routes import job_json
from app.models import Job, Membership, Tenant
from app.security.auth import create_invite
from app.tenancy import Context, Role, tenant_context

router = APIRouter(prefix="/api/admin", tags=["admin"])


class TenantIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?$")
    first_admin_email: EmailStr | None = None


class TenantPatch(BaseModel):
    status: str = Field(pattern="^(active|suspended)$")


@router.get("/tenants")
def list_tenants(ctx: Context = Depends(admin_context)) -> dict:
    with db.session(ctx) as s:
        members = (select(Membership.tenant_id, func.count().label("n"))
                   .group_by(Membership.tenant_id).subquery())
        rows = s.execute(select(Tenant, func.coalesce(members.c.n, 0))
                         .options(undefer(Tenant.ai_monthly_credit_limit))
                         .outerjoin(members, members.c.tenant_id == Tenant.id)
                         .order_by(Tenant.created_at.desc())).all()
    from app import ai_usage
    credits = ai_usage.month_credits_by_tenant(ctx)
    return {"tenants": [{"id": str(t.id), "slug": t.slug, "name": t.name, "status": t.status,
                         "created_at": t.created_at.isoformat(), "member_count": n,
                         "ai_credits_month": credits.get(str(t.id), 0),
                         "ai_monthly_credit_limit": t.ai_monthly_credit_limit}
                        for t, n in rows]}


@router.post("/tenants", status_code=201)
def create_tenant(body: TenantIn, ctx: Context = Depends(admin_context)) -> dict:
    try:
        with db.session(ctx) as s:
            t = Tenant(name=body.name.strip(), slug=body.slug)
            s.add(t)
            s.flush()
            audit.record(s, ctx, "tenant.created", tenant_id=t.id, target_type="tenant", target_id=t.id,
                         after={"name": t.name, "slug": t.slug})
            if body.first_admin_email:
                create_invite(s, tenant_context(ctx.user_id, t.id, None, platform_admin=True),
                              body.first_admin_email, Role.client_admin)
            return {"id": str(t.id), "slug": t.slug, "name": t.name, "status": t.status}
    except IntegrityError as e:
        raise HTTPException(409, "That slug is already taken.") from e


@router.patch("/tenants/{tenant_id}")
def update_tenant(tenant_id: uuid.UUID, body: TenantPatch, ctx: Context = Depends(admin_context)) -> dict:
    with db.session(ctx) as s:
        t = s.get(Tenant, tenant_id)
        if t is None:
            raise HTTPException(404, "Tenant not found.")
        before = t.status
        t.status = body.status
        audit.record(s, ctx, f"tenant.{'suspended' if body.status == 'suspended' else 'reactivated'}",
                     tenant_id=t.id, target_type="tenant", target_id=t.id,
                     before={"status": before}, after={"status": t.status})
        return {"id": str(t.id), "status": t.status}


class AiLimitIn(BaseModel):
    monthly_credits: int | None = Field(default=None, ge=0, le=10_000_000)   # None = no limit


@router.put("/tenants/{tenant_id}/ai-limit")
def set_ai_limit(tenant_id: uuid.UUID, body: AiLimitIn, ctx: Context = Depends(admin_context)) -> dict:
    with db.session(ctx) as s:
        t = s.get(Tenant, tenant_id)
        if t is None:
            raise HTTPException(404, "Tenant not found.")
        before = t.ai_monthly_credit_limit
        t.ai_monthly_credit_limit = body.monthly_credits
        audit.record(s, ctx, "tenant.ai_limit_changed", tenant_id=t.id, target_type="tenant", target_id=t.id,
                     before={"monthly_credits": before}, after={"monthly_credits": body.monthly_credits})
        return {"id": str(t.id), "ai_monthly_credit_limit": t.ai_monthly_credit_limit}


@router.get("/jobs")
def list_jobs(ctx: Context = Depends(admin_context), status: str | None = Query(None),
              limit: int = Query(100, le=500)) -> dict:
    with db.session(ctx) as s:
        q = select(Job).order_by(Job.created_at.desc()).limit(limit)
        if status:
            q = q.where(Job.status == status)
        return {"jobs": [job_json(j, include_internal=True) for j in s.scalars(q)]}


@router.post("/jobs/{job_id}/rerun")
def rerun_job(job_id: uuid.UUID, ctx: Context = Depends(admin_context)) -> dict:
    with db.session(ctx) as s:
        j = s.get(Job, job_id)
        if j is None:
            raise HTTPException(404, "Job not found.")
        if j.status in ("queued", "running"):
            raise HTTPException(409, "Job is already queued or running.")
        jobs.requeue(s, j)
        audit.record(s, ctx, "job.rerun", tenant_id=j.tenant_id, target_type="job", target_id=j.id)
        return job_json(j, include_internal=True)


@router.get("/tenants/{tenant_id}")
def tenant_detail(tenant_id: uuid.UUID, ctx: Context = Depends(admin_context)) -> dict:
    from app.api.report_routes import report_json
    from app.models import Domain, Report, User

    tctx = tenant_context(ctx.user_id, tenant_id, None, platform_admin=True)
    with db.session(tctx) as s:
        t = s.get(Tenant, tenant_id)
        if t is None:
            raise HTTPException(404, "Tenant not found.")
        from app.reports import latest_versions
        latest = latest_versions(s)
        reports = []
        for r in s.scalars(select(Report).where(Report.tenant_id == tenant_id, Report.deleted_at.is_(None))
                           .order_by(Report.created_at.desc())):
            v = latest.get(r.id)
            reports.append(report_json(r, [v] if v else []))
        members = s.execute(select(Membership, User).join(User, User.id == Membership.user_id)
                            .where(Membership.tenant_id == tenant_id)).all()
        jobs_ = s.scalars(select(Job).where(Job.tenant_id == tenant_id).order_by(Job.created_at.desc()).limit(50)).all()
        d = s.scalars(select(Domain).where(Domain.tenant_id == tenant_id, Domain.status != "removed")).first()
        tenant_json = {"id": str(t.id), "slug": t.slug, "name": t.name, "status": t.status,
                       "created_at": t.created_at.isoformat(), "ai_monthly_credit_limit": t.ai_monthly_credit_limit}
        out = {
            "tenant": tenant_json,
            "reports": reports,
            "members": [{"user_id": str(u.id), "email": u.email, "role": m.role,
                         "mfa_enrolled": u.totp_enabled_at is not None} for m, u in members],
            "jobs": [job_json(j, include_internal=True) for j in jobs_],
            "domain": {"hostname": d.hostname, "status": d.status, "failure_reason": d.failure_reason,
                       "last_checked_at": d.last_checked_at.isoformat() if d.last_checked_at else None} if d else None,
        }
    from app import ai_usage
    out["ai_usage"] = ai_usage.summary(tctx)
    return out


@router.get("/issues")
def issues_overview(ctx: Context = Depends(admin_context)) -> dict:
    """Validation quality across tenants: open issues on each version's latest run."""
    from sqlalchemy import and_

    from app.models import Report, ReportVersion, ValidationIssue

    with db.session(ctx) as s:
        latest = (select(ValidationIssue.version_id, func.max(ValidationIssue.run_no).label("run"))
                  .group_by(ValidationIssue.version_id).subquery())
        rows = s.execute(
            select(Tenant.name, Tenant.id, ReportVersion.id, ReportVersion.version_no, Report.fiscal_year, Report.period,
                   Report.report_type, ReportVersion.status, ValidationIssue.check_name, ValidationIssue.severity,
                   func.count())
            .join(latest, latest.c.version_id == ValidationIssue.version_id)
            .join(ReportVersion, ReportVersion.id == ValidationIssue.version_id)
            .join(Report, Report.id == ReportVersion.report_id)
            .join(Tenant, Tenant.id == ValidationIssue.tenant_id)
            .where(and_(ValidationIssue.run_no == latest.c.run, ValidationIssue.status == "open"))
            .group_by(Tenant.name, Tenant.id, ReportVersion.id, ReportVersion.version_no, Report.fiscal_year,
                      Report.period, Report.report_type, ReportVersion.status, ValidationIssue.check_name,
                      ValidationIssue.severity)).all()
        by_check: dict[str, dict] = {}
        versions: dict[str, dict] = {}
        for tname, tid, vid, vno, fy, period, rtype, vstatus, check, sev, n in rows:
            c = by_check.setdefault(check, {"check": check, "blocking": 0, "warning": 0})
            c[sev] += n
            v = versions.setdefault(str(vid), {"tenant": tname, "tenant_id": str(tid), "version_id": str(vid),
                                               "version_no": vno, "report": f"FY{fy} {period.upper()} {rtype}",
                                               "status": vstatus, "blocking": 0, "warning": 0})
            v[sev] += n
        return {"by_check": sorted(by_check.values(), key=lambda c: -c["blocking"]),
                "versions": sorted(versions.values(), key=lambda v: -v["blocking"])}
