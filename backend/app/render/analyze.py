"""Propose theme tokens from a client's website or reference images (brief §5, modes 1 & 3).

Fetching is SSRF-guarded: http(s) only, every hop's host must resolve to public
addresses, redirects are followed manually (max 3) and re-checked, responses are
size- and time-limited. The proposal is only a starting point; the client confirms
or edits it, and validate() + enforce() apply before anything is rendered.
"""

from __future__ import annotations


import colorsys
import io
import ipaddress
import re
import socket
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image

from app.render import theme as theming

MAX_BYTES = 2 * 1024 * 1024
TIMEOUT = 8.0
UA = "PublishPDF-ThemeBot/1.0 (+theme proposal; one-off fetch requested by the site owner)"


class FetchError(Exception):
    pass


def _public_host(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise FetchError(f"Couldn't find {host}. Check the address.") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise FetchError("That address points to a private network and can't be fetched.")


def safe_get(url: str, *, accept: str = "text/html,text/css,*/*") -> tuple[str, bytes, str]:
    """Returns (final_url, body, content_type)."""
    for _ in range(4):
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise FetchError("Only http:// and https:// addresses can be used.")
        if u.port not in (None, 80, 443):
            raise FetchError("Only standard web ports are allowed.")
        _public_host(u.hostname)
        with httpx.Client(timeout=TIMEOUT, follow_redirects=False, headers={"User-Agent": UA, "Accept": accept}) as c:
            with c.stream("GET", url) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code >= 400:
                    raise FetchError(f"The site answered with an error ({r.status_code}).")
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > MAX_BYTES:
                        raise FetchError("The page is too large to analyse.")
                return url, bytes(buf), r.headers.get("content-type", "")
    raise FetchError("Too many redirects.")


class _Scan(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.theme_color: str | None = None
        self.icons: list[str] = []
        self.og_image: str | None = None
        self.logo_imgs: list[str] = []
        self.stylesheets: list[str] = []
        self.styles: list[str] = []
        self.inline: list[str] = []
        self.site_name: str | None = None
        self._in_style = False

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        if a.get("style"):
            self.inline.append(a["style"])
        if tag == "meta":
            if a.get("name") == "theme-color":
                self.theme_color = a.get("content")
            if a.get("property") == "og:image":
                self.og_image = a.get("content")
            if a.get("property") == "og:site_name":
                self.site_name = a.get("content")
        elif tag == "link":
            rel = a.get("rel", "").lower()
            if "stylesheet" in rel and a.get("href"):
                self.stylesheets.append(a["href"])
            if "icon" in rel and a.get("href"):
                self.icons.append(a["href"])
        elif tag == "img":
            blob = " ".join([a.get("class", ""), a.get("id", ""), a.get("alt", ""), a.get("src", "")]).lower()
            if "logo" in blob and a.get("src"):
                self.logo_imgs.append(a["src"])
        elif tag == "style":
            self._in_style = True

    def handle_endtag(self, tag):
        if tag == "style":
            self._in_style = False

    def handle_data(self, data):
        if self._in_style:
            self.styles.append(data)


_HEX = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
_RGB = re.compile(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})")
_FONT = re.compile(r"font-family\s*:\s*([^;}]+)", re.I)
_BODY_BG = re.compile(r"(?:body|html)\s*\{[^}]*background(?:-color)?\s*:\s*(#[0-9a-fA-F]{3,6})", re.I)


def _norm_hex(h: str) -> str:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h.upper()


def _colors(css: str) -> Counter[str]:
    c: Counter[str] = Counter()
    for m in _HEX.finditer(css):
        c[_norm_hex(m.group(0))] += 1
    for m in _RGB.finditer(css):
        r, g, b = (min(255, int(x)) for x in m.groups())
        c[f"#{r:02X}{g:02X}{b:02X}"] += 1
    return c


def _sat_light(hex_: str) -> tuple[float, float]:
    r, g, b = (int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, lum, s = colorsys.rgb_to_hls(r, g, b)
    return s, lum


def _fonts(css: str) -> list[str]:
    out = []
    for m in _FONT.finditer(css):
        first = m.group(1).split(",")[0].strip().strip("'\"")
        if first and not first.startswith("var(") and first.lower() not in ("inherit", "initial"):
            out.append(first)
    return [f for f, _ in Counter(out).most_common(6)]


def _map_font(name: str | None) -> str:
    if not name:
        return "system"
    for g in theming.GOOGLE_FONTS:
        if g.lower() == name.lower():
            return g
    low = name.lower()
    if any(k in low for k in ("georgia", "times", "serif", "garamond", "merriweather")) and "sans" not in low:
        return "serif"
    return "system"


# Neutral fallbacks when a site has no usable brand colour. Deliberately NOT the PublishPDF
# default theme: a proposal built from the customer's website shouldn't come out in our brand.
FALLBACK_PRIMARY = "#1F4FD1"
FALLBACK_SECONDARY = "#0F766E"


def palette_from_counts(colors: Counter[str], *, background: str | None = None) -> dict:
    saturated = [c for c, _ in colors.most_common() if _sat_light(c)[0] > 0.25 and 0.15 < _sat_light(c)[1] < 0.85]
    darks = [c for c, _ in colors.most_common() if _sat_light(c)[1] < 0.25]
    primary = saturated[0] if saturated else FALLBACK_PRIMARY
    secondary = next((c for c in saturated[1:] if theming.contrast(c, primary) > 1.4), None) or FALLBACK_SECONDARY
    bg = background or "#FFFFFF"
    text = darks[0] if darks else "#1B1D21"
    return {"primary": primary, "secondary": secondary, "text": text, "background": bg,
            "header_bg": bg, "header_text": text}


def analyze_website(url: str) -> dict:
    final, body, ctype = safe_get(url)
    if "html" not in ctype and not body.lstrip().startswith(b"<"):
        raise FetchError("That address didn't return a web page.")
    html = body.decode("utf-8", errors="replace")
    scan = _Scan()
    scan.feed(html)
    css = "\n".join(scan.styles + scan.inline)
    for href in scan.stylesheets[:3]:
        try:
            _, data, _ = safe_get(urljoin(final, href), accept="text/css")
            css += "\n" + data.decode("utf-8", errors="replace")
        except FetchError:
            continue
    counts = _colors(css)
    if scan.theme_color and _HEX.fullmatch(scan.theme_color.strip()):
        counts[_norm_hex(scan.theme_color.strip())] += 50
    bgm = _BODY_BG.search(css)
    colors = palette_from_counts(counts, background=_norm_hex(bgm.group(1)) if bgm else None)
    fonts = _fonts(css)
    logo = scan.logo_imgs[0] if scan.logo_imgs else (scan.og_image or (scan.icons[-1] if scan.icons else None))
    return _proposal(colors, fonts, urljoin(final, logo) if logo else None, source=final)


def analyze_images(images: list[bytes]) -> dict:
    counts: Counter[str] = Counter()
    for data in images[:5]:
        try:
            im = Image.open(io.BytesIO(data)).convert("RGB")
        except Exception as e:  # noqa: BLE001
            raise FetchError("One of the files isn't an image we can read (use PNG or JPEG).") from e
        im.thumbnail((300, 300))
        q = im.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
        pal = q.getpalette() or []
        for count, idx in sorted(q.getcolors() or [], reverse=True):
            r, g, b = pal[idx * 3: idx * 3 + 3]
            counts[f"#{r:02X}{g:02X}{b:02X}"] += count
    lightest = max(counts, key=lambda c: _sat_light(c)[1]) if counts else "#FFFFFF"
    bg = lightest if _sat_light(lightest)[1] > 0.9 else "#FFFFFF"
    return _proposal(palette_from_counts(counts, background=bg), [], None, source="reference images")


def analyze_references(urls: list[str], images: list[bytes]) -> dict:
    proposals = [analyze_website(u) for u in urls[:3]]
    if images:
        proposals.append(analyze_images(images))
    if not proposals:
        raise FetchError("Add at least one reference URL or image.")
    base = proposals[0]
    base["sources"] = [p["source"] for p in proposals]
    return base


def _proposal(colors: dict, fonts: list[str], logo_url: str | None, *, source: str) -> dict:
    t = theming.validate({"colors": colors, "typography": {"heading_font": _map_font(fonts[0] if fonts else None),
                                                            "body_font": _map_font(fonts[1] if len(fonts) > 1 else (fonts[0] if fonts else None)),
                                                            "base_size": 16}})
    return {"theme": t, "contrast": theming.contrast_report(t), "logo_url": logo_url,
            "fonts_seen": fonts, "source": source}


def fetch_logo(url: str) -> tuple[bytes, str]:
    _, data, ctype = safe_get(url, accept="image/png,image/jpeg,image/webp")
    return validate_logo(data, ctype)


LOGO_TYPES = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}


def validate_logo(data: bytes, ctype: str = "") -> tuple[bytes, str]:
    """Only raster images (no SVG: it can carry scripts). Re-encode to strip metadata."""
    if len(data) > 1024 * 1024:
        raise FetchError("The logo must be under 1 MB.")
    try:
        im = Image.open(io.BytesIO(data))
        fmt = im.format
        im.load()
    except Exception as e:  # noqa: BLE001
        raise FetchError("The logo must be a PNG, JPEG or WebP image.") from e
    if fmt not in LOGO_TYPES:
        raise FetchError("The logo must be a PNG, JPEG or WebP image.")
    im.thumbnail((800, 240))
    out = io.BytesIO()
    im.save(out, format="PNG")
    return out.getvalue(), "png"


# ------------------------------------------------------------------ palette and brand PDF


def palette_proposal(colors: list[str]) -> dict:
    """Turn a client's palette (1–8 hex colours) into theme tokens: first brand colour ->
    primary, second -> secondary, a very light one -> background, the darkest ->
    the text colour — keeping the order the client gave for brand colours. Contrast is
    reported (and enforced when rendered)."""
    cleaned = []
    for c in colors[:8]:
        c = c.strip()
        if not c.startswith("#"):
            c = "#" + c
        if not _HEX.fullmatch(c):
            raise FetchError(f"“{c}” isn't a hex colour like #1F4FD1.")
        cleaned.append(_norm_hex(c))
    if len(cleaned) < 1:
        raise FetchError("Add at least one colour.")
    lights = [c for c in cleaned if _sat_light(c)[1] > 0.9]
    darks = sorted(cleaned, key=lambda c: _sat_light(c)[1])
    # Brand colours in the order given (people list their main colour first).
    brand = [c for c in cleaned if c not in lights and not (c == darks[0] and _sat_light(c)[1] < 0.2)] or cleaned
    primary = brand[0]
    secondary = brand[1] if len(brand) > 1 else primary
    background = lights[0] if lights else "#FFFFFF"
    text = darks[0] if _sat_light(darks[0])[1] < 0.3 else "#1B1D21"
    colors_ = {"primary": primary, "secondary": secondary, "background": background, "text": text,
               "header_bg": background, "header_text": text}
    return _proposal(colors_, [], None, source="your palette")


def analyze_pdf(data: bytes) -> dict:
    """Colours from the rendered pages and fonts from the embedded font names of a
    brand PDF (e.g. last year's annual report or a brand guide)."""
    import pymupdf

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:  # noqa: BLE001
        raise FetchError("That PDF can't be opened.") from e
    images, fonts = [], []
    for i in range(min(4, doc.page_count)):
        page = doc[i]
        images.append(page.get_pixmap(dpi=40).tobytes("png"))
        for f in page.get_fonts():
            name = re.sub(r"^[A-Z]{6}\+", "", f[3] or "")
            name = re.sub(r"[-,](Bold|Regular|Italic|Medium|Light|Semibold|SemiBold|Black|MT|PS).*$", "", name)
            name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).strip()
            if name:
                fonts.append(name)
    counts: Counter[str] = Counter()
    for png in images:
        im = Image.open(io.BytesIO(png)).convert("RGB")
        q = im.quantize(colors=10, method=Image.Quantize.MEDIANCUT)
        pal = q.getpalette() or []
        for count, idx in q.getcolors() or []:
            r, g, b = pal[idx * 3: idx * 3 + 3]
            counts[f"#{r:02X}{g:02X}{b:02X}"] += count
    ranked = [f for f, _ in Counter(fonts).most_common(4)]
    return _proposal(palette_from_counts(counts), ranked, None, source="your PDF")
