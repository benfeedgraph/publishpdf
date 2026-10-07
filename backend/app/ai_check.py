"""AI double-check of flagged figures (opt-in, per report, estimate first).

Only figures the validation agents flagged are sent: a small crop of the PDF around
each one. The model is asked to transcribe what the crop shows; the answer can only
CONFIRM the value we already extracted (exact digits, sign and kind, the same rule the
visual re-read uses). It never supplies or changes a number. A confirmed figure's issue
drops to a warning; a disagreement stays with a person, with the AI's reading shown.

Cost is estimated before anything runs, from token counts x the configured Gemini
prices, and shown in credits (AI_CREDIT_USD per credit). Actual usage — from the
provider's own token counts — is written to the ai_usage ledger after every request
(app/ai_usage.py), and totalled on the job and in the audit log.
"""

from __future__ import annotations

import base64
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import pymupdf

from app.config import get_settings

CHECKS = ("re_extraction", "low_confidence")     # the per-figure disagreements AI can help settle
BATCH = 12                    # crops per request
MAX_SIDE = 384                # px; Gemini bills an image this size as one 258-token tile
IMAGE_TOKENS = 258
PROMPT_TOKENS = 160           # instructions, per request
LABEL_TOKENS = 8              # "Image 7:" per crop
OUTPUT_TOKENS = 18            # {"i": 7, "text": "1,234.56"} per crop

PROMPT = (
    "Each image is a small crop of a financial report page. For each image, transcribe EXACTLY the "
    "number, percentage or date shown in the crop, character for character as "
    "printed (keep commas, brackets, minus signs, % and currency symbols). If you cannot read it with "
    "certainty, return an empty string. Reply with JSON only: [{\"i\": <image number>, \"text\": \"...\"}]."
)


def eligible(issues: list, schema: dict) -> list[str]:
    """Figure ids worth an AI look: open blocking per-figure disagreements, one per figure,
    not already checked at their current value."""
    out, seen = [], set()
    for i in issues:
        fid = getattr(i, "fid", None) if not isinstance(i, dict) else i.get("fid")
        check = i.check_name if hasattr(i, "check_name") else i.get("check")
        sev = i.severity if hasattr(i, "severity") else i.get("severity")
        status = i.status if hasattr(i, "status") else i.get("status")
        if not fid or check not in CHECKS or sev != "blocking" or status != "open" or fid in seen:
            continue
        f = schema["figures"].get(fid)
        if not f or not (f.get("source") or {}).get("bbox"):
            continue
        done = f.get("ai_check")
        if done and done.get("raw") == f["raw"]:
            continue
        seen.add(fid)
        out.append(fid)
    # Figures read from images (OCR) first — those are the ones a person would otherwise
    # have to look at — then page order, so a run is reproducible.
    return sorted(out, key=lambda fid: (schema["figures"][fid].get("method") != "ocr",
                                        schema["figures"][fid]["source"]["page"], fid))


def items_within(credits: int, n_items: int) -> int:
    """How many of n_items the estimate fits in `credits` (0 if not even one batch)."""
    lo, hi = 0, n_items
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate(mid)["credits"] <= credits:
            lo = mid
        else:
            hi = mid - 1
    return lo


def estimate(n_items: int) -> dict[str, Any]:
    cfg = get_settings()
    calls = math.ceil(n_items / BATCH) if n_items else 0
    tin = n_items * (IMAGE_TOKENS + LABEL_TOKENS) + calls * PROMPT_TOKENS
    tout = n_items * OUTPUT_TOKENS
    usd = tin * cfg.gemini_price_in_per_m / 1e6 + tout * cfg.gemini_price_out_per_m / 1e6
    return {"items": n_items, "requests": calls, "input_tokens": tin, "output_tokens": tout,
            "usd": round(usd, 4), "credits": credits_for(usd), "model": cfg.gemini_model,
            "credit_usd": cfg.ai_credit_usd}


def credits_for(usd: float) -> int:
    cfg = get_settings()
    return math.ceil(usd / cfg.ai_credit_usd - 1e-9) if usd > 0 else 0


def crop_png(doc: pymupdf.Document, f: dict) -> bytes:
    page = doc[f["source"]["page"] - 1]
    x0, y0, x1, y1 = f["source"]["bbox"]
    clip = pymupdf.Rect(x0 - 4, y0 - 2, x1 + 4, y1 + 2) & page.rect
    zoom = min(4.0, MAX_SIDE / max(clip.width, clip.height, 1))
    return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False).tobytes("png")


# A transport takes the request body and returns the provider's JSON response. Tests pass a
# fake one; nothing here reaches the network unless the real transport is used.
Transport = Callable[[dict], dict]


