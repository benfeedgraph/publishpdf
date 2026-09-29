"""Validation checks against seeded errors, the LLM-never-touches-figures rule, the
rendered-page check, the DNS state machine, theme contrast, and RLS on report tables."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError

from app import db, domains, validation
from app.extraction import classify
from app.extraction.schema_builder import extract
from app.render import site
from app.render import theme as theming

CORPUS = Path(__file__).resolve().parents[2] / "corpus" / "synthetic"
META = {"company_name": "Acme Industries Limited", "report_type": "quarterly_results", "fiscal_year": 2026,
        "period": "q2", "currency": "INR", "reporting_unit": "₹ crore"}


@lru_cache
def _clean():
    data = (CORPUS / "acme_q2fy26_results.pdf").read_bytes()
    schema, artifact = extract(data, META)
    return data, schema, artifact


def clean():
    data, schema, artifact = _clean()
    return data, deepcopy(schema), artifact


def fig_by_raw(schema, raw, role="cell"):
    return next(f for f in schema["figures"].values() if f["raw"] == raw and f.get("role") == role)


def checks(schema, data, artifact, bundle=None, reextract=True):
    issues, _ = validation.run_all(schema, data, artifact, bundle, threshold=0.95, reextract=reextract)
    return issues


# ------------------------------------------------------------------ seeded errors


def test_clean_document_has_no_blocking_issues():
    data, schema, artifact = clean()
    bundle = site.render_report(schema, theme=theming.validate({}), disclaimer="d")
    issues = checks(schema, data, artifact, bundle)
    assert [i for i in issues if i.severity == "blocking"] == []


def test_seeded_wrong_digit_is_caught_by_traceability_and_reextraction():
    data, schema, artifact = clean()
    f = fig_by_raw(schema, "1,180.40")
    f["raw"], f["value"] = "1,186.40", "1186.40"            # one digit wrong
    issues = checks(schema, data, artifact)
    by_check = {i.check for i in issues if i.fid == f["id"] and i.severity == "blocking"}
    assert {"traceability", "re_extraction"} <= by_check


def test_seeded_sign_flip_is_caught():
    data, schema, artifact = clean()
    f = fig_by_raw(schema, "(12.30)")
    f["raw"], f["value"], f["negative"] = "12.30", "12.30", False
    issues = checks(schema, data, artifact)
    assert any(i.fid == f["id"] and i.check == "re_extraction" for i in issues)


def test_seeded_total_error_is_caught_by_arithmetic():
    data, schema, artifact = clean()
    # "Total income" for Q1 FY26 is 1,222.15 in the PDF; change the component instead so
    # the total no longer adds up in exactly one column.
    f = fig_by_raw(schema, "41.75")
    f["value"] = "44.75"
    issues = validation.check_arithmetic(schema)
    hit = [i for i in issues if i.check == "arithmetic" and i.severity == "blocking"]
    assert hit and hit[0].actual == "1,222.15" and hit[0].expected == "1225.15"


def test_balance_sheet_imbalance_is_blocking():
    _, schema, _ = clean()
    f = fig_by_raw(schema, "3,781.00")          # total assets, current period
    f["value"] = "3781.50"
    issues = validation.check_arithmetic(schema)
    assert any("Total assets don't equal" in i.message and i.severity == "blocking" for i in issues)


def test_stated_percentage_change_is_checked():
    _, schema, _ = clean()
    f = fig_by_raw(schema, "12.4%")
    f["value"] = "14.4"
    issues = validation.check_arithmetic(schema)
    assert any(i.fid == f["id"] and "Stated change" in i.message for i in issues)


def test_mixed_crore_and_lakh_without_label_is_blocking():
    _, schema, _ = clean()
    schema["units_mentioned"] = ["crore", "lakh"]
    tbl = next(b for s in schema["sections"] for b in s["blocks"] if b["type"] == "table")
    tbl["unit_source"] = "report_metadata"
    issues = validation.check_period_unit(schema)
    assert any(i.severity == "blocking" and "crore and lakh" in i.message for i in issues)


def test_period_mismatch_with_column_is_blocking():
    _, schema, _ = clean()
    f = fig_by_raw(schema, "1,180.40")
    f["period"] = {"raw": "Q2 FY25", "key": "quarter2:FY2025"}
    issues = validation.check_period_unit(schema)
    assert any(i.fid == f["id"] and i.severity == "blocking" for i in issues)


def test_low_confidence_ocr_value_needs_a_person():
    _, schema, _ = clean()
    f = fig_by_raw(schema, "48.20")
    f["method"], f["confidence"] = "ocr", 0.6
    issues = validation.check_low_confidence(schema, 0.95)
    assert any(i.fid == f["id"] and i.severity == "blocking" for i in issues)


def test_completeness_flags_uncaptured_numbers():
    data, schema, artifact = clean()
    victim = fig_by_raw(schema, "89.95")
    del schema["figures"][victim["id"]]
    issues = validation.check_completeness(schema, __import__("pymupdf").open(stream=data, filetype="pdf"), artifact)
    assert any(i.page == 2 and i.check == "completeness" for i in issues)


def test_confirmation_resolves_only_at_the_confirmed_value():
    data, schema, artifact = clean()
    f = fig_by_raw(schema, "1,180.40")
    f["raw"] = "1,186.40"
    f["review"] = {"action": "confirm", "raw": "1,180.40"}   # confirmed a *different* value earlier
    issues = checks(schema, data, artifact, reextract=False)
    validation.apply_reviews(issues, schema)
    assert any(i.fid == f["id"] and i.status == "open" for i in issues)
    f["review"] = {"action": "confirm", "raw": "1,186.40"}
    issues = checks(schema, data, artifact, reextract=False)
    validation.apply_reviews(issues, schema)
    assert all(i.status == "resolved" for i in issues if i.fid == f["id"])


# ------------------------------------------------------------------ rendered page check


def _bundle(schema):
    return site.render_report(schema, theme=theming.validate({}), disclaimer="Filed under Rule 33 of 2015.")


def test_rendered_page_check_passes_on_generated_pages_and_ignores_disclaimer_digits():
    _, schema, _ = clean()
    assert validation.check_bundle(schema, _bundle(schema)) == []


def test_rendered_page_check_catches_a_number_not_in_the_schema():
    _, schema, _ = clean()
    b = _bundle(schema)
    key = "fy2026/q2/results/index.html"
    b[key] = b[key].replace(b"</main>", b"<p>Revenue up 99.9%</p></main>")
    issues = validation.check_bundle(schema, b)
    assert any(i.actual == "99.9%" and i.severity == "blocking" for i in issues)


def test_rendered_page_check_catches_an_altered_figure():
    _, schema, _ = clean()
    f = fig_by_raw(schema, "1,234.56")
    b = _bundle(schema)
    key = "fy2026/q2/results/index.html"
    b[key] = b[key].replace(f'data-fig="{f["id"]}">1,234.56<'.encode(), f'data-fig="{f["id"]}">1,243.56<'.encode())
    issues = validation.check_bundle(schema, b)
    assert any(i.fid == f["id"] and i.actual == "1,243.56" for i in issues)


def test_rendered_page_check_catches_numbers_in_title_and_jsonld():
    _, schema, _ = clean()
    b = _bundle(schema)
    key = "fy2026/q2/results/index.html"
    b[key] = b[key].replace(b"<title>", b"<title>Up 42% \xc2\xb7 ", 1)
    b[key] = b[key].replace(b'"inLanguage": "en"', b'"inLanguage": "en", "revenue": 1234.5', 1)
    issues = validation.check_bundle(schema, b)
    assert {"42%", "1234.5"} <= {i.actual for i in issues}


def test_markdown_numbers_must_come_from_the_schema():
    _, schema, _ = clean()
    b = _bundle(schema)
    b["fy2026/q2/results/report.md"] += b"\nRevenue 7,777.77\n"
    assert any(i.actual == "7,777.77" for i in validation.check_bundle(schema, b))


def test_text_runs_never_contain_digits():
    _, schema, _ = clean()
    def walk(o):
        if isinstance(o, dict):
            if "t" in o and isinstance(o["t"], str):
                assert not any(ch.isdigit() for ch in o["t"]), o
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(schema["sections"])


# ------------------------------------------------------------------ LLM never alters figures


def test_llm_output_can_only_be_section_types_for_known_ids():
    ids = {"s1", "s2"}
    assert classify.parse_llm_classification('{"s1": "profit_and_loss", "s2": "outlook"}', ids) == \
        {"s1": "profit_and_loss", "s2": "outlook"}
    assert classify.parse_llm_classification('{"s1": "revenue 1,234.56", "s9": "outlook", "s2": "profit"}', ids) == {}
    assert classify.parse_llm_classification('not json', ids) == {}
    assert classify.parse_llm_classification('[{"id": "s1", "type": "notes", "value": "12.4"}]', ids) == {}


def test_llm_request_never_contains_digits():
    req = classify.build_llm_request([{"id": "s1", "heading_text": "Q2 FY26 revenue ₹1,234.56 crore",
                                        "sample_text": "grew 12.4% to 30.09.2025"}])
    body = json.loads(req)
    assert not any(ch.isdigit() for ch in body["sections"][0]["heading"] + body["sections"][0]["sample"])


def test_adversarial_llm_cannot_change_any_figure():
    data = (CORPUS / "acme_q2fy26_results.pdf").read_bytes()
    baseline, _ = extract(data, META)

    def evil_llm(prompt: str) -> str:
        req = json.loads(prompt)
        out = {s["id"]: "notes" for s in req["sections"]}
        out["figures"] = {"p2f6": {"raw": "9,999.99"}}         # tries to inject a number
        out["s1"] = "revenue 9,999.99"                          # tries to smuggle digits in a type
        return json.dumps(out)

    attacked, _ = extract(data, META, llm=evil_llm)
    snap = lambda s: sorted((f["id"], f["raw"], f["value"], f["iso"]) for f in s["figures"].values())  # noqa: E731
    assert snap(attacked) == snap(baseline)
    assert all(sec["type"] in classify.SECTION_TYPES for sec in attacked["sections"])
    assert attacked["metadata"]["extraction"]["llm_assist"] is True


# ------------------------------------------------------------------ DNS state machine


def view(cname="edge.platform.example.com", txt=None, caa=None, err=None, token="tok"):
    return domains.DnsView(cname, err, txt if txt is not None else [f"ppdftest-verify={token}"], caa or [])


def ev(v, status="pending", tls=None):
    return domains.evaluate("investors.client.com", "tok", status, v, tls or (lambda h: (False, None, "handshake")))


def test_apex_and_platform_domains_are_rejected():
    for bad, msg in [("client.com", "root"), ("client.co.in", "root"), ("x.platform.example.com", "platform"),
                     ("bad_host.client.com", "valid"), ("com", "register")]:
        with pytest.raises(domains.DomainError, match=msg):
            domains.normalize_hostname(bad)
    assert domains.normalize_hostname("https://Investors.Client.com/") == "investors.client.com"
    assert domains.normalize_hostname("ir.client.co.in") == "ir.client.co.in"


def test_records_and_it_email():
    rs = domains.records("investors.client.com", "abc")
    assert rs[0] == {"type": "CNAME", "name": "investors.client.com", "host_label": "investors",
                     "value": "edge.platform.example.com", "ttl": 3600}
    assert rs[1]["name"] == "_ppdftest.investors.client.com" and rs[1]["value"] == "ppdftest-verify=abc"
    assert rs[1]["host_label"] == "_ppdftest.investors"
    email = domains.it_email("Acme", "investors.client.com", "abc")
    assert "DNS only" in email and "ppdftest-verify=abc" in email


@pytest.mark.parametrize("v,code", [
    (view(cname=None), "cname_missing"),
    (view(cname="acme.herokudns.com"), "cname_elsewhere"),
    (view(cname="investors.client.com.cdn.cloudflare.net"), "cname_proxied"),
    (view(txt=[]), "txt_missing"),
    (view(txt=["ppdftest-verify=wrong"]), "txt_mismatch"),
    (view(caa=[("issue", "digicert.com")]), "caa_blocks"),
])
def test_dns_failures_have_specific_reasons(v, code):
    r = ev(v)
    assert r.status == "failed" and r.failure_code == code and r.failure_reason


def test_caa_allowing_our_ca_passes():
    assert ev(view(caa=[("issue", "letsencrypt.org")])).status == "ssl_issuing"


def test_happy_path_pending_to_ssl_issuing_to_live():
    assert ev(view(), "pending").status == "ssl_issuing"
    exp = datetime.now(timezone.utc) + timedelta(days=80)
    r = ev(view(), "ssl_issuing", tls=lambda h: (True, exp, None))
    assert r.status == "live" and r.cert_expires_at == exp


def test_live_domain_breaks_when_cname_removed_or_cert_expiring():
    exp = datetime.now(timezone.utc) + timedelta(days=80)
    assert ev(view(cname=None), "live", tls=lambda h: (True, exp, None)).failure_code == "cname_missing"
    soon = datetime.now(timezone.utc) + timedelta(days=2)
    assert ev(view(), "live", tls=lambda h: (True, soon, None)).failure_code == "cert_expiring"
    assert ev(view(), "live", tls=lambda h: (False, None, "expired")).failure_code == "cert_failed"


def test_dns_timeout_keeps_state():
    r = ev(view(cname=None, err="timeout"), "live")
    assert r.status == "live" and r.failure_code == "dns_timeout"


def test_domain_flow_through_api_to_live_https(client, monkeypatch):
    """Subdomain entry -> records -> check (pending) -> fix DNS -> ssl_issuing -> live;
    canonical + sitemap switch to the custom domain and preview 301s to it."""
    from app import domain_jobs
    from app.public import app as public_app, clear_caches
    from fastapi.testclient import TestClient
    from tests.conftest import add_member, make_tenant, make_user, sign_in
    from tests.test_pipeline_e2e import drain, upload
    from app.tenancy import Role

    t, u = make_tenant("Acme"), make_user()
    add_member(t, u, Role.client_admin)
    sign_in(client, u)
    up = upload(client, t, "acme_q2fy26_results.pdf")
    drain()
    client.post(f"/api/tenants/{t.id}/reports/{up['report_id']}/versions/{up['version']['id']}/publish",
                json={"confirm_reviewed": True})

    assert client.post(f"/api/tenants/{t.id}/domain", json={"hostname": "acme.com"}).status_code == 400
    host = f"ir-{t.slug}.client-example.com"
    d = client.post(f"/api/tenants/{t.id}/domain", json={"hostname": host}).json()
    token = d["records"][1]["value"].split("=", 1)[1]
    state = {"view": domains.DnsView(None, "nxdomain", [], [])}
    exp = datetime.now(timezone.utc) + timedelta(days=89)
    certs = {"ok": False}
    monkeypatch.setattr(domain_jobs, "RESOLVER", lambda h: state["view"])
    monkeypatch.setattr(domain_jobs, "TLS_PROBE", lambda h: (certs["ok"], exp if certs["ok"] else None, "no cert"))

    r = client.post(f"/api/tenants/{t.id}/domain/check").json()
    assert r["status"] == "failed" and r["failure_code"] == "cname_missing"
    state["view"] = domains.DnsView("edge.platform.example.com", None, [f"ppdftest-verify={token}"], [])
    assert client.post(f"/api/tenants/{t.id}/domain/check").json()["status"] == "ssl_issuing"
    clear_caches()
    pub = TestClient(public_app)
    assert pub.get("/internal/tls-ask", params={"domain": host, "token": "test-internal-token"}).status_code == 200
    assert pub.get("/internal/tls-ask", params={"domain": "evil.example.org", "token": "test-internal-token"}).status_code == 404
    assert pub.get("/internal/tls-ask", params={"domain": host, "token": "wrong"}).status_code == 403
    certs["ok"] = True
    assert client.post(f"/api/tenants/{t.id}/domain/check").json()["status"] == "live"

    clear_caches()
    page = pub.get("/fy2026/q2/results/", headers={"host": host}).text
    assert f'<link rel="canonical" href="https://{host}/fy2026/q2/results/">' in page
    assert f"https://{host}/fy2026/q2/results/" in pub.get("/sitemap.xml", headers={"host": host}).text
    assert "Allow: /" in pub.get("/robots.txt", headers={"host": host}).text
    moved = pub.get("/fy2026/q2/results/?x=1", headers={"host": f"{t.slug}.preview.platform.example.com"},
                    follow_redirects=False)
    assert moved.status_code == 301 and moved.headers["location"] == f"https://{host}/fy2026/q2/results/?x=1"

    # monitoring: CNAME removed -> failed + alert email to client admins
    from app import emailer
    before = len(emailer.OUTBOX)
    state["view"] = domains.DnsView(None, "nxdomain", [], [])
    assert client.post(f"/api/tenants/{t.id}/domain/check").json()["status"] == "failed"
    assert any(m.to == u.email and "Action needed" in m.subject for m in emailer.OUTBOX[before:])


# ------------------------------------------------------------------ theme


def test_contrast_is_enforced_and_suggestions_are_given():
    t = theming.validate({"colors": {"primary": "#FFFF66", "text": "#BBBBBB"}})
    report = theming.contrast_report(t)
    bad = [r for r in report if not r["ok"]]
    assert bad and all("suggestion" in r for r in bad)
    used, changes = theming.enforce(t)
    assert all(r["ok"] for r in theming.contrast_report(used)) and changes


def test_theme_rejects_css_injection():
    for bad in ({"colors": {"primary": "red;} body{display:none"}}, {"typography": {"body_font": "x'; }"}},
                {"header": {"style": "evil"}}, {"script": "x"}):
        with pytest.raises(theming.ThemeError):
            theming.validate(bad)


def test_disclaimer_is_sanitised():
    html = site.disclaimer_html('<script>alert(1)</script> **Bold** [link](https://ok.example/x) [bad](javascript:x)')
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<strong>Bold</strong>" in html and 'href="https://ok.example/x"' in html
    assert 'href="javascript' not in html


def test_ssrf_guard_blocks_private_addresses():
    from app.render import analyze
    for url in ("http://127.0.0.1/", "http://localhost/", "http://10.0.0.5/", "http://169.254.169.254/latest",
                "file:///etc/passwd", "http://example.com:2375/"):
        with pytest.raises(analyze.FetchError):
            analyze.safe_get(url)


# ------------------------------------------------------------------ RLS on report tables


@pytest.mark.parametrize("table", ["reports", "report_versions", "source_files", "validation_issues",
                                   "figure_reviews", "comments", "tenant_settings", "domains", "edge_hits"])
def test_report_tables_are_rls_protected(table):
    with db.get_engine().connect() as c:
        row = c.execute(text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t "
                               "AND relnamespace = 'public'::regnamespace"),
                        {"t": table}).one()
    assert row == (True, True)


def test_cannot_write_report_into_another_tenant():
    from app.models import Report
    from app.tenancy import Role, tenant_context
    from tests.conftest import make_tenant, make_user
    a, b, u = make_tenant(), make_tenant(), make_user()
    with pytest.raises(ProgrammingError, match="row-level security"):
        with db.session(tenant_context(u.id, a.id, Role.client_admin)) as s:
            s.add(Report(tenant_id=b.id, company_name="x", report_type="other", fiscal_year=2026, period="q1",
                         currency="INR", reporting_unit="crore"))
    with db.session(tenant_context(u.id, a.id, Role.client_admin)) as s:
        assert s.scalars(select(Report).where(Report.tenant_id == b.id)).all() == []


def test_figure_review_trail_is_append_only():
    with db.get_engine().connect() as c:
        privs = c.execute(text("SELECT has_table_privilege(current_user, 'figure_reviews', 'UPDATE'), "
                               "has_table_privilege(current_user, 'figure_reviews', 'DELETE')")).one()
    assert privs == (False, False)
