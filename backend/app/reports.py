"""Report lifecycle: upload -> versions -> review actions -> publish / rollback, and the
tenant site build (manifest + index pages) that the public server reads.

All functions take a tenancy Context and a Session opened with it, so RLS scopes
every read and write to the tenant.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, defer

from app import audit, jobs, storage
from app.config import get_settings
from app.extraction.dates import parse_date
from app.extraction.numbers import try_parse_number
from app.extraction.schema_builder import PIPELINE_VERSION, period_label, schema_sha256
from app.models import (
    Domain,
    FigureReview,
    Report,
    ReportVersion,
    SourceFile,
    Tenant,
    TenantSettings,
    ValidationIssue,
)
from app.render import site
from app.render import theme as theming
from app.validation import issue_key
from app.tenancy import Context

REPORT_TYPES = ("quarterly_results", "investor_presentation", "annual_report", "other")
PERIODS = ("q1", "q2", "q3", "q4", "h1", "h2", "9m", "fy")


class ReportError(Exception):
    """Plain-language problem to show the client (HTTP 4xx)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ storage keys


def source_key(sha: str) -> str:
    return f"sources/{sha}.pdf"


def thumb_key(sha: str) -> str:
    """A small cover image for report lists: one per PDF, made when it's first built."""
    return f"sources/{sha}/thumb.webp"


def make_thumb(pdf: bytes) -> bytes:
    """Page 1 at list size (about 240 px wide): a few KB, never the whole page."""
    import io

    import pymupdf
    from PIL import Image
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    try:
        page = doc[0]
        zoom = THUMB_WIDTH / max(1.0, page.rect.width)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    finally:
        doc.close()
    buf = io.BytesIO()
    Image.frombytes("RGB", (pix.width, pix.height), pix.samples).save(buf, "WEBP", quality=72, method=4)
    return buf.getvalue()


THUMB_WIDTH = 240


def pdf_page_key(sha: str, page: int) -> str:
    """The page as printed, rendered once when the web page is built (review screen)."""
    return f"sources/{sha}/pages/p{page:04d}.webp"


def extraction_key(vid: uuid.UUID) -> str:
    return f"versions/{vid}/extraction.json"


def bundle_prefix(vid: uuid.UUID) -> str:
    return f"versions/{vid}/bundle/"


SITE_MANIFEST = "site/manifest.json"
SITE_INDEX_PREFIX = "site/index/"


# ------------------------------------------------------------------ settings & theme


def settings_for(s: Session, ctx: Context) -> TenantSettings:
    st = s.get(TenantSettings, ctx.require_tenant())
    if st is None:
        st = TenantSettings(tenant_id=ctx.tenant_id)
        s.add(st)
        s.flush()
    return st


def effective_theme(st: TenantSettings, report: Report | None = None) -> tuple[dict, list[dict]]:
    t = theming.merge(st.theme or {}, (report.theme_override if report else None) or {})
    t = theming.validate(t)
    return theming.enforce(t)


def effective_disclaimer(st: TenantSettings) -> str | None:
    return (st.disclaimer or "").strip() or (get_settings().disclaimer_default_text or "").strip() or None


def logo_src(theme: dict) -> str | None:
    logo = theme.get("logo")
    if not logo or not logo.get("key"):
        return None
    return f"{site.ORIGIN_PLACEHOLDER}/assets/{logo['key'].split('/')[-1]}"


# ------------------------------------------------------------------ upload


