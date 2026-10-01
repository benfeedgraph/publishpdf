"""Storage and email behaviour that only shows up on real hosts."""

import errno

import pytest

from app import emailer, storage


def test_read_only_disk_says_what_to_configure(tmp_path, monkeypatch):
    backend = storage.LocalBackend(str(tmp_path))

    def ro(*a, **k):
        raise OSError(errno.EROFS, "Read-only file system")
    monkeypatch.setattr(storage.Path, "mkdir", ro)
    with pytest.raises(storage.StorageError, match="STORAGE_BACKEND=s3"):
        backend.put("t/x.pdf", b"x", "application/pdf")


def _s3_env(monkeypatch, **env):
    """Settings from these variables only: the developer's .env file must not leak in."""
    from app.config import Settings
    for k in ("S3_SSE", "S3_ENDPOINT_URL", "S3_REGION", "S3_BUCKET", "BUCKET", "ENDPOINT", "REGION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(storage, "get_settings", lambda: Settings(_env_file=None))
    storage._backend.cache_clear()


@pytest.fixture
def fake_boto(monkeypatch):
    calls = {}

    class Client:
        def put_object(self, **kw):
            calls["put"] = kw

    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **kw: calls.update(client=kw) or Client())
    yield calls
    storage._backend.cache_clear()


def test_s3_compatible_bucket_without_sse_header(monkeypatch, fake_boto):
    """Railway's bucket variable names work, and no SSE header goes to a non-AWS endpoint."""
    _s3_env(monkeypatch, BUCKET="b", ENDPOINT="https://storage.example", ACCESS_KEY_ID="k", SECRET_ACCESS_KEY="s")
    storage._backend().put("a/b", b"x", "text/plain")
    assert fake_boto["client"]["endpoint_url"] == "https://storage.example"
    assert fake_boto["client"]["aws_access_key_id"] == "k" and fake_boto["client"]["region_name"] == "auto"
    assert "ServerSideEncryption" not in fake_boto["put"] and fake_boto["put"]["Bucket"] == "b"


def test_aws_s3_keeps_server_side_encryption(monkeypatch, fake_boto):
    _s3_env(monkeypatch, S3_BUCKET="b", S3_REGION="us-east-1")
    storage._backend().put("a/b", b"x", "text/plain")
    assert fake_boto["put"]["ServerSideEncryption"] == "AES256"


def test_sign_in_email_does_not_wait_for_smtp(monkeypatch):
    sent, started = [], []
    monkeypatch.setattr(emailer, "send", lambda *a: sent.append(a))
    monkeypatch.setattr(emailer.threading, "Thread", lambda target, **k: type("T", (), {"start": lambda self: started.append(target)})())
    monkeypatch.setattr(emailer, "get_settings", lambda: type("S", (), {"email_backend": "smtp"})())
    monkeypatch.delenv("VERCEL", raising=False)
    emailer.send_soon("a@b.c", "s", "b")
    assert started and not sent                          # handed to a thread, request not blocked
    monkeypatch.setenv("VERCEL", "1")
    emailer.send_soon("a@b.c", "s", "b")
    assert sent                                          # serverless: sent before the response


# ------------------------------------------------------------------ Vercel Blob

def _fake_blob_store():
    """Vercel Blob's HTTP API, in memory: the same endpoints the SDK uses."""
    import httpx
    from urllib.parse import unquote, urlparse
    files: dict[str, tuple[bytes, str]] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        host, path = req.url.host, req.url.path
        if host == "vercel.com":
            assert req.headers["authorization"].startswith("Bearer vercel_blob_rw_")
            assert req.headers["x-api-version"] == "12"
            if req.method == "PUT":
                assert req.headers["x-vercel-blob-access"] == "private"
                files[req.url.params["pathname"]] = (req.content, req.headers["x-content-type"])
                return httpx.Response(200, json={"pathname": req.url.params["pathname"]})
            if path.endswith("/delete"):
                import json
                for u in json.loads(req.content)["urls"]:
                    files.pop(unquote(urlparse(u).path.lstrip("/")), None)
                return httpx.Response(200, json={})
            key = unquote(urlparse(req.url.params["url"]).path.lstrip("/"))
            return httpx.Response(200, json={}) if key in files else httpx.Response(404, json={"error": {"code": "not_found"}})
        assert host == "store1.private.blob.vercel-storage.com" and req.url.params["cache"] == "0"
        key = unquote(path.lstrip("/"))
        return httpx.Response(200, content=files[key][0]) if key in files else httpx.Response(404)

    backend = storage.VercelBlobBackend("vercel_blob_rw_STORE1_" + "s" * 30)
    backend.http = httpx.Client(transport=httpx.MockTransport(handler))
    return backend, files


