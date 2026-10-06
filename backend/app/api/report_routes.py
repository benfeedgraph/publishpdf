"""Report endpoints: /api/tenants/{tenant_id}/reports/..."""

from __future__ import annotations

import json
import re
import uuid
from collections import OrderedDict

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import defer

from app import audit, db, storage
from app.api.deps import require
from app.api.tenant_routes import job_json
from app.config import get_settings
from app.extraction.schema_builder import render_page_png
from app.models import Comment, FigureReview, Job, Report, ReportVersion, SourceFile, User, ValidationIssue
from app.render.site import ORIGIN_PLACEHOLDER
from app.reports import (
    ReportError,
    acknowledge_issue,
    apply_bulk_review,
    apply_figure_action,
    create_upload,
    create_version,
    delete_report,
    store_source,
    enqueue_stage,
    flag_figure,
    latest_run_no,
    latest_versions,
    new_draft_from,
    open_blocking,
    publish,
    report_status,
    rollback,
    source_key,
    versions_for,
)
from app.tenancy import Context

router = APIRouter(prefix="/api/tenants/{tenant_id}/reports", tags=["reports"])


def _err(e: ReportError) -> HTTPException:
    return HTTPException(e.status, e.message)


def _iso(d) -> str | None:
    return d.isoformat() if d else None


def _get_report(s, report_id: uuid.UUID) -> Report:
    r = s.get(Report, report_id)
    if r is None or r.deleted_at is not None:
        raise HTTPException(404, "Report not found.")
    return r


def _get_version(s, report_id: uuid.UUID, version_id: uuid.UUID) -> ReportVersion:
    # schema_json is the full extraction. Load it only when a handler reads it.
    v = s.scalars(select(ReportVersion).options(defer(ReportVersion.schema_json))
                  .where(ReportVersion.id == version_id, ReportVersion.report_id == report_id)).first()
    if v is None:
        raise HTTPException(404, "Version not found.")
    return v


def version_json(v: ReportVersion, *, live_id: uuid.UUID | None = None) -> dict:
    return {"id": str(v.id), "version_no": v.version_no, "status": v.status, "stage": v.stage,
            "error": v.error_plain, "created_at": _iso(v.created_at), "published_at": _iso(v.published_at),
            "is_live": live_id == v.id, "source_sha256": v.source_sha256,
            "schema_sha256": v.schema_sha256, "bundle_sha256": v.bundle_sha256,
            "validation": v.validation_summary, "created_from_version_id": str(v.created_from_version_id) if v.created_from_version_id else None,
            "theme_adjustments": (v.theme_snapshot or {}).get("contrast_adjustments"),
            "validated_current": bool(v.schema_sha256 and v.validated_schema_sha256 == v.schema_sha256
                                      and v.validated_bundle_sha256 == v.bundle_sha256)}


def report_json(r: Report, versions: list[ReportVersion]) -> dict:
    latest = versions[0] if versions else None
    from app.render.site import report_base_path
    from app.extraction.schema_builder import period_label
    return {"id": str(r.id), "company_name": r.company_name, "report_type": r.report_type,
            "fiscal_year": r.fiscal_year, "period": r.period, "period_label": period_label(r.period, r.fiscal_year),
            "currency": r.currency, "reporting_unit": r.reporting_unit, "status": report_status(r, latest),
            "live_version_id": str(r.live_version_id) if r.live_version_id else None,
            "latest_version": version_json(latest, live_id=r.live_version_id) if latest else None,
            "path": report_base_path({"fiscal_year": r.fiscal_year, "period": r.period, "report_type": r.report_type}),
            "theme_override": r.theme_override, "created_at": _iso(r.created_at),
            "updated_at": _iso(latest.created_at if latest else r.created_at)}


# ------------------------------------------------------------------ upload & list


@router.post("", status_code=201)
async def upload(file: UploadFile = File(...), company_name: str = Form(...), report_type: str = Form(...),
                 fiscal_year: str = Form(...), period: str = Form(...), currency: str = Form(...),
                 reporting_unit: str = Form(...), period_start: str | None = Form(None),
                 period_end: str | None = Form(None), ctx: Context = Depends(require("report.upload"))) -> dict:
    limit = get_settings().max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if not (file.filename or "").lower().endswith(".pdf") and not data.startswith(b"%PDF-"):
        raise HTTPException(400, "Only PDF files can be uploaded.")
    try:
        with db.session(ctx) as s:
            v = create_upload(s, ctx, data, file.filename or "report.pdf", {
                "company_name": company_name, "report_type": report_type, "fiscal_year": fiscal_year,
                "period": period, "currency": currency, "reporting_unit": reporting_unit,
                "period_start": period_start, "period_end": period_end})
            return {"report_id": str(v.report_id), "version": version_json(v)}
    except ReportError as e:
        raise _err(e) from e


class UploadUrlIn(BaseModel):
    filename: str
    size_bytes: int