def validate_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    out = {}
    out["company_name"] = str(meta.get("company_name", "")).strip()[:200]
    if not out["company_name"]:
        raise ReportError("Enter the company name.")
    if meta.get("report_type") not in REPORT_TYPES:
        raise ReportError("Choose a report type.")
    out["report_type"] = meta["report_type"]
    try:
        fy = int(meta.get("fiscal_year"))
    except (TypeError, ValueError):
        raise ReportError("Enter the fiscal year as a number, e.g. the year it ends.") from None
    if not 1990 <= fy <= 2100:
        raise ReportError("That fiscal year doesn't look right.")
    out["fiscal_year"] = fy
    period = str(meta.get("period", "")).lower()
    if period not in PERIODS:
        raise ReportError("Choose the quarter or period.")
    out["period"] = period
    cur = str(meta.get("currency", "")).upper().strip()
    if len(cur) != 3 or not cur.isalpha():
        raise ReportError("Currency must be a 3-letter code like INR or USD.")
    out["currency"] = cur
    unit = str(meta.get("reporting_unit", "")).strip()[:40]
    if not unit:
        raise ReportError("Enter the reporting unit, e.g. ₹ crore.")
    out["reporting_unit"] = unit
    for k in ("period_start", "period_end"):
        v = meta.get(k)
        if v:
            d = parse_date(str(v))
            if not d or d.precision != "day":
                raise ReportError(f"{k.replace('_', ' ').capitalize()} must be a date.")
            out[k] = d.iso
    return out


def store_source(s: Session, ctx: Context, data: bytes, filename: str) -> SourceFile:
    """Validate and store an uploaded PDF (deduplicated by SHA-256 within the tenant)."""
    settings = get_settings()
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise ReportError(f"The file is larger than the {settings.max_upload_mb} MB limit.", 413)
    from app.extraction.pdf import PdfError, open_pdf
    try:
        doc = open_pdf(data)
    except PdfError as e:
        raise ReportError(str(e)) from e
    if doc.page_count > settings.max_pages:
        raise ReportError(f"This PDF has {doc.page_count} pages; the limit is {settings.max_pages}.")
    sha = hashlib.sha256(data).hexdigest()
    src = s.scalars(select(SourceFile).where(SourceFile.sha256 == sha)).first()
    if src is None:
        storage.put(ctx, source_key(sha), data, "application/pdf")
        src = SourceFile(tenant_id=ctx.require_tenant(), sha256=sha, storage_key=source_key(sha),
                         original_filename=(filename or "report.pdf")[:200], size_bytes=len(data),
                         page_count=doc.page_count, uploaded_by=ctx.user_id)
        s.add(src)
        s.flush()
    return src


def create_version(s: Session, ctx: Context, src: SourceFile, raw_meta: dict[str, Any]) -> ReportVersion:
    meta = validate_metadata(raw_meta)
    tenant_id = ctx.require_tenant()
    report = s.scalars(select(Report).where(Report.fiscal_year == meta["fiscal_year"],
                                            Report.period == meta["period"],
                                            Report.report_type == meta["report_type"],
                                            Report.deleted_at.is_(None))).first()
    if report is None:
        report = Report(tenant_id=tenant_id, created_by=ctx.user_id, **{k: meta[k] for k in (
            "company_name", "report_type", "fiscal_year", "period", "currency", "reporting_unit")})
        s.add(report)
        s.flush()
    else:
        for k in ("company_name", "currency", "reporting_unit"):
            setattr(report, k, meta[k])
    version_no = (s.scalar(select(func.max(ReportVersion.version_no)).where(ReportVersion.report_id == report.id)) or 0) + 1
    v = ReportVersion(tenant_id=tenant_id, report_id=report.id, version_no=version_no, source_file_id=src.id,
                      source_sha256=src.sha256, status="processing", stage="queued", created_by=ctx.user_id)
    s.add(v)
    s.flush()
    audit.record(s, ctx, "report.uploaded", target_type="report_version", target_id=v.id,
                 after={"report_id": str(report.id), "version": version_no, "sha256": src.sha256,
                        "filename": src.original_filename, **{k: meta[k] for k in meta}})
    enqueue_stage(s, ctx, v, "extract")
    return v


def create_upload(s: Session, ctx: Context, data: bytes, filename: str, raw_meta: dict[str, Any]) -> ReportVersion:
    validate_metadata(raw_meta)                     # fail fast before storing
    return create_version(s, ctx, store_source(s, ctx, data, filename), raw_meta)