def test_vercel_blob_round_trip():
    backend, files = _fake_blob_store()
    backend.put("tenants/t/a b.webp", b"img", "image/webp")
    assert files["tenants/t/a b.webp"] == (b"img", "image/webp")
    assert backend.get("tenants/t/a b.webp") == b"img" and backend.exists("tenants/t/a b.webp")
    backend.delete("tenants/t/a b.webp")
    assert not backend.exists("tenants/t/a b.webp")
    with pytest.raises(storage.StorageError):
        backend.get("tenants/t/a b.webp")


def test_blob_is_picked_when_a_store_is_connected(monkeypatch):
    from app.config import Settings
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "vercel_blob_rw_STORE1_" + "s" * 30)
    monkeypatch.setattr(storage, "get_settings", lambda: Settings(_env_file=None))
    storage._backend.cache_clear()
    try:
        assert isinstance(storage._backend(), storage.VercelBlobBackend)
    finally:
        storage._backend.cache_clear()


def test_large_pdf_goes_straight_to_blob_then_inspect(client, monkeypatch):
    """The browser gets a token for one key in its own tenant, uploads there directly,
    and /inspect picks the file up from storage (it never passes through the API)."""
    import base64
    import json
    from pathlib import Path

    from app.tenancy import Role
    from tests.conftest import add_member, make_tenant, make_user, sign_in
    backend, files = _fake_blob_store()
    monkeypatch.setattr(storage, "_backend", lambda: backend)
    t, u = make_tenant(), make_user()
    add_member(t, u, Role.client_admin)
    sign_in(client, u)
    r = client.post(f"/api/tenants/{t.id}/reports/upload-url", json={"filename": "acme.pdf", "size_bytes": 1000})
    dest = r.json()
    assert dest["mode"] == "direct" and dest["pathname"] == f"tenants/{t.id}/{dest['key']}"
    sig_payload = base64.b64decode(dest["token"].split("_", 4)[4]).decode()
    claims = json.loads(base64.b64decode(sig_payload.split(".", 1)[1]))
    assert claims["pathname"] == dest["pathname"] and claims["allowedContentTypes"] == ["application/pdf"]

    pdf = (Path(__file__).resolve().parents[2] / "corpus" / "synthetic" / "acme_q2fy26_results.pdf").read_bytes()
    files[dest["pathname"]] = (pdf, "application/pdf")           # what the browser's upload did
    r = client.post(f"/api/tenants/{t.id}/reports/inspect", data={"incoming": dest["key"], "filename": "acme.pdf"})
    assert r.status_code == 200, r.text
    assert r.json()["filename"] == "acme.pdf" and r.json()["detected"]["company_name"] == "Acme Industries Limited"
    assert dest["pathname"] not in files                             # the incoming copy is cleaned up
    assert any(k.startswith(f"tenants/{t.id}/sources/") for k in files)

    # A key outside the incoming/ shape (e.g. another tenant's path) is refused outright.
    r = client.post(f"/api/tenants/{t.id}/reports/inspect", data={"incoming": "../x/sources/a.pdf"})
    assert r.status_code == 400
    too_big = client.post(f"/api/tenants/{t.id}/reports/upload-url", json={"filename": "x.pdf", "size_bytes": 10 ** 12})
    assert too_big.status_code == 413


def test_form_upload_when_storage_takes_no_direct_uploads(client):
    from app.tenancy import Role
    from tests.conftest import add_member, make_tenant, make_user, sign_in
    t, u = make_tenant(), make_user()
    add_member(t, u, Role.client_admin)
    sign_in(client, u)
    assert client.post(f"/api/tenants/{t.id}/reports/upload-url", json={"filename": "a.pdf", "size_bytes": 10}).json() == {"mode": "form"}
