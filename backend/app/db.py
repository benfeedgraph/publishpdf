"""Database sessions bound to a tenancy Context.

The only way to get a Session is `session(ctx)`. At the start of every transaction
the context is written into transaction-local Postgres settings, which the RLS
policies in migrations/versions/0001_foundations.py read. Because they are
transaction-local (`set_config(..., true)`), a pooled connection can never leak
one request's tenant into the next.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event, exc
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.tenancy import Context

# Every round trip to the database is long-haul in production (the API runs far from it),
# so a session's fixed cost matters more than anything inside it. The driver runs in
# autocommit mode and each transaction opens with ONE simple-protocol statement that is
# both BEGIN and the RLS context; psycopg still sends COMMIT/ROLLBACK because it follows
# the server's real transaction state. Fixed cost per session: 2 round trips, not 4.
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# A pooled connection idle longer than this is pinged before use; a busier one is trusted.
PING_IF_IDLE_SECONDS = 30.0


def _context_sql(ctx: Context) -> str:
    """BEGIN + transaction-local settings in one statement. Values are inlined (simple
    protocol allows no parameters), so each is validated to a UUID or on/off here."""
    def uid(v) -> str:
        sv = str(v) if v else ""
        if sv and not _UUID.match(sv):
            raise RuntimeError("tenancy Context holds a non-UUID id")
        return sv

    def flag(b: bool) -> str:
        return "on" if b else "off"

    return ("BEGIN; SELECT "
            f"set_config('app.tenant_id', '{uid(ctx.tenant_id)}', true), "
            f"set_config('app.user_id', '{uid(ctx.user_id)}', true), "
            f"set_config('app.platform_admin', '{flag(ctx.platform_admin)}', true), "
            f"set_config('app.system', '{flag(ctx.system)}', true)")


@lru_cache
def get_engine() -> Engine:
    engine = create_engine(get_settings().database_app_url, pool_size=5, max_overflow=10,
                           pool_recycle=600)

    @event.listens_for(engine, "connect")
    def _autocommit(dbapi_conn, record) -> None:
        dbapi_conn.autocommit = True
        record.info["used_at"] = time.monotonic()

    @event.listens_for(engine, "checkout")
    def _ping_if_idle(dbapi_conn, record, proxy) -> None:
        # pool_pre_ping costs a round trip on EVERY checkout; only pay it when the
        # connection has sat idle long enough that the proxy may have dropped it.
        if time.monotonic() - record.info.get("used_at", 0) > PING_IF_IDLE_SECONDS:
            try:
                dbapi_conn.execute("SELECT 1")
            except Exception as e:  # noqa: BLE001
                raise exc.DisconnectionError() from e     # pool retries with a fresh connection

    @event.listens_for(engine, "checkin")
    def _touch(dbapi_conn, record) -> None:
        record.info["used_at"] = time.monotonic()

    return engine


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
    connection.connection.dbapi_connection.execute(_context_sql(ctx))


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
