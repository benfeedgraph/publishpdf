from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError

from app import audit, db, jobs
from app.jobs import UserFacingError, handler
from app.models import AuditEntry, Job
from app.tenancy import Role, tenant_context, worker_context
from tests.conftest import add_member, make_tenant, make_user, sign_in


@pytest.fixture
def ctx():
    t, u = make_tenant(), make_user()
    add_member(t, u, Role.client_admin)
    return tenant_context(u.id, t.id, Role.client_admin)


# ------------------------------------------------------------------ audit log


def test_audit_log_is_append_only_for_app_role(ctx):
    with db.session(ctx) as s:
        audit.record(s, ctx, "test.event", after={"x": 1})
    for stmt in ("UPDATE audit_log SET action = 'tampered'", "DELETE FROM audit_log",
                 "TRUNCATE audit_log"):
        with pytest.raises(DBAPIError):
            with db.session(ctx) as s:
                s.execute(text(stmt))
    with db.session(ctx) as s:
        assert s.scalars(select(AuditEntry.action).where(AuditEntry.tenant_id == ctx.tenant_id)).all() == ["test.event"]


def test_audit_log_is_append_only_even_for_table_owner(ctx):
    with db.session(ctx) as s:
        audit.record(s, ctx, "test.event")
    owner = create_engine(os.environ["DATABASE_OWNER_URL"])
    with owner.connect() as c:
        c.execute(text("SELECT set_config('app.platform_admin', 'on', false)"))
        with pytest.raises(DBAPIError, match="append-only"):
            c.execute(text("UPDATE audit_log SET action = 'x' WHERE tenant_id = :t"), {"t": ctx.tenant_id})
        c.rollback()
        with pytest.raises(DBAPIError, match="append-only"):
            c.execute(text("DELETE FROM audit_log WHERE tenant_id = :t"), {"t": ctx.tenant_id})
    owner.dispose()


def test_audit_entry_rolls_back_with_the_change(ctx):
    with pytest.raises(RuntimeError):
        with db.session(ctx) as s:
            audit.record(s, ctx, "test.rolled_back")
            s.flush()
            raise RuntimeError("boom")
    with db.session(ctx) as s:
        assert s.scalars(select(AuditEntry).where(AuditEntry.action == "test.rolled_back")).first() is None


# ------------------------------------------------------------------ job queue


calls: list[str] = []


@handler("test.flaky")
def _flaky(ctx, payload):
    calls.append(payload["id"])
    if payload.get("fail_plain"):
        raise UserFacingError("The PDF is password-protected. Upload an unlocked copy.")
    raise ValueError("internal detail that must not reach the client")


def _drain(worker="w") -> None:
    while jobs.run_one(worker):
        pass


def _make_runnable(job_id) -> None:
    with db.session(tenant_context(None, _tenant_of(job_id), None, platform_admin=True)) as s:
        s.execute(text("UPDATE jobs SET run_after = now() WHERE id = :id"), {"id": job_id})


def _tenant_of(job_id):
    from app.tenancy import system_context
    with db.session(system_context()) as s:
        return s.get(Job, job_id).tenant_id


def test_enqueue_is_idempotent(ctx):
    with db.session(ctx) as s:
        j1 = jobs.enqueue(s, ctx, "system.ping", {"echo": 1}, idempotency_key="same")
        j2 = jobs.enqueue(s, ctx, "system.ping", {"echo": 2}, idempotency_key="same")
    assert j1.id == j2.id


def test_same_idempotency_key_in_different_tenants_is_independent(ctx):
    other_t, other_u = make_tenant(), make_user()
    other = tenant_context(other_u.id, other_t.id, Role.client_admin)
    with db.session(ctx) as s:
        a = jobs.enqueue(s, ctx, "system.ping", {}, idempotency_key="k")
    with db.session(other) as s:
        b = jobs.enqueue(s, other, "system.ping", {}, idempotency_key="k")
    assert a.id != b.id


