"""Foundations: tenants, users, memberships, invites, auth tokens, sessions,
append-only audit log, job queue — with Row-Level Security on every table.

RLS model (see app/tenancy.py):
  app.tenant_id       — the tenant the current transaction acts in
  app.user_id         — the authenticated user
  app.platform_admin  — 'on' for platform-admin requests (cross-tenant read/write)
  app.system          — 'on' only inside app.tenancy.system_context(): auth
                        lookups before a user is known, and the job claimer.

All four are set with set_config(..., is_local => true), i.e. per transaction.
An unset setting reads as NULL, which matches nothing -> fail closed.

Revision ID: 0001
"""

from urllib.parse import urlparse

from alembic import op

from app.config import get_settings

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _app_role() -> str:
    url = get_settings().database_app_url
    role = urlparse(url.replace("postgresql+psycopg", "postgresql")).username
    if not role or not role.replace("_", "").isalnum():
        raise RuntimeError("DATABASE_APP_URL must include a simple role name")
    return role


# Reusable predicates. `current_setting(name, true)` returns NULL when unset.
TENANT = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
ADMIN = "current_setting('app.platform_admin', true) = 'on'"
SYSTEM = "current_setting('app.system', true) = 'on'"
ME = "nullif(current_setting('app.user_id', true), '')::uuid"


