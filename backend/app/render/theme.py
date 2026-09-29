"""Theme tokens: validation, contrast enforcement and CSS generation.

A theme is visual only. Every mode (match website / custom / reference) produces the
same token set; the renderer turns tokens into CSS variables and nothing else, so the
HTML structure and data are identical across themes.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
FONT_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9 \-]{0,40}$")

SYSTEM_FONTS = {
    "system": 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif',
    "serif": 'Georgia, "Times New Roman", serif',
    "mono": 'ui-monospace, SFMono-Regular, Menlo, monospace',
}
GOOGLE_FONTS = {"Inter", "Roboto", "Open Sans", "Lato", "Montserrat", "Source Sans 3", "Noto Sans",
                "Merriweather", "Playfair Display", "Poppins", "IBM Plex Sans", "Work Sans", "Nunito Sans",
                "PT Serif", "Libre Franklin", "DM Sans", "Manrope", "Mulish", "Raleway", "Lora"}

DEFAULT_THEME: dict[str, Any] = {
    "colors": {
        # PublishPDF brand palette: the starting point for a workspace that hasn't set its own
        # design. Any saved theme overrides every token.
        "primary": "#3B37C8",       # links, accents (Ledger Indigo)
        "secondary": "#17153B",     # latest-period bar, highlight rule (Midnight)
        "text": "#14151F",
        "muted": "#5F6477",
        "background": "#FFFFFF",
        "surface": "#F8F8FA",
        "border": "#E4E6EC",
        "header_bg": "#FFFFFF",
        "header_text": "#14151F",
    },
    "typography": {"heading_font": "Manrope", "body_font": "IBM Plex Sans", "base_size": 16},
    "spacing": "normal",            # compact | normal | relaxed
    "header": {"style": "light", "show_company_name": True},   # light | solid | minimal
    "logo": None,                   # {"key": "...", "alt": "...", "sha": "..."}
    "footer": {"show_powered_by": False},
}


class ThemeError(ValueError):
    pass


def merge(base: dict, override: dict | None) -> dict:
    out = deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


def validate(theme: dict) -> dict:
    """Return a cleaned theme or raise ThemeError. Client input never reaches CSS
    except through these whitelisted shapes."""
    theme = {k: v for k, v in (theme or {}).items() if k != "layout"}   # retired option (one layout: the PDF's)
    t = merge(DEFAULT_THEME, theme)
    for k, v in t["colors"].items():
        if k not in DEFAULT_THEME["colors"]:
            raise ThemeError(f"unknown colour token {k!r}")
        if not isinstance(v, str) or not HEX.match(v):
            raise ThemeError(f"{k}: colours must be 6-digit hex like #1F4FD1")
        t["colors"][k] = v.upper()
    for k in ("heading_font", "body_font"):
        f = t["typography"][k]
        if f not in SYSTEM_FONTS and f not in GOOGLE_FONTS:
            raise ThemeError(f"{k}: choose a supported font")
    size = t["typography"]["base_size"]
    if not isinstance(size, int) or not 14 <= size <= 20:
        raise ThemeError("base_size must be 14–20")
    if t["spacing"] not in ("compact", "normal", "relaxed"):
        raise ThemeError("spacing must be compact, normal or relaxed")
    if t["header"].get("style") not in ("light", "solid", "minimal"):
        raise ThemeError("header style must be light, solid or minimal")
    t["header"]["show_company_name"] = bool(t["header"].get("show_company_name", True))
    extra = set(t) - set(DEFAULT_THEME)
    if extra:
        raise ThemeError(f"unknown theme fields: {sorted(extra)}")
    return t


# ------------------------------------------------------------------ contrast (WCAG 2.x)


def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_: str) -> float:
    r, g, b = (int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _mix(hex_: str, toward: str, t: float) -> str:
    a = [int(hex_[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(toward[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(a, b))


def fix_contrast(fg: str, bg: str, target: float = 4.5) -> str:
    """Nudge `fg` toward black or white (whichever the background needs) until it
    reaches `target` contrast against `bg`."""
    if contrast(fg, bg) >= target:
        return fg
    toward = "#000000" if luminance(bg) > 0.5 else "#FFFFFF"
    for i in range(1, 101):
        cand = _mix(fg, toward, i / 100)
        if contrast(cand, bg) >= target:
            return cand
    return toward


PAIRS = [  # (foreground token, background token, minimum ratio, what)
    ("text", "background", 4.5, "Body text on the page background"),
    ("text", "surface", 4.5, "Body text on table/surface background"),
    ("muted", "background", 4.5, "Secondary text on the page background"),
    ("primary", "background", 4.5, "Links on the page background"),
    ("header_text", "header_bg", 4.5, "Header text on the header background"),
]


def contrast_report(theme: dict) -> list[dict]:
    c = theme["colors"]
    out = []
    for fg, bg, target, what in PAIRS:
        ratio = contrast(c[fg], c[bg])
        entry = {"pair": [fg, bg], "what": what, "ratio": round(ratio, 2), "required": target,
                 "ok": ratio >= target}
        if not entry["ok"]:
            entry["suggestion"] = {fg: fix_contrast(c[fg], c[bg], target)}
        out.append(entry)
    return out


def enforce(theme: dict) -> tuple[dict, list[dict]]:
    """Apply contrast fixes (the rendered page is always accessible). Returns the
    theme actually used and the list of adjustments made."""
    t = deepcopy(theme)
    changes = []
    for fg, bg, target, what in PAIRS:
        before = t["colors"][fg]
        after = fix_contrast(before, t["colors"][bg], target)
        if after != before:
            t["colors"][fg] = after
            changes.append({"token": fg, "from": before, "to": after, "reason": what})
    return t, changes


# ------------------------------------------------------------------ CSS


def font_stack(name: str) -> str:
    return SYSTEM_FONTS.get(name) or f'"{name}", {SYSTEM_FONTS["system"]}'


def google_fonts_href(theme: dict) -> str | None:
    fams = sorted({f for f in (theme["typography"]["heading_font"], theme["typography"]["body_font"]) if f in GOOGLE_FONTS})
    if not fams:
        return None
    q = "&".join(f"family={f.replace(' ', '+')}:wght@400;500;600;700;800" for f in fams)
    return f"https://fonts.googleapis.com/css2?{q}&display=swap"


SPACING = {"compact": "0.75", "normal": "1", "relaxed": "1.25"}


def css_variables(theme: dict) -> str:
    c = theme["colors"]
    lines = [f"--c-{k.replace('_', '-')}: {v};" for k, v in c.items()]
    lines.append(f"--font-heading: {font_stack(theme['typography']['heading_font'])};")
    lines.append(f"--font-body: {font_stack(theme['typography']['body_font'])};")
    lines.append(f"--base-size: {int(theme['typography']['base_size'])}px;")
    lines.append(f"--space: {SPACING[theme['spacing']]};")
    return ":root{" + "".join(lines) + "}"