def test_job_runs_under_its_tenant_context(ctx):
    import app.pipeline  # noqa: F401 - registers system.ping

    with db.session(ctx) as s:
        j = jobs.enqueue(s, ctx, "system.ping", {"echo": "hi"}, idempotency_key="run-me")
    _drain()
    with db.session(worker_context(ctx.tenant_id)) as s:
        done = s.get(Job, j.id)
    assert done.status == "succeeded"
    assert done.result == {"tenant_id": str(ctx.tenant_id), "echo": "hi"}


def test_failures_retry_then_fail_with_generic_plain_message(ctx):
    with db.session(ctx) as s:
        j = jobs.enqueue(s, ctx, "test.flaky", {"id": "generic"}, idempotency_key="flaky", max_attempts=2)
    _drain()
    _make_runnable(j.id)
    _drain()
    with db.session(worker_context(ctx.tenant_id)) as s:
        done = s.get(Job, j.id)
    assert done.status == "failed" and done.attempts == 2
    assert done.error_plain == jobs.GENERIC_FAILURE
    assert "internal detail" in done.last_error        # kept for platform admins
    assert "internal detail" not in done.error_plain   # never shown to clients


def test_user_facing_error_fails_immediately_with_its_message(ctx):
    with db.session(ctx) as s:
        j = jobs.enqueue(s, ctx, "test.flaky", {"id": "plain", "fail_plain": True},
                         idempotency_key="plain")
    _drain()
    with db.session(worker_context(ctx.tenant_id)) as s:
        done = s.get(Job, j.id)
    assert done.status == "failed" and done.attempts == 1
    assert done.error_plain == "The PDF is password-protected. Upload an unlocked copy."


def test_client_jobs_endpoint_hides_internal_errors_and_admin_can_rerun(client, ctx):
    with db.session(ctx) as s:
        j = jobs.enqueue(s, ctx, "test.flaky", {"id": "api"}, idempotency_key="api", max_attempts=1)
    _drain()

    from app.models import User
    from app.tenancy import system_context
    with db.session(system_context()) as s:
        u = s.get(User, ctx.user_id)
        s.expunge(u)
    sign_in(client, u)
    body = client.get(f"/api/tenants/{ctx.tenant_id}/jobs").json()["jobs"]
    row = next(x for x in body if x["id"] == str(j.id))
    assert row["status"] == "failed" and "last_error" not in row

    admin_client = type(client)(client.app)
    admin_client.headers["x-ppdf-csrf"] = "1"
    sign_in(admin_client, make_user(platform_admin=True))
    r = admin_client.post(f"/api/admin/jobs/{j.id}/rerun")
    assert r.status_code == 200 and r.json()["status"] == "queued"
    assert admin_client.post(f"/api/admin/jobs/{j.id}/rerun").status_code == 409


def test_permanently_failed_pipeline_step_marks_the_version_failed(ctx):
    """Regression: a render that crashed 3 times left the version 'processing' for hours."""
    from app.models import Report, ReportVersion, SourceFile

    @handler("pipeline.test_crash")
    def _crash(ctx, payload):
        raise RuntimeError("template exploded")

    with db.session(ctx) as s:
        src = SourceFile(tenant_id=ctx.tenant_id, sha256="a" * 64, storage_key="sources/x.pdf", original_filename="x.pdf",
                         size_bytes=1, page_count=1)
        s.add(src)
        s.flush()
        r = Report(tenant_id=ctx.tenant_id, company_name="X", report_type="other", fiscal_year=2026, period="q1",
                   currency="INR", reporting_unit="crore")
        s.add(r)
        s.flush()
        v = ReportVersion(tenant_id=ctx.tenant_id, report_id=r.id, version_no=1, source_file_id=src.id,
                          source_sha256=src.sha256, status="processing", stage="render")
        s.add(v)
        s.flush()
        j = jobs.enqueue(s, ctx, "pipeline.test_crash", {}, idempotency_key="crash", max_attempts=1)
        j.version_id = v.id
        vid = v.id
    _drain()
    with db.session(worker_context(ctx.tenant_id)) as s:
        v = s.get(ReportVersion, vid)
        assert v.status == "failed" and v.error_plain == jobs.GENERIC_FAILURE
