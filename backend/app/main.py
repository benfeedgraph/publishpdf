from __future__ import annotations

import logging
import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from sqlalchemy import text

from app import db
from app.api import admin_routes, auth_routes, report_routes, settings_routes, tenant_routes
from app.tenancy import system_context

_SECRET_IN_URL = re.compile(r"://[^\s/@]+:[^\s/@]+@")

logging.basicConfig(level=logging.INFO)

CSRF_HEADER = "x-ppdf-csrf"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


DB_DOWN = "The service can't reach its database right now. Please try again in a minute."


def _db_unreachable(exc: BaseException) -> bool:
    """A lost or refused database connection: the person gets a plain sentence (and a 503),
    not the server's address; the full error stays in the log."""
    from sqlalchemy.exc import DBAPIError, OperationalError
    if isinstance(exc, OperationalError):
        return True
    return isinstance(exc, DBAPIError) and bool(exc.connection_invalidated)


def _public_failure(exc: Exception) -> str:
    """A sentence safe to show in the browser. Connection strings stay out of it."""
    if isinstance(exc, ValidationError):
        parts = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err.get("loc", ()))
            msg = err.get("msg", "invalid")
            parts.append(f"{loc}: {msg}" if loc else msg)
        return "Server configuration error. " + "; ".join(parts)
    text_value = _SECRET_IN_URL.sub("://***:***@", str(exc)).replace("\n", " ")
    return f"{type(exc).__name__}: {text_value[:400]}"


def _schema_state() -> dict:
    """Database migration vs the newest migration shipped with this code. Nothing on Vercel
    runs migrations (the Railway worker does, on deploy), so code can go live ahead of its
    schema; "behind" makes that visible instead of a mystery 500."""
    from pathlib import Path

    versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    code = max((p.name.split("_", 1)[0] for p in versions.glob("[0-9][0-9][0-9][0-9]_*.py")), default=None)
    with db.session(system_context()) as s:
        current = s.scalar(text("SELECT version_num FROM alembic_version"))
    return {"db": current, "code": code, "behind": bool(code and current and current < code)}


def create_app() -> FastAPI:
    app = FastAPI(title="PublishPDF API", version="0.1.0")
    from starlette.middleware.gzip import GZipMiddleware
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)   # report previews are large HTML

    @app.exception_handler(Exception)
    async def show_failure(request: Request, exc: Exception):
        if isinstance(exc, HTTPException):
            return await http_exception_handler(request, exc)
        import uuid as _uuid
        ref = _uuid.uuid4().hex[:8]
        logging.getLogger("app").exception("request failed (ref %s)", ref)
        if _db_unreachable(exc):
            return JSONResponse({"detail": DB_DOWN}, status_code=503, headers={"Retry-After": "30"})
        from app.config import get_settings
        if get_settings().env == "development" or isinstance(exc, ValidationError):
            return JSONResponse({"detail": _public_failure(exc)}, status_code=500)
        # Production never shows exception text (it can carry SQL and schema details);
        # the reference finds the full traceback in the server log.
        return JSONResponse({"detail": f"Something went wrong on our side. Please try again in a moment. (Reference: {ref})"},
                            status_code=500)

    @app.middleware("http")
    async def csrf_and_headers(request: Request, call_next):
        # Session auth is a cookie, so every state-changing request must carry a custom
        # header. Browsers won't send one cross-site without a CORS preflight, which
        # this API never grants.
        if request.method in UNSAFE_METHODS and request.url.path.startswith("/api/") \
                and request.headers.get(CSRF_HEADER) != "1":
            return JSONResponse({"detail": "Missing CSRF header."}, status_code=403)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/healthz")
    def healthz() -> dict:
        import time
        with db.session(system_context()) as s:
            s.execute(text("SELECT 1"))
            t0 = time.perf_counter()
            s.execute(text("SELECT 1"))
            db_ms = round((time.perf_counter() - t0) * 1000)   # one round trip, API host -> database
        from app import storage
        import os
        from app.config import get_settings
        from app import jobs
        try:
            queue = jobs.queue_health()
        except Exception:  # noqa: BLE001 - the queue report must never take the health check down
            logging.getLogger("app").exception("queue health failed")
            queue = None
        # Stays HTTP 200 when the queue is stalled: the API itself is up, and Railway's
        # deploy health check uses this route. Monitors should alert on "status".
        try:
            schema = _schema_state()
        except Exception:  # noqa: BLE001
            logging.getLogger("app").exception("schema state failed")
            schema = None
        status = "degraded" if (queue and queue["stalled"]) or (schema and schema["behind"]) else "ok"
        # Deployment facts for diagnosing a host's setup: names and yes/no only, never values.
        return {"status": status, "schema": schema, "db_roundtrip_ms": db_ms, "region": os.environ.get("VERCEL_REGION"),
                "queue": queue, "storage": storage.backend_kind(),
                "blob_token": bool(get_settings().blob_read_write_token),
                "blob_store_id": bool(os.environ.get("BLOB_STORE_ID")),       # a store connected via OIDC only
                "storage_backend_setting": get_settings().storage_backend or None,
                "commit": (os.environ.get("VERCEL_GIT_COMMIT_SHA") or "")[:7] or None,
                "vercel_env": os.environ.get("VERCEL_ENV")}

    @app.get("/api/schema/report.schema.json")
    def report_json_schema() -> Response:
        """The published JSON Schema for report documents (brief §4.3)."""
        from app.extraction.jsonschema_check import SCHEMA_PATH
        return Response(SCHEMA_PATH.read_text(), media_type="application/schema+json")

    # Preview sites by path (<platform>/sites/<slug>/...): the hosted platform has no
    # preview hosts or separate site server, so the API serves them too.
    @app.api_route("/sites/{slug}", methods=["GET", "HEAD"], include_in_schema=False)
    def site_root(slug: str) -> Response:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(f"/sites/{slug}/", status_code=301)

    @app.api_route("/sites/{slug}/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def site_page(slug: str, path: str, request: Request) -> Response:
        from app import public
        return public.serve_by_path(slug, path, request)

    app.include_router(auth_routes.router)
    app.include_router(tenant_routes.router)
    app.include_router(report_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(admin_routes.router)
    return app


app = create_app()