def upgrade() -> None:
    app = _app_role()

    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute(
        """
        CREATE TABLE tenants (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            slug        text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?$'),
            name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
            status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
            created_at  timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE users (
            id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            email              text NOT NULL UNIQUE CHECK (email = lower(email)),
            name               text,
            is_platform_admin  boolean NOT NULL DEFAULT false,
            totp_secret_enc    text,
            totp_enabled_at    timestamptz,
            disabled_at        timestamptz,
            created_at         timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE memberships (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role        text NOT NULL CHECK (role IN ('client_admin', 'client_reviewer')),
            created_at  timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, user_id)
        );
        CREATE INDEX memberships_user_idx ON memberships(user_id);

        CREATE TABLE invites (
            id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            email        text NOT NULL CHECK (email = lower(email)),
            role         text NOT NULL CHECK (role IN ('client_admin', 'client_reviewer')),
            token_hash   text NOT NULL UNIQUE,
            invited_by   uuid REFERENCES users(id),
            expires_at   timestamptz NOT NULL,
            accepted_at  timestamptz,
            revoked_at   timestamptz,
            created_at   timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX invites_tenant_idx ON invites(tenant_id);

        CREATE TABLE login_tokens (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash  text NOT NULL UNIQUE,
            expires_at  timestamptz NOT NULL,
            used_at     timestamptz,
            created_at  timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE sessions (
            id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id              uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash           text NOT NULL UNIQUE,
            mfa_verified_at      timestamptz,
            mfa_failed_attempts  integer NOT NULL DEFAULT 0,
            expires_at           timestamptz NOT NULL,
            revoked_at           timestamptz,
            ip                   text,
            user_agent           text,
            created_at           timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX sessions_user_idx ON sessions(user_id);

        -- Append-only. tenant_id NULL = platform-level event (visible to platform admins only).
        CREATE TABLE audit_log (
            id             bigserial PRIMARY KEY,
            tenant_id      uuid REFERENCES tenants(id) ON DELETE RESTRICT,
            actor_user_id  uuid REFERENCES users(id) ON DELETE RESTRICT,
            action         text NOT NULL,
            target_type    text,
            target_id      text,
            before         jsonb,
            after          jsonb,
            ip             text,
            at             timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX audit_log_tenant_at_idx ON audit_log(tenant_id, at DESC);

        CREATE FUNCTION audit_log_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only (% rejected)', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END $$;
        -- Statement-level, so an attempt is refused even when RLS would leave it
        -- matching zero rows (a silent no-op would hide tampering attempts).
        CREATE TRIGGER audit_log_no_modify BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_log
            FOR EACH STATEMENT EXECUTE FUNCTION audit_log_immutable();

        -- Postgres-backed job queue. Every job belongs to a tenant; the
        -- (tenant_id, idempotency_key) pair makes enqueueing idempotent.
        CREATE TABLE jobs (
            id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            kind             text NOT NULL,
            payload          jsonb NOT NULL DEFAULT '{}'::jsonb,
            idempotency_key  text NOT NULL,
            status           text NOT NULL DEFAULT 'queued'
                             CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
            attempts         integer NOT NULL DEFAULT 0,
            max_attempts     integer NOT NULL DEFAULT 3,
            run_after        timestamptz NOT NULL DEFAULT now(),
            locked_at        timestamptz,
            locked_by        text,
            last_error       text,
            error_plain      text,
            result           jsonb,
            created_at       timestamptz NOT NULL DEFAULT now(),
            started_at       timestamptz,
            finished_at      timestamptz,
            UNIQUE (tenant_id, idempotency_key)
        );
        CREATE INDEX jobs_claim_idx ON jobs(run_after) WHERE status = 'queued';
        CREATE INDEX jobs_tenant_idx ON jobs(tenant_id, created_at DESC);
        """
    )

    # ---- Row-Level Security ------------------------------------------------------
    # FORCE so even the table owner is subject to policies.
    for table in ("tenants", "users", "memberships", "invites", "login_tokens",
                  "sessions", "audit_log", "jobs"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # tenants: you see the tenant you're acting in; admins/system see all.
    op.execute(f"""
        CREATE POLICY tenants_rw ON tenants
            USING (id = nullif(current_setting('app.tenant_id', true), '')::uuid OR {ADMIN} OR {SYSTEM})
            WITH CHECK ({ADMIN} OR {SYSTEM});
    """)
    # tenants listed for "which tenants am I in" go through memberships (below).
    op.execute(f"""
        CREATE POLICY tenants_member_read ON tenants FOR SELECT
            USING (EXISTS (SELECT 1 FROM memberships m WHERE m.tenant_id = tenants.id AND m.user_id = {ME}));
    """)

    # users: yourself; members of the tenant you're acting in; admins/system.
    op.execute(f"""
        CREATE POLICY users_read ON users FOR SELECT
            USING (id = {ME} OR {ADMIN} OR {SYSTEM}
                   OR EXISTS (SELECT 1 FROM memberships m
                              WHERE m.user_id = users.id
                                AND m.tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid));
        CREATE POLICY users_write ON users FOR ALL
            USING (id = {ME} OR {ADMIN} OR {SYSTEM})
            WITH CHECK (id = {ME} OR {ADMIN} OR {SYSTEM});
    """)

    # users_write lets a user edit their own row (name, TOTP enrolment). This trigger
    # stops them from escalating: platform-admin / disabled flags change only
    # under a platform-admin or system context.
    op.execute(f"""
        CREATE FUNCTION users_guard_privileged() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT ({ADMIN} OR {SYSTEM}) THEN
                IF TG_OP = 'INSERT' AND NEW.is_platform_admin THEN
                    RAISE EXCEPTION 'cannot create platform admin' USING ERRCODE = 'insufficient_privilege';
                END IF;
                IF TG_OP = 'UPDATE' AND (NEW.is_platform_admin IS DISTINCT FROM OLD.is_platform_admin
                                         OR NEW.disabled_at IS DISTINCT FROM OLD.disabled_at
                                         OR NEW.email IS DISTINCT FROM OLD.email) THEN
                    RAISE EXCEPTION 'privileged user fields are read-only' USING ERRCODE = 'insufficient_privilege';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER users_guard BEFORE INSERT OR UPDATE ON users
            FOR EACH ROW EXECUTE FUNCTION users_guard_privileged();
    """)

    # memberships: tenant-scoped, plus your own rows (to list your tenants).
    op.execute(f"""
        CREATE POLICY memberships_rw ON memberships
            USING ({TENANT} OR user_id = {ME} OR {ADMIN} OR {SYSTEM})
            WITH CHECK ({TENANT} OR {ADMIN} OR {SYSTEM});
    """)

    for table in ("invites", "jobs"):
        op.execute(f"""
            CREATE POLICY {table}_rw ON {table}
                USING ({TENANT} OR {ADMIN} OR {SYSTEM})
                WITH CHECK ({TENANT} OR {ADMIN} OR {SYSTEM});
        """)

    # Auth tables: only the auth module (system context) or the owning user.
    for table in ("login_tokens", "sessions"):
        op.execute(f"""
            CREATE POLICY {table}_rw ON {table}
                USING (user_id = {ME} OR {SYSTEM})
                WITH CHECK (user_id = {ME} OR {SYSTEM});
        """)

    # audit_log: insert where you act (your tenant, or your own account-level events
    # such as enrolling 2FA); read the same. INSERT ... RETURNING also needs the
    # SELECT policy to pass, hence SYSTEM on the read side.
    own_account_event = f"(tenant_id IS NULL AND actor_user_id = {ME})"
    op.execute(f"""
        CREATE POLICY audit_insert ON audit_log FOR INSERT
            WITH CHECK ({TENANT} OR {own_account_event} OR {ADMIN} OR {SYSTEM});
        CREATE POLICY audit_read ON audit_log FOR SELECT
            USING ({TENANT} OR {own_account_event} OR {ADMIN} OR {SYSTEM});
    """)

    # ---- Grants for the runtime role (least privilege) ---------------------------
    op.execute(f"GRANT USAGE ON SCHEMA public TO {app}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON tenants, users, memberships, invites, "
               f"login_tokens, sessions, jobs TO {app}")
    op.execute(f"GRANT SELECT, INSERT ON audit_log TO {app}")  # no UPDATE/DELETE/TRUNCATE
    op.execute(f"GRANT USAGE ON SEQUENCE audit_log_id_seq TO {app}")


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS jobs, audit_log, sessions, login_tokens, invites,
                             memberships, users, tenants CASCADE;
        DROP FUNCTION IF EXISTS audit_log_immutable();
        DROP FUNCTION IF EXISTS users_guard_privileged();
    """)
