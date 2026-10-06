"""Tenant settings: theme (3 modes), logo, disclaimer, robots policy, LLM opt-in,
analytics (GA4 + consent + stats) and the custom domain flow."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app import audit, db, domain_jobs, domains, storage
from app.api.deps import require
from app.config import get_settings as get_platform_settings
from app.models import Domain, EdgeHit, Report, ReportVersion, Tenant
from app.render import analyze, site
from app.render import theme as theming
from app.reports import effective_disclaimer, effective_theme, new_draft_from, public_origin, rebuild_site, settings_for
from app.tenancy import Context

router = APIRouter(prefix="/api/tenants/{tenant_id}", tags=["settings"])


# ------------------------------------------------------------------ general settings


@router.get("/settings")
def get_settings_(ctx: Context = Depends(require("report.view"))) -> dict:
    with db.session(ctx) as s:
        st = settings_for(s, ctx)
        theme, adjustments = effective_theme(st)
        origin, custom = public_origin(s, ctx)
        return {"theme": theme, "theme_mode": st.theme_mode, "contrast": theming.contrast_report(theming.validate(st.theme or {})),
                "contrast_adjustments": adjustments, "disclaimer": st.disclaimer,
                "effective_disclaimer": effective_disclaimer(st), "robots_policy": st.robots_policy,
                "ga4_measurement_id": st.ga4_measurement_id, "consent_banner_enabled": st.consent_banner_enabled,
                "llm_assist_enabled": st.llm_assist_enabled, "site_origin": origin, "custom_domain_live": custom,
                "fonts": {"system": list(theming.SYSTEM_FONTS), "google": sorted(theming.GOOGLE_FONTS)},
                "ai_costs": _ai_costs()}


def _ai_costs() -> dict:
    """What a report may spend on AI, shown before upload: the built-in page layout and
    every check are free; the AI double-check of figures read from images is capped."""
    from app.config import get_settings as cfg_
    c = cfg_()
    on = bool(c.gemini_api_key)
    return {"ai_available": on, "auto_check_cap_credits": c.ai_auto_check_max_credits if on else 0,
            "layout_cap_credits": c.ai_layout_max_credits if on else 0, "credit_usd": c.ai_credit_usd}


class SettingsIn(BaseModel):
    disclaimer: str | None = Field(default=None, max_length=5000)
    robots_policy: str | None = Field(default=None, pattern="^(allow_all|search_and_answer|block_ai)$")
    llm_assist_enabled: bool | None = None


@router.put("/settings")
def put_settings(body: SettingsIn, ctx: Context = Depends(require("theme.manage"))) -> dict:
    with db.session(ctx) as s:
        st = settings_for(s, ctx)
        before = {"disclaimer": st.disclaimer, "robots_policy": st.robots_policy, "llm_assist_enabled": st.llm_assist_enabled}
        if body.disclaimer is not None:
            st.disclaimer = body.disclaimer.strip() or None
        if body.robots_policy is not None:
            st.robots_policy = body.robots_policy
        if body.llm_assist_enabled is not None:
            st.llm_assist_enabled = body.llm_assist_enabled
        st.updated_at = func.now()
        after = {"disclaimer": st.disclaimer, "robots_policy": st.robots_policy, "llm_assist_enabled": st.llm_assist_enabled}
        audit.record(s, ctx, "settings.changed", target_type="tenant_settings", target_id=ctx.tenant_id,
                     before=before, after=after)
        rebuild_site(s, ctx)
        return after


# ------------------------------------------------------------------ AI usage


@router.get("/ai-usage")
def get_ai_usage(ctx: Context = Depends(require("report.view"))) -> dict:
    """This month's AI spend for the workspace, by feature, with the latest requests."""
    from app import ai_usage
    cfg = get_platform_settings()
    out = ai_usage.summary(ctx)
    out["available"] = {"ai_check": bool(cfg.gemini_api_key), "section_labels": bool(cfg.anthropic_api_key)}
    return out


# ------------------------------------------------------------------ theme


class ThemeIn(BaseModel):
    mode: str = Field(pattern="^(match_website|custom|reference)$")
    theme: dict


