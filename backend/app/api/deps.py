"""FastAPI dependencies: who is calling, and which tenant they're acting in."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from fastapi import Depends, HTTPException, Path, Request, status
from sqlalchemy import select

from app import db
from app.config import get_settings
from app.models import Membership, Tenant
from app.security.auth import AuthState, resolve_session
from app.tenancy import Context, Role, tenant_context


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def auth_state(request: Request) -> AuthState | None:
    return resolve_session(request.cookies.get(get_settings().session_cookie_name))


def partial_auth(state: AuthState | None = Depends(auth_state)) -> AuthState:
    """Signed in, MFA not necessarily done. Only for the MFA endpoints, /me and logout."""
    if state is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Please sign in.")
    return state


def full_auth(state: AuthState = Depends(partial_auth)) -> AuthState:
    if not state.fully_authenticated:
        detail = "mfa_enrollment_required" if not state.mfa_enrolled else "mfa_required"
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail)
    return state


def platform_admin(state: AuthState = Depends(full_auth)) -> AuthState:
    if not state.user.is_platform_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Platform admins only.")
    return state


def admin_context(state: AuthState = Depends(platform_admin)) -> Context:
    return Context(user_id=state.user.id, tenant_id=None, platform_admin=True)


def tenant_ctx(tenant_id: uuid.UUID = Path(...), state: AuthState = Depends(full_auth)) -> Context:
    """Resolve the caller's role in `tenant_id`. Non-members get 404 (not 403), so
    tenant ids can't be probed for existence."""
    probe = tenant_context(state.user.id, tenant_id, None, platform_admin=state.user.is_platform_admin)
    with db.session(probe) as s:
        tenant = s.get(Tenant, tenant_id)
        membership = s.scalars(select(Membership).where(Membership.tenant_id == tenant_id,
                                                        Membership.user_id == state.user.id)).first()
    if tenant is None or (membership is None and not state.user.is_platform_admin):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    if tenant.status != "active" and not state.user.is_platform_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This workspace is suspended. Contact support.")
    role = Role(membership.role) if membership else None
    return tenant_context(state.user.id, tenant_id, role, platform_admin=state.user.is_platform_admin)


def require(permission: str) -> Callable[[Context], Context]:
    def check(ctx: Context = Depends(tenant_ctx)) -> Context:
        if not ctx.can(permission):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Your role doesn't allow this.")
        return ctx
    return check
