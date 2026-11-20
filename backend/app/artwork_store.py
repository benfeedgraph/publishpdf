"""A report version's drawn pages, kept between its builds (see `site.render_report(memo=...)`).

They are stored next to the version's page files as one JSON document. Pictures aren't stored
twice: the document names the page file holding each one, with its hash, and they are read back
from there (any file that changed since makes the whole record unusable, and the pages are simply
drawn again). The few drawn pictures that never reach the page files — whole-page images that a
page's own cut-outs replaced — are left out; nothing reads them once the cut-outs exist.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from markupsafe import Markup

from app import storage
from app.tenancy import Context

FILE = "_artwork.json"
_DROP = object()


def read(ctx: Context, prefix: str) -> dict | None:
    """The kept drawing of the version stored at `prefix`, as stored (still pointing at the page
    files), or None when there is none."""
    try:
        return json.loads(storage.get(ctx, prefix + FILE))
    except Exception:  # noqa: BLE001 - nothing kept (first build) or unreadable: draw the pages
        return None


def layouts_of(doc: dict) -> dict[int, dict]:
    """The AI layouts the kept drawing was made with."""
    return _decode(doc.get("layouts") or {"$d": []}, {})


def memo(ctx: Context, prefix: str, doc: dict | None, key: str) -> dict:
    """A memo for render_report holding the kept drawing `doc` (see read) when it was made from
    exactly what `key` names; {} when there is none to reuse."""
    if not doc or doc.get("key") != key:
        return {}
    try:
        refs: dict[str, str] = doc["files"]
        got = storage.get_many(ctx, [prefix + name for name in refs])
        blobs = {}
        for name, sha in refs.items():
            data = got.get(prefix + name)
            if data is None or hashlib.sha256(data).hexdigest() != sha:
                return {}
            blobs[sha] = data
        return {"artwork": (key, _decode(doc["artwork"], blobs))}
    except Exception:  # noqa: BLE001 - a page file is gone or the record is damaged: draw the pages
        return {}


def save(ctx: Context, prefix: str, memo: dict, files: dict[str, bytes], layouts: dict[int, dict]) -> None:
    """Keep the drawing in `memo` (as filled by render_report, with the AI `layouts` it was made
    with) for the version whose page files, just stored at `prefix`, are `files`."""
    stored = {}
    for name, data in files.items():
        stored.setdefault(hashlib.sha256(data).hexdigest(), name)
    used: dict[str, str] = {}
    key, artwork = memo["artwork"]
    doc = {"key": key, "files": used, "layouts": _encode(layouts, stored, used),
           "artwork": _encode(artwork, stored, used)}
    storage.put(ctx, prefix + FILE, json.dumps(doc).encode(), "application/json")


def _encode(x: Any, stored: dict[str, str], used: dict[str, str]) -> Any:
    if isinstance(x, bytes):
        sha = hashlib.sha256(x).hexdigest()
        if sha not in stored:
            return _DROP
        used[stored[sha]] = sha
        return {"$b": sha}
    if isinstance(x, Markup):
        return {"$m": str(x)}
    if isinstance(x, str | int | float | bool) or x is None:
        return x
    if isinstance(x, tuple | list):
        out = [_encode(v, stored, used) for v in x]
        if any(v is _DROP for v in out):
            raise ValueError("a drawn picture in a list isn't among the page files")
        return {"$t": out} if isinstance(x, tuple) else out
    if isinstance(x, dict):
        items = [(k, _encode(v, stored, used)) for k, v in x.items()]
        items = [(k, v) for k, v in items if v is not _DROP]
        if all(isinstance(k, str) and not k.startswith("$") for k, _ in items):
            return dict(items)
        return {"$d": [[_encode(k, stored, used), v] for k, v in items]}
    raise TypeError(f"can't keep a {type(x).__name__} in the page artwork record")


def _decode(x: Any, blobs: dict[str, bytes]) -> Any:
    if isinstance(x, list):
        return [_decode(v, blobs) for v in x]
    if isinstance(x, dict):
        if len(x) == 1:
            (tag, v), = x.items()
            if tag == "$b":
                return blobs[v]
            if tag == "$m":
                return Markup(v)
            if tag == "$t":
                return tuple(_decode(i, blobs) for i in v)
            if tag == "$d":
                return {_decode(k, blobs): _decode(i, blobs) for k, i in v}
        return {k: _decode(v, blobs) for k, v in x.items()}
    return x
