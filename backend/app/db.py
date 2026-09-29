"""Database sessions bound to a tenancy Context.

The only way to get a Session is `session(ctx)`. At the start of every transaction
the context is written into transaction-local Postgres settings, which the RLS
policies in migrations/versions/0001_foundations.py read. Because they are
transaction-local (`set_config(..., true)`), a pooled connection can never leak
one request's tenant into the next.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.tenancy import Context

_SET_CONTEXT = text(
    "SELECT set_config('app.tenant_id', :tenant_id, true),"
    "       set_config('app.user_id', :user_id, true),"
    "       set_config('app.platform_admin', :platform_admin, true),"
    "       set_config('app.system', :system, true)"
)


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_settings().database_app_url, pool_pre_ping=True, pool_size=5,
                         max_overflow=10)


@lru_cache
def _factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


@event.listens_for(Session, "after_begin")
def _apply_context(sess: Session, _transaction, connection) -> None:
    ctx: Context | None = sess.info.get("ctx")
    if ctx is None:
        # A session without a context would see nothing (RLS fails closed), but
        # make the bug loud instead of silently empty.
        raise RuntimeError("Session opened without a tenancy Context; use app.db.session(ctx)")
    connection.execute(_SET_CONTEXT, {
        "tenant_id": str(ctx.tenant_id) if ctx.tenant_id else "",
        "user_id": str(ctx.user_id) if ctx.user_id else "",
        "platform_admin": "on" if ctx.platform_admin else "off",
        "system": "on" if ctx.system else "off",
    })


@contextmanager
def session(ctx: Context) -> Iterator[Session]:
    """Open a session acting as `ctx`. Commits on success, rolls back on error."""
    s = _factory()()
    s.info["ctx"] = ctx
    try:
        yield s
        s.commit()
    except BaseException:
        s.rollback()
        raise
    finally:
        s.close()
