"""SQL fragments shared by migrations. Must match 0001_foundations.py."""

from urllib.parse import urlparse

from app.config import get_settings

TENANT = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
ADMIN = "current_setting('app.platform_admin', true) = 'on'"
SYSTEM = "current_setting('app.system', true) = 'on'"
ME = "nullif(current_setting('app.user_id', true), '')::uuid"


def app_role() -> str:
    role = urlparse(get_settings().database_app_url.replace("postgresql+psycopg", "postgresql")).username
    if not role or not role.replace("_", "").isalnum():
        raise RuntimeError("DATABASE_APP_URL must include a simple role name")
    return role
