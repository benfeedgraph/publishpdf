"""How each report's page is built (with or without AI), and workspace plans.

report_versions.build_mode: 'none' | 'layout' | 'full', chosen at upload (NULL = made
before this existed: the old platform-wide defaults apply). tenants.plan sets the monthly
AI credit allowance (copied into ai_monthly_credit_limit, which stays the enforced value);
plan_requested records an upgrade a workspace asked for, for a platform admin to act on.

Revision ID: 0008
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE report_versions ADD COLUMN build_mode text
            CHECK (build_mode IS NULL OR build_mode IN ('none', 'layout', 'full'));
        ALTER TABLE tenants ADD COLUMN plan text;
        ALTER TABLE tenants ADD COLUMN plan_requested text;
        ALTER TABLE tenants ADD COLUMN plan_requested_at timestamptz;
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE report_versions DROP COLUMN IF EXISTS build_mode;
        ALTER TABLE tenants DROP COLUMN IF EXISTS plan;
        ALTER TABLE tenants DROP COLUMN IF EXISTS plan_requested;
        ALTER TABLE tenants DROP COLUMN IF EXISTS plan_requested_at;
    """)