@router.put("/theme")
def put_theme(body: ThemeIn, ctx: Context = Depends(require("theme.manage"))) -> dict:
    try:
        t = theming.validate(body.theme)
    except theming.ThemeError as e:
        raise HTTPException(400, str(e)) from e
    with db.session(ctx) as s:
        st = settings_for(s, ctx)
        if st.theme and st.theme.get("logo") and not t.get("logo"):
            t["logo"] = st.theme["logo"]          # logo is managed by its own endpoint
        before = {"theme": st.theme, "mode": st.theme_mode}
        st.theme, st.theme_mode = t, body.mode
        audit.record(s, ctx, "theme.changed", target_type="tenant_settings", target_id=ctx.tenant_id,
                     before=before, after={"theme": t, "mode": body.mode})
        rebuild_site(s, ctx)
        used, adjustments = theming.enforce(t)
        return {"theme": t, "contrast": theming.contrast_report(t), "applied": used, "contrast_adjustments": adjustments}


@router.post("/theme/contrast")
def theme_contrast(body: dict, ctx: Context = Depends(require("report.view"))) -> dict:
    try:
        t = theming.validate(body.get("theme", {}))
    except theming.ThemeError as e:
        raise HTTPException(400, str(e)) from e
    return {"contrast": theming.contrast_report(t), "applied": theming.enforce(t)[0]}


class UrlIn(BaseModel):
    url: str = Field(min_length=4, max_length=500)


@router.post("/theme/analyze-website")
def analyze_website(body: UrlIn, ctx: Context = Depends(require("theme.manage"))) -> dict:
    url = body.url if re.match(r"^https?://", body.url) else "https://" + body.url
    try:
        return analyze.analyze_website(url)
    except analyze.FetchError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/theme/analyze-references")
async def analyze_references(urls: str = Form(""), images: list[UploadFile] = File(default=[]),
                             ctx: Context = Depends(require("theme.manage"))) -> dict:
    url_list = [u.strip() if re.match(r"^https?://", u.strip()) else "https://" + u.strip()
                for u in urls.splitlines() if u.strip()]
    blobs = [await f.read(20 * 1024 * 1024) for f in images[:5]]
    pdfs = [b for b in blobs if b.startswith(b"%PDF-")]
    pics = [b for b in blobs if not b.startswith(b"%PDF-")]
    try:
        if pdfs and not url_list and not pics:
            return analyze.analyze_pdf(pdfs[0])
        result = analyze.analyze_references(url_list, pics) if (url_list or pics) else analyze.analyze_pdf(pdfs[0])
        return result
    except analyze.FetchError as e:
        raise HTTPException(400, str(e)) from e


class PaletteIn(BaseModel):
    colors: list[str] = Field(min_length=1, max_length=8)


@router.post("/theme/from-palette")
def theme_from_palette(body: PaletteIn, ctx: Context = Depends(require("report.view"))) -> dict:
    try:
        return analyze.palette_proposal(body.colors)
    except analyze.FetchError as e:
        raise HTTPException(400, str(e)) from e


def _store_logo(s, ctx: Context, png: bytes, alt: str | None) -> dict:
    sha = hashlib.sha256(png).hexdigest()
    key = f"assets/logo-{sha[:16]}.png"
    storage.put(ctx, key, png, "image/png")
    st = settings_for(s, ctx)
    t = theming.validate(st.theme or {})
    before = t.get("logo")
    t["logo"] = {"key": key, "alt": (alt or "")[:120], "sha": sha}
    st.theme = t
    audit.record(s, ctx, "theme.logo_changed", target_type="tenant_settings", target_id=ctx.tenant_id,
                 before={"logo": before}, after={"logo": t["logo"]})
    rebuild_site(s, ctx)
    return t["logo"]


@router.post("/theme/logo")
async def upload_logo(file: UploadFile = File(...), ctx: Context = Depends(require("theme.manage"))) -> dict:
    data = await file.read(2 * 1024 * 1024)
    try:
        png, _ = analyze.validate_logo(data)
    except analyze.FetchError as e:
        raise HTTPException(400, str(e)) from e
    with db.session(ctx) as s:
        return {"logo": _store_logo(s, ctx, png, None)}


@router.post("/theme/logo-from-url")
def logo_from_url(body: UrlIn, ctx: Context = Depends(require("theme.manage"))) -> dict:
    try:
        png, _ = analyze.fetch_logo(body.url)
    except analyze.FetchError as e:
        raise HTTPException(400, str(e)) from e
    with db.session(ctx) as s:
        return {"logo": _store_logo(s, ctx, png, None)}


@router.delete("/theme/logo")
def delete_logo(ctx: Context = Depends(require("theme.manage"))) -> dict:
    with db.session(ctx) as s:
        st = settings_for(s, ctx)
        t = theming.validate(st.theme or {})
        t["logo"] = None
        st.theme = t
        audit.record(s, ctx, "theme.logo_removed", target_type="tenant_settings", target_id=ctx.tenant_id)
        rebuild_site(s, ctx)
        return {"logo": None}


