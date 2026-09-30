"""Object storage, always tenant-scoped.

Callers pass a tenancy Context and a *relative* key; this module builds
`tenants/{tenant_id}/{key}`. There is no API that accepts a full key, so one
tenant's code path cannot address another tenant's files.
"""

from __future__ import annotations

import errno
import hashlib
import re
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from app.config import get_settings
from app.tenancy import Context

_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]{0,400}$")


class StorageError(Exception):
    pass


def scoped_key(ctx: Context, key: str) -> str:
    tenant_id = ctx.require_tenant()
    if not _SAFE_KEY.match(key) or ".." in key.split("/") or "//" in key:
        raise StorageError(f"invalid storage key: {key!r}")
    return f"tenants/{tenant_id}/{key}"


class Backend(Protocol):
    def put(self, full_key: str, data: bytes, content_type: str) -> None: ...
    def get(self, full_key: str) -> bytes: ...
    def exists(self, full_key: str) -> bool: ...
    def delete(self, full_key: str) -> None: ...


class LocalBackend:
    """Filesystem backend for development and tests."""

    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()

    def _path(self, full_key: str) -> Path:
        p = (self.root / full_key).resolve()
        if self.root not in p.parents:
            raise StorageError("path escapes storage root")
        return p

    def put(self, full_key: str, data: bytes, content_type: str) -> None:
        p = self._path(full_key)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            if e.errno == errno.EROFS:      # serverless hosts (Vercel, Lambda) have no writable disk
                raise StorageError("File storage isn't set up on this server: its disk is read-only. Set "
                                   "STORAGE_BACKEND=s3 with an S3-compatible bucket.") from e
            raise
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)

    def get(self, full_key: str) -> bytes:
        try:
            return self._path(full_key).read_bytes()
        except FileNotFoundError as e:
            raise StorageError("not found") from e

    def exists(self, full_key: str) -> bool:
        return self._path(full_key).exists()

    def delete(self, full_key: str) -> None:
        self._path(full_key).unlink(missing_ok=True)


class S3Backend:
    """S3-compatible backend (AWS S3, Cloudflare R2, Railway buckets, MinIO). Bucket must be private."""

    def __init__(self, bucket: str, region: str | None, endpoint_url: str | None, *, access_key: str | None = None,
                 secret_key: str | None = None, sse: str | None = None, path_style: bool = False) -> None:
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.client = boto3.client(
            "s3", region_name=region or ("auto" if endpoint_url else None), endpoint_url=endpoint_url,
            aws_access_key_id=access_key, aws_secret_access_key=secret_key,
            config=Config(s3={"addressing_style": "path" if path_style else "auto"},
                          retries={"max_attempts": 3, "mode": "standard"}))
        self.extra = {"ServerSideEncryption": sse} if sse else {}

    def put(self, full_key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=full_key, Body=data, ContentType=content_type, **self.extra)

    def get(self, full_key: str) -> bytes:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=full_key)["Body"].read()
        except self.client.exceptions.NoSuchKey as e:
            raise StorageError("not found") from e

    def exists(self, full_key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=full_key)
            return True
        except Exception:
            return False

    def delete(self, full_key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=full_key)


@lru_cache
def _backend() -> Backend:
    s = get_settings()
    if s.storage_backend == "s3":
        if not s.s3_bucket:
            raise StorageError("S3_BUCKET is required when STORAGE_BACKEND=s3")
        endpoint, region = s.s3_endpoint_url or None, s.s3_region or None
        sse = s.s3_sse if s.s3_sse else (None if endpoint else "AES256")
        return S3Backend(s.s3_bucket, region, endpoint, access_key=s.s3_access_key_id,
                         secret_key=s.s3_secret_access_key, sse=sse or None, path_style=s.s3_path_style)
    return LocalBackend(s.storage_local_root)


def put(ctx: Context, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Store bytes; returns the sha256 hex digest of the content."""
    _backend().put(scoped_key(ctx, key), data, content_type)
    return hashlib.sha256(data).hexdigest()


def get(ctx: Context, key: str) -> bytes:
    return _backend().get(scoped_key(ctx, key))


def exists(ctx: Context, key: str) -> bool:
    return _backend().exists(scoped_key(ctx, key))


def delete(ctx: Context, key: str) -> None:
    _backend().delete(scoped_key(ctx, key))
