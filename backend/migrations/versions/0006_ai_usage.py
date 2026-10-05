"""AI usage ledger + optional monthly credit limit per workspace.

One row per paid model request (a Gemini batch, a Claude section-labelling call), with
the provider's own token counts and the cost at configured prices. Written as each
request returns, in its own transaction, so a run that fails halfway still records what
it spent. Append-only for the app role.

Revision ID: 0006
"""

from alembic import op

from app.rls_sql import ADMIN, SYSTEM, TENANT, app_role

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    app = app_role()
    op.execute("""
        CREATE TABLE ai_usage (
            id             bigserial PRIMARY KEY,
            tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            version_id     uuid REFERENCES report_versions(id) ON DELETE SET NULL,
            feature        text NOT NULL,          -- 'ai_check' | 'section_labels'
            provider       text NOT NULL,          -- 'gemini' | 'anthropic'
            model          text NOT NULL,
            input_tokens   integer NOT NULL DEFAULT 0,
            output_tokens  integer NOT NULL DEFAULT 0,
            usd            numeric(12, 6) NOT NULL DEFAULT 0,
            credits        numeric(12, 4) NOT NULL DEFAULT 0,   -- exact; rounded up only for display/limits
            ok             boolean NOT NULL DEFAULT true,
            created_at     timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ai_usage_tenant_at_idx ON ai_usage(tenant_id, created_at DESC);

        ALTER TABLE tenants ADD COLUMN ai_monthly_credit_limit integer
            CHECK (ai_monthly_credit_limit IS NULL OR ai_monthly_credit_limit >= 0);

        ALTER TABLE ai_usage ENABLE ROW LEVEL SECURITY;
        ALTER TABLE ai_usage FORCE ROW LEVEL SECURITY;
    """)
    op.execute(f"""
        CREATE POLICY ai_usage_rw ON ai_usage
            USING ({TENANT} OR {ADMIN} OR {SYSTEM})
            WITH CHECK ({TENANT} OR {ADMIN} OR {SYSTEM})
    """)
    op.execute(f"GRANT SELECT, INSERT ON ai_usage TO {app}")      # append-only ledger
    op.execute(f"GRANT USAGE ON SEQUENCE ai_usage_id_seq TO {app}")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai_usage")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS ai_monthly_credit_limit")