@router.get("/theme/logo")
def get_logo(ctx: Context = Depends(require("report.view"))):
    from fastapi.responses import Response
    with db.session(ctx) as s:
        t = settings_for(s, ctx).theme or {}
    logo = t.get("logo")
    if not logo:
        raise HTTPException(404, "No logo.")
    return Response(storage.get(ctx, logo["key"]), media_type="image/png")


PREVIEW_PAGES = 3


@router.post("/theme/preview")
def theme_preview(body: dict, ctx: Context = Depends(require("report.view"))):
    """Live preview for the theme editor: renders the tenant's latest report (or a
    sample) with the given tokens. Identical HTML structure; only CSS differs."""
    from fastapi.responses import HTMLResponse
    try:
        t = theming.validate(body.get("theme", {}))
    except theming.ThemeError as e:
        raise HTTPException(400, str(e)) from e
    used, _ = theming.enforce(t)
    with db.session(ctx) as s:
        q = select(ReportVersion).where(ReportVersion.schema_json.is_not(None))
        if body.get("report_id"):
            q = q.where(ReportVersion.report_id == body["report_id"])
        v = s.scalars(q.order_by(ReportVersion.created_at.desc())).first()
        schema = v.schema_json if v else _sample_schema(s.get(Tenant, ctx.tenant_id).name)
        sha = v.source_sha256 if v else None
        st = settings_for(s, ctx)
        disclaimer = effective_disclaimer(st)
    pdf = None
    if sha:
        from app.reports import source_key
        try:
            pdf = storage.get(ctx, source_key(sha))
        except Exception:  # noqa: BLE001 - no stored PDF: preview the text layout instead
            pdf = None
    # The first few pages are enough to judge a design, and keep the preview instant.
    files = site.render_report(schema, theme=used, disclaimer=disclaimer, pdf_bytes=pdf, max_pages=PREVIEW_PAGES,
                               logo_src=f"/api/tenants/{ctx.tenant_id}/theme/logo" if used.get("logo") else None)
    base = site.report_base_path(schema["metadata"]).lstrip("/")
    html = files[base + "index.html"].decode()
    # One self-contained response: page artwork and fonts travel inline.
    import base64
    for name, data in files.items():
        if name.endswith((".webp", ".woff2")):
            uri = f"data:{site.content_type(name)};base64,{base64.b64encode(data).decode()}"
            html = html.replace(f"{site.ORIGIN_PLACEHOLDER}/{name}", uri)
    html = html.replace(site.ORIGIN_PLACEHOLDER, "#")
    return HTMLResponse(html, headers={"X-Frame-Options": "SAMEORIGIN", "X-Robots-Tag": "noindex"})


def _sample_schema(company: str) -> dict:
    fig = lambda fid, raw, value, kind="number", **kw: {  # noqa: E731
        "id": fid, "kind": kind, "raw": raw, "value": value, "iso": None, "unit": "crore", "currency": "INR",
        "period": None, "row_label": kw.get("row"), "col_label": kw.get("col"), "table_id": "b2", "section_id": "s1",
        "role": kw.get("role", "cell"), "source": {"page": 1, "bbox": [0, 0, 1, 1]}, "method": "text_layer",
        "confidence": 1.0, "status": "active", "review": None}
    figures = {"p1f1": fig("p1f1", "Q2 FY26", None, "period", role="header"),
               "p1f2": fig("p1f2", "Q2 FY25", None, "period", role="header"),
               "p1f3": fig("p1f3", "1,234.56", "1234.56", row="Revenue", col="Q2 FY26"),
               "p1f4": fig("p1f4", "1,098.30", "1098.30", row="Revenue", col="Q2 FY25"),
               "p1f5": fig("p1f5", "12.4%", "12.4", "percent", role="text")}
    return {"schema_version": "1.0", "metadata": {
        "company": company, "report_type": "quarterly_results", "fiscal_year": 2026, "period": "q2",
        "period_label": "Sample", "fiscal_year_label": "Sample", "currency": "INR", "reporting_unit": "₹ crore",
        "reporting_unit_canonical": "crore", "source_pdf": {"sha256": "0" * 64, "page_count": 1},
        "extraction": {"timestamp": "", "pipeline_version": "", "methods": {}}, "version": 1},
        "pages": [], "figures": figures, "sections": [{
            "id": "s1", "type": "highlights", "heading": [{"t": "Sample section"}], "heading_text": "Sample section",
            "order": 1, "slug": "sample", "blocks": [
                {"id": "b1", "type": "paragraph", "runs": [{"t": "Revenue grew "}, {"f": "p1f5"}, {"t": " (sample text)."}],
                 "source": {"page": 1, "bbox": [0, 0, 1, 1]}},
                {"id": "b2", "type": "table", "caption": [{"t": "(₹ in crore)"}], "unit": "crore", "currency": "INR",
                 "columns": [{"label": "Q2 FY26"}, {"label": "Q2 FY25"}],
                 "header_rows": [[{"colspan": 1, "runs": [{"f": "p1f1"}]}, {"colspan": 1, "runs": [{"f": "p1f2"}]}]],
                 "rows": [{"label": [{"t": "Revenue"}], "label_text": "Revenue",
                           "cells": [{"runs": [{"f": "p1f3"}]}, {"runs": [{"f": "p1f4"}]}]}],
                 "source": {"page": 1, "bbox": [0, 0, 1, 1]}}]}]}