@router.post("/upload-url")
def upload_url(body: UploadUrlIn, ctx: Context = Depends(require("report.upload"))) -> dict:
    """Where the browser should send the PDF: straight to storage (a Vercel Blob token, or a
    presigned URL for S3 / Cloudflare R2) so no function size cap applies; otherwise as a
    normal form upload to /inspect."""
    limit = get_settings().max_upload_mb * 1024 * 1024
    if body.size_bytes > limit:
        raise HTTPException(413, f"The file is larger than the {get_settings().max_upload_mb} MB limit.")
    key = f"incoming/{uuid.uuid4().hex}.pdf"
    dest = storage.direct_upload(ctx, key, limit)
    if dest is None:
        return {"mode": "form"}
    return {**dest, "key": key}


_INCOMING = re.compile(r"^incoming/[0-9a-f]{32}\.pdf$")


async def _pdf_from_request(file: UploadFile | None, incoming: str | None) -> tuple[bytes, str, str | None]:
    """The uploaded PDF: sent in this request, or already uploaded straight to storage."""
    limit = get_settings().max_upload_mb * 1024 * 1024
    if file is not None:
        return await file.read(limit + 1), file.filename or "report.pdf", None
    if not incoming or not _INCOMING.match(incoming):
        raise HTTPException(400, "Choose a PDF to upload.")
    return b"", "report.pdf", incoming


def _read_incoming(ctx: Context, key: str) -> bytes:
    try:
        data = storage.get(ctx, key)
    except storage.StorageError as e:
        raise HTTPException(400, "The upload didn't finish. Please choose the file again.") from e
    # A presigned PUT can't cap the size up front (R2 has no POST policies): check it here.
    limit = get_settings().max_upload_mb * 1024 * 1024
    if len(data) > limit:
        storage.delete(ctx, key)
        raise HTTPException(413, f"The file is larger than the {get_settings().max_upload_mb} MB limit.")
    return data


@router.post("/inspect")
async def inspect(file: UploadFile | None = File(None), incoming: str | None = Form(None),
                  filename: str | None = Form(None), ctx: Context = Depends(require("report.upload"))) -> dict:
    """Step 1 of upload: store the PDF and suggest its metadata for the user to confirm."""
    from app.extraction.detect import detect_metadata
    data, name, key = await _pdf_from_request(file, incoming)
    if key:
        data, name = _read_incoming(ctx, key), filename or name
    if not data.startswith(b"%PDF-"):
        raise HTTPException(400, "Only PDF files can be uploaded.")
    try:
        with db.session(ctx) as s:
            src = store_source(s, ctx, data, name)
            out = {"source_file_id": str(src.id), "filename": src.original_filename, "page_count": src.page_count,
                   "size_bytes": src.size_bytes}
    except ReportError as e:
        raise _err(e) from e
    finally:
        if key:                                 # kept under its content hash now (or rejected)
            try:
                storage.delete(ctx, key)
            except storage.StorageError:
                pass
    try:
        detected = detect_metadata(data)
    except Exception:  # noqa: BLE001 - suggestions are best-effort
        detected = {}
    return {**out, "detected": detected}


class CreateIn(BaseModel):
    source_file_id: uuid.UUID
    company_name: str
    report_type: str
    fiscal_year: int
    period: str
    currency: str
    reporting_unit: str


@router.post("/create", status_code=201)
def create_from_source(body: CreateIn, ctx: Context = Depends(require("report.upload"))) -> dict:
    """Step 2 of upload: file the stored PDF under the confirmed metadata and start processing."""
    from app.models import SourceFile
    try:
        with db.session(ctx) as s:
            src = s.get(SourceFile, body.source_file_id)
            if src is None:
                raise HTTPException(404, "Upload not found. Upload the PDF again.")
            v = create_version(s, ctx, src, body.model_dump(exclude={"source_file_id"}))
            return {"report_id": str(v.report_id), "version": version_json(v)}
    except ReportError as e:
        raise _err(e) from e


@router.get("")
def list_reports(ctx: Context = Depends(require("report.view"))) -> dict:
    with db.session(ctx) as s:
        reports = s.scalars(select(Report).where(Report.deleted_at.is_(None))
                            .order_by(Report.fiscal_year.desc(), Report.created_at.desc())).all()
        latest = latest_versions(s)
        return {"reports": [report_json(r, [latest[r.id]] if r.id in latest else []) for r in reports]}


@router.get("/{report_id}")
def get_report(report_id: uuid.UUID, ctx: Context = Depends(require("report.view"))) -> dict:
    with db.session(ctx) as s:
        r = _get_report(s, report_id)
        versions = versions_for(s, r.id)
        out = report_json(r, versions)
        out["versions"] = [version_json(v, live_id=r.live_version_id) for v in versions]
        return out