# ------------------------------------------------------------------ pipeline chaining


def stage_key(v: ReportVersion, stage: str) -> str:
    if stage == "extract":
        return f"extract:{v.id}:{v.source_sha256}:{PIPELINE_VERSION}"
    if stage == "render":
        return f"render:{v.id}:{v.schema_sha256}:{_settings_fingerprint(v)}"
    if stage == "validate":
        return f"validate:{v.id}:{v.schema_sha256}:{v.bundle_sha256}"
    raise ValueError(stage)


def _settings_fingerprint(v: ReportVersion) -> str:
    return hashlib.sha256(json.dumps(v.theme_snapshot or {}, sort_keys=True).encode()).hexdigest()[:16]


def enqueue_stage(s: Session, ctx: Context, v: ReportVersion, stage: str, *, force: bool = False) -> None:
    key = stage_key(v, stage) + (f":rerun:{uuid.uuid4().hex[:8]}" if force else "")
    v.status, v.stage, v.error_plain = "processing", stage, None
    job = jobs.enqueue(s, ctx, f"pipeline.{stage}", {"version_id": str(v.id)}, idempotency_key=key)
    if job.version_id is None:
        job.version_id = v.id
    if job.status in ("succeeded", "failed", "cancelled"):
        # Same inputs seen before (e.g. an edit was undone). Stages are idempotent,
        # so run it again rather than leave the version waiting on a finished job.
        jobs.requeue(s, job)


# ------------------------------------------------------------------ issues & reviews


def latest_run_no(s: Session, vid: uuid.UUID) -> int:
    return s.scalar(select(func.max(ValidationIssue.run_no)).where(ValidationIssue.version_id == vid)) or 0


def open_blocking(s: Session, vid: uuid.UUID) -> int:
    run = latest_run_no(s, vid)
    return s.scalar(select(func.count()).select_from(ValidationIssue).where(
        ValidationIssue.version_id == vid, ValidationIssue.run_no == run,
        ValidationIssue.severity == "blocking", ValidationIssue.status == "open")) or 0


def _require_draft(v: ReportVersion) -> None:
    if v.published_at is not None:
        raise ReportError("This version is published and can't be changed. Create a new draft to make changes.", 409)
    if v.status == "processing":
        raise ReportError("This version is still processing. Try again in a moment.", 409)


def _fig_snapshot(f: dict) -> dict:
    return {k: f.get(k) for k in ("raw", "value", "iso", "kind", "status")}


def apply_figure_action(s: Session, ctx: Context, v: ReportVersion, fid: str, action: str,
                        new_raw: str | None = None, note: str | None = None) -> dict:
    """confirm | edit | not_a_figure — the three actions on a validation issue."""
    _require_draft(v)
    schema = deepcopy(v.schema_json)
    f = _figure_action_on(s, ctx, v, schema, fid, action, new_raw, note)
    _save_schema(s, ctx, v, schema)
    return f


def apply_bulk_review(s: Session, ctx: Context, v: ReportVersion, issues: list[ValidationIssue], action: str,
                      note: str | None = None) -> int:
    """One reviewer decision applied to many issues at once (confirm, or not a figure).
    Each figure is recorded and audited individually, exactly as if clicked one by one,
    but the page is rebuilt and re-checked once, not once per figure."""
    if action not in ("confirm", "not_a_figure"):
        raise ReportError("Only 'Confirm correct' and 'Not a figure' can be applied to several items at once.")
    _require_draft(v)
    schema = deepcopy(v.schema_json)
    done: set[str] = set()
    for i in issues:
        if i.fid:
            if i.fid not in done:
                _figure_action_on(s, ctx, v, schema, i.fid, action, None, note)
                done.add(i.fid)
        elif action == "confirm":
            _acknowledge_on(s, ctx, schema, i, note)
        else:
            raise ReportError("'Not a figure' only applies to issues about a figure.")
    _save_schema(s, ctx, v, schema)
    return len(issues)


