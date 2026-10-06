"""Email-code sign-in & sign-up, two-step upload with metadata detection, palette/PDF
theming, the second-parser and cross-table agents, consensus, and the story page."""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import db, emailer, validation
from app.config import get_settings
from app.models import EmailCode
from app.render import analyze, site, story
from app.render import theme as theming
from app.tenancy import system_context
from tests.conftest import make_user
from tests.test_checks import clean, fig_by_raw
from tests.test_pipeline_e2e import drain

CORPUS = Path(__file__).resolve().parents[2] / "corpus" / "synthetic"


def _last_code(email: str) -> str:
    for m in reversed(emailer.OUTBOX):
        if m.to == email:
            return re.search(r"\b(\d{6})\b", m.body).group(1)
    raise AssertionError("no code email")


def _client(app):
    c = TestClient(app)
    c.headers["x-ppdf-csrf"] = "1"
    return c


# ------------------------------------------------------------------ email codes


def test_existing_user_signs_in_with_a_code(client):
    u = make_user()
    r = client.post("/api/auth/code", json={"email": u.email})
    assert r.status_code == 202 and "dev_code" not in r.json()        # only in development
    r = client.post("/api/auth/code/verify", json={"email": u.email, "code": _last_code(u.email)})
    assert r.json()["status"] == "signed_in"
    assert r.json()["me"]["user"]["email"] == u.email               # no second round trip needed
    assert client.get("/api/auth/me").json() == r.json()["me"]


def test_new_email_signs_up_and_owns_a_workspace(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "mfa_required_for_admins", False)     # the product default
    email = "founder-" + make_user().email      # unique, no account for this exact address
    client.post("/api/auth/code", json={"email": email})
    r = client.post("/api/auth/code/verify", json={"email": email, "code": _last_code(email)})
    assert r.json()["status"] == "needs_profile"
    r = client.post("/api/auth/signup", json={"signup_token": r.json()["signup_token"], "name": "Asha", "company": "Zenith Power Ltd"})
    assert r.status_code == 200
    me = client.get("/api/auth/me").json()
    assert me["fully_authenticated"] and me["tenants"][0]["name"] == "Zenith Power Ltd"
    assert me["tenants"][0]["role"] == "client_admin"
    # they can upload straight away
    t = me["tenants"][0]["id"]
    data = (CORPUS / "acme_q2fy26_results.pdf").read_bytes()
    r = client.post(f"/api/tenants/{t}/reports/inspect", files={"file": ("r.pdf", data, "application/pdf")})
    assert r.status_code == 200, r.text


def test_wrong_codes_are_limited_and_codes_are_single_use(client):
    u = make_user()
    client.post("/api/auth/code", json={"email": u.email})
    code = _last_code(u.email)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        assert client.post("/api/auth/code/verify", json={"email": u.email, "code": wrong}).status_code == 400
    r = client.post("/api/auth/code/verify", json={"email": u.email, "code": code})
    assert r.status_code == 400 and "Too many" in r.json()["detail"]
    client.post("/api/auth/code", json={"email": u.email})
    code = _last_code(u.email)
    assert client.post("/api/auth/code/verify", json={"email": u.email, "code": code}).status_code == 200
    other = _client(client.app)
    assert other.post("/api/auth/code/verify", json={"email": u.email, "code": code}).status_code == 400


def test_only_the_code_hash_is_stored(client):
    u = make_user()
    client.post("/api/auth/code", json={"email": u.email})
    code = _last_code(u.email)
    with db.session(system_context()) as s:
        rows = s.scalars(select(EmailCode.code_hash).where(EmailCode.email == u.email)).all()
    assert rows and all(code not in h for h in rows)


def test_code_requests_are_rate_limited(client):
    u = make_user()
    for _ in range(5):
        assert client.post("/api/auth/code", json={"email": u.email}).status_code == 202
    assert client.post("/api/auth/code", json={"email": u.email}).status_code == 429


