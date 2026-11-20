"""How the company writes its fiscal year.

reports.fiscal_year_label: the year as typed or read from the PDF ("FY2025-26", "FY26",
"2025-26", "FY2026"), shown on the page instead of FY<year>. reports.fiscal_year stays the
year it ends and is still what versions, URLs and sorting use. NULL (every report made
before this): the page says FY<year>, as before.

Revision ID: 0010
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE reports ADD COLUMN fiscal_year_label text
            CHECK (fiscal_year_label IS NULL OR length(fiscal_year_label) BETWEEN 2 AND 20);
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE reports DROP COLUMN IF EXISTS fiscal_year_label;
    """)