def _figure_action_on(s: Session, ctx: Context, v: ReportVersion, schema: dict, fid: str, action: str,
                      new_raw: str | None, note: str | None) -> dict:
    f = schema["figures"].get(fid)
    if f is None:
        raise ReportError("That figure doesn't exist in this version.", 404)
    before = _fig_snapshot(f)
    who = {"by": str(ctx.user_id), "at": _now().isoformat()}
    if action == "confirm":
        f["review"] = {"action": "confirm", "raw": f["raw"], **who}
    elif action == "not_a_figure":
        f["status"] = "not_a_figure"
        f["review"] = {"action": "not_a_figure", **who}
    elif action == "edit":
        new_raw = (new_raw or "").strip()
        if not new_raw:
            raise ReportError("Enter the corrected value exactly as it appears in the PDF.")
        _apply_edit(f, new_raw)
        f["edited"] = {"original_raw": before["raw"], **who}
        f["review"] = {"action": "confirm", "raw": f["raw"], **who}
    else:
        raise ReportError("Unknown action.")
    s.add(FigureReview(tenant_id=ctx.tenant_id, version_id=v.id, fid=fid, action=action, old_value=before,
                       new_value=_fig_snapshot(f), note=(note or "")[:2000] or None, user_id=ctx.user_id))
    audit.record(s, ctx, f"figure.{action}", target_type="figure", target_id=f"{v.id}:{fid}",
                 before=before, after=_fig_snapshot(f) | ({"note": note} if note else {}))
    return f


def _apply_edit(f: dict, new_raw: str) -> None:
    kind = f["kind"]
    if kind in ("number", "percent", "bps", "multiple", "nil"):
        p = try_parse_number(new_raw)
        if p is None:
            raise ReportError("That isn't a valid number. Use the format shown in the PDF, e.g. 1,23,456.78 or (1,234).")
        f["kind"], f["raw"], f["value"], f["negative"] = p.kind, new_raw, p.value_str, p.negative
        f.pop("unparsed_numeric", None)
    elif kind == "identifier" and f.get("unparsed_numeric"):
        p = try_parse_number(new_raw)
        if p is None:
            raise ReportError("That isn't a valid number. Use the format shown in the PDF.")
        f["kind"], f["raw"], f["value"], f["negative"] = p.kind, new_raw, p.value_str, p.negative
        f.pop("unparsed_numeric", None)
        if p.kind == "percent":
            f["unit"] = "percent"
    elif kind == "date":
        d = parse_date(new_raw)
        if d is None:
            raise ReportError("That isn't a valid date.")
        f["raw"], f["iso"], f["precision"], f["ambiguous"] = new_raw, d.iso, d.precision, False
    else:
        f["raw"] = new_raw


def acknowledge_issue(s: Session, ctx: Context, v: ReportVersion, issue: ValidationIssue, note: str | None) -> None:
    """Confirm an issue that isn't about one figure (e.g. a table's unit)."""
    _require_draft(v)
    schema = deepcopy(v.schema_json)
    _acknowledge_on(s, ctx, schema, issue, note)
    _save_schema(s, ctx, v, schema)


def _acknowledge_on(s: Session, ctx: Context, schema: dict, issue: ValidationIssue, note: str | None) -> None:
    schema.setdefault("acknowledged", {})[issue_key(issue.check_name, issue.page, issue.bbox, issue.message)] = {
        "by": str(ctx.user_id), "at": _now().isoformat(), "note": note}
    audit.record(s, ctx, "issue.acknowledged", target_type="validation_issue", target_id=issue.id,
                 after={"check": issue.check_name, "message": issue.message, "note": note})