@router.delete("/{report_id}", status_code=204)
def remove_report(report_id: uuid.UUID, ctx: Context = Depends(require("report.delete"))) -> None:
    with db.session(ctx) as s:
        r = _get_report(s, report_id)
        try:
            delete_report(s, ctx, r)
        except ReportError as e:
            raise _err(e) from e


class ThemeOverrideIn(BaseModel):
    theme_override: dict | None = None


@router.put("/{report_id}/theme")
def set_report_theme(report_id: uuid.UUID, body: ThemeOverrideIn, ctx: Context = Depends(require("theme.manage"))) -> dict:
    from app.render import theme as theming
    with db.session(ctx) as s:
        r = _get_report(s, report_id)
        if body.theme_override:
            try:
                theming.validate(body.theme_override)
            except theming.ThemeError as e:
                raise HTTPException(400, str(e)) from e
        before = r.theme_override
        r.theme_override = body.theme_override or None
        audit.record(s, ctx, "theme.report_override_changed", target_type="report", target_id=r.id,
                     before={"theme_override": before}, after={"theme_override": r.theme_override})
        # Re-build the page in the new design: the latest draft is re-rendered and
        # re-validated; a live version stays untouched and gets a new draft instead.
        latest = s.scalars(select(ReportVersion).where(ReportVersion.report_id == r.id)
                           .order_by(ReportVersion.version_no.desc())).first()
        target = None
        if latest is not None and latest.schema_json:
            if latest.published_at is None and latest.status != "processing":
                enqueue_stage(s, ctx, latest, "render", force=True)
                target = latest
            elif latest.published_at is not None:
                target = new_draft_from(s, ctx, latest)
        return {"theme_override": r.theme_override, "version_id": str(target.id) if target else None}


# ------------------------------------------------------------------ version detail


_SCHEMA_BITS = text("""
    SELECT
      (schema_json #>> '{metadata,source_pdf,page_count}')::int AS page_count,
      (
        SELECT COALESCE(jsonb_agg(jsonb_build_object(
          'page', p->'page', 'width', p->'width', 'height', p->'height',
          'method', p->'method', 'method_reason', p->'method_reason'
        ) ORDER BY ord), '[]'::jsonb)
        FROM jsonb_array_elements(COALESCE(schema_json->'pages', '[]'::jsonb)) WITH ORDINALITY AS t(p, ord)
      ) AS pages,
      (
        SELECT COALESCE(jsonb_agg(jsonb_build_object(
          'id', sec->>'id', 'slug', sec->>'slug', 'type', sec->>'type',
          'heading_text', sec->>'heading_text',
          'page', COALESCE((sec->'source'->>'page')::int, (sec->'blocks'->0->'source'->>'page')::int)
        ) ORDER BY ord), '[]'::jsonb)
        FROM jsonb_array_elements(COALESCE(schema_json->'sections', '[]'::jsonb)) WITH ORDINALITY AS t(sec, ord)
      ) AS sections
    FROM report_versions
    WHERE id = :id
""")


def _as_list(value) -> list:
    if not value:
        return []
    if isinstance(value, str):
        return json.loads(value)
    return list(value)


def _schema_bits(s, version_id: uuid.UUID) -> tuple[int | None, list, list]:
    """Page and section summaries without loading figures or section bodies."""
    row = s.execute(_SCHEMA_BITS, {"id": version_id}).one()
    return row.page_count, _as_list(row.pages), _as_list(row.sections)


@router.get("/{report_id}/versions/{version_id}")
def get_version(report_id: uuid.UUID, version_id: uuid.UUID, ctx: Context = Depends(require("report.view"))) -> dict:
    with db.session(ctx) as s:
        r = _get_report(s, report_id)
        v = _get_version(s, report_id, version_id)
        jobs = s.scalars(select(Job).where(Job.version_id == v.id).order_by(Job.created_at)).all()
        page_count, pages, sections = _schema_bits(s, v.id)
        out = version_json(v, live_id=r.live_version_id)
        out["jobs"] = [job_json(j, include_internal=False) for j in jobs]
        out["open_blocking"] = open_blocking(s, v.id)
        out["page_count"] = page_count
        out["pages"] = pages
        out["sections"] = sections
    out["queue"] = _queue_for(jobs) if v.status == "processing" else None
    return out


def _queue_for(version_jobs: list[Job]) -> dict | None:
    """Whether this version's next step is waiting for a worker, and whether any worker is
    running at all, so the progress screen never says "Reading the PDF" while nothing is."""
    from app import jobs as jobq

    waiting = next((j for j in reversed(version_jobs) if j.status == "queued"), None)
    if waiting is None:
        return None
    try:
        health = jobq.queue_health(waiting_since=waiting.run_after)
    except Exception:  # noqa: BLE001 - progress info is best-effort
        return None
    return {"waiting": True, "waiting_seconds": health["waiting_seconds"],
            "workers_alive": health["workers_alive"], "stalled": health["stalled"]}


