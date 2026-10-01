"""Runtime configuration. Every value comes from the environment (see ../.env.example).

Nothing product-defining is hardcoded here: domains, limits and thresholds are all
placeholders the operator must confirm.
"""

from functools import lru_cache

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Hosts that exist on a laptop or inside Railway, and nowhere Vercel can dial.
_PRIVATE_DB_MARKERS = ("localhost", "127.0.0.1", "0.0.0.0", "railway.internal")


def _private_database_host(url: str) -> bool:
    return any(marker in url for marker in _PRIVATE_DB_MARKERS)


def normalize_database_url(url: str) -> str:
    """Accept Railway's postgresql:// URL and talk to Postgres with psycopg 3.

    ``postgresql://`` makes SQLAlchemy look for psycopg2, which this app does not
    install. Remote hosts also need TLS; the local test database does not.
    """
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql+psycopg2://"):
        url = "postgresql+psycopg://" + url[len("postgresql+psycopg2://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    if not _private_database_host(url) and "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


def choose_database_url(*candidates: str) -> str:
    """Pick a URL Vercel can open. A public URL wins over localhost or Railway's private host."""
    cleaned = [c.strip().strip('"').strip("'") for c in candidates if c and c.strip()]
    if not cleaned:
        raise ValueError(
            "No database URL is set. On the publishpdf API project, set DATABASE_APP_URL and "
            "DATABASE_OWNER_URL to Railway's public URL (the host looks like proxy.rlwy.net)."
        )
    public = [c for c in cleaned if not _private_database_host(c)]
    chosen = public[0] if public else cleaned[0]
    if "railway.internal" in chosen:
        raise ValueError(
            "postgres.railway.internal only works inside Railway. Use the public database URL "
            "(DATABASE_PUBLIC_URL), whose host looks like proxy.rlwy.net."
        )
    return normalize_database_url(chosen)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    env: str = Field(default="development", alias="APP_ENV")

    # --- Database -------------------------------------------------------------
    # Owner role: runs migrations, owns tables. Never used by the app at runtime.
    # Empty defaults let a Railway-style DATABASE_URL fill these in (see the validator).
    database_owner_url: str = Field(default="", alias="DATABASE_OWNER_URL")
    # App role: NOT superuser, NOT table owner -> Row-Level Security applies to it.
    database_app_url: str = Field(default="", alias="DATABASE_APP_URL")
    database_public_url: str = Field(default="", alias="DATABASE_PUBLIC_URL")
    database_url: str = Field(default="", alias="DATABASE_URL")
    postgres_url: str = Field(default="", alias="POSTGRES_URL")

    @model_validator(mode="after")
    def _fill_database_urls(self) -> "Settings":
        fallbacks = (self.database_public_url, self.database_url, self.postgres_url)
        self.database_app_url = choose_database_url(self.database_app_url, *fallbacks)
        self.database_owner_url = choose_database_url(self.database_owner_url, *fallbacks)
        return self

    # --- Platform domains (placeholders; confirm before production) ------------
    platform_domain: str = Field(alias="PLATFORM_DOMAIN")
    platform_target_hostname: str = Field(alias="PLATFORM_TARGET_HOSTNAME")
    preview_url_pattern: str = Field(alias="PREVIEW_URL_PATTERN")
    verify_prefix: str = Field(alias="VERIFY_PREFIX")
    dashboard_base_url: str = Field(alias="DASHBOARD_BASE_URL")

    # --- Auth -----------------------------------------------------------------
    # Fernet key (urlsafe base64, 32 bytes) used to encrypt TOTP secrets at rest.
    app_secret_key: str = Field(alias="APP_SECRET_KEY")
    session_cookie_name: str = Field(default="ppdf_session", alias="SESSION_COOKIE_NAME")
    session_ttl_hours: int = Field(default=12, alias="SESSION_TTL_HOURS")
    magic_link_ttl_minutes: int = Field(default=15, alias="MAGIC_LINK_TTL_MINUTES")
    invite_ttl_days: int = Field(default=7, alias="INVITE_TTL_DAYS")
    totp_issuer: str = Field(default="PublishPDF", alias="TOTP_ISSUER")
    totp_max_attempts: int = Field(default=5, alias="TOTP_MAX_ATTEMPTS")
    cookie_secure: bool = Field(default=False, alias="COOKIE_SECURE")
    # Two-factor (TOTP) is optional per user by default; set true to force it for
    # platform admins and client admins (the original PLAN D14 setting).
    mfa_required_for_admins: bool = Field(default=False, alias="MFA_REQUIRED_FOR_ADMINS")
    email_code_ttl_minutes: int = Field(default=10, alias="EMAIL_CODE_TTL_MINUTES")
    allow_self_signup: bool = Field(default=True, alias="ALLOW_SELF_SIGNUP")

    # --- Email ----------------------------------------------------------------
    email_backend: str = Field(default="console", alias="EMAIL_BACKEND")  # console | smtp
    email_from: str = Field(default="no-reply@example.invalid", alias="EMAIL_FROM")
    smtp_host: str | None = Field(default=None, alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_user: str | None = Field(default=None, alias="SMTP_USER")
    smtp_password: str | None = Field(default=None, alias="SMTP_PASSWORD")

    # --- Storage --------------------------------------------------------------
    # local | s3 | vercel_blob. Unset: Vercel Blob when a Blob store is connected
    # (Vercel injects BLOB_READ_WRITE_TOKEN), otherwise the local disk.
    storage_backend: str = Field(default="", alias="STORAGE_BACKEND")
    blob_read_write_token: str | None = Field(default=None, alias="BLOB_READ_WRITE_TOKEN")

    @model_validator(mode="after")
    def _find_blob_token(self) -> "Settings":
        # Vercel lets a connected Blob store use a custom prefix (e.g. MYSTORE_READ_WRITE_TOKEN);
        # the token itself always starts with vercel_blob_rw_.
        if not self.blob_read_write_token:
            import os
            self.blob_read_write_token = next(
                (v for k, v in sorted(os.environ.items())
                 if k.endswith("READ_WRITE_TOKEN") and v.startswith("vercel_blob_rw_")), None)
        return self
    storage_local_root: str = Field(default="./.data/storage", alias="STORAGE_LOCAL_ROOT")
    # S3-compatible: AWS S3, Cloudflare R2, Railway buckets, MinIO. The names Railway's
    # bucket integration injects (BUCKET, ENDPOINT, ...) are accepted too.
    s3_bucket: str | None = Field(default=None, validation_alias=AliasChoices("S3_BUCKET", "BUCKET"))
    s3_region: str | None = Field(default=None, validation_alias=AliasChoices("S3_REGION", "REGION"))
    s3_endpoint_url: str | None = Field(default=None, validation_alias=AliasChoices("S3_ENDPOINT_URL", "ENDPOINT"))
    s3_access_key_id: str | None = Field(default=None, validation_alias=AliasChoices(
        "S3_ACCESS_KEY_ID", "ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID"))
    s3_secret_access_key: str | None = Field(default=None, validation_alias=AliasChoices(
        "S3_SECRET_ACCESS_KEY", "SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY"))
    # AWS encrypts with SSE-S3; R2/Railway/MinIO encrypt at rest themselves and some
    # reject the header, so it's sent only to AWS unless set explicitly.
    s3_sse: str | None = Field(default=None, alias="S3_SSE")
    s3_path_style: bool = Field(default=False, alias="S3_FORCE_PATH_STYLE")

    # --- Limits (proposed values; to confirm — PLAN.md D12) ---------------------
    max_upload_mb: int = Field(default=100, alias="MAX_UPLOAD_MB")
    max_pages: int = Field(default=600, alias="MAX_PAGES")
    ocr_confidence_threshold: float = Field(default=0.95, alias="OCR_CONFIDENCE_THRESHOLD")

    # --- Public sites ------------------------------------------------------------
    # Scheme and port suffix of tenant sites. Production: https and "".
    public_scheme: str = Field(default="https", alias="PUBLIC_SCHEME")
    public_port_suffix: str = Field(default="", alias="PUBLIC_PORT_SUFFIX")
    # PLAN D11: publishing is blocked until a disclaimer exists (platform default or tenant's own).
    disclaimer_default_text: str | None = Field(default=None, alias="DISCLAIMER_DEFAULT_TEXT")

    # --- Custom domains / TLS (PLAN D5) --------------------------------------------
    # caddy: certificates via Caddy on-demand TLS; we probe the edge for a valid cert.
    # off:   development only — a verified domain is marked live without a certificate.
    tls_mode: str = Field(default="caddy", alias="TLS_MODE")
    edge_probe_address: str | None = Field(default=None, alias="EDGE_PROBE_ADDRESS")   # host:port of the edge
    acme_ca_domains: str = Field(default="letsencrypt.org,pki.goog,sectigo.com", alias="ACME_CA_DOMAINS")
    internal_api_token: str | None = Field(default=None, alias="INTERNAL_API_TOKEN")    # for the TLS ask hook

    # --- LLM assist (PLAN D4; off per tenant by default) ----------------------------
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    llm_model: str = Field(default="claude-sonnet-5", alias="LLM_MODEL")
    # AI double-check of flagged figures (opt-in per report, estimate shown first).
    gemini_api_key: str | None = Field(default=None, validation_alias=AliasChoices("GEMINI_API_KEY", "GEMINI_KEY"))
    gemini_model: str = Field(default="gemini-2.5-flash", alias="GEMINI_MODEL")
    # Your plan's prices, USD per million tokens — confirm against Google's current price list.
    gemini_price_in_per_m: float = Field(default=0.30, alias="GEMINI_PRICE_IN_PER_M")
    gemini_price_out_per_m: float = Field(default=2.50, alias="GEMINI_PRICE_OUT_PER_M")
    # What one credit is worth in USD; estimates and usage are shown in credits.
    ai_credit_usd: float = Field(default=0.01, alias="AI_CREDIT_USD")

    # --- Jobs -----------------------------------------------------------------
    job_max_attempts: int = Field(default=3, alias="JOB_MAX_ATTEMPTS")
    # Several jobs at once, so one long report never holds up a new upload.
    job_concurrency: int = Field(default=3, alias="JOB_CONCURRENCY")
    # A job running longer than this is stopped and recorded as failed (the worker slot restarts).
    job_timeout_seconds: int = Field(default=1200, alias="JOB_TIMEOUT_SECONDS")
    job_poll_seconds: float = Field(default=1.0, alias="JOB_POLL_SECONDS")

    @property
    def is_production(self) -> bool:
        return self.env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
