"""Issue #19: applying a design is its own action. It builds the page in the design and moves no
step; the version says whether its page is in the saved design (for every design setting), so
going on to Review never rebuilds when nothing changed; and a design build leaves "Extract &
verify" as its checks left it instead of making it look unfinished."""

from __future__ import annotations

import time
import uuid

from sqlalchemy import select

from app import db, jobs, page_layouts, storage
from app.jobs import UserFacingError
from app.models import Job, Report, ReportVersion
from app.render import site
from app.render import theme as theming
from app.reports import bundle_prefix, enqueue_stage
from app.tenancy import system_context
from tests.test_design_apply import CREDIT, _footer, _no_drawing
from tests.test_pipeline_e2e import admin_client, drain, upload  # noqa: F401


def _R(t, rid) -> str:
    return f"/api/tenants/{t.id}/reports/{rid}"


def _version(client, t, rid, vid) -> dict:
    r = client.get(f"{_R(t, rid)}/versions/{vid}")
    assert r.status_code == 200, r.text
    return r.json()


def _apply(client, t, rid, override: dict | None) -> dict:
    r = client.post(f"{_R(t, rid)}/design/apply", json={"theme_override": override})
    assert r.status_code == 200, r.text
    return r.json()


def _check(client, t, rid, override: dict | None) -> dict:
    r = client.post(f"{_R(t, rid)}/design/check", json={"theme_override": override})
    assert r.status_code == 200, r.text
    return r.json()


def _saved_theme(client, t, rid) -> dict:
    """What the Design step starts from (and sends back, without the logo)."""
    theme = client.get(f"/api/tenants/{t.id}/settings").json()["theme"]
    theme = {**theme, **(client.get(_R(t, rid)).json()["theme_override"] or {})}
    return {k: v for k, v in theme.items() if k != "logo"}


def _with(theme: dict, **changes) -> dict:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in theme.items()}
    for path, value in changes.items():
        group, _, key = path.partition("__")
        if key:
            out[group][key] = value
        else:
            out[group] = value
    return out


def _page(client, t, rid, vid) -> str:
    return client.get(f"{_R(t, rid)}/versions/{vid}/preview/").text


def _jobs(vid: str) -> list[tuple[str, str, bool]]:
    with db.session(system_context()) as s:
        return [(j.kind, j.status, bool((j.payload or {}).get("design_only")))
                for j in s.scalars(select(Job).where(Job.version_id == uuid.UUID(vid)).order_by(Job.created_at))]


def _run_seconds(vid: str, kind: str) -> list[float]:
    with db.session(system_context()) as s:
        return [(j.finished_at - j.started_at).total_seconds()
                for j in s.scalars(select(Job).where(Job.version_id == uuid.UUID(vid), Job.kind == f"pipeline.{kind}",
                                                     Job.status == "succeeded").order_by(Job.created_at))]


def _uploaded(client, t, name="acme_q2fy26_results.pdf", **meta) -> tuple[str, str]:
    up = upload(client, t, name, **meta)
    drain()
    return up["report_id"], up["version"]["id"]


def test_apply_builds_the_page_in_the_design_and_moves_no_step(admin_client):
    client, t, _ = admin_client
    rid, vid = _uploaded(client, t)
    v = _version(client, t, rid, vid)
    verified = "blocked" if v["open_blocking"] else "done"
    assert v["steps"] == {"verify": verified, "design": "applied", "review": "ready"}
    new = _with(_saved_theme(client, t, rid), colors__primary="#C2185B", typography__heading_font="Lora",
                footer__show_powered_by=True)
    assert _check(client, t, rid, new)["applied"] is False           # unapplied changes, by the server's reckoning

    done = _apply(client, t, rid, new)
    assert done["rebuilt"] is True and done["version_id"] == vid and done["job_id"]
    assert done["design"]["state"] == "applying" and done["design"]["job"]["id"] == done["job_id"]
    # while the page is built: the design is being applied, Extract & verify is as its checks left it
    v = _version(client, t, rid, vid)
    assert v["status"] == "processing"
    assert v["steps"] == {"verify": verified, "design": "applying", "review": "building"}
    assert jobs.run_one("test-worker")                                # the page is built …
    v = _version(client, t, rid, vid)
    assert v["steps"] == {"verify": verified, "design": "applied", "review": "checking"}
    html = _page(client, t, rid, vid)                                 # … the real page, in the new design
    assert "--c-primary: #C2185B" in html and '"Lora"' in html and CREDIT in _footer(client, t, rid, vid)
    drain()
    v = _version(client, t, rid, vid)
    assert v["status"] in ("needs_review", "validation_issues")
    assert v["steps"] == {"verify": verified, "design": "applied", "review": "ready"}
    assert v["design"]["page_design"] == v["design"]["saved_design"] == _check(client, t, rid, new)["design"]
    assert next(j for j in v["jobs"] if j["id"] == done["job_id"])["status"] == "succeeded"
    assert _check(client, t, rid, new) | {"adjustments": None} == {
        "design": v["design"]["page_design"], "page_design": v["design"]["page_design"], "applied": True,
        "saved": True, "adjustments": None}