@router.get("/{report_id}/versions/{version_id}/schema")
def get_schema(report_id: uuid.UUID, version_id: uuid.UUID, download: bool = False,
               ctx: Context = Depends(require("report.view"))) -> Response:
    with db.session(ctx) as s:
        v = _get_version(s, report_id, version_id)
        if not v.schema_json:
            raise HTTPException(409, "The schema isn't ready yet.")
        body = json.dumps(v.schema_json, ensure_ascii=False, indent=2 if download else None)
        headers = {"Content-Disposition": f'attachment; filename="report-v{v.version_no}.schema.json"'} if download else {}
        return Response(body, media_type="application/json", headers=headers)


@router.get("/{report_id}/versions/{version_id}/source.pdf")
def get_source(report_id: uuid.UUID, version_id: uuid.UUID, ctx: Context = Depends(require("report.view"))) -> Response:
    with db.session(ctx) as s:
        v = _get_version(s, report_id, version_id)
        sha = v.source_sha256
    return Response(storage.get(ctx, source_key(sha)), media_type="application/pdf",
                    headers={"Content-Disposition": "inline", "Cache-Control": "private, max-age=300"})


_PNG_CACHE: OrderedDict[tuple[str, int, float], bytes] = OrderedDict()
_PNG_CACHE_MAX = 96


def _cached_png(ctx, sha: str, page: int, zoom: float) -> bytes:
    key = (sha, page, round(float(zoom), 2))
    hit = _PNG_CACHE.get(key)
    if hit is not None:
        _PNG_CACHE.move_to_end(key)
        return hit
    png = render_page_png(storage.get(ctx, source_key(sha)), page, key[2])
    _PNG_CACHE[key] = png
    while len(_PNG_CACHE) > _PNG_CACHE_MAX:
        _PNG_CACHE.popitem(last=False)
    return png


@router.get("/{report_id}/versions/{version_id}/thumb.webp")
def thumb(report_id: uuid.UUID, version_id: uuid.UUID, ctx: Context = Depends(require("report.view"))) -> Response:
    """The report's small cover for lists. Never renders from the PDF here (that meant
    downloading the whole file per card): stored thumbnail, else the stored first page,
    else 404 and the list shows a placeholder."""
    from app.reports import pdf_page_key, thumb_key
    with db.session(ctx) as s:
        sha = s.scalar(select(ReportVersion.source_sha256).where(ReportVersion.id == version_id,
                                                                 ReportVersion.report_id == report_id))
    if not sha:
        raise HTTPException(404, "Not found.")
    for key in (thumb_key(sha), pdf_page_key(sha, 1)):
        try:
            data = storage.get(ctx, key)
        except Exception:  # noqa: BLE001 - not stored (or storage unavailable)
            continue
        # Keyed by the PDF's hash: the image for a version never changes.
        return Response(data, media_type="image/webp", headers={"Cache-Control": "private, max-age=31536000, immutable"})
    raise HTTPException(404, "No thumbnail yet.")


@router.get("/{report_id}/versions/{version_id}/pages/{page}.png")
def page_image(report_id: uuid.UUID, version_id: uuid.UUID, page: int, zoom: float = Query(1.5, ge=0.5, le=3),
               ctx: Context = Depends(require("report.view"))) -> Response:
    with db.session(ctx) as s:
        row = s.execute(
            select(ReportVersion.source_sha256, SourceFile.page_count)
            .join(SourceFile, SourceFile.id == ReportVersion.source_file_id)
            .where(ReportVersion.id == version_id, ReportVersion.report_id == report_id)
        ).one_or_none()
    if row is None:
        raise HTTPException(404, "Version not found.")
    sha, count = row
    if not 1 <= page <= count:
        raise HTTPException(404, "No such page.")
    # Normally the page was drawn once when the web page was built: serve that (fast, and
    # light on serverless hosts). Older versions fall back to drawing it now.
    from app.reports import pdf_page_key
    try:
        data = storage.get(ctx, pdf_page_key(sha, page))
        return Response(data, media_type="image/webp", headers={"Cache-Control": "private, max-age=86400"})
    except Exception:  # noqa: BLE001 - not stored yet
        pass
    return Response(_cached_png(ctx, sha, page, zoom), media_type="image/png",
                    headers={"Cache-Control": "private, max-age=86400"})


# ------------------------------------------------------------------ AI double-check


def _ai_state(s, v: ReportVersion) -> tuple[list[str], dict | None]:
    from app import ai_check
    run = latest_run_no(s, v.id)
    issues = s.scalars(select(ValidationIssue).where(ValidationIssue.version_id == v.id,
                                                      ValidationIssue.run_no == run)).all()
    fids = ai_check.eligible(issues, v.schema_json or {"figures": {}})
    last = s.scalars(select(Job).where(Job.version_id == v.id, Job.kind == "pipeline.ai_check")
                     .order_by(Job.created_at.desc())).first()
    last_json = {"status": last.status, "result": last.result, "error": last.error_plain,
                 "at": _iso(last.finished_at or last.created_at)} if last else None
    return fids, last_json


