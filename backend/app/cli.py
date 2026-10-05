"""Operator commands.

    uv run python -m app.cli migrate
    uv run python -m app.cli create-platform-admin you@example.com
    uv run python -m app.cli worker
    uv run python -m app.cli gen-secret
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply database migrations (owner role)")
    p = sub.add_parser("create-platform-admin", help="create or promote a platform admin")
    p.add_argument("email")
    w = sub.add_parser("worker", help="run background job workers")
    w.add_argument("--concurrency", type=int, default=None, help="jobs at once (default JOB_CONCURRENCY)")
    sub.add_parser("gen-secret", help="print a new APP_SECRET_KEY")
    d = sub.add_parser("seed-demo", help="development: demo workspace with the synthetic sample reports")
    d.add_argument("--admin", default="admin@example.com")
    args = parser.parse_args(argv)

    if args.cmd == "seed-demo":
        return _seed_demo(args.admin)

    if args.cmd == "migrate":
        from alembic import command
        from alembic.config import Config

        command.upgrade(Config("alembic.ini"), "head")
        return 0

    if args.cmd == "create-platform-admin":
        from sqlalchemy import select

        from app import audit, db
        from app.models import User
        from app.security.auth import normalize_email
        from app.tenancy import system_context

        email = normalize_email(args.email)
        with db.session(system_context()) as s:
            user = s.scalars(select(User).where(User.email == email)).first()
            if user is None:
                user = User(email=email, is_platform_admin=True)
                s.add(user)
            else:
                user.is_platform_admin = True
            s.flush()
            audit.record(s, system_context(), "platform_admin.granted", target_type="user",
                         target_id=user.id, after={"email": email, "via": "cli"})
        print(f"{email} is a platform admin. Sign in with a magic link, then set up 2FA.")
        return 0

    if args.cmd == "worker":
        import logging

        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        from app import jobs
        from app.config import get_settings

        refusal = worker_refusal()
        if refusal:
            logging.getLogger("app.cli").error(refusal)
            print(refusal, file=sys.stderr)
            return 2
        jobs.supervise(args.concurrency or get_settings().job_concurrency)
        return 0

    if args.cmd == "gen-secret":
        from cryptography.fernet import Fernet

        print(Fernet.generate_key().decode())
        return 0
    return 1


def worker_refusal() -> str | None:
    """A worker that keeps files on its own disk can only process uploads made on the same
    machine. Pointed at a shared (remote) database it claims other hosts' jobs, can't find
    their PDFs, and fails them: a laptop running scripts/dev.sh against the production
    database failed a live report exactly this way."""
    from app import storage
    from app.config import _private_database_host, get_settings

    s = get_settings()
    if storage.backend_kind() == "local" and not _private_database_host(s.database_owner_url):
        return ("Refusing to start the worker: files are stored on this machine's disk "
                "(STORAGE_BACKEND=local) but the database is remote, so this worker would take "
                "jobs whose PDFs it cannot read and fail them. Point DATABASE_*_URL at a local "
                "database, or configure the shared file store (BLOB_READ_WRITE_TOKEN or S3).")
    return None


def _seed_demo(admin_email: str) -> int:
    """Development only. Demo tenant 'demo-co' with the synthetic corpus uploaded and
    processed; the clean results report is published. Idempotent."""
    from pathlib import Path

    from sqlalchemy import select

    from app import db, domain_jobs, jobs, pipeline  # noqa: F401 - registers handlers
    from app.config import get_settings
    from app.models import Membership, Report, ReportVersion, Tenant, User
    from app.reports import create_upload, publish
    from app.tenancy import Role, system_context, tenant_context

    if get_settings().env != "development":
        print("seed-demo only runs with APP_ENV=development")
        return 1
    corpus = Path(__file__).resolve().parents[2] / "corpus" / "synthetic"
    if not corpus.exists():
        print("Generate the corpus first: uv run python scripts/make_corpus.py")
        return 1
    with db.session(system_context()) as s:
        t = s.scalars(select(Tenant).where(Tenant.slug == "demo-co")).first()
        if t is None:
            t = Tenant(slug="demo-co", name="Demo Co Ltd")
            s.add(t)
            s.flush()
        admin = s.scalars(select(User).where(User.email == admin_email)).first()
        if admin is None:
            admin = User(email=admin_email, is_platform_admin=True)
            s.add(admin)
            s.flush()
        for email, role in ((admin_email, Role.client_admin), ("reviewer@example.com", Role.client_reviewer)):
            u = s.scalars(select(User).where(User.email == email)).first() or User(email=email)
            s.add(u)
            s.flush()
            if not s.scalars(select(Membership).where(Membership.tenant_id == t.id, Membership.user_id == u.id)).first():
                s.add(Membership(tenant_id=t.id, user_id=u.id, role=role.value))
        tid, uid = t.id, admin.id
    ctx = tenant_context(uid, tid, Role.client_admin)
    samples = [
        ("acme_q2fy26_results.pdf", {"report_type": "quarterly_results", "period": "q2"}, True),
        ("acme_q2fy26_results_scanned.pdf", {"report_type": "quarterly_results", "period": "q1"}, False),
        ("acme_q2fy26_broken_text_layer.pdf", {"report_type": "other", "period": "q2"}, False),
        ("acme_q2fy26_presentation.pdf", {"report_type": "investor_presentation", "period": "q2"}, False),
    ]
    for name, meta, do_publish in samples:
        with db.session(ctx) as s:
            exists = s.scalars(select(Report).where(Report.fiscal_year == 2026, Report.period == meta["period"],
                                                    Report.report_type == meta["report_type"],
                                                    Report.deleted_at.is_(None))).first()
            if exists:
                print(f"  {name}: already seeded")
                continue
            v = create_upload(s, ctx, (corpus / name).read_bytes(), name, {
                "company_name": "Acme Industries Limited (synthetic sample)", "fiscal_year": 2026,
                "currency": "INR", "reporting_unit": "₹ crore", **meta})
            vid = v.id
        while jobs.run_one("seed-demo"):
            pass
        with db.session(ctx) as s:
            v = s.get(ReportVersion, vid)
            print(f"  {name}: {v.status} ({(v.validation_summary or {}).get('blocking', '?')} blocking)")
            if do_publish and v.status == "needs_review":
                publish(s, ctx, v, confirm_reviewed=True)
                print("    published")
    cfg = get_settings()
    print(f"Demo workspace ready. Site: {cfg.public_scheme}://{cfg.preview_url_pattern.replace('{tenant_slug}', 'demo-co')}{cfg.public_port_suffix}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