def flag_figure(s: Session, ctx: Context, v: ReportVersion, fid: str | None, section_id: str | None, note: str) -> ValidationIssue:
    """A reviewer flags something; it blocks publishing until an admin resolves it."""
    if v.published_at is not None:
        raise ReportError("This version is published. Flag issues on a new draft.", 409)
    schema = v.schema_json or {}
    f = schema.get("figures", {}).get(fid) if fid else None
    if fid and f is None:
        raise ReportError("That figure doesn't exist in this version.", 404)
    issue = ValidationIssue(tenant_id=ctx.tenant_id, version_id=v.id, run_no=latest_run_no(s, v.id),
                            check_name="reviewer_flag", severity="blocking", fid=fid,
                            page=f["source"]["page"] if f else None, section_id=(f or {}).get("section_id") or section_id,
                            bbox=f["source"]["bbox"] if f else None,
                            message=f"Flagged by a reviewer: {note.strip()[:500]}", actual=f["raw"] if f else None)
    s.add(issue)
    s.add(FigureReview(tenant_id=ctx.tenant_id, version_id=v.id, fid=fid or f"section:{section_id}", action="flag",
                       note=note[:2000], user_id=ctx.user_id))
    audit.record(s, ctx, "figure.flagged", target_type="figure", target_id=f"{v.id}:{fid or section_id}",
                 after={"note": note[:500]})
    if v.status == "needs_review":
        v.status = "validation_issues"
    s.flush()
    return issue


def _save_schema(s: Session, ctx: Context, v: ReportVersion, schema: dict) -> None:
    v.schema_json = schema
    v.schema_sha256 = schema_sha256(schema)
    enqueue_stage(s, ctx, v, "render")


# ------------------------------------------------------------------ publish / rollback


def publish(s: Session, ctx: Context, v: ReportVersion, *, confirm_reviewed: bool) -> ReportVersion:
    if not confirm_reviewed:
        raise ReportError("Tick the box to confirm you have reviewed the figures.")
    if v.published_at is not None:
        raise ReportError("This version is already published.", 409)
    if v.status == "processing":
        raise ReportError("This version is still processing.", 409)
    if v.status == "failed":
        raise ReportError("Processing failed for this version; it can't be published.", 409)
    blocking = open_blocking(s, v.id)
    if blocking:
        raise ReportError(f"Publishing is blocked: {blocking} blocking validation issue(s) are open.", 409)
    if not (v.validated_schema_sha256 == v.schema_sha256 and v.validated_bundle_sha256 == v.bundle_sha256
            and v.schema_sha256 and v.bundle_sha256):
        raise ReportError("This version changed after it was validated. Wait for validation to finish.", 409)
    st = settings_for(s, ctx)
    if not effective_disclaimer(st):
        raise ReportError("Add a disclaimer in Settings before publishing (the PDF must be stated as the official source).", 409)
    report = s.get(Report, v.report_id)
    assert report is not None
    previous = report.live_version_id
    if previous:
        prev = s.get(ReportVersion, previous)
        if prev is not None:
            prev.status = "superseded"
    v.status, v.published_at, v.published_by, v.review_confirmed_by = "published", _now(), ctx.user_id, ctx.user_id
    report.live_version_id = v.id
    s.flush()
    audit.record(s, ctx, "report.published", target_type="report_version", target_id=v.id,
                 before={"live_version_id": str(previous) if previous else None},
                 after={"live_version_id": str(v.id), "version": v.version_no, "schema_sha256": v.schema_sha256,
                        "bundle_sha256": v.bundle_sha256, "source_sha256": v.source_sha256,
                        "review_confirmed": True})
    rebuild_site(s, ctx)
    return v