@router.get("/{report_id}/versions/{version_id}/ai-check")
def ai_check_estimate(report_id: uuid.UUID, version_id: uuid.UUID, ctx: Context = Depends(require("report.view"))) -> dict:
    """What an AI double-check would cost for this report, before anything runs."""
    from app import ai_check
    with db.session(ctx) as s:
        v = _get_version(s, report_id, version_id)
        fids, last = _ai_state(s, v)
    from app import ai_usage
    return {"available": bool(get_settings().gemini_api_key), "estimate": ai_check.estimate(len(fids)), "last": last,
            "allowance": ai_usage.allowance(ctx)}


class AiCheckIn(BaseModel):
    credits_shown: int = Field(ge=0)


@router.post("/{report_id}/versions/{version_id}/ai-check", status_code=202)
def ai_check_start(report_id: uuid.UUID, version_id: uuid.UUID, body: AiCheckIn,
                   ctx: Context = Depends(require("report.edit_figure"))) -> dict:
    """Start the AI double-check the user just saw the estimate for. Refused if the
    estimate has changed since (so nobody is charged for something they didn't see)."""
    from app import ai_check, ai_usage, jobs
    if not get_settings().gemini_api_key:
        raise HTTPException(400, "AI double-check isn't set up on this platform.")
    with db.session(ctx) as s:
        v = _get_version(s, report_id, version_id)
        if v.published_at is not None or v.status == "processing":
            raise HTTPException(409, "Wait until the checks have finished, then try again.")
        fids, _ = _ai_state(s, v)
        est = ai_check.estimate(len(fids))
        if not fids:
            raise HTTPException(409, "There are no flagged figures for the AI to check.")
        if est["credits"] != body.credits_shown:
            raise HTTPException(409, f"The estimate has changed to {est['credits']} credits. Review it and confirm again.")
        try:
            ai_usage.check(ctx, est["credits"])
        except ai_usage.LimitReached as e:
            raise HTTPException(409, e.message) from e
        # One attempt only: a retry would pay for every request again.
        job = jobs.enqueue(s, ctx, "pipeline.ai_check", {"version_id": str(v.id)},
                           idempotency_key=f"ai_check:{v.id}:{v.schema_sha256}", max_attempts=1)
        job.version_id = v.id     # the report itself stays as it is; the AI run has its own status
        audit.record(s, ctx, "report.ai_check_requested", target_type="report_version", target_id=v.id, after=est)
        return {"estimate": est, "job_id": str(job.id)}


@router.post("/{report_id}/versions/{version_id}/rerun")
def rerun(report_id: uuid.UUID, version_id: uuid.UUID, ctx: Context = Depends(require("report.upload"))) -> dict:
    with db.session(ctx) as s:
        v = _get_version(s, report_id, version_id)
        if v.published_at is not None:
            raise HTTPException(409, "Published versions can't be re-processed. Create a new draft.")
        # Full re-run from the PDF. Human reviews on the old schema don't carry over, so
        # only offer it before anyone has reviewed figures on this version.
        from app.models import FigureReview
        if s.scalar(select(func.count()).select_from(FigureReview).where(FigureReview.version_id == v.id)):
            enqueue_stage(s, ctx, v, "render", force=True)
        else:
            enqueue_stage(s, ctx, v, "extract", force=True)
        audit.record(s, ctx, "pipeline.rerun_requested", target_type="report_version", target_id=v.id)
        return version_json(v)


# ------------------------------------------------------------------ validation issues


def issue_json(i: ValidationIssue, figures: dict) -> dict:
    f = figures.get(i.fid) if i.fid else None
    return {"id": str(i.id), "check": i.check_name, "severity": i.severity, "fid": i.fid, "page": i.page,
            "section_id": i.section_id, "bbox": i.bbox, "message": i.message, "expected": i.expected,
            "actual": i.actual, "status": i.status, "resolution": i.resolution,
            "resolved_at": _iso(i.resolved_at),
            "figure": {k: f.get(k) for k in ("raw", "value", "kind", "row_label", "col_label", "unit", "method",
                                              "confidence", "status", "edited", "review")} if f else None}


