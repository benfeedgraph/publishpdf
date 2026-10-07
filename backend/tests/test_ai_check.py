"""AI double-check: estimate before anything runs, confirm-only semantics, real usage
recorded. The network is never touched — a fake transport stands in for Gemini, and
the real one is booby-trapped for the whole module."""

from __future__ import annotations

import json

import pytest

from app import ai_check
from app.config import get_settings
from app.validation import Issue
from tests.test_pipeline_e2e import _version, admin_client, drain, upload  # noqa: F401 (fixture)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(_body):
        raise AssertionError("the real Gemini API must never be called in tests")
    monkeypatch.setattr(ai_check, "gemini_transport", boom)
    cfg = get_settings()
    monkeypatch.setattr(cfg, "gemini_price_in_per_m", 0.30)
    monkeypatch.setattr(cfg, "gemini_price_out_per_m", 2.50)
    monkeypatch.setattr(cfg, "ai_credit_usd", 0.01)
    yield
    ai_check._OVERRIDE.clear()


def fake_gemini(wrong_at: set[int], tokens=(300, 20)):
    """Reads back each crop's value (in request order), except at the positions in
    `wrong_at`, where it reports a different number."""
    calls = []
    seen = [0]

    def transport(body):
        n = sum(1 for p in body["contents"][0]["parts"] if "inline_data" in p)
        raws = fake_gemini.queue[:n]
        del fake_gemini.queue[:n]
        calls.append(n)
        rows = []
        for k, r in enumerate(raws):
            rows.append({"i": k + 1, "text": r + "9" if seen[0] in wrong_at else r})
            seen[0] += 1
        return {"candidates": [{"content": {"parts": [{"text": json.dumps(rows)}]}}],
                "usageMetadata": {"promptTokenCount": tokens[0] * n, "candidatesTokenCount": tokens[1] * n}}
    transport.calls = calls
    return transport


fake_gemini.queue = []


