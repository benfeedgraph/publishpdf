"""Worker heartbeats, so the app can tell "no worker is running" from "the queue is empty".

Each worker slot upserts its row every 30 seconds, busy or idle. /healthz and the
report progress screen read it; nothing else depends on it.

Revision ID: 0005
"""

from alembic import op

from app.rls_sql import SYSTEM, app_role

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    app = app_role()
    op.execute("""
        CREATE TABLE worker_heartbeats (
            worker_id   text PRIMARY KEY,
            started_at  timestamptz NOT NULL DEFAULT now(),
            seen_at     timestamptz NOT NULL DEFAULT now()
        );
        ALTER TABLE worker_heartbeats ENABLE ROW LEVEL SECURITY;
        ALTER TABLE worker_heartbeats FORCE ROW LEVEL SECURITY;
    """)
    op.execute(f"CREATE POLICY worker_heartbeats_system ON worker_heartbeats USING ({SYSTEM}) WITH CHECK ({SYSTEM})")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON worker_heartbeats TO {app}")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS worker_heartbeats")