@router.get("/{report_id}/versions/{version_id}/issues")
def list_issues(report_id: uuid.UUID, version_id: uuid.UUID, page: int | None = None, section_id: str | None = None,
                severity: str | None = None, status: str | None = None, check: str | None = None,
                ctx: Context = Depends(require("report.view"))) -> dict:
    with db.session(ctx) as s:
        v = _get_version(s, report_id, version_id)
        run = latest_run_no(s, v.id)
        q = select(ValidationIssue).where(ValidationIssue.version_id == v.id, ValidationIssue.run_no == run)
        for col, val in ((ValidationIssue.page, page), (ValidationIssue.section_id, section_id),
                         (ValidationIssue.severity, severity), (ValidationIssue.status, status),
                         (ValidationIssue.check_name, check)):
            if val is not None:
                q = q.where(col == val)
        issues = s.scalars(q.order_by(ValidationIssue.severity, ValidationIssue.page, ValidationIssue.check_name)).all()
        figures = (v.schema_json or {}).get("figures", {})
        return {"run": run, "summary": v.validation_summary, "validated_current": version_json(v)["validated_current"],
                "status": v.status, "issues": [issue_json(i, figures) for i in issues]}


class ResolveIn(BaseModel):
    action: str = Field(pattern="^(confirm|edit|not_a_figure)$")
    value: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=2000)


@router.post("/{report_id}/versions/{version_id}/issues/{issue_id}/resolve")
def resolve_issue(report_id: uuid.UUID, version_id: uuid.UUID, issue_id: uuid.UUID, body: ResolveIn,
                  ctx: Context = Depends(require("report.edit_figure"))) -> dict:
    with db.session(ctx) as s:
        v = _get_version(s, report_id, version_id)
        i = s.get(ValidationIssue, issue_id)
        if i is None or i.version_id != v.id:
            raise HTTPException(404, "Issue not found.")
        if i.status != "open":
            raise HTTPException(409, "This issue is already resolved.")
        if i.check_name in ("rendered_page", "schema"):
            raise HTTPException(409, "This is a platform problem, not a figure to review. Our team has been alerted; "
                                     "re-run processing or contact support.")
        try:
            if i.fid:
                apply_figure_action(s, ctx, v, i.fid, body.action, body.value, body.note)
            elif body.action == "confirm":
                acknowledge_issue(s, ctx, v, i, body.note)
            else:
                raise HTTPException(400, "Only 'Confirm correct' applies to this issue.")
        except ReportError as e:
            raise _err(e) from e
        i.status, i.resolution, i.resolved_by = "resolved", body.action, ctx.user_id
        i.resolved_at = func.now()
        if i.check_name == "reviewer_flag":
            # flags are carried across runs; mark every copy resolved
            for other in s.scalars(select(ValidationIssue).where(ValidationIssue.version_id == v.id,
                                                                  ValidationIssue.check_name == "reviewer_flag",
                                                                  ValidationIssue.fid == i.fid,
                                                                  ValidationIssue.status == "open")):
                other.status, other.resolution, other.resolved_by = "resolved", body.action, ctx.user_id
        return {"status": "resolved", "revalidating": True, "version": version_json(v)}


class BulkResolveIn(BaseModel):
    issue_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    action: str = Field(pattern="^(confirm|not_a_figure)$")
    note: str | None = Field(default=None, max_length=2000)


# Never decided in bulk: platform faults can't be waived at all, and a colleague's flag
# deserves its own answer.
NOT_BULK = ("rendered_page", "schema", "reviewer_flag")


@router.post("/{report_id}/versions/{version_id}/issues/resolve-bulk")
def resolve_issues_bulk(report_id: uuid.UUID, version_id: uuid.UUID, body: BulkResolveIn,
                        ctx: Context = Depends(require("report.edit_figure"))) -> dict:
    """Confirm (or mark as not a figure) several reviewed items in one go — e.g. every
    flagged value on a page after checking them side by side. One rebuild and re-check."""
    with db.session(ctx) as s:
        v = _get_version(s, report_id, version_id)
        wanted = set(body.issue_ids)
        issues = [i for i in s.scalars(select(ValidationIssue).where(ValidationIssue.version_id == v.id,
                                                                      ValidationIssue.id.in_(wanted)))]
        if len(issues) != len(wanted):
            raise HTTPException(404, "Some of these issues aren't part of this version.")
        if any(i.status != "open" for i in issues):
            raise HTTPException(409, "Some of these issues are already resolved. Refresh and try again.")
        if any(i.check_name in NOT_BULK for i in issues):
            raise HTTPException(409, "Page-build problems and colleagues' flags can't be resolved in bulk.")
        try:
            n = apply_bulk_review(s, ctx, v, issues, body.action, body.note)
        except ReportError as e:
            raise _err(e) from e
        for i in issues:
            i.status, i.resolution, i.resolved_by = "resolved", body.action, ctx.user_id
            i.resolved_at = func.now()
        return {"resolved": n, "revalidating": True, "version": version_json(v)}


class FigureActionIn(BaseModel):
    action: str = Field(pattern="^(confirm|edit|not_a_figure)$")
    value: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=2000)


