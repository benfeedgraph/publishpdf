"""Email sign-in codes (6 digits) and self-serve sign-up.

One flow for everyone: enter email -> get a code -> enter it. Existing users are
signed in; new users are asked for their name and company, which creates their
workspace. Only the code's hash is stored; attempts per code are capped.

Revision ID: 0003
"""

from alembic import op

from app.rls_sql import SYSTEM, app_role

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    app = app_role()
    op.execute("""
        CREATE TABLE email_codes (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            email       text NOT NULL CHECK (email = lower(email)),
            code_hash   text NOT NULL,
            attempts    integer NOT NULL DEFAULT 0,
            expires_at  timestamptz NOT NULL,
            used_at     timestamptz,
            ip          text,
            created_at  timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX email_codes_email_idx ON email_codes(email, created_at DESC);

        -- Proof of email ownership between "code verified" and "profile submitted" for new users.
        CREATE TABLE signup_tokens (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            email       text NOT NULL CHECK (email = lower(email)),
            token_hash  text NOT NULL UNIQUE,
            expires_at  timestamptz NOT NULL,
            used_at     timestamptz,
            created_at  timestamptz NOT NULL DEFAULT now()
        );
    """)
    for t in ("email_codes", "signup_tokens"):
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY {t}_system ON {t} USING ({SYSTEM}) WITH CHECK ({SYSTEM})")
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON {t} TO {app}")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS signup_tokens, email_codes")
