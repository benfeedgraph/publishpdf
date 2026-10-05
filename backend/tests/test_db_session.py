"""The one-statement BEGIN + RLS context: transactions, rollback and isolation must be
exactly as before, with fewer round trips."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app import db
from app.models import Tenant
from app.tenancy import Context, Role, system_context, tenant_context
from tests.conftest import make_tenant, uniq


def test_an_error_rolls_the_whole_session_back():
    slug = uniq("rb")
    with pytest.raises(RuntimeError):
        with db.session(system_context()) as s:
            s.add(Tenant(slug=slug, name=slug))
            s.flush()
            raise RuntimeError("boom")
    with db.session(system_context()) as s:
        assert s.scalar(text("SELECT count(*) FROM tenants WHERE slug = :s"), {"s": slug}) == 0


def test_settings_are_transaction_local_and_never_leak_to_the_next_session():
    t = make_tenant()
    for _ in range(3):                                   # reuse pooled connections
        with db.session(tenant_context(None, t.id, Role.client_admin)) as s:
            assert s.scalar(text("SELECT current_setting('app.tenant_id', true)")) == str(t.id)
        with db.session(system_context()) as s:
            assert s.scalar(text("SELECT current_setting('app.tenant_id', true)")) in ("", None)
            assert s.scalar(text("SELECT current_setting('app.system', true)")) == "on"


def test_a_session_really_is_one_transaction():
    with db.session(system_context()) as s:
        a = s.scalar(text("SELECT txid_current()"))
        b = s.scalar(text("SELECT txid_current()"))
    assert a == b


def test_non_uuid_ids_are_refused_before_reaching_sql():
    bad = Context(user_id="x'; DROP TABLE tenants; --", tenant_id=uuid.uuid4())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="non-UUID"):
        with db.session(bad) as s:
            s.execute(text("SELECT 1"))
