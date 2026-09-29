"""Client-facing, tenant-scoped endpoints: /api/tenants/{tenant_id}/..."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit, db
from app.api.deps import require, tenant_ctx
from app.models import AuditEntry, Invite, Job, Membership, Tenant, User
from app.security.auth import create_invite
from app.tenancy import Context, Role

router = APIRouter(prefix="/api/tenants/{tenant_id}", tags=["tenant"])


@router.get("")
def get_tenant(ctx: Context = Depends(tenant_ctx)) -> dict:
    with db.session(ctx) as s:
        t = s.get(Tenant, ctx.tenant_id)
        assert t is not None
        return {"id": str(t.id), "slug": t.slug, "name": t.name, "status": t.status,
                "role": ctx.role.value if ctx.role else None, "platform_admin": ctx.platform_admin}


# --------------------------------------------------------------------------- team


class InviteIn(BaseModel):
    email: EmailStr
    role: Role


class RoleIn(BaseModel):
    role: Role


@router.get("/members")
def list_members(ctx: Context = Depends(require("team.view"))) -> dict:
    with db.session(ctx) as s:
        rows = s.execute(select(Membership, User).join(User, User.id == Membership.user_id)
                         .where(Membership.tenant_id == ctx.tenant_id).order_by(User.email)).all()
        invites = []
        if ctx.can("team.manage"):
            invites = s.scalars(select(Invite).where(
                Invite.tenant_id == ctx.tenant_id, Invite.accepted_at.is_(None),
                Invite.revoked_at.is_(None), Invite.expires_at > func.now())).all()
        return {
            "members": [{"user_id": str(u.id), "email": u.email, "name": u.name, "role": m.role,
                         "mfa_enrolled": u.totp_enabled_at is not None} for m, u in rows],
            "pending_invites": [{"id": str(i.id), "email": i.email, "role": i.role,
                                 "expires_at": i.expires_at.isoformat()} for i in invites],
        }


@router.post("/invites", status_code=201)
def invite(body: InviteIn, ctx: Context = Depends(require("team.manage"))) -> dict:
    with db.session(ctx) as s:
        inv = create_invite(s, ctx, body.email, body.role)
        return {"id": str(inv.id), "email": inv.email, "role": inv.role}


@router.delete("/invites/{invite_id}", status_code=204)
def revoke_invite(invite_id: uuid.UUID, ctx: Context = Depends(require("team.manage"))) -> None:
    with db.session(ctx) as s:
        inv = s.get(Invite, invite_id)
        if inv is None or inv.tenant_id != ctx.tenant_id or inv.accepted_at is not None:
            raise HTTPException(404, "Invitation not found.")
        inv.revoked_at = func.now()
        audit.record(s, ctx, "team.invite_revoked", target_type="invite", target_id=inv.id,
                     before={"email": inv.email, "role": inv.role})


@router.patch("/members/{user_id}")
def change_role(user_id: uuid.UUID, body: RoleIn, ctx: Context = Depends(require("team.manage"))) -> dict:
    with db.session(ctx) as s:
        m = _membership(s, ctx, user_id)
        if m.role == Role.client_admin.value and body.role != Role.client_admin:
            _ensure_other_admin(s, ctx, user_id)
        before = m.role
        m.role = body.role.value
        audit.record(s, ctx, "team.role_changed", target_type="user", target_id=user_id,
                     before={"role": before}, after={"role": m.role})
        return {"user_id": str(user_id), "role": m.role}


@router.delete("/members/{user_id}", status_code=204)
def remove_member(user_id: uuid.UUID, ctx: Context = Depends(require("team.manage"))) -> None:
    with db.session(ctx) as s:
        m = _membership(s, ctx, user_id)
        if m.role == Role.client_admin.value:
            _ensure_other_admin(s, ctx, user_id)
        audit.record(s, ctx, "team.member_removed", target_type="user", target_id=user_id,
                     before={"role": m.role})
        s.delete(m)


def _membership(s: Session, ctx: Context, user_id: uuid.UUID) -> Membership:
    m = s.scalars(select(Membership).where(Membership.tenant_id == ctx.tenant_id,
                                           Membership.user_id == user_id)).first()
    if m is None:
        raise HTTPException(404, "Member not found.")
    return m


def _ensure_other_admin(s: Session, ctx: Context, leaving_user_id: uuid.UUID) -> None:
    others = s.scalar(select(func.count()).select_from(Membership).where(
        Membership.tenant_id == ctx.tenant_id, Membership.role == Role.client_admin.value,
        Membership.user_id != leaving_user_id))
    if not others:
        raise HTTPException(409, "A workspace needs at least one admin. Make someone else admin first.")


# --------------------------------------------------------------------------- audit + jobs


@router.get("/audit")
def audit_log(ctx: Context = Depends(require("audit.view")),
              before_id: int | None = Query(None), limit: int = Query(50, le=200)) -> dict:
    with db.session(ctx) as s:
        q = (select(AuditEntry, User.email).outerjoin(User, User.id == AuditEntry.actor_user_id)
             .where(AuditEntry.tenant_id == ctx.tenant_id))
        if before_id:
            q = q.where(AuditEntry.id < before_id)
        rows = s.execute(q.order_by(AuditEntry.id.desc()).limit(limit)).all()
        return {"entries": [_audit_json(e, email) for e, email in rows]}


def _audit_json(e: AuditEntry, email: str | None) -> dict:
    return {"id": e.id, "at": e.at.isoformat(), "actor": email, "action": e.action,
            "target_type": e.target_type, "target_id": e.target_id, "before": e.before,
            "after": e.after}


@router.get("/jobs")
def jobs(ctx: Context = Depends(require("jobs.view")), limit: int = Query(50, le=200)) -> dict:
    with db.session(ctx) as s:
        rows = s.scalars(select(Job).where(Job.tenant_id == ctx.tenant_id)
                         .order_by(Job.created_at.desc()).limit(limit)).all()
        return {"jobs": [job_json(j, include_internal=False) for j in rows]}


def job_json(j: Job, *, include_internal: bool) -> dict:
    out = {"id": str(j.id), "kind": j.kind, "status": j.status, "attempts": j.attempts,
           "max_attempts": j.max_attempts, "error": j.error_plain, "created_at": _iso(j.created_at),
           "started_at": _iso(j.started_at), "finished_at": _iso(j.finished_at)}
    if include_internal:
        out |= {"tenant_id": str(j.tenant_id), "last_error": j.last_error, "locked_by": j.locked_by}
    return out


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None