@router.post("/{report_id}/versions/{version_id}/figures/{fid}")
def figure_action(report_id: uuid.UUID, version_id: uuid.UUID, fid: str, body: FigureActionIn,
                  ctx: Context = Depends(require("report.edit_figure"))) -> dict:
    with db.session(ctx) as s:
        v = _get_version(s, report_id, version_id)
        try:
            f = apply_figure_action(s, ctx, v, fid, body.action, body.value, body.note)
        except ReportError as e:
            raise _err(e) from e
        return {"figure": f, "version": version_json(v)}


class FlagIn(BaseModel):
    fid: str | None = None
    section_id: str | None = None
    note: str = Field(min_length=3, max_length=2000)


@router.post("/{report_id}/versions/{version_id}/flags", status_code=201)
def flag(report_id: uuid.UUID, version_id: uuid.UUID, body: FlagIn,
         ctx: Context = Depends(require("report.flag_issue"))) -> dict:
    with db.session(ctx) as s:
        v = _get_version(s, report_id, version_id)
        try:
            i = flag_figure(s, ctx, v, body.fid, body.section_id, body.note)
        except ReportError as e:
            raise _err(e) from e
        return issue_json(i, (v.schema_json or {}).get("figures", {}))


@router.get("/{report_id}/versions/{version_id}/reviews")
def reviews(report_id: uuid.UUID, version_id: uuid.UUID, ctx: Context = Depends(require("report.view"))) -> dict:
    with db.session(ctx) as s:
        v = _get_version(s, report_id, version_id)
        rows = s.execute(select(FigureReview, User.email).join(User, User.id == FigureReview.user_id)
                         .where(FigureReview.version_id == v.id).order_by(FigureReview.at.desc())).all()
        return {"reviews": [{"id": str(r.id), "fid": r.fid, "action": r.action, "old": r.old_value, "new": r.new_value,
                             "note": r.note, "by": email, "at": _iso(r.at)} for r, email in rows]}


# ------------------------------------------------------------------ comments


class CommentIn(BaseModel):
    section_id: str = Field(min_length=1, max_length=20)
    body: str = Field(min_length=1, max_length=5000)


@router.get("/{report_id}/versions/{version_id}/comments")
def list_comments(report_id: uuid.UUID, version_id: uuid.UUID, ctx: Context = Depends(require("report.view"))) -> dict:
    with db.session(ctx) as s:
        v = _get_version(s, report_id, version_id)
        rows = s.execute(select(Comment, User.email).join(User, User.id == Comment.user_id)
                         .where(Comment.version_id == v.id).order_by(Comment.created_at)).all()
        return {"comments": [{"id": str(c.id), "section_id": c.section_id, "body": c.body, "by": email,
                              "at": _iso(c.created_at), "resolved": c.resolved_at is not None} for c, email in rows]}


@router.post("/{report_id}/versions/{version_id}/comments", status_code=201)
def add_comment(report_id: uuid.UUID, version_id: uuid.UUID, body: CommentIn,
                ctx: Context = Depends(require("report.comment"))) -> dict:
    with db.session(ctx) as s:
        v = _get_version(s, report_id, version_id)
        if not any(sec["id"] == body.section_id for sec in (v.schema_json or {}).get("sections", [])):
            raise HTTPException(400, "Unknown section.")
        c = Comment(tenant_id=ctx.tenant_id, version_id=v.id, section_id=body.section_id, user_id=ctx.user_id,
                    body=body.body.strip())
        s.add(c)
        s.flush()
        audit.record(s, ctx, "comment.added", target_type="comment", target_id=c.id,
                     after={"section_id": body.section_id, "version": v.version_no})
        return {"id": str(c.id)}


@router.post("/{report_id}/versions/{version_id}/comments/{comment_id}/resolve")
def resolve_comment(report_id: uuid.UUID, version_id: uuid.UUID, comment_id: uuid.UUID,
                    ctx: Context = Depends(require("report.comment"))) -> dict:
    with db.session(ctx) as s:
        _get_version(s, report_id, version_id)
        c = s.get(Comment, comment_id)
        if c is None or c.version_id != version_id:
            raise HTTPException(404, "Comment not found.")
        c.resolved_at, c.resolved_by = func.now(), ctx.user_id
        return {"status": "resolved"}


# ------------------------------------------------------------------ preview


PREVIEW_SYNC = """<script>(function(){var s=[].slice.call(document.querySelectorAll('[data-section]'));
var P=[].slice.call(document.querySelectorAll('.leaf[id^="p"]'));
function num(e){return +e.id.slice(1)}
function cur(L){var b=null;L.forEach(function(e){if(e.getBoundingClientRect().top<120)b=e});return b||L[0]}
var last=null;function tick(){var L=s.length?s:P,e=cur(L);if(!e||e===last)return;last=e;
parent.postMessage(s.length?{ppdf:'section',id:e.getAttribute('data-section')}:{ppdf:'page',page:num(e)},'*')}
addEventListener('scroll',tick,{passive:true});
addEventListener('load',function(){if(P.length&&!s.length){var menu=[].slice.call(document.querySelectorAll('.edition-rail a')).map(function(a){return{label:a.textContent.trim(),page:+a.getAttribute('href').slice(2)}});
parent.postMessage({ppdf:'edition',menu:menu,parts:P.map(num)},'*')}tick()});
addEventListener('message',function(m){var d=m.data||{};if(d.ppdf==='goto'){var e=document.querySelector('[data-section="'+d.id+'"]');if(e)e.scrollIntoView()}
if(d.ppdf==='gotoPage'){var t=P.filter(function(e){return num(e)>=d.page})[0]||P[P.length-1];if(t)t.scrollIntoView()}});
document.addEventListener('click',function(ev){var a=ev.target.closest('[data-fig]');if(a)parent.postMessage({ppdf:'figure',id:a.getAttribute('data-fig')},'*')});
})();</script>"""


