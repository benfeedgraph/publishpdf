"""Issue #8: a change to the design alone ("Show Powered by PublishPDF", colours, fonts, header,
logo) draws none of the PDF's pages again and re-runs only the checks that read the web page —
and the built page and the findings are exactly what a full rebuild and a full re-check give."""

from __future__ import annotations

import io
import uuid

import pymupdf
import pytest
from PIL import Image, ImageDraw
from sqlalchemy import func, select

from app import artwork_store, db, page_layouts, storage, validation
from app.extraction.schema_builder import extract
from app import code_version as code_version_mod
from app import pipeline
from app.models import Job, ReportVersion, ValidationIssue
from app.render import pages as pages_mod
from app.render import site
from app.render import theme as theming
from app.reports import enqueue_stage
from app.tenancy import system_context, tenant_context
from tests.test_pipeline_e2e import CORPUS, admin_client, drain, upload  # noqa: F401

CREDIT = "Powered by PublishPDF"


def _no_drawing(m) -> None:
    def drew(*a, **k):
        raise AssertionError("the PDF's pages were drawn again")

    def asked(*a, **k):
        raise AssertionError("the AI layouts were looked up again")
    m.setattr(pages_mod, "render_pages", drew)
    m.setattr(site, "_web_pages", drew)
    m.setattr(page_layouts, "layouts_for", asked)


def _no_full_checks(m) -> None:
    def ran(*a, **k):
        raise AssertionError("every check ran again")
    m.setattr(validation, "run_all", ran)


def _apply(client, t, rid, *, credit: bool, **design) -> str:
    """What "Apply design & review" sends."""
    r = client.put(f"/api/tenants/{t.id}/reports/{rid}/theme",
                   json={"theme_override": {"footer": {"show_powered_by": credit}, **design}})
    assert r.status_code == 200, r.text
    drain()
    return r.json()["version_id"]


