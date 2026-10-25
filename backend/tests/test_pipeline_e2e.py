"""Upload -> extract -> render -> validate -> review -> publish -> public site, through
the real API and job queue, on the synthetic corpus."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import db, jobs
from app.models import Report, ReportVersion
from app.tenancy import Role, system_context
from tests.conftest import add_member, make_tenant, make_user, sign_in

CORPUS = Path(__file__).resolve().parents[2] / "corpus" / "synthetic"
META = {"company_name": "Acme Industries Limited", "report_type": "quarterly_results", "fiscal_year": "2026",
        "period": "q2", "currency": "INR", "reporting_unit": "₹ crore"}


def drain() -> None:
    import app.domain_jobs  # noqa: F401
    import app.pipeline  # noqa: F401
    for _ in range(200):
        if not jobs.run_one("test-worker"):
            return
    raise AssertionError("job queue did not drain")


def upload(client, tenant, name: str, **meta) -> dict:
    data = (CORPUS / name).read_bytes()
    r = client.post(f"/api/tenants/{tenant.id}/reports", files={"file": (name, data, "application/pdf")},
                    data={**META, **meta})
    assert r.status_code == 201, r.text
    return r.json()


def _long_pdf_bytes(pages: int = 22) -> bytes:
    """A synthetic report with 20+ pages: a highlights page (KPI tiles + bullet highlights)
    then many segment pages with small tables. Built with reportlab's built-in Helvetica so
    it needs no font files. Not part of the fixed corpus — only the landing-page test needs
    a long document, and the corpus maxes out at 4 pages."""
    import io

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4

    def text(x, y, s, size=10, bold=False, align="left"):
        from reportlab.pdfbase.pdfmetrics import stringWidth
        font = "Helvetica-Bold" if bold else "Helvetica"
        w = stringWidth(s, font, size)
        if align == "right":
            x -= w
        c.setFont(font, size)
        c.drawString(x, H - y, s)

    def para(x, y, width, s, size=10, leading=14):
        from reportlab.pdfbase.pdfmetrics import stringWidth
        line = ""
        for word in s.split(" "):
            trial = f"{line} {word}".strip()
            if stringWidth(trial, "Helvetica", size) > width and line:
                text(x, y, line, size); y += leading; line = word
            else:
                line = trial
        if line:
            text(x, y, line, size); y += leading
        return y

    text(40, 70, "Q2 FY26 Results", 20, bold=True)
    text(40, 92, "Quarter ended 30.09.2025", 10)
    text(40, 125, "Key highlights", 14, bold=True)
    for i, (label, val) in enumerate([("Revenue", "Rs.1,234.56 crore"), ("EBITDA margin", "19.9%"),
                                      ("Net profit", "Rs.142.35 crore")]):
        x = 40 + i * 170
        c.setStrokeGray(0.7)
        c.rect(x, H - 210, 150, 60)
        text(x + 12, 165, label, 10)
        text(x + 12, 190, val, 15, bold=True)
    y = 240
    for b in ["Revenue from operations grew 12.4% year on year to Rs.1,234.56 crore.",
              "EBITDA rose to Rs.245.10 crore with a margin of 19.9%, up 110 bps.",
              "Net profit for the quarter was Rs.142.35 crore against Rs.118.20 crore.",
              "The Board declared an interim dividend of Rs.3.50 per share on 24.10.2025."]:
        y = para(40, y, W - 80, "• " + b, 10, 15) + 2
    text(40, y + 16, "Outlook", 14, bold=True)
    para(40, y + 36, W - 80, "Management expects demand to remain steady in H2 FY26 and maintains its "
         "capital expenditure plan of Rs.600 crore for FY26.", 10)
    c.showPage()

    divisions = ["Chemicals", "Materials", "Services", "Logistics", "Retail", "Energy", "Healthcare",
                 "Technology", "Agriculture", "Textiles", "Automotive", "Construction", "Media",
                 "Finance", "Hospitality", "Mining", "Pharma", "Telecom", "Utilities", "Aerospace"]
    for i in range(2, pages + 1):
        div = divisions[(i - 2) % len(divisions)]
        text(40, 70, f"Segment Results — {div} Division", 14, bold=True)
        text(40, 88, "(Rs. in crore)", 9)
        bse = 100 + i
        y = 120
        text(40, y, "Line item", 9, bold=True)
        text(360, y, "Q2 FY26", 9, bold=True, align="right")
        text(460, y, "Q2 FY25", 9, bold=True, align="right")
        y += 16
        for label, a, b in [("Revenue", bse * 10.0, bse * 9.1), ("Expenses", bse * 6.2, bse * 5.8),
                            ("Segment profit", bse * 3.8, bse * 3.3)]:
            text(40, y, label, 9)
            text(360, y, f"{a:,.2f}", 9, align="right")
            text(460, y, f"{b:,.2f}", 9, align="right")
            y += 15
        para(40, y + 20, W - 80, f"The {div} division delivered steady growth in Q2 FY26, with margins "
             "broadly stable compared with the prior year.", 10)
        c.showPage()
    c.save()
    return buf.getvalue()


def _inspect_and_create(client, tenant, data: bytes, *, landing: bool, build_mode: str = "layout", **meta) -> dict:
    """The two-step upload flow: /inspect (which returns build options) then /create."""
    ins = client.post(f"/api/tenants/{tenant.id}/reports/inspect",
                      files={"file": ("long.pdf", data, "application/pdf")})
    assert ins.status_code == 200, ins.text
    body = ins.json()
    credits = next(o["max_credits"] for o in body["build_options"]["options"] if o["mode"] == build_mode)
    r = client.post(f"/api/tenants/{tenant.id}/reports/create", json={
        "source_file_id": body["source_file_id"], **META, **meta,
        "build_mode": build_mode, "credits_shown": credits, "landing": landing})
    assert r.status_code == 201, r.text
    return {"inspect": body, **r.json()}


@pytest.fixture
def admin_client(client):
    t = make_tenant("Acme Industries Limited")
    u = make_user()
    add_member(t, u, Role.client_admin)
    sign_in(client, u)
    return client, t, u


def _version(client, t, rid, vid) -> dict:
    return client.get(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}").json()


def test_clean_pdf_goes_all_the_way_to_a_public_page(admin_client):
    client, t, _ = admin_client
    up = upload(client, t, "acme_q2fy26_results.pdf")
    rid, vid = up["report_id"], up["version"]["id"]
    drain()
    v = _version(client, t, rid, vid)
    assert v["status"] == "needs_review", v
    assert v["validation"]["blocking"] == 0 and v["validation"]["figures_checked"] > 100
    assert [j["kind"] for j in v["jobs"]] == ["pipeline.extract", "pipeline.render", "pipeline.validate"]
    # the review screen's as-printed page images are a separate job, off the critical path
    from app.models import Job
    with db.session(system_context()) as s:
        assert s.scalars(select(Job).where(Job.kind == "pipeline.page_images", Job.tenant_id == t.id)).first() is not None

    schema = client.get(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/schema").json()
    raws = {f["raw"] for f in schema["figures"].values()}
    assert {"1,234.56", "(12.30)", "12.4%", "30.09.2025"} <= raws

    preview = client.get(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/preview/")
    assert preview.status_code == 200 and "noindex" in preview.text and 'data-fig="' in preview.text
    # the dashboard links without the trailing slash (hosted /api forwarding drops those)
    bare = client.get(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/preview", follow_redirects=False)
    assert bare.status_code == 200 and bare.text == preview.text

    # publish needs the review checkbox
    assert client.post(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/publish", json={}).status_code == 400
    r = client.post(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/publish", json={"confirm_reviewed": True})
    assert r.status_code == 200, r.text and r.json()["is_live"]

    # the public preview host serves it without JavaScript, noindex, canonical to itself
    from app.public import app as public_app, clear_caches
    clear_caches()
    pub = TestClient(public_app)
    host = f"{t.slug}.preview.platform.example.com"
    page = pub.get("/fy2026/q2/results/", headers={"host": host})
    assert page.status_code == 200
    assert page.headers["x-robots-tag"].startswith("noindex")
    assert f'<link rel="canonical" href="https://{host}/fy2026/q2/results/">' in page.text
    assert 'class="web"' in page.text and ">1,234.56<" in page.text and ">(12.30)<" in page.text  # web edition
    art = pub.get("/fy2026/q2/results/pages/p0001.webp", headers={"host": host})
    assert art.status_code == 200 and art.headers["content-type"] == "image/webp"
    assert pub.get("/fy2026/q2/results/full/", headers={"host": host}).status_code == 404   # no second page
    # readable with JavaScript disabled: the only <script> is non-executable JSON-LD
    assert page.text.count("<script") == page.text.count('<script type="application/ld+json">') == 1
    # served compressed (a 400-page report is ~7 MB of HTML, ~1 MB gzipped)
    gz = pub.get("/fy2026/q2/results/", headers={"host": host, "accept-encoding": "gzip"})
    assert gz.headers.get("content-encoding") == "gzip" and gz.text == page.text
    assert pub.get("/latest/", headers={"host": host}).status_code == 200
    assert "Disallow: /" in pub.get("/robots.txt", headers={"host": host}).text          # preview: never indexed
    sm = pub.get("/sitemap.xml", headers={"host": host}).text
    assert "/fy2026/q2/results/</loc>" in sm and "/statement-of-profit-and-loss/" not in sm
    assert "report.md" in pub.get("/llms.txt", headers={"host": host}).text
    assert pub.get("/fy2026/q2/results/figures.json", headers={"host": host}).json()["figures"]
    pdf = pub.get("/fy2026/q2/results/source.pdf", headers={"host": host})
    assert pdf.content.startswith(b"%PDF-")
    # the same site by path on the API (the hosted platform: no preview hosts, no site server)
    from app.config import get_settings
    cfg = get_settings()
    base = f"https://{cfg.platform_domain}/sites/{t.slug}"
    old = (cfg.public_sites_base_url, cfg.preview_url_pattern)
    cfg.public_sites_base_url = None
    cfg.preview_url_pattern = "{tenant_slug}.preview.localhost"          # copied from a laptop's .env
    import os
    os.environ["VERCEL"] = "1"
    try:
        clear_caches()
        assert cfg.preview_origin(t.slug) == base                          # never localhost once deployed
        assert client.get(f"/api/tenants/{t.id}/settings").json()["site_origin"] == base
        by_path = client.get(f"/sites/{t.slug}/fy2026/q2/results/")
        assert by_path.status_code == 200 and by_path.headers["x-robots-tag"].startswith("noindex")
        assert f'<link rel="canonical" href="{base}/fy2026/q2/results/">' in by_path.text
        assert "localhost" not in by_path.text and ">1,234.56<" in by_path.text
        assert client.get(f"/sites/{t.slug}/fy2026/q2/results/source.pdf").content.startswith(b"%PDF-")
        assert client.get(f"/sites/{t.slug}", follow_redirects=False).headers["location"] == f"/sites/{t.slug}/"
        assert client.get("/sites/no-such-workspace/").status_code == 404
    finally:
        os.environ.pop("VERCEL", None)
        cfg.public_sites_base_url, cfg.preview_url_pattern = old
        clear_caches()
    # other tenants' hosts don't see it
    other = make_tenant()
    assert pub.get("/fy2026/q2/results/", headers={"host": f"{other.slug}.preview.platform.example.com"}).status_code == 404


def test_published_version_is_immutable_and_rollback_works(admin_client):
    client, t, _ = admin_client
    up = upload(client, t, "acme_q2fy26_results.pdf")
    rid, v1 = up["report_id"], up["version"]["id"]
    drain()
    assert client.post(f"/api/tenants/{t.id}/reports/{rid}/versions/{v1}/publish", json={"confirm_reviewed": True}).status_code == 200
    schema = client.get(f"/api/tenants/{t.id}/reports/{rid}/versions/{v1}/schema").json()
    fid = next(f["id"] for f in schema["figures"].values() if f["raw"] == "48.20")
    # can't edit the published version
    r = client.post(f"/api/tenants/{t.id}/reports/{rid}/versions/{v1}/figures/{fid}", json={"action": "confirm"})
    assert r.status_code == 409
    with pytest.raises(Exception, match="immutable"):
        with db.session(system_context()) as s:
            s.get(ReportVersion, v1).schema_json = {}
    # new draft -> publish -> rollback to v1
    v2 = client.post(f"/api/tenants/{t.id}/reports/{rid}/versions/{v1}/draft").json()["id"]
    drain()
    assert client.post(f"/api/tenants/{t.id}/reports/{rid}/versions/{v2}/publish", json={"confirm_reviewed": True}).status_code == 200
    rep = client.get(f"/api/tenants/{t.id}/reports/{rid}").json()
    assert rep["live_version_id"] == v2
    assert client.post(f"/api/tenants/{t.id}/reports/{rid}/rollback", json={"version_id": v1}).status_code == 200
    assert client.get(f"/api/tenants/{t.id}/reports/{rid}").json()["live_version_id"] == v1
    audit = client.get(f"/api/tenants/{t.id}/audit").json()["entries"]
    assert {"report.published", "report.rolled_back", "report.uploaded"} <= {e["action"] for e in audit}


def test_reupload_reheals_a_missing_source_blob(admin_client):
    """A PDF uploaded once leaves a source_files row keyed by its SHA. If that object later
    vanishes from storage (lifecycle expiry, a storage backend switch, a manual cleanup),
    re-uploading the same PDF used to skip the upload (dedup by SHA) and then fail extract
    with "not found" — unrecoverable, since the bytes were gone. store_source now notices
    the missing object and re-stores it, so the re-upload heals itself and processes."""
    from app import storage
    from app.reports import source_key
    from app.tenancy import worker_context

    client, t, _ = admin_client
    up = upload(client, t, "acme_q2fy26_results.pdf", period="q2")
    rid, vid = up["report_id"], up["version"]["id"]
    drain()
    assert _version(client, t, rid, vid)["status"] == "needs_review"

    # the blob goes missing while the source_files row survives
    with db.session(system_context()) as s:
        sha = s.get(ReportVersion, vid).source_sha256
    ctx = worker_context(t.id)
    storage.delete(ctx, source_key(sha))
    assert not storage.exists(ctx, source_key(sha))

    # same PDF, a new period: must process, not fail with "not found"
    again = upload(client, t, "acme_q2fy26_results.pdf", period="q3")
    rid2, vid2 = again["report_id"], again["version"]["id"]
    drain()
    v2 = _version(client, t, rid2, vid2)
    assert v2["status"] == "needs_review", v2
    assert storage.exists(ctx, source_key(sha))


def test_seeded_text_layer_error_blocks_publishing_until_fixed(admin_client):
    """The hidden text layer says 46.20 where the page prints 48.20."""
    client, t, _ = admin_client
    up = upload(client, t, "acme_q2fy26_broken_text_layer.pdf", period="q1")
    rid, vid = up["report_id"], up["version"]["id"]
    drain()
    v = _version(client, t, rid, vid)
    assert v["status"] == "validation_issues" and v["validation"]["blocking"] > 0
    issues = client.get(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/issues",
                        params={"check": "re_extraction"}).json()["issues"]
    seeded = next(i for i in issues if i["expected"] == "46.20")
    assert seeded["severity"] == "blocking" and "48.2" in seeded["actual"]
    r = client.post(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/publish", json={"confirm_reviewed": True})
    assert r.status_code == 409 and "blocking" in r.json()["detail"]

    # the reviewer's fix: edit the value to what the page actually prints
    r = client.post(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/issues/{seeded['id']}/resolve",
                    json={"action": "edit", "value": "48.20", "note": "matches printed page"})
    assert r.status_code == 200, r.text
    drain()
    schema = client.get(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/schema").json()
    f = schema["figures"][seeded["fid"]]
    assert f["raw"] == "48.20" and f["edited"]["original_raw"] == "46.20"
    after = client.get(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/issues",
                       params={"check": "re_extraction"}).json()["issues"]
    assert not any(i["fid"] == seeded["fid"] and i["status"] == "open" for i in after)
    trail = client.get(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/reviews").json()["reviews"]
    assert trail[0]["action"] == "edit" and trail[0]["old"]["raw"] == "46.20" and trail[0]["new"]["raw"] == "48.20"


def test_scanned_pdf_is_ocrd_and_low_confidence_values_need_a_person(admin_client):
    client, t, _ = admin_client
    up = upload(client, t, "acme_q2fy26_results_scanned.pdf", period="q3")
    rid, vid = up["report_id"], up["version"]["id"]
    drain()
    v = _version(client, t, rid, vid)
    assert {p["method"] for p in v["pages"]} == {"ocr"}
    issues = client.get(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/issues",
                        params={"severity": "blocking"}).json()["issues"]
    assert any(i["check"] == "low_confidence" for i in issues)
    # confirming a low-confidence value resolves it on the next validation run
    target = next(i for i in issues if i["check"] == "low_confidence" and i["fid"])
    assert client.post(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/issues/{target['id']}/resolve",
                       json={"action": "confirm"}).status_code == 200
    drain()
    now = client.get(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/issues").json()["issues"]
    assert all(i["status"] == "resolved" for i in now if i["fid"] == target["fid"] and i["check"] == "low_confidence")


def test_reviewer_can_flag_and_comment_but_not_publish_or_edit(client, admin_client):
    admin, t, _ = admin_client
    up = upload(admin, t, "acme_q2fy26_results.pdf", period="h1")
    rid, vid = up["report_id"], up["version"]["id"]
    drain()
    reviewer = make_user()
    add_member(t, reviewer, Role.client_reviewer)
    rc = TestClient(client.app)
    rc.headers["x-ppdf-csrf"] = "1"
    sign_in(rc, reviewer)
    schema = rc.get(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/schema").json()
    fid = next(iter(schema["figures"]))
    sec = schema["sections"][0]["id"]
    assert rc.post(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/comments",
                   json={"section_id": sec, "body": "Looks right"}).status_code == 201
    assert rc.post(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/flags",
                   json={"fid": fid, "note": "Please double-check"}).status_code == 201
    assert rc.post(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/figures/{fid}", json={"action": "confirm"}).status_code == 403
    assert rc.post(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/publish", json={"confirm_reviewed": True}).status_code == 403
    # the flag blocks the admin until resolved
    r = admin.post(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/publish", json={"confirm_reviewed": True})
    assert r.status_code == 409


def test_cross_tenant_report_access_is_404(client, admin_client):
    admin, t, _ = admin_client
    up = upload(admin, t, "borealis_annual_usd.pdf", report_type="annual_report", period="fy", currency="USD",
                reporting_unit="USD million")
    rid, vid = up["report_id"], up["version"]["id"]
    drain()
    other_t, other_u = make_tenant(), make_user()
    add_member(other_t, other_u, Role.client_admin)
    oc = TestClient(client.app)
    oc.headers["x-ppdf-csrf"] = "1"
    sign_in(oc, other_u)
    for path in ("", f"/{rid}", f"/{rid}/versions/{vid}/schema", f"/{rid}/versions/{vid}/source.pdf",
                 f"/{rid}/versions/{vid}/preview/"):
        assert oc.get(f"/api/tenants/{t.id}/reports{path}").status_code == 404
        # ...and using their own tenant id with our report id finds nothing either
        if path:
            assert oc.get(f"/api/tenants/{other_t.id}/reports{path}").status_code == 404
    with db.session(system_context()) as s:
        assert s.scalars(select(Report).where(Report.id == rid)).one().tenant_id == t.id


def test_upload_rejects_non_pdf_and_bad_metadata(admin_client):
    client, t, _ = admin_client
    r = client.post(f"/api/tenants/{t.id}/reports", files={"file": ("x.pdf", b"hello", "application/pdf")}, data=META)
    assert r.status_code == 400 and "PDF" in r.json()["detail"]
    data = (CORPUS / "borealis_annual_usd.pdf").read_bytes()
    r = client.post(f"/api/tenants/{t.id}/reports", files={"file": ("a.pdf", data, "application/pdf")},
                    data={**META, "currency": "rupees"})
    assert r.status_code == 400 and "3-letter" in r.json()["detail"]


def test_ga4_only_on_that_tenants_published_pages(admin_client, client):
    admin, t, _ = admin_client
    up = upload(admin, t, "acme_q2fy26_results.pdf", period="q4")
    rid, vid = up["report_id"], up["version"]["id"]
    drain()
    admin.post(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/publish", json={"confirm_reviewed": True})
    assert admin.put(f"/api/tenants/{t.id}/analytics", json={"ga4_measurement_id": "bad"}).status_code == 400
    assert admin.put(f"/api/tenants/{t.id}/analytics", json={"ga4_measurement_id": "G-TEST12345"}).status_code == 200

    from app.public import app as public_app, clear_caches
    clear_caches()
    pub = TestClient(public_app)
    page = pub.get("/fy2026/q4/results/", headers={"host": f"{t.slug}.preview.platform.example.com",
                                                    "user-agent": "Mozilla/5.0"}).text
    assert "G-TEST12345" in page and 'id="ppdf-consent"' in page          # banner on by default
    # not in the dashboard preview, not on other tenants
    assert "G-TEST12345" not in admin.get(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/preview/").text
    other = make_tenant()
    ou = make_user()
    add_member(other, ou, Role.client_admin)
    assert "G-TEST12345" not in pub.get("/", headers={"host": f"{other.slug}.preview.platform.example.com"}).text
    # crawler visits are counted per crawler
    pub.get("/fy2026/q4/results/", headers={"host": f"{t.slug}.preview.platform.example.com",
                                             "user-agent": "Mozilla/5.0 (compatible; GPTBot/1.2)"})
    stats = admin.get(f"/api/tenants/{t.id}/analytics/stats").json()
    rep = next(r for r in stats["reports"] if r["report_id"] == rid)
    assert rep["views"] >= 1 and rep["crawlers"].get("GPTBot") == 1


def test_rendered_html_is_identical_across_themes(admin_client):
    """Theme is visual only: HTML minus <style> is byte-identical across themes."""
    from app.render import site, theme as theming
    client, t, _ = admin_client
    up = upload(client, t, "acme_q2fy26_results.pdf", period="9m")
    drain()
    schema = client.get(f"/api/tenants/{t.id}/reports/{up['report_id']}/versions/{up['version']['id']}/schema").json()
    themes = [theming.validate({}),
              theming.validate({"colors": {"primary": "#C2185B", "background": "#FFF8E1", "text": "#222222"},
                                "typography": {"heading_font": "Merriweather", "body_font": "Inter", "base_size": 17},
                                "spacing": "relaxed", "header": {"style": "solid", "show_company_name": True}}),
              theming.validate({"colors": {"primary": "#FFEE58", "background": "#FFFFFF"}})]  # fails contrast
    outs = []
    for th in themes:
        used, _ = theming.enforce(th)
        files = site.render_report(schema, theme=used, disclaimer="Disclaimer.")
        html = files["fy2026/9m/results/index.html"].decode()
        stripped = re.sub(r"<style>.*?</style>", "", re.sub(r'<link rel="(preconnect|stylesheet)"[^>]*>', "", html), flags=re.S)
        stripped = stripped.replace("header-solid", "header-light")           # body class for the header style
        outs.append("\n".join(line for line in stripped.splitlines() if line.strip()))
    assert outs[0] == outs[1] == outs[2]
    used, changes = theming.enforce(themes[2])
    assert changes and theming.contrast(used["colors"]["primary"], used["colors"]["background"]) >= 4.5


def test_admin_can_delete_a_live_report_and_file_that_period_again(admin_client, client):
    admin, t, _ = admin_client
    up = upload(admin, t, "acme_q2fy26_results.pdf", period="h2", report_type="annual_report")
    rid, vid = up["report_id"], up["version"]["id"]
    reviewer = make_user()
    add_member(t, reviewer, Role.client_reviewer)
    rc = TestClient(client.app)
    rc.headers["x-ppdf-csrf"] = "1"
    sign_in(rc, reviewer)
    assert rc.delete(f"/api/tenants/{t.id}/reports/{rid}").status_code == 403

    drain()
    assert admin.post(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/publish",
                      json={"confirm_reviewed": True}).status_code == 200
    assert admin.get(f"/api/tenants/{t.id}/reports/{rid}").json()["status"] == "Live"
    from app.public import app as public_app, clear_caches
    clear_caches()
    pub = TestClient(public_app)
    host = f"{t.slug}.preview.platform.example.com"
    assert pub.get("/fy2026/h2/annual-report/", headers={"host": host}).status_code == 200

    assert admin.delete(f"/api/tenants/{t.id}/reports/{rid}").status_code == 204
    assert admin.get(f"/api/tenants/{t.id}/reports/{rid}").status_code == 404
    assert all(r["id"] != rid for r in admin.get(f"/api/tenants/{t.id}/reports").json()["reports"])
    clear_caches()
    assert pub.get("/fy2026/h2/annual-report/", headers={"host": host}).status_code == 404
    audit = admin.get(f"/api/tenants/{t.id}/audit").json()["entries"]
    assert any(e["action"] == "report.deleted" and e["target_id"] == rid for e in audit)

    again = upload(admin, t, "acme_q2fy26_results.pdf", period="h2", report_type="annual_report")
    assert again["report_id"] != rid
    assert admin.get(f"/api/tenants/{t.id}/reports/{again['report_id']}").status_code == 200


def test_progress_says_waiting_until_a_worker_takes_the_job(admin_client):
    """The screen must never claim "Reading the PDF" while nothing is reading it."""
    client, t, _ = admin_client
    rep = upload(client, t, "acme_q2fy26_results.pdf", period="q3")
    rid, vid = rep["report_id"], rep["version"]["id"]
    v = _version(client, t, rid, vid)
    assert v["status"] == "processing" and v["queue"]["waiting"] is True
    assert v["queue"]["waiting_seconds"] >= 0 and "workers_alive" in v["queue"]
    drain()
    assert _version(client, t, rid, vid)["queue"] is None


def test_a_rerender_uploads_only_changed_files_and_review_images_are_stored(admin_client, monkeypatch):
    from app import storage
    from app.reports import enqueue_stage, pdf_page_key
    client, t, _ = admin_client
    up = upload(client, t, "acme_q2fy26_results.pdf", period="q4")
    rid, vid = up["report_id"], up["version"]["id"]
    drain()
    with db.session(system_context()) as s:
        v = s.get(ReportVersion, __import__("uuid").UUID(vid))
        sha, tid = v.source_sha256, v.tenant_id
    from app.tenancy import tenant_context
    ctx = tenant_context(None, tid, None)
    assert storage.exists(ctx, pdf_page_key(sha, 1))                 # background review images ran
    puts = []
    real = storage.put_many
    monkeypatch.setattr(storage, "put_many", lambda c, items, on_done=None: (puts.extend(k for k, *_ in items), real(c, items, on_done))[1])
    with db.session(ctx) as s:
        enqueue_stage(s, ctx, s.get(ReportVersion, v.id), "render", force=True)
    drain()
    assert puts == []                                                # nothing changed: nothing re-uploaded


def test_validation_issues_are_saved_in_bulk(admin_client, monkeypatch):
    """~2,000 issues on a large report must not be one database round trip each."""
    from sqlalchemy import event

    from app import validation
    client, t, _ = admin_client
    up = upload(client, t, "acme_q2fy26_results.pdf", period="h1")
    real = validation.run_all

    def many_issues(*a, **k):
        issues, summary = real(*a, **k)
        extra = [validation.Issue("completeness", "warning", f"synthetic {n}") for n in range(2500)]
        return issues + extra, summary
    monkeypatch.setattr(validation, "run_all", many_issues)
    calls = []
    eng = db.get_engine()
    listener = lambda conn, cur, stmt, params, ctx, many: calls.append(stmt) if "INSERT INTO validation_issues" in stmt else None  # noqa: E731
    event.listen(eng, "before_cursor_execute", listener)
    try:
        drain()
    finally:
        event.remove(eng, "before_cursor_execute", listener)
    assert 0 < len(calls) <= 5, len(calls)
    v = _version(client, t, up["report_id"], up["version"]["id"])
    assert v["validation"]["warnings"] >= 2500


def test_confirming_a_figure_reuses_the_checks_instead_of_rerunning_them(admin_client):
    from app.models import Job
    client, t, _ = admin_client
    up = upload(client, t, "acme_q2fy26_results_scanned.pdf", period="h2")
    rid, vid = up["report_id"], up["version"]["id"]
    drain()
    base = f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}"
    before = _version(client, t, rid, vid)
    blocking = [i for i in client.get(f"{base}/issues", params={"status": "open"}).json()["issues"]
                if i["severity"] == "blocking" and i.get("fid")]
    assert blocking, "the scanned sample should have something to confirm"
    with db.session(system_context()) as s:
        validates = len(s.scalars(select(Job).where(Job.kind == "pipeline.validate", Job.tenant_id == t.id)).all())
    r = client.post(f"{base}/figures/{blocking[0]['fid']}", json={"action": "confirm"})
    assert r.status_code == 200, r.text
    drain()
    after = _version(client, t, rid, vid)
    with db.session(system_context()) as s:
        assert len(s.scalars(select(Job).where(Job.kind == "pipeline.validate", Job.tenant_id == t.id)).all()) == validates
    assert after["status"] in ("needs_review", "validation_issues")
    assert after["validation"]["blocking"] < before["validation"]["blocking"]
    still = {i["id"] for i in client.get(f"{base}/issues", params={"status": "open"}).json()["issues"]}
    assert blocking[0]["id"] not in still
    # validated against the current schema + page, so publishing isn't held up by staleness
    with db.session(system_context()) as s:
        v = s.get(ReportVersion, __import__("uuid").UUID(vid))
        assert v.validated_schema_sha256 == v.schema_sha256 and v.validated_bundle_sha256 == v.bundle_sha256


def test_opt_in_landing_page_for_long_reports(admin_client):
    """A 20+ page report, built with the opt-in landing page, gets BOTH the full report at its
    canonical URL and an extractive "overview" page at <base>overview/ — composed entirely
    from the schema (no AI), with its figures covered by the rendered-page check."""
    client, t, _ = admin_client
    data = _long_pdf_bytes(22)

    # the option is offered for a long report...
    made = _inspect_and_create(client, t, data, landing=True, period="q2")
    assert made["inspect"]["page_count"] >= 20
    assert made["inspect"]["build_options"]["landing"] == {"available": True, "min_pages": 20}
    rid, vid = made["report_id"], made["version"]["id"]
    drain()
    v = _version(client, t, rid, vid)
    assert v["status"] == "needs_review", v
    assert v["validation"]["blocking"] == 0          # the landing page's figures validate too
    assert v["landing"] is True
    assert v["has_overview"] is True                 # the dashboard can offer the overview link

    base = f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/preview"
    # the full report is still served at the root of the bundle, and links to the overview
    root = client.get(base + "/")
    assert root.status_code == 200 and 'class="web"' in root.text and "overview/" in root.text
    # the overview page: extractive summary, figures as data-fig, links to the full report
    ov = client.get(base + "/fy2026/q2/results/overview/")
    assert ov.status_code == 200
    assert 'class="web landing"' in ov.text and "web-hero" in ov.text
    assert 'data-fig="' in ov.text and "Key figures" in ov.text and "In this report" in ov.text
    assert "Read the full report" in ov.text and "1,234.56" in ov.text
    # the landing page itself is always extractive (no AI), whatever the build mode
    assert v["build_mode"] == "layout"

    # published: both pages live on the public site, each canonical to itself; sitemap lists both
    assert client.post(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/publish",
                       json={"confirm_reviewed": True}).status_code == 200
    from app.public import app as public_app, clear_caches
    clear_caches()
    pub = TestClient(public_app)
    host = f"{t.slug}.preview.platform.example.com"
    full = pub.get("/fy2026/q2/results/", headers={"host": host})
    assert full.status_code == 200
    assert f'<link rel="canonical" href="https://{host}/fy2026/q2/results/">' in full.text
    over = pub.get("/fy2026/q2/results/overview/", headers={"host": host})
    assert over.status_code == 200
    assert f'<link rel="canonical" href="https://{host}/fy2026/q2/results/overview/">' in over.text
    assert "1,234.56" in over.text and "Read the full report" in over.text
    # overview served with JavaScript off, and without a trailing slash it redirects to add one
    assert over.text.count("<script") == 0
    assert pub.get("/fy2026/q2/results/overview", headers={"host": host},
                   follow_redirects=False).status_code == 301
    sm = pub.get("/sitemap.xml", headers={"host": host}).text
    assert "/fy2026/q2/results/</loc>" in sm and "/fy2026/q2/results/overview/</loc>" in sm


def test_landing_page_is_not_offered_or_built_for_short_reports(admin_client):
    """Under 20 pages the option is hidden, and asking for it anyway produces no landing page."""
    client, t, _ = admin_client
    data = (CORPUS / "acme_q2fy26_results.pdf").read_bytes()         # 4 pages
    made = _inspect_and_create(client, t, data, landing=True, period="q3")
    assert made["inspect"]["page_count"] < 20
    assert made["inspect"]["build_options"]["landing"] == {"available": False, "min_pages": 20}
    rid, vid = made["report_id"], made["version"]["id"]
    drain()
    v = _version(client, t, rid, vid)
    assert v["status"] == "needs_review" and v["landing"] is False    # coerced off below the threshold
    assert v["has_overview"] is False                                  # so no overview link is offered
    base = f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/preview"
    assert client.get(base + "/").status_code == 200                   # full report still built
    assert client.get(base + "/fy2026/q3/results/overview/").status_code == 404   # but no landing page


def test_report_list_thumbnail_is_small_cached_and_never_renders_the_pdf(admin_client, monkeypatch):
    from app import storage
    from app.reports import pdf_page_key, thumb_key
    client, t, _ = admin_client
    up = upload(client, t, "acme_q2fy26_results.pdf", period="q1")
    rid, vid = up["report_id"], up["version"]["id"]
    drain()
    r = client.get(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/thumb.webp")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert r.content[:4] == b"RIFF" and len(r.content) < 60_000
    assert "immutable" in r.headers["cache-control"]
    # nothing stored: 404 at once — the PDF is never fetched to draw one
    with db.session(system_context()) as s:
        sha = s.get(ReportVersion, __import__("uuid").UUID(vid)).source_sha256
    from app.tenancy import tenant_context
    ctx = tenant_context(None, t.id, None)
    storage.delete(ctx, thumb_key(sha))
    storage.delete(ctx, pdf_page_key(sha, 1))
    fetched = []
    real_get = storage.get
    monkeypatch.setattr(storage, "get", lambda c, k: (fetched.append(k), real_get(c, k))[1])
    assert client.get(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/thumb.webp").status_code == 404
    assert not any(k.endswith(".pdf") for k in fetched)
