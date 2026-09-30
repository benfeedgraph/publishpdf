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