@router.get("/{report_id}/versions/{version_id}/preview")
def preview_root(report_id: uuid.UUID, version_id: uuid.UUID, ctx: Context = Depends(require("report.view"))) -> Response:
    """The preview's landing page without a trailing slash: some hosts' /api forwarding
    drops paths that end in "/", so the dashboard links here."""
    return preview(report_id, version_id, "", ctx)


@router.get("/{report_id}/versions/{version_id}/preview/{path:path}")
def preview(report_id: uuid.UUID, version_id: uuid.UUID, path: str, ctx: Context = Depends(require("report.view"))) -> Response:
    """Serves the stored bundle exactly as it will publish; only the origin is set to
    this preview URL and a small scroll-sync script is added (dashboard only)."""
    with db.session(ctx) as s:
        v = _get_version(s, report_id, version_id)
        if not v.bundle_key:
            raise HTTPException(409, "The page hasn't been generated yet.")
        bundle, sha = v.bundle_key, v.source_sha256
        files = json.loads(storage.get(ctx, bundle + "_files.json"))
        base = min((f for f in files if f.endswith("/index.html")), key=len, default=files[0])   # the landing page
    root = f"/api/tenants/{ctx.tenant_id}/reports/{report_id}/versions/{version_id}/preview"
    rel = path.strip("/")
    if rel.endswith("source.pdf"):
        return Response(storage.get(ctx, source_key(sha)), media_type="application/pdf")
    candidates = [rel, rel + "/index.html"] if rel else [base]
    if rel.startswith(("latest", "archive")) or rel == "":
        candidates = [base]
    name = next((c for c in candidates if c in files), None)
    if name is None:
        return Response("<!doctype html><title>Not in this report</title><p>That page isn't part of this report "
                        "preview.</p>", status_code=404, media_type="text/html")
    data = storage.get(ctx, bundle + name).replace(ORIGIN_PLACEHOLDER.encode(), root.encode())
    if name.endswith(".html"):
        html = data.decode()
        html = html.replace("<head>", '<head><meta name="robots" content="noindex">', 1)
        html = html.replace("</body>", PREVIEW_SYNC + "</body>", 1)
        return Response(html, media_type="text/html", headers={"X-Robots-Tag": "noindex",
                                                                "Content-Security-Policy": "frame-ancestors 'self'",
                                                                "X-Frame-Options": "SAMEORIGIN"})
    from app.render.site import content_type
    return Response(data, media_type=content_type(name))


# ------------------------------------------------------------------ publish / rollback / drafts


class PublishIn(BaseModel):
    confirm_reviewed: bool = False


@router.post("/{report_id}/versions/{version_id}/publish")
def publish_version(report_id: uuid.UUID, version_id: uuid.UUID, body: PublishIn,
                    ctx: Context = Depends(require("report.publish"))) -> dict:
    with db.session(ctx) as s:
        r = _get_report(s, report_id)
        v = _get_version(s, report_id, version_id)
        try:
            publish(s, ctx, v, confirm_reviewed=body.confirm_reviewed)
        except ReportError as e:
            raise _err(e) from e
        return version_json(v, live_id=r.live_version_id)


class RollbackIn(BaseModel):
    version_id: uuid.UUID


@router.post("/{report_id}/rollback")
def rollback_report(report_id: uuid.UUID, body: RollbackIn, ctx: Context = Depends(require("report.publish"))) -> dict:
    with db.session(ctx) as s:
        r = _get_report(s, report_id)
        v = _get_version(s, report_id, body.version_id)
        try:
            rollback(s, ctx, r, v)
        except ReportError as e:
            raise _err(e) from e
        return {"live_version_id": str(r.live_version_id)}


@router.post("/{report_id}/versions/{version_id}/draft", status_code=201)
def new_draft(report_id: uuid.UUID, version_id: uuid.UUID, ctx: Context = Depends(require("report.upload"))) -> dict:
    with db.session(ctx) as s:
        v = _get_version(s, report_id, version_id)
        try:
            nv = new_draft_from(s, ctx, v)
        except ReportError as e:
            raise _err(e) from e
        return version_json(nv)
