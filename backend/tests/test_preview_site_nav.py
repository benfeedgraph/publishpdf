"""Issue #18: the site's own navigation ("Latest results", "Archive", "Download PDF") does what it
will do for visitors inside a report's previews, instead of nothing: Latest results is the report,
Archive is the site's archive as it will read once the report is published, the PDF opens."""

from __future__ import annotations

import re

from sqlalchemy import select

from app import db
from app.models import Report
from app.tenancy import system_context
from tests.test_pipeline_e2e import admin_client, drain, upload  # noqa: F401

NOT_YET = "This report · not published yet"
NOTHING_ELSE = "No earlier reports published yet."


def _report(client, t, period: str, *, publish: bool) -> str:
    up = upload(client, t, "acme_q2fy26_results.pdf", period=period)
    drain()
    rid, vid = up["report_id"], up["version"]["id"]
    if publish:
        r = client.post(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/publish", json={"confirm_reviewed": True})
        assert r.status_code == 200, r.text
        drain()
    return rid


def _nav(client, t, rid) -> dict:
    r = client.get(f"/api/tenants/{t.id}/reports/{rid}/archive")
    assert r.status_code == 200, r.text
    return r.json()


def _latest_version(client, t, rid) -> str:
    return client.get(f"/api/tenants/{t.id}/reports/{rid}").json()["versions"][0]["id"]


def test_archive_endpoint_with_no_other_published_reports(admin_client):
    client, t, _ = admin_client
    rid = _report(client, t, "q2", publish=False)
    nav = _nav(client, t, rid)
    assert nav["latest"]["report_id"] == rid and nav["latest"]["period_label"] == "Q2 FY2026"
    assert nav["latest"]["published"] is False
    assert nav["archive"] == []


def test_archive_endpoint_with_one_published_report(admin_client):
    client, t, _ = admin_client
    q1 = _report(client, t, "q1", publish=True)
    q2 = _report(client, t, "q2", publish=False)
    nav = _nav(client, t, q2)
    assert [a["period_label"] for a in nav["archive"]] == ["Q1 FY2026"]
    assert nav["archive"][0]["url"].endswith("/fy2026/q1/results/") and nav["archive"][0]["published_at"]
    # the published report itself: it is "latest" here, and a draft isn't in anyone's archive
    mine = _nav(client, t, q1)
    assert mine["latest"]["published"] is True and mine["archive"] == []


def test_archive_endpoint_with_many_published_reports_newest_first(admin_client):
    client, t, _ = admin_client
    for period in ("q2", "q1", "q3"):                     # published out of order
        _report(client, t, period, publish=True)
    gone = _report(client, t, "h1", publish=True)
    r = client.delete(f"/api/tenants/{t.id}/reports/{gone}")
    assert r.status_code in (200, 204), r.text
    draft = _report(client, t, "q4", publish=False)
    nav = _nav(client, t, draft)
    assert [a["period_label"] for a in nav["archive"]] == ["Q3 FY2026", "Q2 FY2026", "Q1 FY2026"]   # deleted one gone
    assert nav["latest"]["period_label"] == "Q4 FY2026" and nav["latest"]["published"] is False


def _design_preview(client, t, rid) -> str:
    theme = client.get(f"/api/tenants/{t.id}/settings").json()["theme"]
    return client.post(f"/api/tenants/{t.id}/theme/preview", json={"theme": theme, "report_id": rid}).text


def _nav_links(html: str) -> dict[str, str]:
    nav = re.search(r'<nav aria-label="Site">(.*?)</nav>', html, re.S).group(1)
    return {re.sub(r"<[^>]+>", "", m.group(2)).strip(): m.group(1) for m in re.finditer(r"<a ([^>]*)>(.*?)</a>", nav)}


def test_design_preview_navigation_does_something_for_every_link(admin_client):
    client, t, _ = admin_client
    rid = _report(client, t, "q2", publish=False)
    html = _design_preview(client, t, rid)
    links = _nav_links(html)
    assert set(links) == {"Latest results", "Archive", "Download PDF"}
    assert all("data-preview-inert" not in a for a in links.values())          # none is silently ignored
    assert 'href="#"' in links["Latest results"]                                   # back to this report
    assert 'href="#ppdf-archive"' in links["Archive"]
    assert re.search(r'href="[^"]*/preview/source\.pdf" target="_blank"', links["Download PDF"])
    # the archive it opens: this report, marked, and nothing else published yet
    archive = html[html.index('id="ppdf-archive"'):]
    assert "report archive" in archive and NOT_YET in archive and NOTHING_ELSE in archive
    assert "Back to the report" in archive and "body:has(.ppdf-archive:target) main{display:none}" in html
    assert html.index('id="ppdf-archive"') < html.rindex('<footer class="site-footer">')      # where the report was
    # other site links (machine-readable files) still can't load the dashboard inside the preview
    footer = html[html.rfind('<footer class="site-footer">'):]
    assert 'data-preview-inert="1"' in footer.split("Machine-readable", 1)[1]


def test_design_preview_archive_lists_the_companys_published_reports(admin_client):
    client, t, _ = admin_client
    _report(client, t, "q1", publish=True)
    _report(client, t, "q3", publish=True)
    rid = _report(client, t, "q2", publish=False)
    html = _design_preview(client, t, rid)
    archive = html[html.index('id="ppdf-archive"'):]
    items = re.findall(r"<li>(.*?)</li>", archive, re.S)
    texts = [re.sub(r"<[^>]+>", " ", li).split() for li in items]
    assert [" ".join(x[:3]) for x in texts] == ["Q3 FY2026 results", "Q2 FY2026 results", "Q1 FY2026 results"]
    assert NOT_YET in items[1] and 'href="#"' in items[1]                         # this report, in its place
    assert all('target="_blank"' in items[i] and "/results/" in items[i] for i in (0, 2))   # live pages, new tab
    assert NOTHING_ELSE not in archive


def test_review_preview_archive_and_latest(admin_client):
    client, t, _ = admin_client
    _report(client, t, "q1", publish=True)
    rid = _report(client, t, "q2", publish=False)
    vid = _latest_version(client, t, rid)
    root = f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/preview"
    page = client.get(f"{root}/archive/")
    assert page.status_code == 200 and "<title>Report archive" in page.text
    assert NOT_YET in page.text and f'href="{root}/"' in page.text and "Back to the report" in page.text
    assert re.search(r'href="[^"]*/fy2026/q1/results/" target="_blank"', page.text)
    assert 'name="robots" content="noindex"' in page.text
    latest = client.get(f"{root}/latest/")
    landing = client.get(f"{root}/")
    assert latest.status_code == 200 and latest.text == landing.text and "<title>Report archive" not in latest.text


def test_the_published_archive_has_no_preview_additions(admin_client):
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.public import app as public_app, clear_caches
    client, t, _ = admin_client
    _report(client, t, "q1", publish=True)
    _report(client, t, "q2", publish=False)
    clear_caches()
    host = get_settings().preview_url_pattern.replace("{tenant_slug}", t.slug)
    page = TestClient(public_app).get("/archive/", headers={"host": host})
    assert page.status_code == 200 and "Q1 FY2026" in page.text
    assert "This report" not in page.text and "Back to the report" not in page.text and "Q2 FY2026" not in page.text
    with db.session(system_context()) as s:
        assert s.scalar(select(Report).where(Report.tenant_id == t.id, Report.period == "q2")) is not None


def test_a_live_version_previewed_beside_a_newer_draft_is_marked_as_published(admin_client):
    client, t, _ = admin_client
    rid = _report(client, t, "q2", publish=True)
    live = _latest_version(client, t, rid)
    r = client.post(f"/api/tenants/{t.id}/reports/{rid}/versions/{live}/draft")
    assert r.status_code in (200, 201), r.text
    drain()
    draft = _latest_version(client, t, rid)
    assert draft != live
    page = lambda vid: client.get(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/preview/archive/").text  # noqa: E731
    assert NOT_YET not in page(live) and "This report" in page(live)
    assert NOT_YET in page(draft)
    assert _nav(client, t, rid)["latest"]["published"] is False                   # the report's newest: the draft