def test_two_factor_is_optional_by_default(client, monkeypatch):
    from tests.conftest import add_member, make_tenant
    from app.tenancy import Role
    monkeypatch.setattr(get_settings(), "mfa_required_for_admins", False)
    t, u = make_tenant(), make_user()
    add_member(t, u, Role.client_admin)
    client.post("/api/auth/code", json={"email": u.email})
    client.post("/api/auth/code/verify", json={"email": u.email, "code": _last_code(u.email)})
    assert client.get(f"/api/tenants/{t.id}").status_code == 200        # no 2FA wall


def test_signup_can_be_disabled(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "allow_self_signup", False)
    email = "nobody-" + make_user().email
    client.post("/api/auth/code", json={"email": email})
    r = client.post("/api/auth/code/verify", json={"email": email, "code": _last_code(email)})
    assert r.status_code == 400 and "invitation" in r.json()["detail"]


# ------------------------------------------------------------------ two-step upload


def test_inspect_detects_metadata_then_create_processes(client):
    from tests.conftest import add_member, make_tenant, sign_in
    from app.tenancy import Role
    t, u = make_tenant(), make_user()
    add_member(t, u, Role.client_admin)
    sign_in(client, u)
    data = (CORPUS / "acme_q2fy26_results.pdf").read_bytes()
    r = client.post(f"/api/tenants/{t.id}/reports/inspect", files={"file": ("acme.pdf", data, "application/pdf")})
    d = r.json()["detected"]
    assert d["company_name"] == "Acme Industries Limited" and d["fiscal_year"] == 2026 and d["period"] == "q2"
    assert d["currency"] == "INR" and d["reporting_unit"] == "₹ crore" and d["report_type"] == "quarterly_results"
    r = client.post(f"/api/tenants/{t.id}/reports/create", json={
        "source_file_id": r.json()["source_file_id"], "company_name": d["company_name"], "report_type": d["report_type"],
        "fiscal_year": d["fiscal_year"], "period": d["period"], "currency": d["currency"], "reporting_unit": d["reporting_unit"]})
    assert r.status_code == 201, r.text
    drain()
    v = client.get(f"/api/tenants/{t.id}/reports/{r.json()['report_id']}/versions/{r.json()['version']['id']}").json()
    assert v["status"] == "needs_review"
    assert v["validation"]["consensus"]["3+"] > 100
    assert {a["key"] for a in v["validation"]["agents"]} >= {"traceability", "second_parser", "re_extraction", "cross_table"}


def test_inspect_rejects_other_tenants_source(client):
    from tests.conftest import add_member, make_tenant, sign_in
    from app.tenancy import Role
    a, b, ua, ub = make_tenant(), make_tenant(), make_user(), make_user()
    add_member(a, ua, Role.client_admin)
    add_member(b, ub, Role.client_admin)
    sign_in(client, ua)
    r = client.post(f"/api/tenants/{a.id}/reports/inspect", files={"file": ("x.pdf", (CORPUS / "borealis_annual_usd.pdf").read_bytes(), "application/pdf")})
    src = r.json()["source_file_id"]
    other = _client(client.app)
    sign_in(other, ub)
    r = other.post(f"/api/tenants/{b.id}/reports/create", json={"source_file_id": src, "company_name": "X", "report_type": "other",
                                                                "fiscal_year": 2026, "period": "q1", "currency": "USD", "reporting_unit": "USD million"})
    assert r.status_code == 404


# ------------------------------------------------------------------ design sources


def test_palette_keeps_brand_order_and_assigns_background_and_text():
    t = analyze.palette_proposal(["#0B3D91", "#F2A900", "#FFFFFF", "#111111"])["theme"]["colors"]
    assert (t["primary"], t["secondary"], t["background"], t["text"]) == ("#0B3D91", "#F2A900", "#FFFFFF", "#111111")
    with pytest.raises(analyze.FetchError):
        analyze.palette_proposal(["red"])


