from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text

from app import db
from app.api import admin_routes, auth_routes, report_routes, settings_routes, tenant_routes
from app.tenancy import system_context

logging.basicConfig(level=logging.INFO)

CSRF_HEADER = "x-ppdf-csrf"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def create_app() -> FastAPI:
    app = FastAPI(title="PublishPDF API", version="0.1.0")

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
