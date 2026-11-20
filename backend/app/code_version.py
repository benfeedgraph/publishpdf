"""A fingerprint of this build of the app: every file of the app package (code, templates, fonts)
and the locked dependencies. Work stored by one build — the drawn pages of a report, the
findings of the checks — is reused only by the same build; any update starts afresh."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def code_version() -> str:
    root = Path(__file__).parent
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            h.update(str(p.relative_to(root)).encode())
            h.update(b"\0")
            h.update(p.read_bytes())
    lock = root.parent / "uv.lock"
    if lock.is_file():
        h.update(lock.read_bytes())
    return h.hexdigest()[:16]
