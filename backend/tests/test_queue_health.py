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
    monkeypatch.setattr(get_settings(), "env", "production")
    assert cli.worker_refusal() is None


def test_a_development_worker_refuses_the_live_database(monkeypatch):
    """A laptop with shared storage + the live DB took live jobs and ran its own code and
    keys on them (an AI check failed with the laptop's key)."""
    from app import cli, storage
    from app.config import get_settings

    monkeypatch.setattr(storage, "backend_kind", lambda: "s3")
    monkeypatch.setattr(get_settings(), "env", "development")
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("ALLOW_REMOTE_WORKER", raising=False)
    monkeypatch.setattr(get_settings(), "database_owner_url", "postgresql+psycopg://u:p@db.proxy.rlwy.net:1/x")
    assert "development machine" in cli.worker_refusal()
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")         # the Railway worker
    assert cli.worker_refusal() is None
    monkeypatch.delenv("RAILWAY_ENVIRONMENT")
    monkeypatch.setenv("ALLOW_REMOTE_WORKER", "1")                  # deliberate
    assert cli.worker_refusal() is None


def test_healthz_reports_the_schema_against_the_code(client):
    body = client.get("/healthz").json()
    assert body["schema"]["code"] == body["schema"]["db"] and body["schema"]["behind"] is False


def test_production_500s_never_show_exception_text(client, monkeypatch):
    from app.config import get_settings
    from app.main import app

    @app.get("/api/_boom_for_test")
    def boom():
        raise RuntimeError('column tenants.secret does not exist [SQL: SELECT tenants.secret FROM tenants]')

    monkeypatch.setattr(get_settings(), "env", "production")
    c = type(client)(app, raise_server_exceptions=False)
    r = c.get("/api/_boom_for_test")
    assert r.status_code == 500
    assert "SQL" not in r.text and "tenants" not in r.text and "Reference:" in r.json()["detail"]


def test_sign_in_does_not_load_the_ai_limit_column():
    """Code can deploy before migration 0006; nothing on the sign-in path may select it."""
    from sqlalchemy import select

    from app.models import Tenant
    sql = str(select(Tenant).compile())
    assert "ai_monthly_credit_limit" not in sql


def test_ocr_split_is_the_same_on_any_machine(monkeypatch):
    """OCR readings depend on which crops share a batch, so how the work is split must not
    depend on the machine's CPU count, and chunks must be whole batches."""
    import os

    from app import validation

    figs = {f"f{i}": {"id": f"f{i}", "kind": "number", "raw": "1", "status": "active",
                      "source": {"page": 1 + i // 30, "bbox": [0, 0, 1, 1]}} for i in range(2000)}
    seen = []

    class Pool:
        def submit(self, fn, pdf, chunk, per):
            seen.append([f["id"] for f in chunk])

            class F:
                def result(self):
                    return {}
            return F()

    for cores in (2, 10, 64):
        seen.clear()
        monkeypatch.setattr(os, "cpu_count", lambda c=cores: c)
        validation.reextraction_reads_parallel({"figures": figs}, b"", pool=Pool())
        if cores == 2:
            first = [list(c) for c in seen]
        assert seen == first
    assert len(first) == validation.REREAD_CHUNKS
    assert all(len(c) % validation.BATCH == 0 for c in first[:-1])


def test_stored_job_errors_never_hold_credentials(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "gemini_api_key", "AQ.secret-gemini-key-123")
    tb = ("HTTPStatusError: 401 for url 'https://x.googleapis.com/m:gen?key=AQ.secret-gemini-key-123' "
          "token=abc123 password=hunter2 AQ.secret-gemini-key-123")
    out = jobs.redact(tb)
    assert "secret-gemini" not in out and "abc123" not in out and "hunter2" not in out
    assert "key=[redacted]" in out


def test_keep_warm_pings_several_instances_at_once(monkeypatch):
    """One request at a time warms one serverless instance; the dashboard fires several
    requests at once after sign-in, so the pings must overlap."""
    import threading
    import time

    import httpx

    from app import domain_jobs
    from app.config import get_settings

    cfg = get_settings()
    monkeypatch.setattr(cfg, "keep_warm_url", "https://api.example.test/healthz")
    monkeypatch.setattr(cfg, "keep_warm_instances", 3)
    lock, live, peak, calls = threading.Lock(), [0], [0], []

    def fake_get(url, **kw):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
            calls.append(url)
        time.sleep(0.2)
        with lock:
            live[0] -= 1

    monkeypatch.setattr(httpx, "get", fake_get)
    domain_jobs.keep_api_warm()
    assert len(calls) == 3 and peak[0] == 3
