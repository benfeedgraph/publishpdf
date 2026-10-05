"""ORM mappings. The schema itself (including RLS policies and triggers) is owned by
the Alembic migrations; these classes must mirror it."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TZ = TIMESTAMP(timezone=True)


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))


def _now() -> Mapped[datetime]:
    return mapped_column(TZ, server_default=text("now()"))


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[uuid.UUID] = _uuid_pk()
    slug: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="active")
    created_at: Mapped[datetime] = _now()
    ai_monthly_credit_limit: Mapped[int | None] = mapped_column(Integer)   # None = no limit


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str | None] = mapped_column(Text)
    is_platform_admin: Mapped[bool] = mapped_column(server_default=text("false"))
    totp_secret_enc: Mapped[str | None] = mapped_column(Text)
    totp_enabled_at: Mapped[datetime | None] = mapped_column(TZ)
    disabled_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = _now()


class Membership(Base):
    __tablename__ = "memberships"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    role: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class Invite(Base):
    __tablename__ = "invites"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    email: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    invited_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(TZ)
    accepted_at: Mapped[datetime | None] = mapped_column(TZ)
    revoked_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = _now()


class LoginToken(Base):
    __tablename__ = "login_tokens"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    expires_at: Mapped[datetime] = mapped_column(TZ)
    used_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = _now()


class AuthSession(Base):
    __tablename__ = "sessions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    mfa_verified_at: Mapped[datetime | None] = mapped_column(TZ)
    mfa_failed_attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    expires_at: Mapped[datetime] = mapped_column(TZ)
    revoked_at: Mapped[datetime | None] = mapped_column(TZ)
    ip: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class AuditEntry(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id"))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(Text)
    target_type: Mapped[str | None] = mapped_column(Text)
    target_id: Mapped[str | None] = mapped_column(Text)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ip: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = _now()


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    idempotency_key: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="queued")
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, server_default=text("3"))
    run_after: Mapped[datetime] = _now()
    locked_at: Mapped[datetime | None] = mapped_column(TZ)
    locked_by: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    error_plain: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()
    started_at: Mapped[datetime | None] = mapped_column(TZ)
    finished_at: Mapped[datetime | None] = mapped_column(TZ)
    version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


# ----------------------------------------------------------------------- Phase 1+


class SourceFile(Base):
    __tablename__ = "source_files"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    sha256: Mapped[str] = mapped_column(Text)
    storage_key: Mapped[str] = mapped_column(Text)
    original_filename: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    page_count: Mapped[int] = mapped_column(Integer)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _now()


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    company_name: Mapped[str] = mapped_column(Text)
    report_type: Mapped[str] = mapped_column(Text)
    fiscal_year: Mapped[int] = mapped_column(Integer)
    period: Mapped[str] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(Text)
    reporting_unit: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    theme_override: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    live_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    deleted_at: Mapped[datetime | None] = mapped_column(TZ)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _now()


class ReportVersion(Base):
    __tablename__ = "report_versions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    report_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reports.id"))
    version_no: Mapped[int] = mapped_column(Integer)
    source_file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source_files.id"))
    source_sha256: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="processing")
    stage: Mapped[str | None] = mapped_column(Text)
    error_plain: Mapped[str | None] = mapped_column(Text)
    created_from_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    extraction_key: Mapped[str | None] = mapped_column(Text)
    schema_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    schema_sha256: Mapped[str | None] = mapped_column(Text)
    bundle_key: Mapped[str | None] = mapped_column(Text)
    bundle_sha256: Mapped[str | None] = mapped_column(Text)
    theme_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    validation_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    validated_schema_sha256: Mapped[str | None] = mapped_column(Text)
    validated_bundle_sha256: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(TZ)
    published_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    review_confirmed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _now()


class ValidationIssue(Base):
    __tablename__ = "validation_issues"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("report_versions.id"))
    run_no: Mapped[int] = mapped_column(Integer)
    check_name: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    fid: Mapped[str | None] = mapped_column(Text)
    page: Mapped[int | None] = mapped_column(Integer)
    section_id: Mapped[str | None] = mapped_column(Text)
    bbox: Mapped[list[float] | None] = mapped_column(JSONB)
    message: Mapped[str] = mapped_column(Text)
    expected: Mapped[str | None] = mapped_column(Text)
    actual: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="open")
    resolution: Mapped[str | None] = mapped_column(Text)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = _now()


class FigureReview(Base):
    __tablename__ = "figure_reviews"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("report_versions.id"))
    fid: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    old_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    new_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    note: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    at: Mapped[datetime] = _now()


class Comment(Base):
    __tablename__ = "comments"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("report_versions.id"))
    section_id: Mapped[str] = mapped_column(Text)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(TZ)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _now()


class TenantSettings(Base):
    __tablename__ = "tenant_settings"
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    theme: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    theme_mode: Mapped[str | None] = mapped_column(Text)
    disclaimer: Mapped[str | None] = mapped_column(Text)
    robots_policy: Mapped[str] = mapped_column(Text, server_default="allow_all")
    ga4_measurement_id: Mapped[str | None] = mapped_column(Text)
    consent_banner_enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    consent_banner_off_ack: Mapped[str | None] = mapped_column(Text)
    llm_assist_enabled: Mapped[bool] = mapped_column(server_default=text("false"))
    updated_at: Mapped[datetime] = _now()


class Domain(Base):
    __tablename__ = "domains"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    hostname: Mapped[str] = mapped_column(Text, unique=True)
    verify_token: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    failure_code: Mapped[str | None] = mapped_column(Text)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(TZ)
    verified_at: Mapped[datetime | None] = mapped_column(TZ)
    live_at: Mapped[datetime | None] = mapped_column(TZ)
    cert_expires_at: Mapped[datetime | None] = mapped_column(TZ)
    last_alert_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = _now()


class EdgeHit(Base):
    __tablename__ = "edge_hits"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    host: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text)
    report_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[int] = mapped_column(Integer)
    agent_class: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = _now()


class EmailCode(Base):
    __tablename__ = "email_codes"
    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(Text)
    code_hash: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    expires_at: Mapped[datetime] = mapped_column(TZ)
    used_at: Mapped[datetime | None] = mapped_column(TZ)
    ip: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class SignupToken(Base):
    __tablename__ = "signup_tokens"
    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(Text)
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    expires_at: Mapped[datetime] = mapped_column(TZ)
    used_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = _now()
