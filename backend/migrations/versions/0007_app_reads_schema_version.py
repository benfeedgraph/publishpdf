"""Let the app role read the migration version, so /healthz can say when the database
is behind the code that is deployed (nothing on Vercel runs migrations).

Revision ID: 0007
"""

from alembic import op

from app.rls_sql import app_role

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(f"GRANT SELECT ON alembic_version TO {app_role()}")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT ON alembic_version FROM {app_role()}")