def test_theme_from_a_brand_pdf():
    p = analyze.analyze_pdf((CORPUS / "acme_q2fy26_presentation.pdf").read_bytes())
    assert theming.validate(p["theme"]) and p["source"] == "your PDF"


# ------------------------------------------------------------------ new agents


def test_second_parser_flags_a_value_it_reads_differently():
    data, schema, artifact = clean()
    f = fig_by_raw(schema, "1,180.40")
    f["raw"] = "1,180.46"
    issues = validation.check_second_parser(schema, data, artifact)
    assert any(i.fid == f["id"] and i.severity == "blocking" for i in issues)


def test_cross_table_inconsistency_is_reported():
    _, schema, _ = clean()
    f = fig_by_raw(schema, "1,234.56")
    twin = deepcopy(f)
    twin.update(id="p9f1", table_id="bX", raw="1,243.56", value="1243.56")
    schema["figures"]["p9f1"] = twin
    issues = validation.check_cross_tables(schema)
    assert any(i.check == "cross_table" and i.actual == "1,243.56" for i in issues)


def test_consensus_counts_independent_confirmations():
    data, schema, artifact = clean()
    _, summary = validation.run_all(schema, data, artifact, None, threshold=0.95)
    assert summary["consensus"]["0"] == 0 and summary["consensus"]["3+"] > 100


# ------------------------------------------------------------------ report page


def test_results_report_is_one_page_with_every_table_and_passes_the_web_page_check():
    _, schema, _ = clean()
    files = site.render_report(schema, theme=theming.validate({}), disclaimer="Filed under Rule 33.")
    assert [k for k in files if k.endswith(".html")] == ["fy2026/q2/results/index.html"]
    html = files["fy2026/q2/results/index.html"].decode()
    assert validation.check_bundle(schema, files) == []
    # every table cell figure is on the page, with its exact raw string
    cells = [f for f in schema["figures"].values() if f.get("role") == "cell" and f.get("status") == "active"]
    assert cells and all(re.search(rf'data-fig="{f["id"]}"[^>]*>{re.escape(f["raw"])}<', html) for f in cells)


def test_long_tables_collapse_without_javascript():
    _, schema, _ = clean()
    files = site.render_report(schema, theme=theming.validate({}), disclaimer="d")
    html = files["fy2026/q2/results/index.html"].decode()
    assert 'class="rows-toggle"' in html and 'class="extra"' in html and "<script" not in html.split("</head>")[1]


# ------------------------------------------------------------------ real-report edge cases (regression)


def test_index_page_numbers_microtext_currency_and_wrapped_dates_all_verify():
    """From a real report: short page numbers in a dotted-rule index table, a hidden
    0.66pt text layer behind a chart image, 'Rs.' prefixes, 'x' multiples and a date
    that wraps across lines must all verify (or be excluded) without false blocks."""
    from app.extraction.schema_builder import extract
    data = (CORPUS / "edge_cases_index_microtext.pdf").read_bytes()
    schema, artifact = extract(data, {"company_name": "Edge", "report_type": "other", "fiscal_year": 2027, "period": "q1",
                                      "currency": "INR", "reporting_unit": "₹ crore"})
    raws = [f["raw"] for f in schema["figures"].values()]
    assert "15,000" not in raws                                  # hidden microtext excluded
    assert {"16", "17", "Rs.8.0", "2.1x"} <= set(raws)
    issues, summary = validation.run_all(schema, data, artifact, None, threshold=0.95)
    blocking = [(i.check, i.expected, i.actual) for i in issues if i.severity == "blocking"]
    assert blocking == [], blocking
    assert any("too small to read" in i.message for i in issues)
    # ...and a wrong digit in the same places is still caught
    f = next(f for f in schema["figures"].values() if f["raw"] == "16")
    f["raw"], f["value"] = "18", "18"
    issues, _ = validation.run_all(schema, data, artifact, None, threshold=0.95)
    assert any(i.fid == f["id"] and i.check == "re_extraction" for i in issues)
