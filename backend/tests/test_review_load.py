"""Keeping human review to what needs a human: evidence-based clearing of OCR noise and
low-confidence reads that independent agents already agree on, the checker fixes found on
a 404-page annual report, and confirming many reviewed values in one step."""

from __future__ import annotations

from app import validation
from app.extraction.tokens import looks_numeric_unparsed
from tests.test_pipeline_e2e import _version, admin_client, drain, upload  # noqa: F401 (fixture)


def _fig(fid, raw, *, method="ocr", conf=0.9, kind="number", **kw):
    return {"id": fid, "raw": raw, "kind": kind, "method": method, "confidence": conf, "status": "active",
            "source": {"page": 1, "bbox": [0, 0, 1, 1]}, **kw}


def test_ocr_noise_is_not_a_contradiction_but_different_digits_are():
    noise = [("129", "1129"), ("31", "131,"), ("22/07/2022", "2210712022"), ("06-Oct-2035", "-06-0-2035"),
             ("45", "s"), ("51%", ""), ("2014", "12014.")]
    for raw, read in noise:
        assert not validation.ocr_contradicts(raw, read), (raw, read)
    real = [("23.53", "23.93"), ("5", "9"), ("46.20", "48.20"), ("2,576", "2,076"), ("12", "2"), ("126", "128")]
    for raw, read in real:
        assert validation.ocr_contradicts(raw, read), (raw, read)


def test_low_confidence_read_is_cleared_only_when_an_independent_reread_agrees():
    schema = {"figures": {f["id"]: f for f in [
        _fig("a", "1,016.41", conf=0.91),            # re-read agreed      -> warning
        _fig("b", "338", conf=0.69),                 # re-read agreed      -> warning
        _fig("c", "8", conf=0.23),                   # agreed, but a guess -> still a person
        _fig("d", "1,171.55", conf=0.77),            # re-read disagreed   -> still a person
        _fig("e", "2005-2018", kind="identifier", unparsed_numeric=True),   # a tenure, not a number
    ]}}
    issues = {i.fid: i.severity for i in validation.check_low_confidence(schema, 0.95, reread_ok={"a", "b", "c"})}
    assert issues == {"a": "warning", "b": "warning", "c": "blocking", "d": "blocking"}


def test_year_ranges_are_not_unreadable_numbers():
    assert not looks_numeric_unparsed("2005-2018") and not looks_numeric_unparsed("2024-25")
    assert looks_numeric_unparsed("55-00")


def test_checker_fixes_from_the_annual_report():
    # a value printed with a footnote mark is its own figure; the markdown check accepts it
    schema = {"figures": {"x": {"raw": "(38.19)*"}}, "metadata": {}}
    assert validation.check_markdown(schema, "| Provision | (38.19)* |") == []
    # a non-breaking hyphen or a stray control byte isn't a different value
    assert validation._same_glyphs("IEPF‑5") == "IEPF-5" and validation._same_glyphs("\x072") == "2"


def test_bulk_confirm_resolves_many_issues_with_one_recheck(admin_client):
    client, t, _ = admin_client
    up = upload(client, t, "acme_q2fy26_results_scanned.pdf", period="q2")
    rid, vid = up["report_id"], up["version"]["id"]
    drain()
    base = f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}"
    issues = [i for i in client.get(f"{base}/issues", params={"status": "open"}).json()["issues"]
              if i["fid"] and i["check"] not in ("rendered_page", "schema")]
    assert len(issues) >= 2
    pick = issues[:2]
    r = client.post(f"{base}/issues/resolve-bulk", json={"issue_ids": [i["id"] for i in pick], "action": "confirm"})
    assert r.status_code == 200, r.text and r.json()["resolved"] == 2
    # each figure is on the review trail individually
    trail = client.get(f"{base}/reviews").json()["reviews"]
    assert {x["fid"] for x in trail} >= {i["fid"] for i in pick}
    # already-resolved issues can't be resolved again, edits aren't a bulk action
    assert client.post(f"{base}/issues/resolve-bulk", json={"issue_ids": [pick[0]["id"]], "action": "confirm"}).status_code == 409
    assert client.post(f"{base}/issues/resolve-bulk", json={"issue_ids": [issues[-1]["id"]], "action": "edit"}).status_code == 422
    drain()
    v = _version(client, t, rid, vid)
    assert v["status"] in ("needs_review", "validation_issues")
    now = client.get(f"{base}/issues").json()["issues"]
    for i in pick:
        assert all(x["status"] == "resolved" for x in now if x["fid"] == i["fid"] and x["check"] == i["check"])
