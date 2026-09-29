"""Soft-delete reports so a period can be filed again.

Published versions stay in the database (the immutability trigger still forbids
deleting them). A deleted report is hidden from the workspace and dropped from
the public site. The old unique key included deleted rows, which blocked
uploading the same period again.

Revision ID: 0004
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE reports ADD COLUMN deleted_at timestamptz;
        ALTER TABLE reports DROP CONSTRAINT reports_tenant_id_fiscal_year_period_report_type_key;
        CREATE UNIQUE INDEX reports_active_period_uq
            ON reports (tenant_id, fiscal_year, period, report_type)
            WHERE deleted_at IS NULL;
    """)


def downgrade() -> None:
    op.execute("""
        DROP INDEX IF EXISTS reports_active_period_uq;
        ALTER TABLE reports DROP COLUMN IF EXISTS deleted_at;
        ALTER TABLE reports ADD CONSTRAINT reports_tenant_id_fiscal_year_period_report_type_key
            UNIQUE (tenant_id, fiscal_year, period, report_type);
    """)