def test_estimate_is_tokens_times_price_in_credits():
    e = ai_check.estimate(144)
    assert e["items"] == 144 and e["requests"] == 12
    assert e["input_tokens"] == 144 * (258 + 8) + 12 * 160 and e["output_tokens"] == 144 * 18
    usd = e["input_tokens"] * 0.30 / 1e6 + e["output_tokens"] * 2.50 / 1e6
    assert e["usd"] == round(usd, 4) and e["credits"] == -(-usd // 0.01)       # rounded up to whole credits
    assert ai_check.estimate(0)["credits"] == 0


def test_ai_can_only_confirm_never_change_a_value():
    schema = {"figures": {
        "a": {"id": "a", "raw": "1,016.41", "kind": "number"},
        "b": {"id": "b", "raw": "46.20", "kind": "number"},       # hidden text says 46.20, page prints 48.20
    }}
    schema["figures"]["a"]["ai_check"] = {"raw": "1,016.41", "read": "1,016.41", "match": True}
    schema["figures"]["b"]["ai_check"] = {"raw": "46.20", "read": "48.20", "match": False}
    issues = [Issue("re_extraction", "blocking", "Disagrees.", "a"), Issue("low_confidence", "blocking", "Low.", "b")]
    ai_check.apply_confirmations(issues, schema)
    assert issues[0].severity == "warning" and "agrees exactly" in issues[0].message
    assert issues[1].severity == "blocking" and "48.20" in issues[1].message
    assert schema["figures"]["b"]["raw"] == "46.20"                         # never rewritten
    # a later edit invalidates the old AI reading
    schema["figures"]["a"]["raw"] = "1,016.14"
    again = [Issue("re_extraction", "blocking", "Disagrees.", "a")]
    ai_check.apply_confirmations(again, schema)
    assert again[0].severity == "blocking"


def test_estimate_run_and_usage_end_to_end(admin_client, monkeypatch):
    client, t, _ = admin_client
    up = upload(client, t, "acme_q2fy26_results_scanned.pdf", period="q4")
    rid, vid = up["report_id"], up["version"]["id"]
    drain()
    url = f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/ai-check"
    base = f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}"

    # without a key: estimate shown, running refused
    monkeypatch.setattr(get_settings(), "gemini_api_key", None)
    st = client.get(url).json()
    assert st["available"] is False and st["estimate"]["items"] > 0
    assert client.post(url, json={"credits_shown": st["estimate"]["credits"]}).status_code == 400

    monkeypatch.setattr(get_settings(), "gemini_api_key", "test-key-not-real")
    st = client.get(url).json()
    est = st["estimate"]
    blocking_before = _version(client, t, rid, vid)["validation"]["blocking"]
    # a stale estimate is refused: nobody pays for something they didn't see
    assert client.post(url, json={"credits_shown": est["credits"] + 5}).status_code == 409

    schema = client.get(f"{base}/schema").json()
    issues = client.get(f"{base}/issues", params={"status": "open"}).json()["issues"]
    fids = ai_check.eligible(issues, schema)
    assert len(fids) == est["items"]
    fake_gemini.queue = [schema["figures"][f]["raw"] for f in fids]
    tr = fake_gemini({0})                                                   # the first one disagrees
    ai_check._OVERRIDE.append(tr)

    assert client.post(url, json={"credits_shown": est["credits"]}).status_code == 202
    drain()
    done = client.get(url).json()["last"]
    assert done["status"] == "succeeded", done
    res = done["result"]
    assert res["items"] == len(fids) and res["confirmed"] == len(fids) - 1 and res["disagreed"] == 1
    assert res["input_tokens"] == 300 * len(fids) and res["output_tokens"] == 20 * len(fids)   # provider's own counts
    assert res["credits"] >= 1 and sum(tr.calls) == len(fids)
    after = _version(client, t, rid, vid)["validation"]["blocking"]
    assert after < blocking_before                                          # confirmed items stopped blocking
    open_now = client.get(f"{base}/issues", params={"status": "open", "severity": "blocking"}).json()["issues"]
    assert any(i["fid"] == fids[0] for i in open_now)                      # the disagreement still needs a person
    audit = client.get(f"/api/tenants/{t.id}/audit").json()["entries"]
    assert {"report.ai_check_requested", "report.ai_check"} <= {e["action"] for e in audit}
    # nothing left to check at these values: the estimate drops to the one disagreement or zero
    assert client.get(url).json()["estimate"]["items"] == 0


def test_vertex_express_keys_go_to_vertex_and_studio_keys_to_the_gemini_api():
    """An "AQ." key (Vertex AI express) sent to generativelanguage.googleapis.com gets
    401/404 — every AI check failed that way. The key decides the address unless
    GEMINI_API says otherwise."""
    from app.ai_check import gemini_url
    assert gemini_url("AQ.Ab8RN6x", "gemini-2.5-flash").startswith(
        "https://aiplatform.googleapis.com/v1/publishers/google/models/gemini-2.5-flash:")
    assert gemini_url("AIzaSyX", "gemini-2.5-flash").startswith(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:")
    assert "aiplatform" in gemini_url("AIzaSyX", "gemini-2.5-flash", "vertex")
    assert "generativelanguage" in gemini_url("AQ.x", "gemini-2.5-flash", "gemini")


def test_a_refusal_shows_googles_reason_without_the_key():
    from app.ai_check import _google_reason

    class R:
        status_code = 403
        def json(self):
            return {"error": {"code": 403, "status": "PERMISSION_DENIED",
                              "message": "Vertex AI API has not been used in project 123 before or it is disabled. key AQ.secret",
                              "details": [{"reason": "SERVICE_DISABLED"}]}}
    out = _google_reason(R(), "AQ.secret")
    assert "PERMISSION_DENIED" in out and "SERVICE_DISABLED" in out and "has not been used in project" in out
    assert "AQ.secret" not in out
