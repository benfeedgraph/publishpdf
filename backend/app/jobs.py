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
STALE_LOCK = timedelta(minutes=30)


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

_RECOVER_STALE = text("""
    UPDATE jobs SET status = 'queued', locked_at = NULL, locked_by = NULL
    WHERE status = 'running' AND locked_at < now() - make_interval(secs => :secs)
""")


def run_one(worker_id: str) -> bool:
    """Claim and run a single job. Returns False when the queue is empty."""
    with db.session(system_context()) as s:
        s.execute(_RECOVER_STALE, {"secs": STALE_LOCK.total_seconds()})
        row = s.execute(_CLAIM, {"worker": worker_id}).mappings().first()
    if row is None:
        return False

    job_id: uuid.UUID = row["id"]
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


def _record_failure(ctx: Context, job_id: uuid.UUID, attempts: int, max_attempts: int,
                    exc: Exception) -> None:
    detail = "".join(traceback.format_exception(exc))[-8000:]
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


def work_forever() -> None:  # pragma: no cover - process entrypoint
    worker_id = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
    poll = get_settings().job_poll_seconds
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
