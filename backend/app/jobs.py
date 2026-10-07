"""Postgres-backed job queue.

- Enqueue happens inside the caller's transaction, so a job exists iff the change
  that needs it committed.
- (tenant_id, idempotency_key) is unique: enqueueing the same work twice returns
  the existing job instead of running it twice. Pipeline stages derive the key from
  their input hashes + stage version, which is what makes re-runs idempotent.
- Claiming uses FOR UPDATE SKIP LOCKED, so any number of workers can poll.
- Handlers run under worker_context(tenant_id): the same RLS as a user in that tenant.
"""

from __future__ import annotations

import logging
import socket
import time
import traceback
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import db
from app.config import get_settings
from app.models import Job
from app.tenancy import Context, system_context, worker_context

log = logging.getLogger(__name__)

Handler = Callable[[Context, dict[str, Any]], dict[str, Any] | None]
_HANDLERS: dict[str, Handler] = {}

GENERIC_FAILURE = "Something went wrong while processing this step. Our team has been notified; you can retry."
# A running job's lock is refreshed every HEARTBEAT; a lock older than STALE_LOCK means its
# worker died, and any other worker takes the job back.
HEARTBEAT = 30.0
STALE_LOCK = timedelta(minutes=3)


class UserFacingError(Exception):
    """Raise from a handler to show `message` to the client verbatim (plain language).
    `retryable=False` fails the job immediately instead of retrying."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


def handler(kind: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        if kind in _HANDLERS:
            raise RuntimeError(f"duplicate job handler for {kind!r}")
        _HANDLERS[kind] = fn
        return fn
    return register


def enqueue(s: Session, ctx: Context, kind: str, payload: dict[str, Any], *,
            idempotency_key: str, max_attempts: int | None = None) -> Job:
    tenant_id = ctx.require_tenant()
    stmt = (
        insert(Job)
        .values(tenant_id=tenant_id, kind=kind, payload=payload, idempotency_key=idempotency_key,
                max_attempts=max_attempts or get_settings().job_max_attempts)
        .on_conflict_do_nothing(index_elements=["tenant_id", "idempotency_key"])
    )
    s.execute(stmt)
    return s.scalars(select(Job).where(Job.tenant_id == tenant_id,
                                       Job.idempotency_key == idempotency_key)).one()


def requeue(s: Session, job: Job) -> None:
    """Re-run a failed/cancelled job (platform-admin "re-run" action)."""
    job.status = "queued"
    job.attempts = 0
    job.run_after = s.scalar(text("SELECT now()"))
    job.last_error = None
    job.error_plain = None
    job.finished_at = None


_CLAIM = text("""
    UPDATE jobs SET status = 'running', attempts = attempts + 1, locked_at = now(),
                    locked_by = :worker, started_at = coalesce(started_at, now())
    WHERE id = (
        SELECT id FROM jobs
        WHERE status = 'queued' AND run_after <= now()
        ORDER BY run_after
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, tenant_id, kind, payload, attempts, max_attempts
""")

# A job whose worker died goes back to the queue — unless it has used all its attempts
# (a report that kills the worker every time would otherwise loop forever).
_STOPPED = ("Processing stopped because the server ran out of resources on this report. "
            "Our team has been notified; you can retry.")
_RECOVER_STALE = text("""
    WITH recovered AS (
      UPDATE jobs SET
        status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
        finished_at = CASE WHEN attempts >= max_attempts THEN now() ELSE NULL END,
        error_plain = CASE WHEN attempts >= max_attempts THEN :plain ELSE error_plain END,
        last_error = CASE WHEN attempts >= max_attempts
                          THEN 'Its worker stopped responding mid-job on every attempt (the container was '
                               || 'restarted or killed — usually out of memory).' ELSE last_error END,
        locked_at = NULL, locked_by = NULL
      WHERE status = 'running' AND locked_at < now() - make_interval(secs => :secs)
      RETURNING kind, status, version_id)
    -- a pipeline step that gave up must not leave its report "processing" forever
    UPDATE report_versions v SET status = 'failed', error_plain = :plain
    FROM recovered r
    WHERE r.status = 'failed' AND r.kind LIKE 'pipeline.%' AND v.id = r.version_id
      AND v.published_at IS NULL AND v.status = 'processing'
""")


def run_one(worker_id: str) -> bool:
    """Claim and run a single job. Returns False when the queue is empty."""
    with db.session(system_context()) as s:
        s.execute(_RECOVER_STALE, {"secs": STALE_LOCK.total_seconds(), "plain": _STOPPED})
        row = s.execute(_CLAIM, {"worker": worker_id}).mappings().first()
    if row is None:
        return False

    job_id: uuid.UUID = row["id"]
    _CURRENT.update(id=job_id, since=time.monotonic(), worker=worker_id)
    try:
        return _run_claimed(row, job_id)
    finally:
        _CURRENT.clear()


def _run_claimed(row, job_id: uuid.UUID) -> bool:
    ctx = worker_context(row["tenant_id"])
    fn = _HANDLERS.get(row["kind"])
    log.info("running %s (job %s, attempt %s)", row["kind"], job_id, row["attempts"])
    try:
        if fn is None:
            raise UserFacingError(GENERIC_FAILURE, retryable=False)
        result = fn(ctx, row["payload"]) or {}
    except Exception as exc:  # noqa: BLE001 - every failure must be recorded
        _record_failure(ctx, job_id, row["attempts"], row["max_attempts"], exc)
        return True

    with db.session(ctx) as s:
        s.execute(text("""UPDATE jobs SET status = 'succeeded', result = CAST(:result AS jsonb),
                          finished_at = now(), locked_at = NULL, locked_by = NULL WHERE id = :id"""),
                  {"id": job_id, "result": _json(result)})
    return True


_SECRET_PARAM = __import__("re").compile(r"(?i)\b(key|api_key|token|access_token|secret|password)=([^&\s'\"]+)")


def redact(text_: str) -> str:
    """Job errors are stored and shown to platform admins: no credentials in them."""
    cfg = get_settings()
    for secret in (cfg.gemini_api_key, cfg.anthropic_api_key, cfg.blob_read_write_token, cfg.app_secret_key):
        if secret and len(secret) >= 8:
            text_ = text_.replace(secret, "[redacted]")
    return _SECRET_PARAM.sub(r"\1=[redacted]", text_)


def _record_failure(ctx: Context, job_id: uuid.UUID, attempts: int, max_attempts: int,
                    exc: Exception) -> None:
    detail = redact("".join(traceback.format_exception(exc)))[-8000:]
    if isinstance(exc, UserFacingError):
        plain, retry = exc.message, exc.retryable and attempts < max_attempts
    else:
        plain, retry = GENERIC_FAILURE, attempts < max_attempts
    log.warning("job %s failed (attempt %s/%s): %s", job_id, attempts, max_attempts, exc)
    with db.session(ctx) as s:
        if retry:
            s.execute(text("""UPDATE jobs SET status = 'queued', last_error = :err, error_plain = :plain,
                              locked_at = NULL, locked_by = NULL,
                              run_after = now() + make_interval(secs => :backoff) WHERE id = :id"""),
                      {"id": job_id, "err": detail, "plain": plain, "backoff": 2 ** attempts * 5})
        else:
            s.execute(text("""UPDATE jobs SET status = 'failed', last_error = :err, error_plain = :plain,
                              finished_at = now(), locked_at = NULL, locked_by = NULL WHERE id = :id"""),
                      {"id": job_id, "err": detail, "plain": plain})
            # A pipeline step that has given up must not leave its report version
            # "processing" forever: mark it failed so the client sees why and can retry.
            s.execute(text("""UPDATE report_versions v SET status = 'failed', error_plain = :plain
                              FROM jobs j WHERE j.id = :id AND j.kind LIKE 'pipeline.%'
                                AND v.id = j.version_id AND v.published_at IS NULL AND v.status = 'processing'"""),
                      {"id": job_id, "plain": plain})


def _json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, default=str)


PERIODIC: list[tuple[float, Callable[[], object]]] = []   # (interval seconds, fn)


def periodic(interval: float) -> Callable[[Callable[[], object]], Callable[[], object]]:
    def register(fn: Callable[[], object]) -> Callable[[], object]:
        PERIODIC.append((interval, fn))
        return fn
    return register


_CURRENT: dict[str, Any] = {}          # the job this worker slot is running: {"id", "since", "worker"}

# A worker slot beats every HEARTBEAT whether busy or idle; one not seen for WORKER_ALIVE
# is treated as gone. Lets /healthz and the progress screen tell "no worker is running"
# apart from "the queue is empty".
WORKER_ALIVE = timedelta(seconds=90)
# A job queued longer than this with no live worker is reported as stalled.
QUEUE_STALL = timedelta(minutes=2)


def beat(worker_id: str) -> None:
    with db.session(system_context()) as s:
        s.execute(text("""INSERT INTO worker_heartbeats (worker_id) VALUES (:w)
                          ON CONFLICT (worker_id) DO UPDATE SET seen_at = now()"""), {"w": worker_id})
        s.execute(text("DELETE FROM worker_heartbeats WHERE seen_at < now() - interval '1 day'"))


_QUEUE_HEALTH = text("""
    SELECT q.queued, q.running, q.oldest, hb.live, hb.last,
           extract(epoch FROM now() - CAST(:since AS timestamptz)) AS since
    FROM (SELECT count(*) FILTER (WHERE status = 'queued' AND run_after <= now()) AS queued,
                 count(*) FILTER (WHERE status = 'running') AS running,
                 extract(epoch FROM now() - min(run_after) FILTER (
                     WHERE status = 'queued' AND run_after <= now())) AS oldest
          FROM jobs WHERE status IN ('queued', 'running')) q,
         (SELECT count(*) FILTER (WHERE seen_at > now() - make_interval(secs => :alive)) AS live,
                 extract(epoch FROM now() - max(seen_at)) AS last
          FROM worker_heartbeats) hb
""")


def queue_health(waiting_since: Any = None) -> dict[str, Any]:
    """Counts only (no tenant data): live workers, queue depth, and how long the oldest
    queued job has waited. `stalled` = work is waiting and nothing is taking it.
    One statement: /healthz and the progress screen poll this. With `waiting_since`,
    also returns `waiting_seconds` (server clock, so the browser's clock never matters)."""
    with db.session(system_context()) as s:
        q = s.execute(_QUEUE_HEALTH, {"alive": WORKER_ALIVE.total_seconds(), "since": waiting_since}).one()
    oldest = round(q.oldest) if q.oldest is not None else None
    stalled = bool(q.queued) and not q.live and (oldest or 0) > QUEUE_STALL.total_seconds()
    out = {"workers_alive": q.live, "worker_last_seen_seconds": round(q.last) if q.last is not None else None,
           "queued": q.queued, "running": q.running, "oldest_queued_seconds": oldest, "stalled": stalled}
    if waiting_since is not None:
        out["waiting_seconds"] = max(0, round(q.since or 0))
    return out


def _watchdog() -> None:  # pragma: no cover - runs in the worker process
    """Keeps the running job's lock fresh, and stops a job that runs past the time limit:
    it's recorded as failed (so its report shows why) and the slot exits to be restarted."""
    import os
    import threading

    limit = get_settings().job_timeout_seconds

    def loop() -> None:
        while True:
            time.sleep(HEARTBEAT)
            try:
                beat(_WORKER_ID[0])
            except Exception:  # noqa: BLE001
                log.exception("worker heartbeat failed")
            cur = dict(_CURRENT)
            if not cur:
                continue
            try:
                with db.session(system_context()) as s:
                    s.execute(text("UPDATE jobs SET locked_at = now() WHERE id = :id AND locked_by = :w"),
                              {"id": cur["id"], "w": cur["worker"]})
            except Exception:  # noqa: BLE001
                log.exception("heartbeat failed")
            if time.monotonic() - cur["since"] > limit:
                log.error("job %s exceeded %ss; stopping this worker slot", cur["id"], limit)
                try:
                    with db.session(system_context()) as s:
                        s.execute(text("""UPDATE jobs SET status = 'failed', finished_at = now(), locked_at = NULL,
                                          locked_by = NULL, error_plain = :plain, last_error = :err WHERE id = :id"""),
                                  {"id": cur["id"], "plain": "This step took too long and was stopped. Retry it; "
                                   "if it happens again, contact support.", "err": f"timeout after {limit}s"})
                        s.execute(text("""UPDATE report_versions v SET status = 'failed',
                                          error_plain = 'Processing took too long and was stopped. Try again.'
                                          FROM jobs j WHERE j.id = :id AND v.id = j.version_id
                                          AND v.published_at IS NULL AND v.status = 'processing'"""), {"id": cur["id"]})
                finally:
                    os._exit(3)

    threading.Thread(target=loop, daemon=True, name="job-watchdog").start()


_WORKER_ID: list[str] = [""]


def new_worker_id() -> str:
    return f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"


def work_forever(worker_id: str | None = None) -> None:  # pragma: no cover - process entrypoint
    worker_id = worker_id or new_worker_id()
    _WORKER_ID[0] = worker_id
    poll = get_settings().job_poll_seconds
    try:
        beat(worker_id)
    except Exception:  # noqa: BLE001 - e.g. migration 0005 not applied yet; jobs still run
        log.exception("worker heartbeat failed")
    _watchdog()
    log.info("worker %s started; handlers: %s", worker_id, sorted(_HANDLERS))
    last: dict[int, float] = {}
    while True:
        now = time.monotonic()
        for i, (interval, fn) in enumerate(PERIODIC):
            if now - last.get(i, 0) >= interval:
                last[i] = now
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    log.exception("periodic task %s failed", getattr(fn, "__name__", fn))
        if not run_one(worker_id):
            time.sleep(poll)


def _slot(worker_id: str) -> None:  # pragma: no cover - child process entrypoint
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from app import domain_jobs, pipeline  # noqa: F401 - registers handlers
    work_forever(worker_id)


def exit_reason(code: int | None) -> str:
    """Why a worker slot process ended, in words."""
    import signal
    if code is not None and code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = f"signal {-code}"
        if -code == signal.SIGKILL:
            return f"the worker process was killed ({name}) — almost always the container running out of memory"
        return f"the worker process was stopped by {name}"
    return f"the worker process exited unexpectedly (exit code {code})"


def release_dead_slot(worker_id: str, code: int | None) -> int:
    """The job a dead slot was running goes straight back to the queue (or fails, once out
    of attempts) instead of waiting STALE_LOCK for its lock to go stale. The reason is
    recorded on the job, so a crash is visible without the host's logs."""
    reason = exit_reason(code)
    with db.session(system_context()) as s:
        rows = s.execute(text("""
            UPDATE jobs SET
              status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
              finished_at = CASE WHEN attempts >= max_attempts THEN now() ELSE NULL END,
              locked_at = NULL, locked_by = NULL,
              last_error = :reason,
              error_plain = CASE WHEN attempts >= max_attempts THEN :plain ELSE error_plain END
            WHERE status = 'running' AND locked_by = :wid
            RETURNING id, status, version_id, kind"""),
            {"wid": worker_id, "reason": f"Worker slot {worker_id}: {reason}.",
             "plain": "Processing stopped because the server ran out of resources on this report. "
                      "Our team has been notified; you can retry."}).all()
        for r in rows:
            if r.status == "failed" and r.kind.startswith("pipeline.") and r.version_id:
                s.execute(text("""UPDATE report_versions SET status = 'failed', error_plain = :plain
                                  WHERE id = :vid AND published_at IS NULL AND status = 'processing'"""),
                          {"vid": r.version_id, "plain": "Processing stopped because the server ran out of resources "
                                                         "on this report. Our team has been notified; you can retry."})
    return len(rows)


def supervise(concurrency: int) -> None:  # pragma: no cover - process entrypoint
    """Runs `concurrency` worker slots, each its own process, and restarts any slot that
    exits (a crash, or a job stopped for running too long). Slots are not daemonic, so a
    slot can still build a large report's pages in parallel."""
    import multiprocessing as mp
    import signal

    spawn = mp.get_context("spawn")
    slots: list = []

    def start():
        wid = new_worker_id()
        p = spawn.Process(target=_slot, args=(wid,), name="job-slot")
        p.start()
        p.worker_id = wid
        return p

    def stop(*_a):
        for p in slots:
            p.terminate()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    slots = [start() for _ in range(max(1, concurrency))]
    log.info("supervising %s worker slots", len(slots))
    while True:
        time.sleep(2)
        for i, p in enumerate(slots):
            if not p.is_alive():
                log.warning("worker slot %s exited (%s); restarting", p.pid, exit_reason(p.exitcode))
                try:
                    if release_dead_slot(p.worker_id, p.exitcode):
                        log.warning("released the job worker slot %s was running", p.worker_id)
                except Exception:  # noqa: BLE001 - stale-lock recovery still catches it later
                    log.exception("could not release worker slot %s's job", p.worker_id)
                slots[i] = start()
