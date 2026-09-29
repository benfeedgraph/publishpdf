"""Cross-tenant access must be impossible — at the SQL (RLS), repository, API,
storage and worker layers. Each test sets up two tenants and attacks one from the other."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from app import audit, db, jobs, storage
from app.models import AuditEntry, Invite, Job, Membership, Tenant, User
from app.tenancy import Context, Role, tenant_context, worker_context
from tests.conftest import add_member, make_tenant, make_user, sign_in


@pytest.fixture
def two_tenants():
    a, b = make_tenant("Alpha Ltd"), make_tenant("Beta Ltd")
    ua, ub = make_user(), make_user()
    add_member(a, ua, Role.client_admin)
    add_member(b, ub, Role.client_admin)
    ctx_a = tenant_context(ua.id, a.id, Role.client_admin)
    ctx_b = tenant_context(ub.id, b.id, Role.client_admin)
    # Give each tenant a job, an invite and an audit entry.
    for ctx in (ctx_a, ctx_b):
        with db.session(ctx) as s:
            jobs.enqueue(s, ctx, "system.ping", {}, idempotency_key="seed")
            s.add(Invite(tenant_id=ctx.tenant_id, email=f"x-{ctx.tenant_id}@example.com",
                         role="client_reviewer", token_hash=f"h-{ctx.tenant_id}",
                         expires_at=text("now() + interval '1 day'")))
            audit.record(s, ctx, "test.seed")
    return a, b, ua, ub, ctx_a, ctx_b


# ------------------------------------------------------------------ SQL / RLS layer


@pytest.mark.parametrize("model", [Tenant, Membership, Invite, Job, AuditEntry])
def test_rls_hides_other_tenants_rows(two_tenants, model):
    a, b, _, _, ctx_a, _ = two_tenants
    with db.session(ctx_a) as s:
        col = model.id if model is Tenant else model.tenant_id
        seen = set(s.scalars(select(col)).all())
    assert b.id not in seen
    assert a.id in seen


def test_rls_rejects_writing_into_other_tenant(two_tenants):
    _, b, _, _, ctx_a, _ = two_tenants
    with pytest.raises(ProgrammingError, match="row-level security"):
        with db.session(ctx_a) as s:
            s.add(Job(tenant_id=b.id, kind="system.ping", payload={}, idempotency_key="attack"))


def test_rls_update_and_delete_of_other_tenant_affect_nothing(two_tenants):
    _, b, _, ub, ctx_a, _ = two_tenants
    with db.session(ctx_a) as s:
        assert s.execute(text("UPDATE jobs SET status='cancelled' WHERE tenant_id=:b"), {"b": b.id}).rowcount == 0
        assert s.execute(text("DELETE FROM memberships WHERE tenant_id=:b"), {"b": b.id}).rowcount == 0
        assert s.execute(text("UPDATE tenants SET name='pwned' WHERE id=:b"), {"b": b.id}).rowcount == 0
    with db.session(tenant_context(ub.id, b.id, Role.client_admin)) as s:
        assert s.get(Tenant, b.id).name == "Beta Ltd"


def test_other_tenants_users_are_invisible(two_tenants):
    _, _, _, ub, ctx_a, _ = two_tenants
    with db.session(ctx_a) as s:
        assert s.get(User, ub.id) is None


def test_session_without_context_is_refused():
    from app.db import _factory

    s = _factory()()
    with pytest.raises(RuntimeError, match="without a tenancy Context"):
        s.execute(text("SELECT 1"))
    s.close()


def test_raw_app_role_connection_without_settings_sees_nothing(two_tenants):
    """Fail closed: a connection that never set app.* settings sees zero tenant rows."""
    from app.config import get_settings

    eng = create_engine(get_settings().database_app_url)
    with eng.connect() as c:
        for table in ("tenants", "memberships", "invites", "jobs", "audit_log", "users", "sessions"):
            assert c.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0, table
    eng.dispose()


def test_context_does_not_leak_across_pooled_connections(two_tenants):
    _, b, _, _, ctx_a, ctx_b = two_tenants
    for _ in range(3):  # reuse pooled connections in alternation
        with db.session(ctx_a) as s:
            assert b.id not in set(s.scalars(select(Job.tenant_id)))
        with db.session(ctx_b) as s:
            assert set(s.scalars(select(Job.tenant_id))) == {b.id}
    with db.get_engine().connect() as c:
        assert c.execute(text("SELECT current_setting('app.tenant_id', true)")).scalar() in (None, "")


def test_app_role_is_not_privileged():
    with db.get_engine().connect() as c:
        row = c.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")).one()
    assert row == (False, False)


def test_member_cannot_self_escalate_to_platform_admin(two_tenants):
    _, _, ua, _, ctx_a, _ = two_tenants
    with pytest.raises(DBAPIError, match="privileged user fields"):
        with db.session(ctx_a) as s:
            s.execute(text("UPDATE users SET is_platform_admin = true WHERE id = :id"), {"id": ua.id})


def test_membership_cannot_be_added_to_other_tenant(two_tenants):
    _, b, ua, _, ctx_a, _ = two_tenants
    with pytest.raises(ProgrammingError, match="row-level security"):
        with db.session(ctx_a) as s:
            s.add(Membership(tenant_id=b.id, user_id=ua.id, role="client_admin"))


# ------------------------------------------------------------------ worker layer


def test_worker_context_is_tenant_scoped(two_tenants):
    a, b, *_ = two_tenants
    with db.session(worker_context(a.id)) as s:
        assert set(s.scalars(select(Job.tenant_id))) == {a.id}


# ------------------------------------------------------------------ storage layer


def test_storage_keys_are_tenant_prefixed_and_isolated(two_tenants):
    *_, ctx_a, ctx_b = two_tenants
    storage.put(ctx_b, "reports/secret.pdf", b"beta-only")
    assert not storage.exists(ctx_a, "reports/secret.pdf")
    with pytest.raises(storage.StorageError):
        storage.get(ctx_a, "reports/secret.pdf")
    assert storage.get(ctx_b, "reports/secret.pdf") == b"beta-only"


@pytest.mark.parametrize("key", ["../x", "a/../../b", "/etc/passwd", "a//b", "", "tenants/x", ".hidden"])
def test_storage_rejects_traversal_and_absolute_keys(two_tenants, key):
    *_, ctx_a, _ = two_tenants
    if key == "tenants/x":
        # Allowed as a relative key, but it nests under A's prefix — never escapes it.
        assert storage.scoped_key(ctx_a, key).startswith(f"tenants/{ctx_a.tenant_id}/")
        return
    with pytest.raises(storage.StorageError):
        storage.scoped_key(ctx_a, key)


def test_storage_requires_tenant_context():
    with pytest.raises(PermissionError):
        storage.scoped_key(Context(user_id=None, tenant_id=None), "x")


# ------------------------------------------------------------------ API layer


@pytest.mark.parametrize("method,path", [
    ("get", ""), ("get", "/members"), ("get", "/audit"), ("get", "/jobs"),
    ("post", "/invites"),
])
def test_api_member_of_a_gets_404_on_tenant_b(client, two_tenants, method, path):
    _, b, ua, *_ = two_tenants
    sign_in(client, ua)
    kwargs = {"json": {"email": "x@example.com", "role": "client_reviewer"}} if method == "post" else {}
    r = getattr(client, method)(f"/api/tenants/{b.id}{path}", **kwargs)
    assert r.status_code == 404, r.text


def test_api_tenant_list_only_shows_own_tenants(client, two_tenants):
    a, b, ua, *_ = two_tenants
    sign_in(client, ua)
    ids = {t["id"] for t in client.get("/api/auth/me").json()["tenants"]}
    assert ids == {str(a.id)}


def test_api_admin_endpoints_reject_client_admins(client, two_tenants):
    _, _, ua, *_ = two_tenants
    sign_in(client, ua)
    assert client.get("/api/admin/tenants").status_code == 403
    assert client.get("/api/admin/jobs").status_code == 403


def test_platform_admin_can_see_across_tenants(client, two_tenants):
    a, b, *_ = two_tenants
    admin = make_user(platform_admin=True)
    sign_in(client, admin)
    ids = {t["id"] for t in client.get("/api/admin/tenants").json()["tenants"]}
    assert {str(a.id), str(b.id)} <= ids
    assert client.get(f"/api/tenants/{b.id}/members").status_code == 200
