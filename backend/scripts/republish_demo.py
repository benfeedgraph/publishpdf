"""Development helper: re-render every live report in the demo workspace with the current
templates (as a new draft), validate it, and publish it if it has no blocking issues."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app import db, domain_jobs, jobs, pipeline  # noqa: F401 - registers handlers
from app.models import Report, ReportVersion, Tenant, User
from app.reports import new_draft_from, publish
from app.tenancy import Role, system_context, tenant_context

with db.session(system_context()) as s:
    t = s.scalars(select(Tenant).where(Tenant.slug == "demo-co")).one()
    u = s.scalars(select(User).where(User.email == "admin@example.com")).one()
    ctx = tenant_context(u.id, t.id, Role.client_admin)
with db.session(ctx) as s:
    drafts = [new_draft_from(s, ctx, s.get(ReportVersion, r.live_version_id)).id
              for r in s.scalars(select(Report).where(Report.live_version_id.is_not(None)))]
while jobs.run_one("republish"):
    pass
with db.session(ctx) as s:
    for vid in drafts:
        v = s.get(ReportVersion, vid)
        if v.status == "needs_review":
            publish(s, ctx, v, confirm_reviewed=True)
        print(f"v{v.version_no}: {v.status}")
