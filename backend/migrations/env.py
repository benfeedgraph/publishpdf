"""Alembic runs as the OWNER role (DATABASE_OWNER_URL), never the runtime app role."""

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import get_settings


def run_migrations_online() -> None:
    url = context.config.attributes.get("url") or get_settings().database_owner_url
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("Offline migrations are not supported; run against a database.")
run_migrations_online()
