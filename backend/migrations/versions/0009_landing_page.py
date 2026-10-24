"""Opt-in extractive landing page (overview) for long reports.

report_versions.landing: when true (and the PDF has >= 20 pages), the bundle also gets an
extractive "overview" page at <base>overview/ alongside the full report. It is composed
entirely from content already in the extracted schema — no AI, no credits, no new words or
figures. Chosen per report at upload, orthogonal to build_mode.

Revision ID: 0009
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE report_versions ADD COLUMN landing boolean NOT NULL DEFAULT false;
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE report_versions DROP COLUMN IF EXISTS landing;
    """)
