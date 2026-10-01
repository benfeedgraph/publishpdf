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
                raise StorageError("File storage isn't set up on this server: its disk is read-only and no "
                                   "BLOB_READ_WRITE_TOKEN is set. Copy the read-write token from your Vercel "
                                   "Blob store into this project's environment variables, then redeploy.") from e
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


class VercelBlobBackend:
    """Vercel Blob, private store: every read needs the store token, nothing is public.

    Speaks the same HTTP API as Vercel's own SDK (@vercel/blob, API version 12), so the
    worker can run anywhere (e.g. next to the database) with just the token."""

    API = "https://vercel.com/api/blob"
    API_VERSION = "12"

    def __init__(self, token: str) -> None:
        import httpx

        parts = token.split("_")
        if not token.startswith("vercel_blob_rw_") or len(parts) < 5:
            raise StorageError("BLOB_READ_WRITE_TOKEN isn't a Vercel Blob read-write token")
        self.token, self.store_id = token, parts[3]
        self.http = httpx.Client(timeout=httpx.Timeout(120, connect=15), follow_redirects=True,
                                 transport=httpx.HTTPTransport(retries=3))

    def _headers(self, **extra: str) -> dict[str, str]:
        return {"authorization": f"Bearer {self.token}", "x-api-version": self.API_VERSION,
                "x-vercel-blob-store-id": self.store_id, **extra}

    def url(self, full_key: str) -> str:
        from urllib.parse import quote
        return f"https://{self.store_id}.private.blob.vercel-storage.com/{quote(full_key)}"

    def _check(self, r, what: str) -> None:
        if r.status_code >= 400:
            try:
                detail = r.json().get("error", {}).get("message", "")
            except ValueError:
                detail = ""
            raise StorageError(f"Vercel Blob {what} failed ({r.status_code}) {detail}".strip())

    def put(self, full_key: str, data: bytes, content_type: str) -> None:
        r = self.http.put(self.API + "/", params={"pathname": full_key}, content=data, headers=self._headers(**{
            "x-vercel-blob-access": "private", "x-add-random-suffix": "0", "x-allow-overwrite": "1",
            "x-content-type": content_type, "x-cache-control-max-age": "60"}))
        self._check(r, "upload")

    def get(self, full_key: str) -> bytes:
        # cache=0: a re-rendered file keeps its name, so a cached copy could be stale.
        r = self.http.get(self.url(full_key), params={"cache": "0"}, headers={"authorization": f"Bearer {self.token}"})
        if r.status_code == 404:
            raise StorageError("not found")
        self._check(r, "download")
        return r.content

    def exists(self, full_key: str) -> bool:
        r = self.http.get(self.API, params={"url": self.url(full_key)}, headers=self._headers())
        if r.status_code == 404:
            return False
        self._check(r, "lookup")
        return True

    def delete(self, full_key: str) -> None:
        r = self.http.post(self.API + "/delete", json={"urls": [self.url(full_key)]}, headers=self._headers())
        if r.status_code != 404:
            self._check(r, "delete")

    def client_upload_token(self, full_key: str, *, max_bytes: int, valid_seconds: int = 900) -> str:
        """A token the BROWSER uploads with, straight to the store: large PDFs never pass
        through a size-capped function. It allows one PDF at exactly this key, until it
        expires. Same format as @vercel/blob's generateClientTokenFromReadWriteToken."""
        import base64
        import hmac
        import json
        import time
        payload = base64.b64encode(json.dumps({
            "pathname": full_key, "allowedContentTypes": ["application/pdf"], "maximumSizeInBytes": max_bytes,
            "addRandomSuffix": False, "allowOverwrite": True, "validUntil": int((time.time() + valid_seconds) * 1000),
        }, separators=(",", ":")).encode()).decode()
        signature = hmac.new(self.token.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return f"vercel_blob_client_{self.store_id}_" + base64.b64encode(f"{signature}.{payload}".encode()).decode()


def _serverless() -> bool:
    import os
    return bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def backend_kind() -> str:
    """Which store is in use. A serverless host has no writable disk, so "local" there
    (e.g. STORAGE_BACKEND=local copied from a development .env) yields to a connected
    Vercel Blob store instead of failing every upload."""
    s = get_settings()
    kind = (s.storage_backend or "").strip().lower()
    if kind in ("", "local") and s.blob_read_write_token and (not kind or _serverless()):
        return "vercel_blob"
    return kind or "local"


@lru_cache
def _backend() -> Backend:
    s = get_settings()
    kind = backend_kind()
    if kind == "vercel_blob":
        if not s.blob_read_write_token:
            raise StorageError("BLOB_READ_WRITE_TOKEN is required when STORAGE_BACKEND=vercel_blob "
                               "(connect a Blob store to the project in Vercel)")
        return VercelBlobBackend(s.blob_read_write_token)
    if kind == "s3":
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


def direct_upload_token(ctx: Context, key: str, max_bytes: int) -> str | None:
    """A browser upload token for `key`, when the store takes uploads straight from the
    browser (Vercel Blob). None: the file goes through the API as a normal form upload."""
    backend = _backend()
    if not isinstance(backend, VercelBlobBackend):
        return None
    return backend.client_upload_token(scoped_key(ctx, key), max_bytes=max_bytes)
