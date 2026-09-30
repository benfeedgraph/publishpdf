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


def create_app() -> FastAPI:
    app = FastAPI(title="PublishPDF API", version="0.1.0")

    @app.exception_handler(Exception)
    async def show_failure(request: Request, exc: Exception):
        if isinstance(exc, HTTPException):
            return await http_exception_handler(request, exc)
        logging.getLogger("app").exception("request failed")
        if _db_unreachable(exc):
            return JSONResponse({"detail": DB_DOWN}, status_code=503, headers={"Retry-After": "30"})
        return JSONResponse({"detail": _public_failure(exc)}, status_code=500)

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
    def healthz() -> dict[str, str]:
        with db.session(system_context()) as s:
            s.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.get("/api/schema/report.schema.json")
    def report_json_schema() -> Response:
        """The published JSON Schema for report documents (brief §4.3)."""
        from app.extraction.jsonschema_check import SCHEMA_PATH
        return Response(SCHEMA_PATH.read_text(), media_type="application/schema+json")

    app.include_router(auth_routes.router)
    app.include_router(tenant_routes.router)
    app.include_router(report_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(admin_routes.router)
    return app


app = create_app()