def _footer(client, t, rid, vid) -> str:
    html = client.get(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/preview/").text
    return html[html.rfind('<footer class="site-footer">'):]


def _version(client, t, rid, vid) -> dict:
    return client.get(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}").json()


def _findings(vid) -> list[tuple]:
    """The latest check run's findings, as a person sees them."""
    with db.session(system_context()) as s:
        run = s.scalar(select(func.max(ValidationIssue.run_no)).where(ValidationIssue.version_id == uuid.UUID(vid)))
        rows = s.scalars(select(ValidationIssue).where(ValidationIssue.version_id == uuid.UUID(vid),
                                                       ValidationIssue.run_no == run)).all()
        return sorted((r.check_name, r.severity, r.status, r.resolution or "", r.fid or "", r.page or 0, r.message,
                       str(r.bbox), r.expected or "", r.actual or "") for r in rows)


def _last_job(vid, kind) -> dict:
    with db.session(system_context()) as s:
        job = s.scalars(select(Job).where(Job.version_id == uuid.UUID(vid), Job.kind == f"pipeline.{kind}")
                        .order_by(Job.finished_at.desc().nulls_last())).first()
        return job.result or {}


def _check_everything_again(t, vid) -> None:
    """A full re-check of the version's current page (every check, as before this change)."""
    ctx = tenant_context(None, t.id, None)
    with db.session(ctx) as s:
        enqueue_stage(s, ctx, s.get(ReportVersion, uuid.UUID(vid)), "validate", force=True)
    drain()


def test_apply_design_draws_nothing_and_rechecks_only_the_web_page(admin_client, monkeypatch):
    client, t, _ = admin_client
    up = upload(client, t, "acme_q2fy26_results_scanned.pdf")      # scanned: the slowest checks to repeat
    rid, vid = up["report_id"], up["version"]["id"]
    drain()
    assert CREDIT not in _footer(client, t, rid, vid)
    with monkeypatch.context() as m:
        _no_drawing(m)
        _no_full_checks(m)
        assert _apply(client, t, rid, credit=True) == vid
        assert CREDIT in _footer(client, t, rid, vid)
        assert _last_job(vid, "render")["artwork"] == "reused"
        assert _last_job(vid, "validate")["checks"] == "page checks only"
        fast = (_findings(vid), _version(client, t, rid, vid)["validation"])
    # exactly what checking everything again finds, and the same summary on every screen
    _check_everything_again(t, vid)
    assert "checks" not in _last_job(vid, "validate")
    assert (_findings(vid), _version(client, t, rid, vid)["validation"]) == fast
    # and back: every Apply builds the footer the box asks for
    with monkeypatch.context() as m:
        _no_drawing(m)
        _no_full_checks(m)
        _apply(client, t, rid, credit=False, colors={"primary": "#C2185B"}, typography={"heading_font": "Lora"})
        footer = _footer(client, t, rid, vid)
        assert CREDIT not in footer
        v = _version(client, t, rid, vid)
        assert v["status"] in ("needs_review", "validation_issues")
        assert "#C2185B" in client.get(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/preview/").text


@pytest.mark.parametrize("name", ["acme_q2fy26_results.pdf", "acme_q2fy26_presentation.pdf",
                                  "acme_q2fy26_results_scanned.pdf"])
def test_a_kept_drawing_builds_the_same_page_byte_for_byte(admin_client, monkeypatch, name):
    client, t, _ = admin_client
    up = upload(client, t, name)
    drain()
    schema = client.get(f"/api/tenants/{t.id}/reports/{up['report_id']}/versions/{up['version']['id']}/schema").json()
    _same_page_from_a_kept_drawing(t, schema, (CORPUS / name).read_bytes(), monkeypatch)


def test_picture_pages_cut_into_web_sections_are_kept_too(admin_client, monkeypatch):
    """A page that is mostly a picture is rebuilt from cut-outs of it: those are kept as well."""
    from tests.test_faq_story import BODY, META
    client, t, _ = admin_client
    doc = pymupdf.open()
    doc.new_page().insert_textbox(pymupdf.Rect(72, 72, 520, 760), (BODY + " ") * 3, fontname="helv", fontsize=11)
    page = doc.new_page()
    picture = Image.new("RGB", (900, 600), (24, 80, 160))
    ImageDraw.Draw(picture).rectangle((120, 120, 700, 420), fill=(240, 180, 40))
    png = io.BytesIO()
    picture.save(png, "PNG")
    page.insert_image(pymupdf.Rect(72, 110, 520, 410), stream=png.getvalue())
    page.insert_text((72, 440), "Our new plant", fontname="helv", fontsize=12)
    doc.new_page().insert_textbox(pymupdf.Rect(72, 72, 520, 760), (BODY + " ") * 3, fontname="helv", fontsize=11)
    pdf = doc.tobytes()
    schema, _ = extract(pdf, META)
    drawing = _same_page_from_a_kept_drawing(t, schema, pdf, monkeypatch)
    assert drawing["web_pages"] and drawing["web_crops"]


def _same_page_from_a_kept_drawing(t, schema: dict, pdf: bytes, monkeypatch) -> dict:
    """A page built from the kept drawing in another design is byte for byte the page a full
    build in that design gives — kept in memory, or stored with the page files and read back."""
    plain, _ = theming.enforce(theming.validate({}))
    other, _ = theming.enforce(theming.validate({
        "colors": {"primary": "#C2185B", "background": "#FFF8E1"}, "typography": {"heading_font": "Lora"},
        "spacing": "relaxed", "header": {"style": "solid"}, "footer": {"show_powered_by": True}}))
    memo: dict = {}
    first = site.render_report(schema, theme=plain, disclaimer="Our disclaimer.", pdf_bytes=pdf, memo=memo)
    redesigned = site.render_report(schema, theme=other, disclaimer="Our disclaimer.", pdf_bytes=pdf)
    assert memo["artwork"][0] == site.artwork_key(schema, pdf, None, None)
    ctx = tenant_context(None, t.id, None)
    prefix = f"artwork-test/{uuid.uuid4()}/"
    storage.put_many(ctx, [(prefix + k, v, "application/octet-stream") for k, v in first.items()])
    artwork_store.save(ctx, prefix, memo, first, {})
    kept = artwork_store.memo(ctx, prefix, artwork_store.read(ctx, prefix), site.artwork_key(schema, pdf, None, None))
    assert kept
    with monkeypatch.context() as m:
        _no_drawing(m)
        assert site.render_report(schema, theme=other, disclaimer="Our disclaimer.", pdf_bytes=pdf, memo=memo) == redesigned
        assert site.render_report(schema, theme=other, disclaimer="Our disclaimer.", pdf_bytes=pdf, memo=kept) == redesigned
    pages = [k for k in redesigned if k.endswith(".html")]
    assert pages and all(CREDIT in redesigned[k].decode() and CREDIT not in first[k].decode() for k in pages)
    return memo["artwork"][1]


def test_a_kept_drawing_is_not_used_when_a_page_file_changed_or_it_was_made_from_other_things(admin_client):
    client, t, _ = admin_client
    ctx = tenant_context(None, t.id, None)
    prefix = f"artwork-test/{uuid.uuid4()}/"
    from markupsafe import Markup
    files = {"r/pages/p0001.webp": b"one", "r/pages/p0002.webp": b"two"}
    storage.put_many(ctx, [(prefix + k, v, "image/webp") for k, v in files.items()])
    drawing = {"parts": [{"n": 1, "bg": "pages/p0001.webp", "vis": {"b1": {"box": (1, 2)}}}],
               "art": {"pages/p0001.webp": b"one", "pages/p0003-print.webp": b"never stored"},
               "web_pages": {2: Markup("<p>two</p>")}, "web_crops": {"pages/p0002.webp": b"two"}}
    layouts = {2: {"fingerprint": "f", "sections": [{"type": "picture"}]}}
    artwork_store.save(ctx, prefix, {"artwork": ("k1", drawing)}, files, layouts)
    doc = artwork_store.read(ctx, prefix)
    assert artwork_store.layouts_of(doc) == layouts
    back = artwork_store.memo(ctx, prefix, doc, "k1")["artwork"][1]
    assert back["parts"] == drawing["parts"] and back["web_crops"] == drawing["web_crops"]
    assert back["web_pages"] == {2: Markup("<p>two</p>")} and isinstance(back["web_pages"][2], Markup)
    assert back["art"] == {"pages/p0001.webp": b"one"}          # never stored: not kept, not needed
    assert artwork_store.memo(ctx, prefix, doc, "another key") == {}
    storage.put(ctx, prefix + "r/pages/p0002.webp", b"changed", "image/webp")
    assert artwork_store.memo(ctx, prefix, doc, "k1") == {}
    assert artwork_store.read(ctx, f"artwork-test/{uuid.uuid4()}/") is None     # nothing kept yet


def test_an_app_update_stale_findings_and_rerun_still_check_everything(admin_client, monkeypatch):
    client, t, _ = admin_client
    up = upload(client, t, "acme_q2fy26_results.pdf", period="q4")
    rid, vid = up["report_id"], up["version"]["id"]
    drain()
    full = []
    real = validation.run_all
    monkeypatch.setattr(validation, "run_all", lambda *a, **k: (full.append(1), real(*a, **k))[1])
    _apply(client, t, rid, credit=True)
    assert full == []
    # another build of the app: its checks (and drawing) may differ, so everything runs again
    with monkeypatch.context() as m:
        m.setattr(code_version_mod, "code_version", lambda: "another-build")
        m.setattr(pipeline, "code_version", lambda: "another-build")
        _apply(client, t, rid, credit=False)
        assert full == [1] and _last_job(vid, "render")["artwork"] == "drawn"
    # findings older than the data they describe (a check run that didn't finish): everything runs
    with db.session(system_context()) as s:
        v = s.get(ReportVersion, uuid.UUID(vid))
        v.validation_summary = {**v.validation_summary, "checked_content_sha": "older data"}
    _apply(client, t, rid, credit=True)
    assert full == [1, 1]
    # "Re-run" means every check, design or not
    r = client.post(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/rerun")
    assert r.status_code == 200, r.text
    drain()
    assert full == [1, 1, 1]


def test_a_new_design_for_a_live_page_starts_from_its_findings(admin_client, monkeypatch):
    client, t, _ = admin_client
    up = upload(client, t, "acme_q2fy26_results.pdf")
    rid, live = up["report_id"], up["version"]["id"]
    drain()
    assert _version(client, t, rid, live)["status"] == "needs_review"
    r = client.post(f"/api/tenants/{t.id}/reports/{rid}/versions/{live}/publish", json={"confirm_reviewed": True})
    assert r.status_code == 200, r.text
    with monkeypatch.context() as m:
        _no_full_checks(m)
        draft = _apply(client, t, rid, credit=True)              # a live page is never changed: a new draft
        assert draft != live and CREDIT in _footer(client, t, rid, draft)
        assert _last_job(draft, "validate")["checks"] == "page checks only"
        fast = (_findings(draft), _version(client, t, rid, draft)["validation"])
    assert CREDIT not in _footer(client, t, rid, live)
    _check_everything_again(t, draft)
    assert (_findings(draft), _version(client, t, rid, draft)["validation"]) == fast


def test_preview_draws_the_pages_once_and_shows_every_change(admin_client, monkeypatch):
    client, t, _ = admin_client
    up = upload(client, t, "acme_q2fy26_results.pdf")
    drain()
    theme = client.get(f"/api/tenants/{t.id}/settings").json()["theme"]
    P = f"/api/tenants/{t.id}/theme/preview"
    first = client.post(P, json={"theme": theme, "report_id": up["report_id"]}).text
    with monkeypatch.context() as m:
        _no_drawing(m)
        for on in (True, False):
            html = client.post(P, json={"theme": {**theme, "footer": {"show_powered_by": on}},
                                        "report_id": up["report_id"]}).text
            assert (CREDIT in html[html.rfind('<footer class="site-footer">'):]) == on
        same = client.post(P, json={"theme": theme, "report_id": up["report_id"]}).text
    assert same == first


def test_preview_keeps_the_drawings_of_only_a_few_recent_reports(monkeypatch):
    from app.api import settings_routes as sr
    monkeypatch.setattr(sr, "_preview_memos", type(sr._preview_memos)())
    for n in range(sr.PREVIEW_KEEP_REPORTS + 5):
        sr._preview_memo(("tenant", n))["artwork"] = ("key", {"art": {"p": b"x" * 10}, "web_crops": {}})
    assert list(sr._preview_memos) == [("tenant", n) for n in range(5, sr.PREVIEW_KEEP_REPORTS + 5)]
    monkeypatch.setattr(sr, "PREVIEW_KEEP_BYTES", 25)                # pictures beyond the cap go, oldest first
    sr._preview_memo(("tenant", 99))
    assert list(sr._preview_memos)[-1] == ("tenant", 99) and len(sr._preview_memos) == 3
