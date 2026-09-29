"""Who is acting, and in which tenant.

Every database session is opened with a `Context`. The session layer (app/db.py)
copies it into Postgres transaction-local settings, where Row-Level Security
policies enforce it. Code never passes a bare tenant id around to "filter by":
if the context says tenant A, tenant B's rows do not exist for that transaction.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass


class Role(str, enum.Enum):
    client_admin = "client_admin"
    client_reviewer = "client_reviewer"


# What each role may do. Platform admins bypass this table (they act across tenants).
PERMISSIONS: dict[str, set[Role]] = {
    "report.view": {Role.client_admin, Role.client_reviewer},
    "report.upload": {Role.client_admin},
    "report.comment": {Role.client_admin, Role.client_reviewer},
    "report.flag_issue": {Role.client_admin, Role.client_reviewer},
    "report.edit_figure": {Role.client_admin},
    "report.publish": {Role.client_admin},
    "theme.manage": {Role.client_admin},
    "domain.manage": {Role.client_admin},
    "analytics.manage": {Role.client_admin},
    "team.view": {Role.client_admin, Role.client_reviewer},
    "team.manage": {Role.client_admin},
    "audit.view": {Role.client_admin},
    "jobs.view": {Role.client_admin, Role.client_reviewer},
}


@dataclass(frozen=True)
class Context:
    user_id: uuid.UUID | None
    tenant_id: uuid.UUID | None
    role: Role | None = None
    platform_admin: bool = False
    # System context: only for pre-authentication lookups and the job claimer.
    system: bool = False

    def can(self, permission: str) -> bool:
        if self.platform_admin:
            return True
        if self.role is None:
            return False
        return self.role in PERMISSIONS[permission]

    def require_tenant(self) -> uuid.UUID:
        if self.tenant_id is None:
            raise PermissionError("this operation requires a tenant context")
        return self.tenant_id


def user_context(user_id: uuid.UUID, *, platform_admin: bool = False) -> Context:
    """Authenticated, not yet acting inside a tenant."""
    return Context(user_id=user_id, tenant_id=None, platform_admin=platform_admin)


def tenant_context(user_id: uuid.UUID | None, tenant_id: uuid.UUID, role: Role | None,
                   *, platform_admin: bool = False) -> Context:
    return Context(user_id=user_id, tenant_id=tenant_id, role=role, platform_admin=platform_admin)


def worker_context(tenant_id: uuid.UUID) -> Context:
    """A background job acting on behalf of one tenant — RLS-scoped like a user."""
    return Context(user_id=None, tenant_id=tenant_id, role=None)


def system_context() -> Context:
    """Bypasses tenant RLS. Grep-able on purpose: allowed only in app/security/auth.py
    (lookups before a user is known) and app/jobs.py (claiming the next job)."""
    return Context(user_id=None, tenant_id=None, system=True)
