"""Reports pipeline, validation, review, publishing, theming, domains, analytics.

Every table is tenant-scoped with the same RLS predicate as 0001.

Revision ID: 0002
"""

from alembic import op

from app.rls_sql import ADMIN, SYSTEM, TENANT, app_role

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TABLES = ("source_files", "reports", "report_versions", "validation_issues", "figure_reviews",
          "comments", "tenant_settings", "domains", "edge_hits")


def upgrade() -> None:
    app = app_role()
    op.execute("""
        CREATE TABLE source_files (
            id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id          uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            sha256             text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
            storage_key        text NOT NULL,
            original_filename  text NOT NULL,
            size_bytes         bigint NOT NULL,
            page_count         integer NOT NULL,
            uploaded_by        uuid REFERENCES users(id),
            created_at         timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, sha256)
        );

        CREATE TABLE reports (
            id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            company_name     text NOT NULL,
            report_type      text NOT NULL CHECK (report_type IN
                             ('quarterly_results', 'investor_presentation', 'annual_report', 'other')),
            fiscal_year      integer NOT NULL CHECK (fiscal_year BETWEEN 1990 AND 2100),
            period           text NOT NULL CHECK (period IN ('q1', 'q2', 'q3', 'q4', 'h1', 'h2', '9m', 'fy')),
            currency         text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
            reporting_unit   text NOT NULL,
            title            text,
            theme_override   jsonb,
            live_version_id  uuid,
            created_by       uuid REFERENCES users(id),
            created_at       timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, fiscal_year, period, report_type)
        );

        CREATE TABLE report_versions (
            id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id              uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            report_id              uuid NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
            version_no             integer NOT NULL,
            source_file_id         uuid NOT NULL REFERENCES source_files(id),
            source_sha256          text NOT NULL,
            status                 text NOT NULL DEFAULT 'processing' CHECK (status IN
                                   ('processing', 'failed', 'needs_review', 'validation_issues',
                                    'published', 'superseded')),
            stage                  text,
            error_plain            text,
            created_from_version_id uuid REFERENCES report_versions(id),
            extraction_key         text,
            schema_json            jsonb,
            schema_sha256          text,
            bundle_key             text,
            bundle_sha256          text,
            theme_snapshot         jsonb,
            validation_summary     jsonb,
            validated_schema_sha256 text,
            validated_bundle_sha256 text,
            published_at           timestamptz,
            published_by           uuid REFERENCES users(id),
            review_confirmed_by    uuid REFERENCES users(id),
            created_by             uuid REFERENCES users(id),
            created_at             timestamptz NOT NULL DEFAULT now(),
            UNIQUE (report_id, version_no)
        );
        CREATE INDEX report_versions_report_idx ON report_versions(report_id, version_no DESC);
        ALTER TABLE reports ADD CONSTRAINT reports_live_version_fk
            FOREIGN KEY (live_version_id) REFERENCES report_versions(id);

        -- A published version is immutable: content columns can never change again.
        CREATE FUNCTION report_versions_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.published_at IS NOT NULL AND (
                NEW.schema_json IS DISTINCT FROM OLD.schema_json OR
                NEW.schema_sha256 IS DISTINCT FROM OLD.schema_sha256 OR
                NEW.bundle_key IS DISTINCT FROM OLD.bundle_key OR
                NEW.bundle_sha256 IS DISTINCT FROM OLD.bundle_sha256 OR
                NEW.theme_snapshot IS DISTINCT FROM OLD.theme_snapshot OR
                NEW.source_sha256 IS DISTINCT FROM OLD.source_sha256 OR
                NEW.published_at IS DISTINCT FROM OLD.published_at
            ) THEN
                RAISE EXCEPTION 'published report versions are immutable'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER report_versions_guard BEFORE UPDATE ON report_versions
            FOR EACH ROW EXECUTE FUNCTION report_versions_immutable();
        CREATE FUNCTION report_versions_no_delete() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.published_at IS NOT NULL THEN
                RAISE EXCEPTION 'published report versions cannot be deleted'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN OLD;
        END $$;
        CREATE TRIGGER report_versions_no_delete BEFORE DELETE ON report_versions
            FOR EACH ROW EXECUTE FUNCTION report_versions_no_delete();

        CREATE TABLE validation_issues (
            id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            version_id   uuid NOT NULL REFERENCES report_versions(id) ON DELETE CASCADE,
            run_no       integer NOT NULL,
            check_name   text NOT NULL,
            severity     text NOT NULL CHECK (severity IN ('blocking', 'warning')),
            fid          text,
            page         integer,
            section_id   text,
            bbox         jsonb,
            message      text NOT NULL,
            expected     text,
            actual       text,
            status       text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
            resolution   text,
            resolved_by  uuid REFERENCES users(id),
            resolved_at  timestamptz,
            created_at   timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX validation_issues_version_idx ON validation_issues(version_id, run_no);

        -- Human review trail for figures: confirm / edit / not_a_figure / flag.
        CREATE TABLE figure_reviews (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            version_id  uuid NOT NULL REFERENCES report_versions(id) ON DELETE CASCADE,
            fid         text NOT NULL,
            action      text NOT NULL CHECK (action IN ('confirm', 'edit', 'not_a_figure', 'flag')),
            old_value   jsonb,
            new_value   jsonb,
            note        text,
            user_id     uuid NOT NULL REFERENCES users(id),
            at          timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX figure_reviews_version_idx ON figure_reviews(version_id, fid);

        CREATE TABLE comments (
            id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            version_id   uuid NOT NULL REFERENCES report_versions(id) ON DELETE CASCADE,
            section_id   text NOT NULL,
            user_id      uuid NOT NULL REFERENCES users(id),
            body         text NOT NULL CHECK (length(body) BETWEEN 1 AND 5000),
            resolved_at  timestamptz,
            resolved_by  uuid REFERENCES users(id),
            created_at   timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX comments_version_idx ON comments(version_id);

        CREATE TABLE tenant_settings (
            tenant_id              uuid PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
            theme                  jsonb,
            theme_mode             text,
            disclaimer             text,
            robots_policy          text NOT NULL DEFAULT 'allow_all'
                                   CHECK (robots_policy IN ('allow_all', 'search_and_answer', 'block_ai')),
            ga4_measurement_id     text CHECK (ga4_measurement_id ~ '^G-[A-Z0-9]{4,12}$'),
            consent_banner_enabled boolean NOT NULL DEFAULT true,
            consent_banner_off_ack text,
            llm_assist_enabled     boolean NOT NULL DEFAULT false,
            updated_at             timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE domains (
            id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            hostname         text NOT NULL UNIQUE CHECK (hostname = lower(hostname)),
            verify_token     text NOT NULL,
            status           text NOT NULL DEFAULT 'pending' CHECK (status IN
                             ('pending', 'verified', 'ssl_issuing', 'live', 'failed', 'removed')),
            failure_code     text,
            failure_reason   text,
            last_checked_at  timestamptz,
            verified_at      timestamptz,
            live_at          timestamptz,
            cert_expires_at  timestamptz,
            last_alert_at    timestamptz,
            created_at       timestamptz NOT NULL DEFAULT now()
        );
        -- One active custom domain per tenant.
        CREATE UNIQUE INDEX domains_one_active_per_tenant ON domains(tenant_id) WHERE status <> 'removed';

        CREATE TABLE edge_hits (
            id          bigserial PRIMARY KEY,
            tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            host        text NOT NULL,
            path        text NOT NULL,
            report_id   uuid,
            status      integer NOT NULL,
            agent_class text NOT NULL,   -- 'human' | 'bot:<name>'
            at          timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX edge_hits_tenant_at_idx ON edge_hits(tenant_id, at DESC);

        ALTER TABLE jobs ADD COLUMN version_id uuid REFERENCES report_versions(id) ON DELETE CASCADE;
        CREATE INDEX jobs_version_idx ON jobs(version_id);
    """)

    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {table}_rw ON {table}
                USING ({TENANT} OR {ADMIN} OR {SYSTEM})
                WITH CHECK ({TENANT} OR {ADMIN} OR {SYSTEM});
        """)
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(TABLES)} TO {app}")
    # Review trail is append-only for the app role.
    op.execute(f"REVOKE UPDATE, DELETE ON figure_reviews FROM {app}")
    op.execute(f"GRANT USAGE ON SEQUENCE edge_hits_id_seq TO {app}")


def downgrade() -> None:
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS version_id")
    op.execute("ALTER TABLE reports DROP CONSTRAINT IF EXISTS reports_live_version_fk")
    op.execute(f"DROP TABLE IF EXISTS {', '.join(reversed(TABLES))} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS report_versions_immutable(); DROP FUNCTION IF EXISTS report_versions_no_delete();")
