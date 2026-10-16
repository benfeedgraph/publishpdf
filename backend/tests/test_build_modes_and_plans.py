"""Build options (without AI / AI just for layout / AI throughout), credit history, plans."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from app import ai_check, ai_usage, build_modes, db, page_layouts, plans
from app.config import get_settings
from app.models import Job
from app.tenancy import Role, system_context, tenant_context
from tests.conftest import add_member, make_tenant, make_user, sign_in
from tests.test_pipeline_e2e import CORPUS, admin_client, drain  # noqa: F401 (fixture)


@pytest.fixture(autouse=True)
def no_live_ai(monkeypatch):
    """Every model call in here is a fake; a real one fails the test."""
    calls = []

    def fake(body):
        calls.append(body)
        return {"candidates": [{"content": {"parts": [{"text": '{"sections": []}'}]}}],
                "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 100}}
    ai_check._OVERRIDE.append(fake)
    monkeypatch.setattr(get_settings(), "gemini_api_key", "test-key-not-real")
    monkeypatch.setattr(get_settings(), "anthropic_api_key", None)
    page_layouts._REJECTED_UNTIL[0] = 0.0
    yield calls
    ai_check._OVERRIDE.remove(fake)


def _inspect(client, t, name="acme_q2fy26_results.pdf"):
    r = client.post(f"/api/tenants/{t.id}/reports/inspect",
                    files={"file": (name, (CORPUS / name).read_bytes(), "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()


def _create(client, t, ins, mode, credits_shown=None, period="q2"):
    d = ins["detected"]
    return client.post(f"/api/tenants/{t.id}/reports/create", json={
        "source_file_id": ins["source_file_id"], "company_name": d.get("company_name") or "Acme",
        "report_type": "quarterly_results", "fiscal_year": d.get("fiscal_year") or 2026, "period": period,
        "currency": "INR", "reporting_unit": "₹ crore", "build_mode": mode, "credits_shown": credits_shown})


def test_estimates_are_upper_bounds_from_the_page_count():
    assert build_modes.caps("none", 400) == {"layout": 0, "labels": 0, "check": 0}
    assert build_modes.layout_credits(1) == 0                       # page 1 is the cover
    assert build_modes.layout_credits(101) > build_modes.layout_credits(11) > 0
    full = build_modes.caps("full", 100)
    assert full["layout"] == build_modes.caps("layout", 100)["layout"] and full["check"] == get_settings().ai_auto_check_max_credits
    assert build_modes.max_credits("layout", 100) < build_modes.max_credits("full", 100)


def test_upload_offers_three_options_with_their_credits(admin_client):
    client, t, _ = admin_client
    ins = _inspect(client, t)
    bo = ins["build_options"]
    modes = {o["mode"]: o for o in bo["options"]}
    assert set(modes) == {"none", "layout", "full"} and bo["default"] == "full"
    assert modes["none"]["max_credits"] == 0 and all("models" not in o for o in bo["options"])
    assert modes["layout"]["max_credits"] == build_modes.max_credits("layout", ins["page_count"])
    assert modes["full"]["max_credits"] >= modes["layout"]["max_credits"]


def test_an_ai_option_needs_the_estimate_that_was_shown(admin_client):
    client, t, _ = admin_client
    ins = _inspect(client, t)
    r = _create(client, t, ins, "layout", credits_shown=None)
    assert r.status_code == 409 and "credit estimate" in r.json()["detail"]
    r = _create(client, t, ins, "layout", credits_shown=build_modes.max_credits("layout", ins["page_count"]) + 1)
    assert r.status_code == 409


def test_without_ai_spends_nothing_and_uses_no_cached_layout(admin_client, no_live_ai, monkeypatch):
    client, t, _ = admin_client
    monkeypatch.setattr(get_settings(), "ai_auto_check_max_credits", 50)
    ins = _inspect(client, t, "acme_q2fy26_results_scanned.pdf")         # scanned: figures get flagged
    r = _create(client, t, ins, "none")
    assert r.status_code == 201, r.text
    drain()
    v = client.get(f"/api/tenants/{t.id}/reports/{r.json()['report_id']}/versions/{r.json()['version']['id']}").json()
    assert v["build_mode"] == "none" and v["status"] in ("needs_review", "validation_issues")
    assert no_live_ai == []
    with db.session(system_context()) as s:
        assert s.scalar(text("SELECT count(*) FROM ai_usage WHERE tenant_id = :t"), {"t": t.id}) == 0
        assert not s.scalars(select(Job).where(Job.tenant_id == t.id, Job.kind == "pipeline.ai_check")).all()
    assert page_layouts.layouts_for(tenant_context(None, t.id, None), None, "x", {"sections": []}, b"", mode="none") == {}


def test_full_ai_runs_the_double_check_and_layout_only_does_not(admin_client, no_live_ai, monkeypatch):
    client, t, _ = admin_client
    monkeypatch.setattr(get_settings(), "ai_auto_check_max_credits", 50)

    def ai_jobs():
        with db.session(system_context()) as s:
            return s.scalars(select(Job).where(Job.tenant_id == t.id, Job.kind == "pipeline.ai_check")).all()

    ins = _inspect(client, t, "acme_q2fy26_results_scanned.pdf")
    r = _create(client, t, ins, "layout", build_modes.max_credits("layout", ins["page_count"]), period="q1")
    assert r.status_code == 201, r.text
    drain()
    assert ai_jobs() == []
    ins = _inspect(client, t, "acme_q2fy26_results_scanned.pdf")
    r = _create(client, t, ins, "full", build_modes.max_credits("full", ins["page_count"]), period="q3")
    assert r.status_code == 201, r.text
    drain()
    assert len(ai_jobs()) == 1


def test_rebuild_with_another_option_keeps_reviews_and_checks_the_estimate(admin_client):
    client, t, _ = admin_client
    ins = _inspect(client, t)
    r = _create(client, t, ins, "none")
    drain()
    base = f"/api/tenants/{t.id}/reports/{r.json()['report_id']}/versions/{r.json()['version']['id']}"
    v = client.get(base).json()
    opts = {o["mode"]: o for o in v["build_options"]["options"]}
    assert client.post(base + "/build-mode", json={"build_mode": "layout"}).status_code == 409
    r2 = client.post(base + "/build-mode", json={"build_mode": "layout", "credits_shown": opts["layout"]["max_credits"]})
    assert r2.status_code == 200, r2.text and r2.json()["build_mode"] == "layout"
    drain()
    assert client.get(base).json()["build_mode"] == "layout"


def test_credit_history_by_month_and_report(admin_client):
    client, t, _ = admin_client
    ctx = tenant_context(None, t.id, None)
    ai_usage.record(ctx, feature="page_layout", provider="gemini", model="m", input_tokens=100_000, output_tokens=10_000)
    ai_usage.record(ctx, feature="ai_check", provider="gemini", model="m", input_tokens=1000, output_tokens=10)
    h = client.get(f"/api/tenants/{t.id}/ai-usage/history").json()
    assert h["total"] == 2 and len(h["rows"]) == 2 and h["months"][0]["requests"] == 2
    assert h["reports"][0]["report"] is None and {f["feature"] for f in h["reports"][0]["features"]} == {"page_layout", "ai_check"}
    csv = client.get(f"/api/tenants/{t.id}/ai-usage/history.csv").text
    assert csv.splitlines()[0].startswith("time_utc,report") and len(csv.splitlines()) == 3
    assert client.get(f"/api/tenants/{t.id}/ai-usage/history?month=2020-01").json()["total"] == 0
    assert client.get(f"/api/tenants/{t.id}/ai-usage/history?month=nope").status_code == 422


def test_credit_history_is_per_workspace(client):
    a, b, ua = make_tenant(), make_tenant(), make_user()
    add_member(a, ua, Role.client_admin)
    ai_usage.record(tenant_context(None, b.id, None), feature="ai_check", provider="gemini", model="m",
                    input_tokens=10, output_tokens=1)
    sign_in(client, ua)
    assert client.get(f"/api/tenants/{a.id}/ai-usage/history").json()["total"] == 0
    assert client.get(f"/api/tenants/{b.id}/ai-usage/history").status_code in (403, 404)


def test_plans_request_and_admin_assignment_set_the_limit(client):
    t, owner, admin = make_tenant(), make_user(), make_user(platform_admin=True)
    add_member(t, owner, Role.client_admin)
    sign_in(client, owner)
    st = client.get(f"/api/tenants/{t.id}/plan").json()
    assert [p["key"] for p in st["plans"]] == ["free", "starter", "pro", "enterprise"]
    r = client.post(f"/api/tenants/{t.id}/plan/request", json={"plan": "pro"})
    assert r.status_code == 200 and r.json()["requested"] == "pro"
    sign_in(client, admin)
    assert client.get("/api/admin/tenants").json()["tenants"][0] is not None
    r = client.put(f"/api/admin/tenants/{t.id}/plan", json={"plan": "pro"})
    assert r.status_code == 200 and r.json()["ai_monthly_credit_limit"] == plans.BY_KEY["pro"]["credits"]
    r = client.put(f"/api/admin/tenants/{t.id}/plan", json={"plan": "enterprise", "monthly_credits": 12345})
    assert r.json()["ai_monthly_credit_limit"] == 12345
    with db.session(system_context()) as s:
        row = s.execute(text("SELECT plan, plan_requested, ai_monthly_credit_limit FROM tenants WHERE id = :t"), {"t": t.id}).one()
    assert row.plan == "enterprise" and row.plan_requested is None and row.ai_monthly_credit_limit == 12345


def test_a_reviewer_cannot_request_a_plan(client):
    t, rv = make_tenant(), make_user()
    add_member(t, rv, Role.client_reviewer)
    sign_in(client, rv)
    assert client.post(f"/api/tenants/{t.id}/plan/request", json={"plan": "pro"}).status_code == 403
    _ = uuid