@router.post("/theme/apply-to-live")
def apply_theme_to_live(ctx: Context = Depends(require("theme.manage"))) -> dict:
    """Published versions are immutable, so a new theme reaches live reports as new
    drafts (same figures, new styling) that go through validation and approval."""
    created = []
    with db.session(ctx) as s:
        for r in s.scalars(select(Report).where(Report.live_version_id.is_not(None))).all():
            v = s.get(ReportVersion, r.live_version_id)
            if v is not None:
                created.append(str(new_draft_from(s, ctx, v).id))
    return {"drafts_created": created}


# ------------------------------------------------------------------ analytics


class AnalyticsIn(BaseModel):
    ga4_measurement_id: str | None = Field(default=None, max_length=20)
    consent_banner_enabled: bool = True
    consent_banner_off_ack: str | None = Field(default=None, max_length=500)


GA4_RE = re.compile(r"^G-[A-Z0-9]{4,12}$")


@router.put("/analytics")
def put_analytics(body: AnalyticsIn, ctx: Context = Depends(require("analytics.manage"))) -> dict:
    gid = (body.ga4_measurement_id or "").strip().upper() or None
    if gid and not GA4_RE.match(gid):
        raise HTTPException(400, "A GA4 Measurement ID looks like G-XXXXXXXXXX (from GA4 Admin → Data streams).")
    if not body.consent_banner_enabled and not (body.consent_banner_off_ack or "").strip():
        raise HTTPException(400, "To turn off the consent banner, confirm you have another lawful basis for "
                                 "analytics cookies in your markets.")
    with db.session(ctx) as s:
        st = settings_for(s, ctx)
        before = {"ga4_measurement_id": st.ga4_measurement_id, "consent_banner_enabled": st.consent_banner_enabled}
        st.ga4_measurement_id = gid
        st.consent_banner_enabled = body.consent_banner_enabled
        st.consent_banner_off_ack = None if body.consent_banner_enabled else body.consent_banner_off_ack
        after = {"ga4_measurement_id": gid, "consent_banner_enabled": st.consent_banner_enabled,
                 "consent_banner_off_ack": st.consent_banner_off_ack}
        audit.record(s, ctx, "analytics.changed", target_type="tenant_settings", target_id=ctx.tenant_id,
                     before=before, after=after)
        # The tag is injected when pages are served, so the change is live on the next request
        # for every published page (no content re-render needed; approved bundles stay byte-identical).
        return after