def delete_report(s: Session, ctx: Context, report: Report) -> None:
    """Hide a report from the workspace and, if it was live, from the public site.

    Published versions are immutable, so the rows stay. The audit log records who
    removed the report. The same period can be uploaded again afterwards.
    """
    was_live = report.live_version_id is not None
    audit.record(s, ctx, "report.deleted", target_type="report", target_id=report.id,
                 before={"company_name": report.company_name, "report_type": report.report_type,
                         "fiscal_year": report.fiscal_year, "period": report.period,
                         "live_version_id": str(report.live_version_id) if report.live_version_id else None})
    report.live_version_id = None
    report.deleted_at = _now()
    s.flush()
    if was_live:
        rebuild_site(s, ctx)


def rollback(s: Session, ctx: Context, report: Report, target: ReportVersion) -> None:
    if target.report_id != report.id or target.published_at is None:
        raise ReportError("You can only roll back to a version that was published before.", 409)
    if report.live_version_id == target.id:
        raise ReportError("That version is already live.", 409)
    cur = s.get(ReportVersion, report.live_version_id) if report.live_version_id else None
    if cur is not None:
        cur.status = "superseded"
    target.status = "published"
    before = str(report.live_version_id) if report.live_version_id else None
    report.live_version_id = target.id
    s.flush()
    audit.record(s, ctx, "report.rolled_back", target_type="report", target_id=report.id,
                 before={"live_version_id": before}, after={"live_version_id": str(target.id), "version": target.version_no})
    rebuild_site(s, ctx)


def new_draft_from(s: Session, ctx: Context, v: ReportVersion) -> ReportVersion:
    """Copy a version's schema into a new draft (to edit figures or re-theme after publishing)."""
    if not v.schema_json:
        raise ReportError("This version has no schema to copy.", 409)
    no = (s.scalar(select(func.max(ReportVersion.version_no)).where(ReportVersion.report_id == v.report_id)) or 0) + 1
    schema = deepcopy(v.schema_json)
    schema["metadata"]["version"] = no
    nv = ReportVersion(tenant_id=ctx.tenant_id, report_id=v.report_id, version_no=no, source_file_id=v.source_file_id,
                       source_sha256=v.source_sha256, status="processing", stage="render",
                       created_from_version_id=v.id, extraction_key=v.extraction_key, schema_json=schema,
                       schema_sha256=schema_sha256(schema), created_by=ctx.user_id)
    s.add(nv)
    s.flush()
    audit.record(s, ctx, "report.draft_created", target_type="report_version", target_id=nv.id,
                 after={"from_version": v.version_no, "version": no})
    enqueue_stage(s, ctx, nv, "render")
    return nv


# ------------------------------------------------------------------ public site build


def public_origin(s: Session, ctx: Context) -> tuple[str, bool]:
    """(origin, is_custom_domain). Custom domain once live, else the noindex preview host."""
    cfg = get_settings()
    d = s.scalars(select(Domain).where(Domain.status == "live")).first()
    if d is not None:
        return f"https://{d.hostname}", True
    tenant = s.get(Tenant, ctx.require_tenant())
    assert tenant is not None
    return cfg.preview_origin(tenant.slug), False


