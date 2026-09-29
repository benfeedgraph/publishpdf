"""Object storage, always tenant-scoped.

Callers pass a tenancy Context and a *relative* key; this module builds
`tenants/{tenant_id}/{key}`. There is no API that accepts a full key, so one
tenant's code path cannot address another tenant's files.
"""

from __future__ import annotations

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
        p.parent.mkdir(parents=True, exist_ok=True)
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
    """S3-compatible backend (AWS S3, MinIO, GCS interop). Bucket must be private."""

    def __init__(self, bucket: str, region: str | None, endpoint_url: str | None) -> None:
        import boto3

        self.bucket = bucket
        self.client = boto3.client("s3", region_name=region, endpoint_url=endpoint_url)

    def put(self, full_key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=full_key, Body=data, ContentType=content_type,
                               ServerSideEncryption="AES256")

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
        return S3Backend(s.s3_bucket, s.s3_region, s.s3_endpoint_url)
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
