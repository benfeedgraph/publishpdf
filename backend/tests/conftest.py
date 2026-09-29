"""Test harness. Needs the dev cluster: `scripts/dev-db.sh init`.

The schema is dropped and re-migrated once per test session. Tests don't clean up
after themselves (audit_log is append-only by design, even for tests) — each test
creates its own uniquely-named tenants and users instead.
"""

from __future__ import annotations

import os
import re
import tempfile
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet

_PORT = os.environ.get("PGPORT", "54329")
os.environ.update({
    "APP_ENV": "test",
    "DATABASE_OWNER_URL": f"postgresql+psycopg://publishpdf_owner:owner_dev_pw@localhost:{_PORT}/publishpdf_test",
    "DATABASE_APP_URL": f"postgresql+psycopg://publishpdf_app:app_dev_pw@localhost:{_PORT}/publishpdf_test",
    "PLATFORM_DOMAIN": "platform.example.com",
    "PLATFORM_TARGET_HOSTNAME": "edge.platform.example.com",
    "PREVIEW_URL_PATTERN": "{tenant_slug}.preview.platform.example.com",
    "VERIFY_PREFIX": "ppdftest",
    "DASHBOARD_BASE_URL": "http://dashboard.example.com",
    "APP_SECRET_KEY": Fernet.generate_key().decode(),
    "EMAIL_BACKEND": "console",
    "STORAGE_BACKEND": "local",
    "STORAGE_LOCAL_ROOT": tempfile.mkdtemp(prefix="ppdf-storage-"),
    # pin everything a developer's ../.env might set
    "PUBLIC_SCHEME": "https", "PUBLIC_PORT_SUFFIX": "", "TLS_MODE": "caddy",
    "DISCLAIMER_DEFAULT_TEXT": "Test disclaimer: the PDF filing is the official document.",
    "INTERNAL_API_TOKEN": "test-internal-token", "ANTHROPIC_API_KEY": "", "COOKIE_SECURE": "false",
    "MAX_UPLOAD_MB": "100", "MFA_REQUIRED_FOR_ADMINS": "true", "ALLOW_SELF_SIGNUP": "true", "MAX_PAGES": "600", "OCR_CONFIDENCE_THRESHOLD": "0.95",
})

import pyotp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from app import db, emailer  # noqa: E402
from app.models import Membership, Tenant, User  # noqa: E402
from app.tenancy import Role, system_context  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def migrated_db():
    owner = create_engine(os.environ["DATABASE_OWNER_URL"], isolation_level="AUTOCOMMIT")
    with owner.connect() as c:
        c.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
        c.execute(text("GRANT USAGE ON SCHEMA public TO publishpdf_app"))
    owner.dispose()
    from alembic import command
    from alembic.config import Config

    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "migrations"))
    command.upgrade(cfg, "head")
    yield


def uniq(prefix: str = "t") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def make_tenant(name: str | None = None) -> Tenant:
    slug = uniq("tenant")
    with db.session(system_context()) as s:
        t = Tenant(slug=slug, name=name or slug)
        s.add(t)
        s.flush()
        s.expunge(t)
        return t


def make_user(email: str | None = None, *, platform_admin: bool = False) -> User:
    with db.session(system_context()) as s:
        u = User(email=email or f"{uniq('user')}@example.com", is_platform_admin=platform_admin)
        s.add(u)
        s.flush()
        s.expunge(u)
        return u


def add_member(tenant: Tenant, user: User, role: Role) -> None:
    with db.session(system_context()) as s:
        s.add(Membership(tenant_id=tenant.id, user_id=user.id, role=role.value))


def last_link(to: str, path: str) -> str:
    for mail in reversed(emailer.OUTBOX):
        if mail.to == to:
            m = re.search(r"https?://\S+", mail.body)
            if m and urlparse(m.group(0)).path == path:
                return parse_qs(urlparse(m.group(0)).query)["token"][0]
    raise AssertionError(f"no {path} email to {to}")


@pytest.fixture
def client() -> TestClient:
    c = TestClient(__import__("app.main", fromlist=["app"]).app)
    c.headers["x-ppdf-csrf"] = "1"
    return c


def sign_in(client: TestClient, user: User) -> None:
    """Magic-link sign-in; completes MFA (enrolling if needed) when the user needs it."""
    client.post("/api/auth/login", json={"email": user.email})
    token = last_link(user.email, "/auth/verify")
    assert client.post("/api/auth/verify", json={"token": token}).status_code == 200
    me = client.get("/api/auth/me").json()
    if me["mfa"]["required"]:
        complete_mfa(client, me)


_SECRETS: dict[str, str] = {}


def complete_mfa(client: TestClient, me: dict) -> None:
    email = me["user"]["email"]
    if not me["mfa"]["enrolled"]:
        uri = client.post("/api/auth/mfa/enroll").json()["otpauth_uri"]
        _SECRETS[email] = parse_qs(urlparse(uri).query)["secret"][0]
        r = client.post("/api/auth/mfa/confirm", json={"code": pyotp.TOTP(_SECRETS[email]).now()})
    else:
        r = client.post("/api/auth/mfa/verify", json={"code": pyotp.TOTP(_SECRETS[email]).now()})
    assert r.status_code == 200, r.text


def totp_secret(email: str) -> str:
    return _SECRETS[email]