def gemini_url(api_key: str | None, model: str, api: str = "") -> str:
    """Where a Gemini request goes. Google issues two kinds of key: Gemini API (AI Studio)
    keys start "AIza" and use generativelanguage.googleapis.com; Vertex AI express-mode
    keys start "AQ." and only work on aiplatform.googleapis.com (sent to the other address
    they get 401/404). GEMINI_API picks one explicitly: "gemini" or "vertex"."""
    vertex = api == "vertex" or (api != "gemini" and (api_key or "").startswith("AQ."))
    if vertex:
        return f"https://aiplatform.googleapis.com/v1/publishers/google/models/{model}:generateContent"
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def gemini_transport(body: dict) -> dict:  # pragma: no cover - live network, never used in tests
    import httpx

    cfg = get_settings()
    url = gemini_url(cfg.gemini_api_key, cfg.gemini_model, cfg.gemini_api)
    # The key goes in a header, never the URL: URLs end up in tracebacks, logs and job records.
    r = httpx.post(url, headers={"x-goog-api-key": cfg.gemini_api_key or ""}, json=body, timeout=60)
    if r.status_code in (401, 403):
        raise ProviderRejected("The AI provider rejected the platform's Gemini API key (it may be invalid, "
                               "expired or not enabled for this API). Nothing was charged.")
    if r.status_code == 404:
        raise ProviderRejected(f"The AI provider doesn't offer the model '{cfg.gemini_model}' to this key "
                               "(check GEMINI_MODEL, and that the key is for the Gemini API or Vertex AI). "
                               "Nothing was charged.")
    r.raise_for_status()
    return r.json()


class ProviderRejected(Exception):
    """The provider refused the platform's credentials: retrying can't help."""


_OVERRIDE: list[Transport] = []      # tests install a fake transport here; never the network


def transport() -> Transport:
    return _OVERRIDE[-1] if _OVERRIDE else gemini_transport


def _request(pngs: list[bytes]) -> dict:
    parts: list[dict] = [{"text": PROMPT}]
    for k, png in enumerate(pngs, start=1):
        parts.append({"text": f"Image {k}:"})
        parts.append({"inline_data": {"mime_type": "image/png", "data": base64.b64encode(png).decode()}})
    return {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}


def _parse(resp: dict, n: int) -> list[str]:
    text = "".join(p.get("text", "") for c in resp.get("candidates", [])[:1]
                   for p in (c.get("content") or {}).get("parts", []))
    try:
        rows = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return [""] * n
    out = [""] * n
    for row in rows if isinstance(rows, list) else []:
        try:
            k = int(row.get("i")) - 1
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 <= k < n:
            out[k] = str(row.get("text") or "").strip()
    return out


@dataclass
class Outcome:
    confirmed: int
    disagreed: int
    input_tokens: int
    output_tokens: int
    usd: float
    credits: int


UsageSink = Callable[[int, int], None]     # (input_tokens, output_tokens) of one request


def run(schema: dict, pdf_bytes: bytes, fids: list[str], transport: Transport,
        on_usage: UsageSink | None = None) -> Outcome:
    """Reads each figure's crop and records the outcome on the figure (schema is updated
    in place). Only confirmations change anything downstream, and only to a warning.
    `on_usage` is called after EACH request, so spend is recorded even if a later one fails."""
    from app.validation import reextraction_matches

    cfg = get_settings()
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    tin = tout = 0
    confirmed = disagreed = 0
    now = datetime.now(timezone.utc).isoformat()
    try:
        for start in range(0, len(fids), BATCH):
            batch = fids[start:start + BATCH]
            figs = [schema["figures"][fid] for fid in batch]
            resp = transport(_request([crop_png(doc, f) for f in figs]))
            usage = resp.get("usageMetadata") or {}
            bin_, bout = int(usage.get("promptTokenCount") or 0), int(usage.get("candidatesTokenCount") or 0)
            tin += bin_
            tout += bout
            if on_usage is not None:
                on_usage(bin_, bout)
            for f, read in zip(figs, _parse(resp, len(figs))):
                match = bool(read) and reextraction_matches(f["raw"], read, f["kind"])
                f["ai_check"] = {"raw": f["raw"], "read": read, "match": match, "model": cfg.gemini_model, "at": now}
                confirmed += match
                disagreed += not match
    finally:
        doc.close()
    usd = tin * cfg.gemini_price_in_per_m / 1e6 + tout * cfg.gemini_price_out_per_m / 1e6
    return Outcome(confirmed, disagreed, tin, tout, round(usd, 4), credits_for(usd))


def apply_confirmations(issues: list, schema: dict) -> None:
    """An AI reading that agrees exactly with the extracted value (at its current raw)
    counts as one more independent confirmation: the figure's re-read/low-confidence
    issue becomes a warning. Disagreements are left blocking, with the AI's reading."""
    for i in issues:
        if i.check not in CHECKS or i.severity != "blocking" or not i.fid:
            continue
        f = schema["figures"].get(i.fid) or {}
        ac = f.get("ai_check")
        if not ac or ac.get("raw") != f.get("raw"):
            continue
        if ac.get("match"):
            i.severity = "warning"
            i.message += " An independent AI reading of the page agrees exactly."
        elif ac.get("read"):
            i.message += f" An AI reading of the page shows “{ac['read']}”."
