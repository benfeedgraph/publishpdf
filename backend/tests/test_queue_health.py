"""Worker heartbeats + queue health: "no worker is running" must be visible, not shown
as "Reading the PDF…" forever."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app import db, jobs
from app.config import choose_database_url
from app.tenancy import Role, system_context, tenant_context
from tests.conftest import add_member, make_tenant, make_user


@pytest.fixture
def ctx():
    t, u = make_tenant(), make_user()
    add_member(t, u, Role.client_admin)
    return tenant_context(u.id, t.id, Role.client_admin)


def _old_queued_job(ctx, minutes: int = 10):
    with db.session(ctx) as s:
        j = jobs.enqueue(s, ctx, "system.ping", {}, idempotency_key=f"old-{minutes}-{ctx.tenant_id}")
        jid = j.id
    with db.session(system_context()) as s:
        s.execute(text("UPDATE jobs SET run_after = now() - make_interval(mins => :m) WHERE id = :id"),
                  {"m": minutes, "id": jid})
    return jid


def _clear_heartbeats():
    with db.session(system_context()) as s:
        s.execute(text("DELETE FROM worker_heartbeats"))


def test_waiting_work_with_no_worker_is_stalled(ctx, client):
    _clear_heartbeats()
    _old_queued_job(ctx)
    h = jobs.queue_health()
    assert h["workers_alive"] == 0 and h["queued"] >= 1
    assert h["oldest_queued_seconds"] >= 600
    assert h["stalled"] is True
    body = client.get("/healthz")
    assert body.status_code == 200                      # the API is up; Railway's deploy check uses this
    assert body.json()["status"] == "degraded" and body.json()["queue"]["stalled"] is True


def test_a_live_worker_is_not_stalled(ctx):
    _clear_heartbeats()
    _old_queued_job(ctx)
    jobs.beat("test-worker:1")
    jobs.beat("test-worker:1")                          # upsert, not a second row
    h = jobs.queue_health()
    assert h["workers_alive"] == 1 and h["worker_last_seen_seconds"] <= 5
    assert h["stalled"] is False


def test_a_worker_not_seen_recently_counts_as_gone(ctx):
    _clear_heartbeats()
    jobs.beat("test-worker:gone")
    with db.session(system_context()) as s:
        s.execute(text("UPDATE worker_heartbeats SET seen_at = now() - interval '5 minutes'"))
    _old_queued_job(ctx)
    assert jobs.queue_health()["workers_alive"] == 0
    assert jobs.queue_health()["stalled"] is True


def test_tenant_sessions_cannot_read_heartbeats(ctx):
    jobs.beat("test-worker:rls")
    with db.session(ctx) as s:
        assert s.scalar(text("SELECT count(*) FROM worker_heartbeats")) == 0


def test_railway_private_db_host_allowed_only_on_railway(monkeypatch):
    url = "postgresql://u:p@postgres.railway.internal:5432/railway"
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("RAILWAY_PROJECT_ID", raising=False)
    with pytest.raises(ValueError):
        choose_database_url(url)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    assert choose_database_url(url).startswith("postgresql+psycopg://u:p@postgres.railway.internal")


def test_local_disk_worker_refuses_a_remote_database(monkeypatch):
    from app import cli, storage
    from app.config import get_settings

    monkeypatch.setattr(storage, "backend_kind", lambda: "local")
    monkeypatch.setattr(get_settings(), "database_owner_url", "postgresql+psycopg://u:p@db.proxy.rlwy.net:1/x")
    assert "Refusing to start the worker" in cli.worker_refusal()
    monkeypatch.setattr(get_settings(), "database_owner_url", "postgresql+psycopg://u:p@localhost:54329/x")
    assert cli.worker_refusal() is None
    monkeypatch.setattr(storage, "backend_kind", lambda: "vercel_blob")
    monkeypatch.setattr(get_settings(), "database_owner_url", "postgresql+psycopg://u:p@db.proxy.rlwy.net:1/x")
    assert cli.worker_refusal() is None
