"""Pipeline stage handlers.

    extract  ->  render  ->  validate
    (text layer + OCR, schema)   (static bundle)   (all checks incl. rendered page)

Each stage reads its inputs from the version row + storage, writes its outputs, and
enqueues the next stage with an idempotency key derived from its inputs. Re-running
a stage with the same inputs produces the same outputs.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import insert, select, update

from app import audit, db, jobs, storage
from app.config import get_settings
from app.extraction import classify
from app.extraction.pdf import PdfError
from app.extraction.schema_builder import extract, schema_sha256
from app.jobs import UserFacingError, handler
from app.models import Report, ReportVersion, ValidationIssue
from app.render import site
from app.reports import (
    bundle_prefix,
    effective_disclaimer,
    effective_theme,
    enqueue_stage,
    extraction_key,
    latest_run_no,
    logo_src,
    settings_for,
    source_key,
)
from app.tenancy import Context
from app import ai_check, validation


@handler("system.ping")
def ping(ctx: Context, payload: dict[str, Any]) -> dict[str, Any]:
    return {"tenant_id": str(ctx.tenant_id), "echo": payload.get("echo")}


def _version(s, payload) -> ReportVersion:
    v = s.get(ReportVersion, uuid.UUID(payload["version_id"]))
    if v is None:
        raise UserFacingError("This report version no longer exists.")
    return v


def _set_stage(ctx: Context, vid: uuid.UUID, stage: str) -> None:
    # Progress ticks every couple of seconds. Update only the stage columns so a
    # long report doesn't reload its whole extraction over the database link.
    with db.session(ctx) as s:
        s.execute(update(ReportVersion).where(ReportVersion.id == vid, ReportVersion.published_at.is_(None))
                  .values(status="processing", stage=stage))


def _fail(ctx: Context, vid: uuid.UUID, message: str) -> None:
    with db.session(ctx) as s:
        s.execute(update(ReportVersion).where(ReportVersion.id == vid, ReportVersion.published_at.is_(None))
                  .values(status="failed", error_plain=message))


def _llm_for(ctx: Context, version_id: uuid.UUID | None = None) -> classify.LlmCall | None:
    """Only when the tenant opted in AND a key is configured (PLAN D4), and the workspace
    hasn't used up its monthly AI credits. Every call is recorded in the AI usage ledger."""
    from app import ai_usage

    cfg = get_settings()
    if not cfg.anthropic_api_key:
        return None
    with db.session(ctx) as s:
        if not settings_for(s, ctx).llm_assist_enabled:
            return None
    if not ai_usage.can_run_automatic(ctx):
        return None

    def call(prompt: str) -> str:
        import httpx

        r = httpx.post("https://api.anthropic.com/v1/messages", timeout=60, headers={
            "x-api-key": cfg.anthropic_api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": cfg.llm_model, "max_tokens": 1024, "system":
                  "Classify each report section. Reply with JSON only: {\"<id>\": \"<type>\"} using allowed_types. "
                  "Never output numbers or dates.", "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        body = r.json()
        usage = body.get("usage") or {}
        ai_usage.record(ctx, feature="section_labels", provider="anthropic", model=body.get("model") or cfg.llm_model,
                        input_tokens=int(usage.get("input_tokens") or 0),
                        output_tokens=int(usage.get("output_tokens") or 0), version_id=version_id)
        return "".join(b.get("text", "") for b in body.get("content", []))
    return call


# ------------------------------------------------------------------ extract


@handler("pipeline.extract")
def stage_extract(ctx: Context, payload: dict[str, Any]) -> dict[str, Any]:
    with db.session(ctx) as s:
        v = _version(s, payload)
        report = s.get(Report, v.report_id)
        assert report is not None
        vid, sha, version_no = v.id, v.source_sha256, v.version_no
        meta = {"company_name": report.company_name, "report_type": report.report_type,
                "fiscal_year": report.fiscal_year, "period": report.period, "currency": report.currency,
                "reporting_unit": report.reporting_unit, "version": version_no}
        from app.models import SourceFile
        src = s.get(SourceFile, v.source_file_id)
        meta["filename"] = src.original_filename if src else None
    _set_stage(ctx, vid, "extract")
    pdf = storage.get(ctx, source_key(sha))
    try:
        last = {"t": 0.0}

        def on_page(done: int, total: int) -> None:
            import time
            if done == total or time.monotonic() - last["t"] > 1.5:
                last["t"] = time.monotonic()
                _set_stage(ctx, vid, f"extract: reading page {done} of {total}")

        schema, artifact = extract(pdf, meta, llm=_llm_for(ctx, vid), max_pages=get_settings().max_pages, progress=on_page)
    except PdfError as e:
        _fail(ctx, vid, str(e))
        raise UserFacingError(str(e)) from e
    storage.put(ctx, extraction_key(vid), json.dumps(artifact).encode(), "application/json")
    methods = sorted(set(schema["metadata"]["extraction"]["methods"].values()))
    with db.session(ctx) as s:
        v = s.get(ReportVersion, vid)
        assert v is not None
        v.extraction_key = extraction_key(vid)
        v.schema_json = schema
        v.schema_sha256 = schema_sha256(schema)
        audit.record(s, ctx, "pipeline.extracted", target_type="report_version", target_id=vid,
                     after={"figures": len(schema["figures"]), "sections": len(schema["sections"]),
                            "methods": methods, "schema_sha256": v.schema_sha256})
        enqueue_stage(s, ctx, v, "render")
    return {"figures": len(schema["figures"]), "sections": len(schema["sections"]), "methods": methods}


# ------------------------------------------------------------------ render


@handler("pipeline.render")
def stage_render(ctx: Context, payload: dict[str, Any]) -> dict[str, Any]:
    with db.session(ctx) as s:
        v = _version(s, payload)
        if v.published_at is not None:
            return {"skipped": "published"}
        report = s.get(Report, v.report_id)
        st = settings_for(s, ctx)
        theme, adjustments = effective_theme(st, report)
        disclaimer = effective_disclaimer(st)
        schema, vid, v_sha = v.schema_json, v.id, v.source_sha256
    _set_stage(ctx, vid, "render")
    pdf = storage.get(ctx, source_key(v_sha))
    last = {"t": 0.0}

    def on_page(done: int, total: int) -> None:
        import time
        if done == total or time.monotonic() - last["t"] > 1.5:
            last["t"] = time.monotonic()
            _set_stage(ctx, vid, f"render: building page {done} of {total}")

    from app.reports import pdf_page_key

    files = site.render_report(schema, theme=theme, disclaimer=disclaimer, logo_src=logo_src(theme), pdf_bytes=pdf,
                               progress=on_page)
    prefix = bundle_prefix(vid)
    # A re-render (a confirmed figure, a new theme) changes few files: upload only those.
    # _hashes.json records what is stored; anything missing from it is uploaded.
    hashes = {rel: hashlib.sha256(data).hexdigest() for rel, data in files.items()}
    try:
        stored = json.loads(storage.get(ctx, prefix + "_hashes.json"))
    except Exception:  # noqa: BLE001 - first render of this version
        stored = {}
    todo = [(prefix + rel, data, site.content_type(rel)) for rel, data in files.items() if stored.get(rel) != hashes[rel]]
    done = {"n": 0, "t": 0.0}

    def saved(_key: str) -> None:
        import time
        done["n"] += 1
        if done["n"] == len(todo) or time.monotonic() - done["t"] > 1.5:
            done["t"] = time.monotonic()
            _set_stage(ctx, vid, f"render: saving file {done['n']} of {len(todo)}")
    storage.put_many(ctx, todo, on_done=saved)
    # The file list goes last: its presence means the whole bundle is stored.
    storage.put(ctx, prefix + "_hashes.json", json.dumps(hashes).encode(), "application/json")
    storage.put(ctx, prefix + "_files.json", json.dumps(sorted(files)).encode(), "application/json")
    bsha = site.bundle_sha256(files)
    with db.session(ctx) as s:
        v = s.get(ReportVersion, vid)
        assert v is not None
        v.bundle_key, v.bundle_sha256 = prefix, bsha
        v.theme_snapshot = {"theme": theme, "contrast_adjustments": adjustments, "disclaimer": disclaimer}
        if _review_only_change(s, ctx, v, bsha):
            return {"files": len(files), "bundle_sha256": bsha, "checks": "reused"}
        enqueue_stage(s, ctx, v, "validate")
        # The review screen's as-printed page images: off the critical path (it renders a
        # page on demand until they're stored).
        jobs.enqueue(s, ctx, "pipeline.page_images", {"version_id": str(vid)},
                     idempotency_key=f"page_images:{v_sha}", max_attempts=2)
    return {"files": len(files), "bundle_sha256": bsha}


@handler("pipeline.page_images")
def stage_page_images(ctx: Context, payload: dict[str, Any]) -> dict[str, Any]:
    """Each PDF page as printed, for side-by-side review. Keyed by the PDF's hash, so a
    re-run or a new version of the same PDF reuses them."""
    from app.render.pages import BG_SCALE, webp
    from app.reports import pdf_page_key
    import pymupdf
    from PIL import Image

    with db.session(ctx) as s:
        v = _version(s, payload)
        sha = v.source_sha256
    pdf = storage.get(ctx, source_key(sha))
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    count = doc.page_count
    up = storage.Uploader(ctx)
    try:
        for n in range(1, count + 1):
            pix = doc[n - 1].get_pixmap(matrix=pymupdf.Matrix(BG_SCALE, BG_SCALE), alpha=False)
            up.put(pdf_page_key(sha, n), webp(Image.frombytes("RGB", (pix.width, pix.height), pix.samples)), "image/webp")
    finally:
        doc.close()
        up.finish()
    return {"pages": count}


def _review_only_change(s, ctx: Context, v: ReportVersion, bsha: str) -> bool:
    """After a person confirms figures, the checks would find exactly what they found last
    time when neither what they read (the schema minus review decisions) nor the rendered
    page changed. Then: apply the decisions to the last run's findings and finish — no
    minutes-long re-run of every check on a 400-page report."""
    prev = v.validation_summary or {}
    if not (prev.get("checked_content_sha") and prev.get("bundle_sha") == bsha and v.schema_json
            and prev["checked_content_sha"] == validation.checked_content_sha(v.schema_json)):
        return False
    run = latest_run_no(s, v.id)
    rows = s.scalars(select(ValidationIssue).where(ValidationIssue.version_id == v.id,
                                                    ValidationIssue.run_no == run)).all()
    found = [validation.Issue(r.check_name, r.severity, r.message, r.fid, r.page, r.section_id, r.bbox,
                              r.expected, r.actual, r.status, r.resolution) for r in rows]
    # Exactly what a full run applies after its checks: people's decisions, then the AI's.
    validation.apply_reviews(found, v.schema_json)
    # (stored findings may already carry the AI's note from an earlier pass: never add it twice)
    ai_check.apply_confirmations([i for i in found if "AI reading" not in i.message], v.schema_json)
    for r, i in zip(rows, found):
        if r.status == "open" and i.status != "open":
            r.status, r.resolution = i.status, i.resolution
        if (r.severity, r.message) != (i.severity, i.message):
            r.severity, r.message = i.severity, i.message
    summary = {**validation.summarize(v.schema_json, found), "agents": prev.get("agents"),
               "consensus": prev.get("consensus"), "checked_content_sha": prev["checked_content_sha"], "bundle_sha": bsha}
    v.validation_summary = summary
    v.validated_schema_sha256, v.validated_bundle_sha256 = v.schema_sha256, bsha
    v.status = "validation_issues" if summary["blocking"] else "needs_review"
    v.stage = "done"
    audit.record(s, ctx, "pipeline.validated", target_type="report_version", target_id=v.id,
                 after={"run": run, "reused_checks": True,
                        **{k: summary[k] for k in ("figures_checked", "passed", "warnings", "blocking")}})
    return True


# ------------------------------------------------------------------ validate


@handler("pipeline.validate")
def stage_validate(ctx: Context, payload: dict[str, Any]) -> dict[str, Any]:
    with db.session(ctx) as s:
        v = _version(s, payload)
        if v.published_at is not None:
            return {"skipped": "published"}
        vid, schema, sha, bundle_key, bsha, sch_sha = v.id, v.schema_json, v.source_sha256, v.bundle_key, v.bundle_sha256, v.schema_sha256
        ext_key = v.extraction_key
    _set_stage(ctx, vid, "validate")
    first = storage.get_many(ctx, [source_key(sha), ext_key, bundle_key + "_files.json"])
    pdf, artifact = first[source_key(sha)], json.loads(first[ext_key])
    names = json.loads(first[bundle_key + "_files.json"])
    got = storage.get_many(ctx, [bundle_key + n for n in names])
    files = {n: got[bundle_key + n] for n in names}
    issues, summary = validation.run_all(
        schema, pdf, artifact, files, threshold=get_settings().ocr_confidence_threshold,
        progress=lambda name, n: _set_stage(ctx, vid, f"validate: {name} ({n} of {len(validation.AGENTS) - 1})"))
    validation.apply_reviews(issues, schema)
    ai_check.apply_confirmations(issues, schema)
    summary = {**validation.summarize(schema, issues), "agents": summary["agents"], "consensus": summary["consensus"],
               "checked_content_sha": validation.checked_content_sha(schema), "bundle_sha": bsha}
    with db.session(ctx) as s:
        v = s.get(ReportVersion, vid)
        assert v is not None
        if v.schema_sha256 != sch_sha or v.bundle_sha256 != bsha:
            return {"skipped": "stale — a newer edit is being processed"}
        run = latest_run_no(s, vid) + 1
        # Reviewer flags stay open across runs until an admin resolves them.
        flags = s.scalars(select(ValidationIssue).where(ValidationIssue.version_id == vid,
                                                        ValidationIssue.check_name == "reviewer_flag",
                                                        ValidationIssue.status == "open")).all()
        # One multi-row INSERT per ~1000 issues, not one round trip each: a 400-page report
        # has ~2,000 issues, and a far-away database turned row-by-row into minutes.
        if issues:
            s.execute(insert(ValidationIssue), [dict(
                tenant_id=ctx.tenant_id, version_id=vid, run_no=run, check_name=i.check, severity=i.severity,
                fid=i.fid, page=i.page, section_id=i.section_id, bbox=i.bbox, message=i.message,
                expected=i.expected, actual=i.actual, status=i.status, resolution=i.resolution) for i in issues])
        for fl in flags:
            fl.run_no = run
            summary["blocking"] += 1
            summary.setdefault("by_check", {})["reviewer_flag"] = summary["by_check"].get("reviewer_flag", 0) + 1
        v.validation_summary = summary
        v.validated_schema_sha256, v.validated_bundle_sha256 = sch_sha, bsha
        v.status = "validation_issues" if summary["blocking"] else "needs_review"
        v.stage = "done"
        audit.record(s, ctx, "pipeline.validated", target_type="report_version", target_id=vid,
                     after={"run": run, **{k: summary[k] for k in ("figures_checked", "passed", "warnings", "blocking")}})
    return summary


def mark_failed_versions() -> None:  # pragma: no cover - hook for operators
    pass


# ------------------------------------------------------------------ AI double-check (opt-in)


@handler("pipeline.ai_check")
def stage_ai_check(ctx: Context, payload: dict[str, Any]) -> dict[str, Any]:
    """Runs only when an admin asked for it after seeing the estimate. Reads the flagged
    figures' crops, records each outcome on the figure, then re-renders and re-checks."""
    from app.reports import _save_schema

    if not get_settings().gemini_api_key:
        raise UserFacingError("AI double-check isn't set up on this platform (no Gemini key).")
    with db.session(ctx) as s:
        v = _version(s, payload)
        if v.published_at is not None:
            return {"skipped": "published"}
        run = latest_run_no(s, v.id)
        issues = s.scalars(select(ValidationIssue).where(ValidationIssue.version_id == v.id,
                                                          ValidationIssue.run_no == run)).all()
        schema = json.loads(json.dumps(v.schema_json))
        fids = ai_check.eligible(issues, schema)
        sha, vid = v.source_sha256, v.id
    if not fids:
        return {"items": 0}
    from app import ai_usage
    try:                                   # re-checked here: others may have spent since the click
        ai_usage.check(ctx, ai_check.estimate(len(fids))["credits"])
    except ai_usage.LimitReached as e:
        raise UserFacingError(e.message) from e
    pdf = storage.get(ctx, source_key(sha))
    model = get_settings().gemini_model

    def spent(tin: int, tout: int) -> None:
        ai_usage.record(ctx, feature="ai_check", provider="gemini", model=model,
                        input_tokens=tin, output_tokens=tout, version_id=vid)

    try:
        out = ai_check.run(schema, pdf, fids, ai_check.transport(), on_usage=spent)
    except ai_check.ProviderRejected as e:
        raise UserFacingError(str(e)) from e
    with db.session(ctx) as s:
        v = s.get(ReportVersion, vid)
        assert v is not None
        _save_schema(s, ctx, v, schema)
        result = {"items": len(fids), "confirmed": out.confirmed, "disagreed": out.disagreed,
                  "input_tokens": out.input_tokens, "output_tokens": out.output_tokens,
                  "usd": out.usd, "credits": out.credits, "model": get_settings().gemini_model}
        audit.record(s, ctx, "report.ai_check", target_type="report_version", target_id=vid, after=result)
    return result
