"""Runtime configuration. Every value comes from the environment (see ../.env.example).

Nothing product-defining is hardcoded here: domains, limits and thresholds are all
placeholders the operator must confirm.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    env: str = Field(default="development", alias="APP_ENV")

    # --- Database -------------------------------------------------------------
    # Owner role: runs migrations, owns tables. Never used by the app at runtime.
    database_owner_url: str = Field(alias="DATABASE_OWNER_URL")
    # App role: NOT superuser, NOT table owner -> Row-Level Security applies to it.
    database_app_url: str = Field(alias="DATABASE_APP_URL")

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
    storage_backend: str = Field(default="local", alias="STORAGE_BACKEND")  # local | s3
    storage_local_root: str = Field(default="./.data/storage", alias="STORAGE_LOCAL_ROOT")
    s3_bucket: str | None = Field(default=None, alias="S3_BUCKET")
    s3_region: str | None = Field(default=None, alias="S3_REGION")
    s3_endpoint_url: str | None = Field(default=None, alias="S3_ENDPOINT_URL")

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

    # --- Jobs -----------------------------------------------------------------
    job_max_attempts: int = Field(default=3, alias="JOB_MAX_ATTEMPTS")
    job_poll_seconds: float = Field(default=1.0, alias="JOB_POLL_SECONDS")

    @property
    def is_production(self) -> bool:
        return self.env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