def test_applying_the_design_the_page_already_has_rebuilds_nothing(admin_client):
    client, t, _ = admin_client
    rid, vid = _uploaded(client, t)
    theme = _saved_theme(client, t, rid)
    new = _with(theme, typography__body_font="Merriweather", spacing="relaxed")
    assert _apply(client, t, rid, new)["rebuilt"] is True
    drain()
    before = (_jobs(vid), _version(client, t, rid, vid)["bundle_sha256"])
    # "Continue to review" with nothing changed: no build, no new job, the same page
    again = _apply(client, t, rid, new)
    assert again["rebuilt"] is False and again["job_id"] is None and again["design"]["state"] == "applied"
    assert client.put(f"{_R(t, rid)}/theme", json={"theme_override": new}).json()["rebuilt"] is False   # the old route too
    drain()
    v = _version(client, t, rid, vid)
    assert (_jobs(vid), v["bundle_sha256"]) == before and v["status"] in ("needs_review", "validation_issues")

    # a colour the contrast fix turns into one already on the page: the page wouldn't change
    typed = _with(new, colors__primary="#E23744")
    used = theming.fix_contrast("#E23744", theming.validate(typed)["colors"]["background"])
    assert used != "#E23744"
    seen = _check(client, t, rid, typed)
    assert seen["applied"] is False and {"token": "primary", "from": "#E23744", "to": used} in [
        {k: a[k] for k in ("token", "from", "to")} for a in seen["adjustments"]]
    assert _apply(client, t, rid, typed)["rebuilt"] is True
    drain()
    assert f"--c-primary: {used}" in _page(client, t, rid, vid)
    as_used = _with(new, colors__primary=used)
    assert _check(client, t, rid, as_used)["applied"] is True
    same = _apply(client, t, rid, as_used)
    assert same["rebuilt"] is False and client.get(_R(t, rid)).json()["theme_override"]["colors"]["primary"] == used


def test_a_live_page_gets_a_new_draft_only_for_a_new_design(admin_client):
    client, t, _ = admin_client
    rid, live = _uploaded(client, t)
    r = client.post(f"{_R(t, rid)}/versions/{live}/publish", json={"confirm_reviewed": True})
    assert r.status_code == 200, r.text
    theme = _saved_theme(client, t, rid)
    assert _apply(client, t, rid, theme)["rebuilt"] is False
    assert [v["id"] for v in client.get(_R(t, rid)).json()["versions"]] == [live]
    done = _apply(client, t, rid, _with(theme, header__style="solid"))
    assert done["rebuilt"] is True and done["version_id"] != live
    draft = _version(client, t, rid, done["version_id"])
    # its figures are the live page's, checked and with nothing blocking: only the page is built
    assert draft["steps"] == {"verify": "done", "design": "applying", "review": "building"}
    drain()
    assert _version(client, t, rid, done["version_id"])["steps"] == {"verify": "done", "design": "applied", "review": "ready"}
    assert _version(client, t, rid, live)["design"]["state"] == "not_applied"     # the live page keeps its look


def test_every_design_setting_counts_and_nothing_else_does(admin_client):
    client, t, _ = admin_client
    rid, vid = _uploaded(client, t)
    theme = _saved_theme(client, t, rid)
    page = _check(client, t, rid, theme)
    assert page["applied"] is True and page["saved"] is True
    for path, value in {**{f"colors__{k}": "#7A1F5C" for k in theming.DEFAULT_THEME["colors"]},
                        "typography__heading_font": "Lora", "typography__body_font": "Lora", "typography__base_size": 18,
                        "spacing": "compact", "header__style": "minimal", "header__show_company_name": False,
                        "footer__show_powered_by": not theme["footer"].get("show_powered_by"),
                        "logo": {"key": "logos/x.png", "alt": "Acme", "sha": "abc"}}.items():
        if path.startswith("colors__") and theme["colors"][path[8:]] == value:
            continue
        seen = _check(client, t, rid, _with(theme, **{path: value}))
        assert seen["applied"] is False and seen["design"] != page["design"], path
    # the same design written another way (key order, colour case) is the same design
    shuffled = {k: theme[k] for k in reversed(list(theme))}
    shuffled["colors"] = {k: c.lower() for k, c in reversed(list(theme["colors"].items()))}
    assert _check(client, t, rid, shuffled)["design"] == page["design"]
    # a change made elsewhere (the workspace design, in Settings): this page isn't in it yet
    r = client.put(f"/api/tenants/{t.id}/theme", json={"mode": "custom", "theme": _with(theme, spacing="relaxed")})
    assert r.status_code == 200, r.text
    assert _version(client, t, rid, vid)["design"]["state"] == "not_applied"
    assert _check(client, t, rid, None)["applied"] is False
    assert _apply(client, t, rid, None)["rebuilt"] is True
    drain()
    assert _version(client, t, rid, vid)["design"]["state"] == "applied"


