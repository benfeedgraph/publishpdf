"""Append-only audit trail. Rows can only be inserted (DB grants + trigger)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditEntry
from app.tenancy import Context


def record(s: Session, ctx: Context, action: str, *, target_type: str | None = None,
           target_id: object | None = None, before: dict[str, Any] | None = None,
           after: dict[str, Any] | None = None, ip: str | None = None,
           tenant_id: object | None = None, actor_user_id: object | None = None) -> None:
    """Write one audit entry in the caller's transaction (so it commits or rolls
    back together with the change it describes)."""
    s.add(AuditEntry(
        tenant_id=tenant_id if tenant_id is not None else ctx.tenant_id,
        actor_user_id=actor_user_id if actor_user_id is not None else ctx.user_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        before=before,
        after=after,
        ip=ip,
    ))
