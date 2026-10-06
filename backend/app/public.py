"""Public tenant sites, routed by Host header (brief §6 step 7).

    uv run uvicorn app.public:app --port 8080

Hosts:
  <custom subdomain>              live custom domain  -> canonical origin https://<host>
  PREVIEW_URL_PATTERN (per slug)  platform preview    -> noindex; 301 to the custom domain once live
  anything else                   404

Published bundles are immutable; this server only (a) fills in the origin, (b) adds
the tenant's GA4 tag/consent banner to HTML, (c) sets headers, and (d) logs visits.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from starlette.middleware.gzip import GZipMiddleware
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy import select

from app import db, storage
from app.config import get_settings
from app.models import Domain, EdgeHit, Tenant, TenantSettings
from app.render.site import ORIGIN_PLACEHOLDER, robots_txt
from app.reports import SITE_MANIFEST
from app.storage import StorageError
from app.tenancy import system_context, worker_context

log = logging.getLogger(__name__)
app = FastAPI(title="PublishPDF public sites", docs_url=None, redoc_url=None, openapi_url=None)
# A 400-page report is ~7 MB of HTML and ~1 MB compressed: always compress text responses.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)


@dataclass
class Site:
    tenant_id: object
    slug: str
    origin: str
    preview: bool
    redirect_to: str | None


_HOST_CACHE: dict[str, tuple[float, Site | None]] = {}
_MANIFEST_CACHE: dict[object, tuple[float, dict]] = {}
TTL = 5.0


def _preview_regex() -> re.Pattern[str]:
    pat = re.escape(get_settings().preview_url_pattern.lower()).replace(re.escape("{tenant_slug}"), r"([a-z0-9-]+)")
    return re.compile(f"^{pat}$")


def resolve_host(host: str) -> Site | None:
    host = host.split(":")[0].lower().rstrip(".")
    hit = _HOST_CACHE.get(host)
    if hit and time.monotonic() - hit[0] < TTL:
        return hit[1]
    cfg = get_settings()
    site = None
    with db.session(system_context()) as s:
        m = _preview_regex().match(host)
        if m:
            t = s.scalars(select(Tenant).where(Tenant.slug == m.group(1))).first()
            if t is not None and t.status == "active":
                live = s.scalars(select(Domain).where(Domain.tenant_id == t.id, Domain.status == "live")).first()
                origin = f"{cfg.public_scheme}://{host}{cfg.public_port_suffix}"
                site = Site(t.id, t.slug, origin, True, f"https://{live.hostname}" if live else None)
        else:
            d = s.scalars(select(Domain).where(Domain.hostname == host,
                                               Domain.status.in_(("verified", "ssl_issuing", "live")))).first()
            if d is not None:
                t = s.get(Tenant, d.tenant_id)
                if t is not None and t.status == "active":
                    site = Site(t.id, t.slug, f"https://{host}", False, None)
    _HOST_CACHE[host] = (time.monotonic(), site)
    return site


def load_manifest(site: Site) -> dict | None:
    hit = _MANIFEST_CACHE.get(site.tenant_id)
    if hit and time.monotonic() - hit[0] < TTL:
        return hit[1]
    ctx = worker_context(site.tenant_id)  # type: ignore[arg-type]
    try:
        manifest = json.loads(storage.get(ctx, SITE_MANIFEST))
    except StorageError:
        manifest = None
    _MANIFEST_CACHE[site.tenant_id] = (time.monotonic(), manifest)  # type: ignore[assignment]
    return manifest


def clear_caches() -> None:
    _HOST_CACHE.clear()
    _MANIFEST_CACHE.clear()


# ------------------------------------------------------------------ crawler classification

BOTS = [("GPTBot", "GPTBot"), ("OAI-SearchBot", "OAI-SearchBot"), ("ChatGPT-User", "ChatGPT-User"),
        ("ClaudeBot", "ClaudeBot"), ("Claude-User", "Claude-User"), ("Claude-SearchBot", "Claude-SearchBot"),
        ("anthropic-ai", "anthropic-ai"), ("PerplexityBot", "PerplexityBot"), ("Perplexity-User", "Perplexity-User"),
        ("Google-Extended", "Google-Extended"), ("Googlebot", "Googlebot"), ("bingbot", "Bingbot"),
        ("CCBot", "CCBot"), ("Applebot", "Applebot"), ("Bytespider", "Bytespider"), ("Amazonbot", "Amazonbot"),
        ("meta-externalagent", "Meta"), ("FacebookBot", "Meta"), ("DuckAssistBot", "DuckAssistBot"),
        ("DuckDuckBot", "DuckDuckBot"), ("YandexBot", "YandexBot"), ("Baiduspider", "Baiduspider"),
        ("cohere-ai", "Cohere"), ("MistralAI-User", "MistralAI-User"), ("YouBot", "YouBot")]
_GENERIC = re.compile(r"bot|crawler|spider|slurp|fetch|python-requests|curl|wget|httpx", re.I)


def classify_agent(ua: str) -> str:
    for needle, name in BOTS:
        if needle.lower() in ua.lower():
            return f"bot:{name}"
    if _GENERIC.search(ua):
        return "bot:Other"
    return "human"


# ------------------------------------------------------------------ analytics injection


def ga_snippet(ga_id: str, consent_banner: bool) -> str:
    gid = json.dumps(ga_id)
    loader = ("function ppdfGA(){if(window.__ppdfGA)return;window.__ppdfGA=1;var s=document.createElement('script');"
              "s.async=1;s.src='https://www.googletagmanager.com/gtag/js?id='+encodeURIComponent(" + gid + ");"
              "document.head.appendChild(s);window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments)}"
              "window.gtag=gtag;gtag('js',new Date());gtag('config'," + gid + ",{anonymize_ip:true});}")
    if not consent_banner:
        return f"<script>{loader}ppdfGA();</script>"
    return (
        '<div id="ppdf-consent" role="dialog" aria-live="polite" aria-label="Cookie consent" hidden '
        'style="position:fixed;left:16px;right:16px;bottom:16px;max-width:560px;margin:0 auto;background:#fff;color:#14151F;'
        'border:1px solid #E4E6EC;border-radius:14px;padding:16px 18px;box-shadow:0 1px 2px rgba(20,21,31,.06),0 8px 24px rgba(20,21,31,.12);z-index:50;'
        'font:15px/1.5 \'IBM Plex Sans\',system-ui,sans-serif">'
        '<p style="margin:0 0 10px">We use analytics cookies to understand how this page is used. You can accept or decline; '
        'the report is available either way.</p>'
        '<button type="button" id="ppdf-accept" style="background:#3B37C8;color:#fff;border:0;border-radius:10px;min-height:44px;padding:0 18px;font:inherit;font-weight:600;margin-right:8px;cursor:pointer">Accept</button>'
        '<button type="button" id="ppdf-decline" style="background:#fff;color:#14151F;border:1px solid #C9CCD6;border-radius:10px;min-height:44px;padding:0 18px;font:inherit;font-weight:600;cursor:pointer">Decline</button>'
        '</div>'
        "<script>(function(){" + loader +
        "var k='ppdf-consent',v=null;try{v=localStorage.getItem(k)}catch(e){}"
        "var b=document.getElementById('ppdf-consent');"
        "if(v==='granted'){ppdfGA();return}if(v==='denied')return;b.hidden=false;"
        "document.getElementById('ppdf-accept').onclick=function(){try{localStorage.setItem(k,'granted')}catch(e){}b.hidden=true;ppdfGA()};"
        "document.getElementById('ppdf-decline').onclick=function(){try{localStorage.setItem(k,'denied')}catch(e){}b.hidden=true};"
        "})();</script>")


_MARK_SVG = (Path(__file__).parent / "render" / "assets" / "publishpdf-mark-color.svg").read_text()
_NOT_FOUND_FONTS = ("https://fonts.googleapis.com/css2?family=Manrope:wght@800&family=IBM+Plex+Sans:wght@400"
                    "&display=swap")

CSP = ("default-src 'self'; img-src 'self' data: https://www.google-analytics.com https://www.googletagmanager.com; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
       "script-src 'self' 'unsafe-inline' https://www.googletagmanager.com; "
       "connect-src 'self' https://*.google-analytics.com https://*.analytics.google.com https://www.googletagmanager.com; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def _settings(tenant_id) -> TenantSettings | None:
    with db.session(worker_context(tenant_id)) as s:
        st = s.get(TenantSettings, tenant_id)
        if st is not None:
            s.expunge(st)
        return st


def _log_hit(site: Site, host: str, path: str, report_id: str | None, status: int, ua: str) -> None:
    try:
        with db.session(worker_context(site.tenant_id)) as s:  # type: ignore[arg-type]
            s.add(EdgeHit(tenant_id=site.tenant_id, host=host[:253], path=path[:500], report_id=report_id,
                          status=status, agent_class=classify_agent(ua)[:60]))
    except Exception:  # noqa: BLE001 - logging must never break serving
        log.exception("edge hit logging failed")


# ------------------------------------------------------------------ routes


@app.get("/internal/tls-ask")
def tls_ask(domain: str, token: str = "") -> Response:
    """Caddy on-demand TLS 'ask' hook: allow a certificate only for hosts we serve."""
    cfg = get_settings()
    if cfg.internal_api_token and token != cfg.internal_api_token:
        return PlainTextResponse("forbidden", status_code=403)
    clear_caches()
    return PlainTextResponse("ok") if resolve_host(domain) else PlainTextResponse("no", status_code=404)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.api_route("/{path:path}", methods=["GET", "HEAD"])
def serve(path: str, request: Request) -> Response:
    host = request.headers.get("host", "")
    site = resolve_host(host)
    if site is None:
        return PlainTextResponse("This site isn't set up.", status_code=404)
    req_path = "/" + unquote(path)
    if site.redirect_to:
        qs = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(site.redirect_to + req_path + qs, status_code=301)
    ua = request.headers.get("user-agent", "")
    headers = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "strict-origin-when-cross-origin",
               "Content-Security-Policy": CSP, "Cache-Control": "public, max-age=300"}
    if site.preview:
        headers["X-Robots-Tag"] = "noindex, nofollow"
        headers["Cache-Control"] = "no-cache"          # reviewers must always see the latest publish
    if ".." in req_path.split("/") or "\\" in req_path:
        return PlainTextResponse("Not found", status_code=404, headers=headers)

    manifest = load_manifest(site)
    if req_path == "/robots.txt":
        body = robots_txt("allow_all", preview=True) if site.preview else None
        if body is not None:
            return PlainTextResponse(body, headers=headers)
    if manifest is None:
        _log_hit(site, host, req_path, None, 404, ua)
        return _not_found(headers, "Nothing has been published here yet.")

    target = req_path
    if req_path in ("/latest", "/latest/") and manifest.get("latest"):
        target = manifest["latest"]
    if target != "/" and not target.endswith("/") and "." not in target.rsplit("/", 1)[-1]:
        if (target + "/") in manifest["paths"]:
            return RedirectResponse(req_path + "/", status_code=301)
    entry = manifest["paths"].get(target)
    if entry is None:
        _log_hit(site, host, req_path, None, 404, ua)
        return _not_found(headers, "That page doesn't exist.")
    ctx = worker_context(site.tenant_id)  # type: ignore[arg-type]
    try:
        data = storage.get(ctx, entry["key"])
    except StorageError:
        return _not_found(headers, "That page is temporarily unavailable.")
    key = entry["key"]
    ctype = entry.get("content_type") or _ctype(key)
    _log_hit(site, host, req_path, entry.get("report_id"), 200, ua)
    if ctype.startswith(("text/", "application/json", "application/xml")):
        text = data.decode("utf-8").replace(ORIGIN_PLACEHOLDER, site.origin)
        if ctype.startswith("text/html"):
            if site.preview:
                text = text.replace("<head>", '<head><meta name="robots" content="noindex, nofollow">', 1)
            st = _settings(site.tenant_id)
            if st and st.ga4_measurement_id:
                text = text.replace("</body>", ga_snippet(st.ga4_measurement_id, st.consent_banner_enabled) + "</body>", 1)
        return Response(text, media_type=ctype, headers=headers)
    if ctype == "application/pdf":
        headers["Content-Disposition"] = "inline"
    return Response(data, media_type=ctype, headers=headers)


def _ctype(key: str) -> str:
    from app.render.site import content_type
    return content_type(key)


_NOT_FOUND_CSS = (
    "body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px 16px;background:#F8F8FA;color:#3D4152;"
    "font:17px/1.6 'IBM Plex Sans',system-ui,sans-serif}"
    ".card{max-width:480px;width:100%;background:#fff;border:1px solid #E4E6EC;border-radius:14px;padding:32px;"
    "box-shadow:0 1px 2px rgba(20,21,31,.06),0 8px 24px rgba(20,21,31,.05)}"
    "h1{font:800 2rem/1.1 Manrope,system-ui,sans-serif;letter-spacing:-.025em;color:#14151F;margin:0 0 .4em}p{margin:0 0 1em}"
    ".home{display:inline-flex;align-items:center;min-height:44px;padding:0 20px;border-radius:10px;background:#3B37C8;color:#fff;"
    "font:700 15px Manrope,system-ui,sans-serif;text-decoration:none}.home:hover{background:#2E2AA3}"
    ".home:focus-visible{outline:3px solid #DDE3FF;outline-offset:2px}"
    ".host{display:flex;align-items:center;gap:6px;margin:24px 0 0;font-size:14px;color:#5F6477}")


def _not_found(headers: dict, msg: str) -> Response:
    mark = _MARK_SVG.replace('width="32" height="32"', 'width="18" height="18" aria-hidden="true"')
    return Response(f"<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
                    f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Not found</title>"
                    f"<link rel=\"stylesheet\" href=\"{_NOT_FOUND_FONTS}\"><style>{_NOT_FOUND_CSS}</style>"
                    f"<body><main class=\"card\"><h1>Not found</h1><p>{msg}</p><a class=\"home\" href=\"/\">Go to the home page</a>"
                    f"<p class=\"host\">{mark}Hosted on PublishPDF</p></main></body></html>",
                    status_code=404, media_type="text/html", headers=headers)