def test_steps_while_figures_are_rechecked_and_when_a_design_build_fails(admin_client, monkeypatch):
    client, t, _ = admin_client
    rid, vid = _uploaded(client, t)
    verified = "blocked" if _version(client, t, rid, vid)["open_blocking"] else "done"
    # re-reading the PDF is Extract & verify's own work: that step is running
    r = client.post(f"{_R(t, rid)}/versions/{vid}/rerun")
    assert r.status_code == 200, r.text
    v = _version(client, t, rid, vid)
    assert v["steps"]["verify"] == "running" and v["steps"]["review"] == "building"
    drain()
    # a design build that fails: the design says so; the figures' checks still stand
    def broken(*a, **k):
        raise UserFacingError("The page couldn't be built.", retryable=False)
    compact = _with(_saved_theme(client, t, rid), spacing="compact")
    with monkeypatch.context() as m:
        m.setattr(site, "render_report", broken)
        _apply(client, t, rid, compact)
        drain()
    v = _version(client, t, rid, vid)
    assert v["status"] == "failed"
    assert v["steps"] == {"verify": verified, "design": "failed", "review": "failed"}
    # applying the same design again retries the build (the page already "has" the design, but
    # nothing else would get the version out of failed)
    assert _apply(client, t, rid, compact)["rebuilt"] is True
    drain()
    v = _version(client, t, rid, vid)
    assert v["status"] in ("needs_review", "validation_issues")
    assert v["steps"] == {"verify": verified, "design": "applied", "review": "ready"}


def test_a_design_build_reads_nothing_again_and_asks_the_ai_nothing(admin_client, monkeypatch):
    """The work a design change saves, and how much less time its build takes than the full
    rebuild every design change used to run (same report, back to back)."""
    from tests.test_pipeline_e2e import _inspect_and_create, _long_pdf_bytes
    client, t, _ = admin_client
    made = _inspect_and_create(client, t, _long_pdf_bytes(22), landing=False, period="q2")
    rid, vid = made["report_id"], made["version"]["id"]
    drain()
    ctx = _tenant_ctx(t)
    theme = _saved_theme(client, t, rid)
    with db.session(ctx) as s:          # the old Apply: a new design, then draw and check everything again
        v = s.get(ReportVersion, uuid.UUID(vid))
        s.get(Report, v.report_id).theme_override = _with(theme, colors__secondary="#0F766E")
        enqueue_stage(s, ctx, v, "render", force=True)
    t0 = time.monotonic()
    drain()
    full_wall, full = time.monotonic() - t0, _run_seconds(vid, "render")[-1]
    with monkeypatch.context() as m:
        _no_drawing(m)                               # no page drawn again, no AI layout looked up
        t0 = time.monotonic()
        _apply(client, t, rid, _with(theme, colors__secondary="#7A1F5C"))
        drain()
        design_wall = time.monotonic() - t0
    design = _run_seconds(vid, "render")[-1]
    assert [k for k, _, d in _jobs(vid) if d] == ["pipeline.render", "pipeline.validate"]
    assert [k for k, _, _ in _jobs(vid)].count("pipeline.extract") == 1          # the PDF isn't read again
    print(f"\n22 pages: full rebuild {full:.2f}s (to a checked page {full_wall:.2f}s); "
          f"design build {design:.2f}s (to a checked page {design_wall:.2f}s)")
    # (a text-only report has little to draw: most of what a design change saves is the checks)
    assert design_wall < full_wall

    # a page built before drawings were kept: its pages are drawn again, but with the layouts
    # already made for the PDF — the AI is never asked
    storage.delete(ctx, bundle_prefix(uuid.UUID(vid)) + "_artwork.json")
    asked = []
    real = page_layouts.layouts_for
    monkeypatch.setattr(page_layouts, "layouts_for", lambda *a, **k: (asked.append(k.get("cached_only")), real(*a, **k))[1])
    _apply(client, t, rid, _with(theme, colors__secondary="#B45309"))
    drain()
    assert asked == [True] and _version(client, t, rid, vid)["design"]["state"] == "applied"


def test_cached_only_layouts_never_call_the_model(monkeypatch):
    from app import ai_check
    from app.config import get_settings
    from app.extraction.schema_builder import extract
    from tests.test_ai_usage import _ctx, _spread_pdf
    from tests.test_faq_story import META

    pdf = _spread_pdf()
    schema, _ = extract(pdf, META)
    _, ctx = _ctx()
    cfg = get_settings()
    monkeypatch.setattr(cfg, "gemini_api_key", "test-key-not-real")
    monkeypatch.setattr(cfg, "ai_layout_max_credits", 5)
    page_layouts._REJECTED_UNTIL[0] = 0.0

    def called(_body):
        raise AssertionError("the model was asked")
    ai_check._OVERRIDE.append(called)
    try:
        assert page_layouts.layouts_for(ctx, None, "sha-cached-only", schema, pdf, cached_only=True) == {}
    finally:
        ai_check._OVERRIDE.remove(called)


def _tenant_ctx(t):
    from app.tenancy import tenant_context
    return tenant_context(None, t.id, None)
