"""AI usage ledger + monthly limits. No network: Gemini is a fake transport, and the
Claude HTTP call is replaced by a fake httpx.post."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select, text

from app import ai_check, ai_usage, db, pipeline
from app.config import get_settings
from app.models import ReportVersion
from app.tenancy import Role, system_context, tenant_context
from tests.conftest import add_member, make_tenant, make_user, sign_in
from tests.test_ai_check import fake_gemini
from tests.test_pipeline_e2e import _version, admin_client, drain, upload  # noqa: F401 (fixture)


@pytest.fixture(autouse=True)
def prices(monkeypatch):
    def boom(_body):
        raise AssertionError("the real Gemini API must never be called in tests")
    monkeypatch.setattr(ai_check, "gemini_transport", boom)
    cfg = get_settings()
    for k, v in {"gemini_price_in_per_m": 0.30, "gemini_price_out_per_m": 2.50,
                 "anthropic_price_in_per_m": 2.00, "anthropic_price_out_per_m": 10.00, "ai_credit_usd": 0.01}.items():
        monkeypatch.setattr(cfg, k, v)
    yield
    ai_check._OVERRIDE.clear()


def _ctx():
    t, u = make_tenant(), make_user()
    add_member(t, u, Role.client_admin)
    return t, tenant_context(u.id, t.id, Role.client_admin)


def _set_limit(tenant_id, n):
    with db.session(system_context()) as s:
        s.execute(text("UPDATE tenants SET ai_monthly_credit_limit = :n WHERE id = :t"), {"n": n, "t": tenant_id})


def test_ledger_prices_by_provider_and_rounds_up_only_for_display():
    _, ctx = _ctx()
    row = ai_usage.record(ctx, feature="section_labels", provider="anthropic", model="claude-sonnet-5",
                          input_tokens=1500, output_tokens=200)
    assert row["usd"] == pytest.approx(1500 * 2 / 1e6 + 200 * 10 / 1e6)          # $0.005
    assert row["credits"] == pytest.approx(0.5)                                   # exact in the ledger
    ai_usage.record(ctx, feature="ai_check", provider="gemini", model="gemini-2.5-flash",
                    input_tokens=3000, output_tokens=200)
    s = ai_usage.summary(ctx)
    assert s["month"]["requests"] == 2 and s["month"]["input_tokens"] == 4500
    assert s["month"]["credits"] == 1                                             # 0.5 + 0.14 -> shown as 1
    assert {f["feature"] for f in s["by_feature"]} == {"ai_check", "section_labels"}
    assert s["limit"] is None and s["remaining"] is None


def test_usage_is_private_to_its_workspace():
    _, a = _ctx()
    _, b = _ctx()
    ai_usage.record(a, feature="ai_check", provider="gemini", model="m", input_tokens=10_000, output_tokens=0)
    assert ai_usage.summary(b)["month"]["requests"] == 0
    with db.session(b) as s:
        assert s.scalar(text("SELECT count(*) FROM ai_usage")) == 0


def test_the_ledger_is_append_only_for_the_app():
    _, ctx = _ctx()
    ai_usage.record(ctx, feature="ai_check", provider="gemini", model="m", input_tokens=1, output_tokens=1)
    with pytest.raises(Exception):
        with db.session(ctx) as s:
            s.execute(text("UPDATE ai_usage SET usd = 0"))
    with pytest.raises(Exception):
        with db.session(ctx) as s:
            s.execute(text("DELETE FROM ai_usage"))


def test_monthly_limit_blocks_user_started_and_stops_automatic_ai():
    t, ctx = _ctx()
    _set_limit(t.id, 3)
    ai_usage.check(ctx, 3)                                                        # exactly at the limit is fine
    ai_usage.record(ctx, feature="ai_check", provider="gemini", model="m", input_tokens=0, output_tokens=8_000)  # $0.02 = 2
    assert ai_usage.allowance(ctx) == {"limit": 3, "used": 2, "remaining": 1}
    with pytest.raises(ai_usage.LimitReached, match="1 of its 3"):
        ai_usage.check(ctx, 2)
    assert ai_usage.can_run_automatic(ctx) is True
    ai_usage.record(ctx, feature="ai_check", provider="gemini", model="m", input_tokens=0, output_tokens=4_000)
    assert ai_usage.can_run_automatic(ctx) is False


def test_a_failed_run_still_records_what_it_spent(admin_client, monkeypatch):
    client, t, _ = admin_client
    up = upload(client, t, "acme_q2fy26_results_scanned.pdf", period="q1")
    rid, vid = up["report_id"], up["version"]["id"]
    drain()
    url = f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/ai-check"
    monkeypatch.setattr(get_settings(), "gemini_api_key", "test-key-not-real")
    monkeypatch.setattr(ai_check, "BATCH", 1)                   # one crop per request
    est = client.get(url).json()["estimate"]
    assert est["requests"] >= 2, "needs at least two batches to fail on the second"
    schema = client.get(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/schema").json()
    issues = client.get(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/issues", params={"status": "open"}).json()["issues"]
    fake_gemini.queue = [schema["figures"][f]["raw"] for f in ai_check.eligible(issues, schema)]
    ok = fake_gemini(set())
    calls = {"n": 0}

    def flaky(body):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("provider timed out")
        return ok(body)
    ai_check._OVERRIDE.append(flaky)

    assert client.post(url, json={"credits_shown": est["credits"]}).status_code == 202
    drain()
    last = client.get(url).json()["last"]
    assert last["status"] == "failed"
    assert calls["n"] == 2                                       # one attempt only: no paid retries
    usage = client.get(f"/api/tenants/{t.id}/ai-usage").json()
    assert usage["month"]["requests"] == 1                      # the batch that returned is on the ledger
    assert usage["month"]["input_tokens"] == 300
    assert usage["recent"][0]["report"] and usage["recent"][0]["feature"] == "ai_check"


def test_ai_check_refused_over_the_limit(admin_client, monkeypatch):
    client, t, _ = admin_client
    up = upload(client, t, "acme_q2fy26_results_scanned.pdf", period="q2")
    rid, vid = up["report_id"], up["version"]["id"]
    drain()
    url = f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/ai-check"
    monkeypatch.setattr(get_settings(), "gemini_api_key", "test-key-not-real")
    _set_limit(t.id, 0)
    st = client.get(url).json()
    assert st["allowance"] == {"limit": 0, "used": 0, "remaining": 0}
    r = client.post(url, json={"credits_shown": st["estimate"]["credits"]})
    assert r.status_code == 409 and "monthly AI credits" in r.json()["detail"]


def test_section_labelling_records_claude_usage_and_respects_the_limit(monkeypatch):
    import httpx

    t, ctx = _ctx()
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "test-key-not-real")
    with db.session(ctx) as s:
        from app.reports import settings_for
        settings_for(s, ctx).llm_assist_enabled = True

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"model": "claude-sonnet-5", "content": [{"type": "text", "text": json.dumps({"s1": "notes"})}],
                    "usage": {"input_tokens": 1200, "output_tokens": 40}}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: Resp())

    call = pipeline._llm_for(ctx)
    assert call is not None and "notes" in call("prompt")
    s = ai_usage.summary(ctx)
    assert s["by_feature"][0]["feature"] == "section_labels" and s["month"]["input_tokens"] == 1200
    _set_limit(t.id, 0)
    assert pipeline._llm_for(ctx) is None                       # limit used up: labelling is skipped


def test_platform_admin_sees_usage_and_sets_the_limit(client):
    t = make_tenant()
    admin = make_user(platform_admin=True)
    sign_in(client, admin)
    ctx = tenant_context(None, t.id, None, platform_admin=True)
    ai_usage.record(ctx, feature="ai_check", provider="gemini", model="m", input_tokens=0, output_tokens=8_000)
    r = client.put(f"/api/admin/tenants/{t.id}/ai-limit", json={"monthly_credits": 50})
    assert r.status_code == 200, r.text
    row = next(x for x in client.get("/api/admin/tenants").json()["tenants"] if x["id"] == str(t.id))
    assert row["ai_credits_month"] == 2 and row["ai_monthly_credit_limit"] == 50
    detail = client.get(f"/api/admin/tenants/{t.id}").json()
    assert detail["ai_usage"]["remaining"] == 48
    assert client.put(f"/api/admin/tenants/{t.id}/ai-limit", json={"monthly_credits": None}).json()["ai_monthly_credit_limit"] is None


def test_a_rejected_key_is_a_plain_message_not_a_retry(admin_client, monkeypatch):
    client, t, _ = admin_client
    up = upload(client, t, "acme_q2fy26_results_scanned.pdf", period="q3")
    rid, vid = up["report_id"], up["version"]["id"]
    drain()
    url = f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/ai-check"
    monkeypatch.setattr(get_settings(), "gemini_api_key", "test-key-not-real")

    def rejected(_body):
        raise ai_check.ProviderRejected("The AI provider rejected the platform's Gemini API key. Nothing was charged.")
    ai_check._OVERRIDE.append(rejected)
    est = client.get(url).json()["estimate"]
    assert client.post(url, json={"credits_shown": est["credits"]}).status_code == 202
    drain()
    last = client.get(url).json()["last"]
    assert last["status"] == "failed" and "rejected the platform's Gemini API key" in last["error"]


def test_ai_checks_uncertain_figures_by_itself_only_under_the_cap(admin_client, monkeypatch):
    from app.models import Job
    client, t, _ = admin_client
    cfg = get_settings()
    monkeypatch.setattr(cfg, "gemini_api_key", "test-key-not-real")
    calls = []

    def fake(body):
        calls.append(1)
        return {"candidates": [{"content": {"parts": [{"text": "[]"}]}}], "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 1}}
    ai_check._OVERRIDE.append(fake)

    def auto_jobs():
        with db.session(system_context()) as s:
            return s.scalars(select(Job).where(Job.tenant_id == t.id, Job.kind == "pipeline.ai_check")).all()

    # off by default: the button only
    monkeypatch.setattr(cfg, "ai_auto_check_max_credits", 0)
    upload(client, t, "acme_q2fy26_results_scanned.pdf", period="q1")
    drain()
    assert auto_jobs() == [] and calls == []

    # a cap below the estimate: still nothing runs
    monkeypatch.setattr(cfg, "ai_auto_check_max_credits", 1)
    monkeypatch.setattr(cfg, "ai_credit_usd", 0.000001)          # every estimate becomes many credits
    upload(client, t, "acme_q2fy26_results_scanned.pdf", period="q2")
    drain()
    assert auto_jobs() == [] and calls == []

    # within the cap: runs once by itself, and never again for that version
    monkeypatch.setattr(cfg, "ai_credit_usd", 0.01)
    monkeypatch.setattr(cfg, "ai_auto_check_max_credits", 50)
    up = upload(client, t, "acme_q2fy26_results_scanned.pdf", period="q3")
    drain()
    jobs_ = auto_jobs()
    assert len(jobs_) == 1 and jobs_[0].status == "succeeded" and calls
    n = len(calls)
    with db.session(system_context()) as s:
        from app.reports import enqueue_stage
        from app.tenancy import tenant_context
        ctx = tenant_context(None, t.id, None)
        v = s.get(ReportVersion, __import__("uuid").UUID(up["version"]["id"]))
    with db.session(ctx) as s:
        enqueue_stage(s, ctx, s.get(ReportVersion, v.id), "validate", force=True)
    drain()
    assert len(auto_jobs()) == 1 and len(calls) == n               # once per version


def _spread_pdf():
    import pymupdf
    doc = pymupdf.open()
    prose = doc.new_page()
    prose.insert_textbox(pymupdf.Rect(72, 90, 520, 700), ("The Company continued to invest behind its brands. " * 20),
                         fontname="helv", fontsize=11)
    spread = doc.new_page()
    spread.insert_text((60, 80), "Our Brands", fontname="hebo", fontsize=22)
    for i, (x, y) in enumerate(((60, 120), (320, 120), (60, 420), (320, 420))):
        spread.draw_rect(pymupdf.Rect(x, y, x + 220, y + 220), color=None, fill=(0.2 + 0.15 * i, 0.4, 0.7))
        spread.insert_text((x, y + 240), f"Caption for brand range {chr(65 + i)}", fontname="helv", fontsize=9)
    return doc.tobytes()


def test_ai_lays_out_design_pages_with_ids_only_cached_and_capped(monkeypatch):
    """The model returns ids; the page shows the PDF's own words as web sections. A second
    render reuses the cached layout (no call); a cap of 0 makes no call at all."""
    import json as _json

    from app import page_layouts, storage, validation
    from app.extraction.schema_builder import extract
    from app.render import site, theme as theming
    from tests.test_faq_story import META

    pdf = _spread_pdf()
    schema, _ = extract(pdf, META)
    t, ctx = _ctx()
    cfg = get_settings()
    monkeypatch.setattr(cfg, "gemini_api_key", "test-key-not-real")
    monkeypatch.setattr(cfg, "ai_layout_max_credits", 5)
    page_layouts._REJECTED_UNTIL[0] = 0.0
    seen = []

    def fake(body):
        prompt = body["contents"][0]["parts"][0]["text"]
        seen.append(prompt)
        ids = [line.split()[0] for line in prompt.split("Elements:\n", 1)[1].splitlines()]
        text_ids = [i for i in ids if i.startswith("t")]
        layout = {"sections": [{"type": "heading", "level": 1, "ids": text_ids[:1]},
                               {"type": "paragraph", "ids": ["t999"]},          # unknown id: dropped
                               {"type": "skip", "ids": text_ids[1:2]}]}          # long text can't be skipped
        return {"candidates": [{"content": {"parts": [{"text": _json.dumps(layout)}]}}],
                "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 60}}
    ai_check._OVERRIDE.append(fake)
    import uuid as _uuid
    vid = None                                      # no report version: usage is still recorded
    lays = page_layouts.layouts_for(ctx, vid, "sha-test-1", schema, pdf)
    assert list(lays) == [2] and len(seen) == 1
    assert "Caption for brand range A" in seen[0] and "Never write any text yourself" in seen[0]
    files = site.render_report(schema, theme=theming.validate({}), disclaimer="d", pdf_bytes=pdf, layouts=lays)
    html_ = [v for k, v in files.items() if k.endswith(".html")][0].decode()
    assert 'class="ai-page"' in html_ and '<h2 class="ai-h ai-h-a">Our Brands</h2>' in html_
    for c in "ABCD":                                    # nothing the model left out is lost
        assert html_.count(f"Caption for brand range {c}") == 1
    assert validation.check_bundle(schema, files) == []
    assert ai_usage.summary(ctx)["by_feature"][0]["feature"] == "page_layout"
    # cached: a second render makes no call
    assert page_layouts.layouts_for(ctx, vid, "sha-test-1", schema, pdf) == lays and len(seen) == 1
    # cap 0: no call, no layout (the page keeps its designed look)
    monkeypatch.setattr(cfg, "ai_layout_max_credits", 0)
    assert page_layouts.layouts_for(ctx, vid, "sha-test-2", schema, pdf) == {} and len(seen) == 1
    storage.delete(ctx, page_layouts.cache_key("sha-test-1"))


def test_a_rejected_key_stops_layout_calls(monkeypatch):
    from app import page_layouts
    from app.extraction.schema_builder import extract
    from tests.test_faq_story import META
    pdf = _spread_pdf()
    schema, _ = extract(pdf, META)
    _, ctx = _ctx()
    monkeypatch.setattr(get_settings(), "gemini_api_key", "bad")
    monkeypatch.setattr(get_settings(), "ai_layout_max_credits", 5)
    page_layouts._REJECTED_UNTIL[0] = 0.0
    calls = []

    def rejected(_b):
        calls.append(1)
        raise ai_check.ProviderRejected("no")
    ai_check._OVERRIDE.append(rejected)
    import uuid as _uuid
    assert page_layouts.layouts_for(ctx, _uuid.uuid4(), "sha-x", schema, pdf) == {}
    assert page_layouts.layouts_for(ctx, _uuid.uuid4(), "sha-y", schema, pdf) == {}
    assert len(calls) == 1                                 # paused after the first rejection
    page_layouts._REJECTED_UNTIL[0] = 0.0