@router.get("/analytics/stats")
def analytics_stats(days: int = 30, ctx: Context = Depends(require("report.view"))) -> dict:
    days = max(1, min(days, 365))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    with db.session(ctx) as s:
        rows = s.execute(select(EdgeHit.report_id, EdgeHit.agent_class, func.count())
                         .where(EdgeHit.at >= since, EdgeHit.status < 400)
                         .group_by(EdgeHit.report_id, EdgeHit.agent_class)).all()
        daily = s.execute(select(func.date_trunc("day", EdgeHit.at).label("d"), EdgeHit.agent_class, func.count())
                          .where(EdgeHit.at >= since, EdgeHit.status < 400)
                          .group_by("d", EdgeHit.agent_class).order_by("d")).all()
        reports = {str(r.id): r for r in s.scalars(select(Report)).all()}
    per: dict[str, dict] = {}
    for rid, cls, n in rows:
        key = str(rid) if rid else "site"
        e = per.setdefault(key, {"report_id": key, "views": 0, "crawlers": {}})
        if cls == "human":
            e["views"] += n
        else:
            e["crawlers"][cls.removeprefix("bot:")] = e["crawlers"].get(cls.removeprefix("bot:"), 0) + n
    from app.extraction.schema_builder import period_label
    for k, e in per.items():
        r = reports.get(k)
        e["label"] = f"{period_label(r.period, r.fiscal_year)} {site.REPORT_TYPE_LABEL[r.report_type]}" if r else "Site pages"
    series: dict[str, dict] = {}
    for d, cls, n in daily:
        day = d.date().isoformat()
        e = series.setdefault(day, {"day": day, "views": 0, "crawler_visits": 0})
        e["views" if cls == "human" else "crawler_visits"] += n
    return {"days": days, "reports": sorted(per.values(), key=lambda e: -e["views"]), "daily": list(series.values())}


# ------------------------------------------------------------------ custom domain


def _domain_json(d: Domain | None, company: str) -> dict | None:
    if d is None:
        return None
    return {"id": str(d.id), "hostname": d.hostname, "status": d.status, "failure_code": d.failure_code,
            "failure_reason": d.failure_reason, "last_checked_at": d.last_checked_at.isoformat() if d.last_checked_at else None,
            "verified_at": d.verified_at.isoformat() if d.verified_at else None,
            "live_at": d.live_at.isoformat() if d.live_at else None,
            "cert_expires_at": d.cert_expires_at.isoformat() if d.cert_expires_at else None,
            "records": domains.records(d.hostname, d.verify_token),
            "providers": domains.PROVIDER_NOTES, "it_email": domains.it_email(company, d.hostname, d.verify_token)}


@router.get("/domain")
def get_domain(ctx: Context = Depends(require("report.view"))) -> dict:
    with db.session(ctx) as s:
        d = s.scalars(select(Domain).where(Domain.status != "removed")).first()
        tenant = s.get(Tenant, ctx.tenant_id)
        origin, custom = public_origin(s, ctx)
        from app.config import get_settings
        cfg = get_settings()
        preview = cfg.preview_origin(tenant.slug)
        return {"domain": _domain_json(d, tenant.name), "preview_origin": preview, "site_origin": origin,
                "custom_domain_live": custom}


class DomainIn(BaseModel):
    hostname: str = Field(min_length=3, max_length=253)


@router.post("/domain", status_code=201)
def add_domain(body: DomainIn, ctx: Context = Depends(require("domain.manage"))) -> dict:
    try:
        host = domains.normalize_hostname(body.hostname)
    except domains.DomainError as e:
        raise HTTPException(400, str(e)) from e
    try:
        with db.session(ctx) as s:
            if s.scalars(select(Domain).where(Domain.status != "removed")).first():
                raise HTTPException(409, "Remove the current domain before adding another.")
            d = Domain(tenant_id=ctx.tenant_id, hostname=host, verify_token=domains.new_token())
            s.add(d)
            s.flush()
            audit.record(s, ctx, "domain.added", target_type="domain", target_id=d.id, after={"hostname": host})
            return _domain_json(d, s.get(Tenant, ctx.tenant_id).name)
    except IntegrityError as e:
        raise HTTPException(409, "That domain is already connected to another account.") from e


@router.post("/domain/check")
def check_domain(ctx: Context = Depends(require("domain.manage"))) -> dict:
    with db.session(ctx) as s:
        d = s.scalars(select(Domain).where(Domain.status != "removed")).first()
        if d is None:
            raise HTTPException(404, "No domain to check.")
        did = d.id
    d = domain_jobs.run_check(ctx, did, actor=ctx.user_id)
    with db.session(ctx) as s:
        return _domain_json(s.get(Domain, did), s.get(Tenant, ctx.tenant_id).name)


@router.delete("/domain", status_code=204)
def remove_domain(ctx: Context = Depends(require("domain.manage"))) -> None:
    with db.session(ctx) as s:
        d = s.scalars(select(Domain).where(Domain.status != "removed")).first()
        if d is None:
            raise HTTPException(404, "No domain connected.")
        was_live = d.status == "live"
        # free the hostname for reuse; keep the row for history
        d.hostname = f"{d.hostname}.removed-{d.id.hex[:8]}"
        d.status = "removed"
        audit.record(s, ctx, "domain.removed", target_type="domain", target_id=d.id)
        if was_live:
            rebuild_site(s, ctx)