def rebuild_site(s: Session, ctx: Context) -> dict:
    """Write site/manifest.json (path -> storage key) and the tenant index pages."""
    tenant = s.get(Tenant, ctx.require_tenant())
    assert tenant is not None
    st = settings_for(s, ctx)
    theme, _ = effective_theme(st)
    reports = s.scalars(select(Report).where(Report.deleted_at.is_(None),
                                             Report.live_version_id.is_not(None))).all()
    entries = []
    paths: dict[str, dict] = {}
    for r in reports:
        v = s.get(ReportVersion, r.live_version_id)
        if v is None or not v.bundle_key or not v.schema_json:
            continue
        m = v.schema_json["metadata"]
        base = site.report_base_path(m)
        files = json.loads(storage.get(ctx, v.bundle_key + "_files.json"))
        for rel in files:
            paths["/" + rel] = {"key": v.bundle_key + rel, "report_id": str(r.id), "version_id": str(v.id)}
            if rel.endswith("/index.html"):
                paths["/" + rel[: -len("index.html")]] = paths["/" + rel]
        paths[base + "source.pdf"] = {"key": source_key(v.source_sha256), "report_id": str(r.id),
                                      "version_id": str(v.id), "content_type": "application/pdf"}
        entries.append({"base": base, "period_label": m["period_label"],
                        "type_label": site.REPORT_TYPE_LABEL[m["report_type"]],
                        "sections": [sec["slug"] for sec in v.schema_json["sections"]],
                        "lastmod": v.published_at.date().isoformat() if v.published_at else None,
                        "sort": (m["fiscal_year"], _period_order(m["period"]), v.published_at.isoformat() if v.published_at else ""),
                        "report_id": str(r.id)})
    entries.sort(key=lambda e: e["sort"], reverse=True)
    company = reports[0].company_name if reports else tenant.name
    index_files = site.render_site_index(company, [dict(e) for e in entries], theme=theme,
                                         disclaimer=effective_disclaimer(st), robots_policy=st.robots_policy,
                                         logo_src=logo_src(theme))
    storage.put_many(ctx, [(SITE_INDEX_PREFIX + rel, data, "application/octet-stream") for rel, data in index_files.items()])
    for rel in index_files:
        paths["/" + rel] = {"key": SITE_INDEX_PREFIX + rel}
        if rel.endswith("index.html"):
            paths["/" + rel[: -len("index.html")]] = paths["/" + rel]
    if theme.get("logo") and theme["logo"].get("key"):
        paths["/assets/" + theme["logo"]["key"].split("/")[-1]] = {"key": theme["logo"]["key"]}
    manifest = {"generated_at": _now().isoformat(), "paths": paths,
                "latest": entries[0]["base"] if entries else None,
                "reports": [{k: e[k] for k in ("base", "period_label", "type_label", "report_id")} for e in entries]}
    storage.put(ctx, SITE_MANIFEST, json.dumps(manifest).encode(), "application/json")
    return manifest


def _period_order(p: str) -> int:
    return {"q1": 1, "h1": 2, "q2": 2, "q3": 3, "9m": 3, "q4": 4, "h2": 4, "fy": 5}[p]


# ------------------------------------------------------------------ listing helpers


STATUS_LABEL = {"processing": "Processing", "failed": "Failed", "needs_review": "Needs review",
                "validation_issues": "Validation issues", "published": "Live", "superseded": "Superseded"}


def report_status(report: Report, latest: ReportVersion | None) -> str:
    if latest is None:
        return "Processing"
    live_id = report.live_version_id
    if live_id is not None and latest.id != live_id and latest.status not in ("superseded",):
        return "Draft changes"
    if live_id is not None and latest.id == live_id:
        return "Live"
    return STATUS_LABEL.get(latest.status, latest.status)


def versions_for(s: Session, report_id: uuid.UUID) -> list[ReportVersion]:
    """Every version of one report, newest first, without the extracted document."""
    return list(s.scalars(
        select(ReportVersion).where(ReportVersion.report_id == report_id)
        .options(defer(ReportVersion.schema_json))
        .order_by(ReportVersion.version_no.desc())))


def latest_versions(s: Session) -> dict[uuid.UUID, ReportVersion]:
    """The newest version of each report in this tenant, without the extracted document.

    The home page only needs status and a summary. `schema_json` is the whole
    extraction (every figure and section) and is what made the list slow.
    """
    rows = s.scalars(
        select(ReportVersion)
        .options(defer(ReportVersion.schema_json))
        .distinct(ReportVersion.report_id)
        .order_by(ReportVersion.report_id, ReportVersion.version_no.desc())
    ).all()
    return {v.report_id: v for v in rows}


def decimal_str(v: Decimal) -> str:
    return format(v, "f")


def default_title(meta: dict) -> str:
    return f"{period_label(meta['period'], meta['fiscal_year'])} {site.REPORT_TYPE_LABEL[meta['report_type']]}"
