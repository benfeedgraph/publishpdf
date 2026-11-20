"""Step 1's numbers add up (issue #3): every figure has exactly one status (passed, needs your
attention, has notes), so the headline adds up to the figures extracted; the "Your turn" count,
the check cards and the review list are all worked out from the same open items; and all of
them follow a decision at once and after every re-check.

The end-to-end test runs on the synthetic corpus. Set STEP1_COUNTS_PDFS to a folder of PDFs to
run it on each of those too (e.g. real reports mounted into the test container)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app import validation
from app.validation import Issue
from tests.test_checks import clean
from tests.test_pipeline_e2e import CORPUS, META, _version, admin_client, drain  # noqa: F401 (fixture)

NOT_BULK = ("rendered_page", "schema", "reviewer_flag")


# ------------------------------------------------------------------ the rule itself


def test_each_figure_gets_one_status_however_many_checks_flag_it():
    s = validation.figure_states({"f1", "f2", "f3", "f4", "f5"}, [
        ("f1", "re_extraction", "blocking"), ("f1", "low_confidence", "warning"),   # blocking wins
        ("f2", "low_confidence", "warning"), ("f2", "period_unit", "warning"),      # two notes: one figure
        ("f3", "arithmetic", "blocking"), ("f3", "re_extraction", "blocking"),      # two blocking: one figure
        (None, "period_unit", "warning"), (None, "period_unit", "warning"),         # a table's unit labels
        (None, "rendered_page", "blocking"),                                        # the web page
        ("gone", "reviewer_flag", "blocking"),                                      # a flag on a removed figure
    ])
    assert {k: s[k] for k in ("figures", "passed", "need_attention", "notes")} == \
        {"figures": 5, "passed": 2, "need_attention": 2, "notes": 1}
    assert s["passed"] + s["need_attention"] + s["notes"] == s["figures"]
    assert (s["other_need_attention"], s["other_notes"]) == (2, 2)
    assert s["to_decide"] == 4                       # f1, f3, the web page, the flag
    # the cards count each check's own results: f1 and f3 are both on the re-read card
    assert s["by_check"] == {"re_extraction": {"blocking": 2, "warnings": 0},
                             "low_confidence": {"blocking": 0, "warnings": 2},
                             "period_unit": {"blocking": 0, "warnings": 3},
                             "arithmetic": {"blocking": 1, "warnings": 0},
                             "rendered_page": {"blocking": 1, "warnings": 0},
                             "reviewer_flag": {"blocking": 1, "warnings": 0}}


def test_nothing_open_means_every_figure_passed():
    s = validation.figure_states({"a", "b"}, [])
    assert (s["passed"], s["need_attention"], s["notes"], s["to_decide"], s["by_check"]) == (2, 0, 0, 0, {})


def test_cards_take_their_counts_from_what_is_open():
    agents = [{"key": "re_extraction", "confirmed": 9, "blocking": 3, "warnings": 1},
              {"key": "period_unit", "confirmed": 0, "blocking": 0, "warnings": 2}]
    out = validation.with_open_counts(agents, {"re_extraction": {"blocking": 1, "warnings": 0}})
    assert [(a["confirmed"], a["blocking"], a["warnings"]) for a in out] == [(9, 1, 0), (0, 0, 0)]
    assert validation.with_open_counts(None, {}) is None


def test_states_of_a_run_skip_what_people_already_decided():
    schema = {"figures": {"f1": {"id": "f1"}, "f2": {"id": "f2"}, "f3": {"id": "f3", "status": "not_a_figure"}}}
    issues = [Issue("re_extraction", "blocking", "m", "f1", status="resolved"),
              Issue("low_confidence", "warning", "m", "f2"), Issue("re_extraction", "blocking", "m", "f3")]
    s = validation.figure_states_of(schema, issues)
    assert (s["figures"], s["passed"], s["need_attention"], s["notes"], s["other_need_attention"]) == (2, 1, 0, 1, 1)


def test_not_confirmed_counts_figures_no_check_and_no_person_confirmed():
    s = validation.figure_states({"f1", "f2", "f3"}, [], unconfirmed=["f1", "f2", "gone"], person_confirmed={"f2"})
    assert s["not_confirmed"] == 1                   # f2: a person confirmed it; "gone": no longer a figure
    assert validation.figure_states({"f1"}, [])["not_confirmed"] is None    # a run that didn't record it


def test_a_run_records_the_figures_no_check_confirmed():
    data, schema, artifact = clean()
    f = next(f for f in schema["figures"].values() if f["kind"] not in validation.REEXTRACT_KINDS)
    f["edited"] = True                                 # the trace and the second parser skip it now
    _, summary = validation.run_all(schema, data, artifact, None, threshold=0.95)
    assert summary["unconfirmed_fids"] == [f["id"]]
    assert summary["consensus"]["0"] == 1
    f["review"] = {"action": "confirm", "raw": f["raw"]}
    assert validation.reviewer_confirmed(f)


# ------------------------------------------------------------------ end to end, through the API


def _upload_bytes(client, t, name: str, data: bytes) -> dict:
    r = client.post(f"/api/tenants/{t.id}/reports", files={"file": (name, data, "application/pdf")}, data=META)
    assert r.status_code == 201, r.text
    return r.json()


def _check_counts(client, t, rid, vid, when: str) -> dict:
    """Every number Step 1 shows, checked against the items and figures themselves."""
    base = f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}"
    v = _version(client, t, rid, vid)
    h, s = v["headline"], v["validation"]
    items = client.get(f"{base}/issues", params={"status": "open"}).json()["issues"]
    figures = client.get(f"{base}/schema").json()["figures"]
    active = {k for k, f in figures.items() if f.get("status", "active") == "active"}
    on_fig = {}
    for i in items:
        if i["fid"] in active:
            on_fig.setdefault(i["fid"], set()).add(i["severity"])
    blocking = {f for f, sev in on_fig.items() if "blocking" in sev}
    where = f"{when}: {h}"
    # one status per figure, adding up to the figures extracted
    assert h["passed"] + h["need_attention"] + h["notes"] == h["figures"] == len(active), where
    assert h["need_attention"] == len(blocking) and h["notes"] == len(on_fig) - len(blocking), where
    others = [i for i in items if i["fid"] not in active]
    other_blocking = {i["fid"] or i["id"] for i in others if i["severity"] == "blocking"}
    assert h["other_need_attention"] == len(other_blocking), where
    assert h["other_notes"] == len({i["fid"] or i["id"] for i in others} - other_blocking), where
    # "Your turn": one decision per figure plus one per other item, as the review list groups them
    assert h["to_decide"] == len(blocking) + len(other_blocking), where
    assert (h["to_decide"] > 0) == (v["open_blocking"] > 0), where
    # every card counts that check's open results, so all cards together are the open items
    for a in s["agents"]:
        mine = [i for i in items if i["check"] == a["key"]]
        assert (a["blocking"], a["warnings"]) == (sum(1 for i in mine if i["severity"] == "blocking"),
                                                  sum(1 for i in mine if i["severity"] == "warning")), (where, a)
    assert "unconfirmed_fids" not in s, where         # only the count goes out
    if v["status"] != "processing":                  # the run's own numbers: only final once it's done
        assert h["figures"] == s["figures_checked"], where
        assert sum(s["consensus"].values()) == h["figures"], where
        # nobody confirmed these: never more than the bar's "not confirmed", fewer once people confirm
        confirmed = {k for k, f in figures.items() if validation.reviewer_confirmed(f)}
        assert h["not_confirmed"] is not None and h["not_confirmed"] <= s["consensus"]["0"], where
        assert h["not_confirmed"] >= s["consensus"]["0"] - len(confirmed & active), where
    return {"v": v, "items": items}


def _counts_hold_through_review(client, t, rid, vid):
    base = f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}"
    drain()
    first = _check_counts(client, t, rid, vid, "first run")
    blocking = [i for i in first["items"] if i["severity"] == "blocking" and i["fid"] and i["check"] not in NOT_BULK]
    if not blocking:
        return first["v"]
    # 1. Confirm one blocking figure: it passes at once, and its check's card drops with it.
    one = next((i for i in blocking if i["check"] == "arithmetic"), blocking[0])
    card = lambda v: next(a for a in v["validation"]["agents"] if a["key"] == one["check"])  # noqa: E731
    before = card(first["v"])["blocking"]
    on_it = sum(1 for i in blocking if i["fid"] == one["fid"] and i["check"] == one["check"])
    assert client.post(f"{base}/issues/{one['id']}/resolve", json={"action": "confirm"}).status_code == 200
    mid = _check_counts(client, t, rid, vid, "right after a confirm")
    assert one["fid"] not in {i["fid"] for i in mid["items"] if i["check"] not in NOT_BULK}
    assert card(mid["v"])["blocking"] == before - on_it
    drain()
    after = _check_counts(client, t, rid, vid, "after the re-check")
    assert card(after["v"])["blocking"] == before - on_it
    # 2. Confirm everything else that can be confirmed in bulk (500 at a time, the API's limit;
    #    a figure's other items go with it, so each batch is read from what is still open).
    while rest := [i["id"] for i in client.get(f"{base}/issues", params={"status": "open", "severity": "blocking"}).json()["issues"]
                   if i["check"] not in NOT_BULK][:500]:
        r = client.post(f"{base}/issues/resolve-bulk", json={"issue_ids": rest, "action": "confirm"})
        assert r.status_code == 200, r.text
        _check_counts(client, t, rid, vid, "right after a bulk confirm")
    drain()
    done = _check_counts(client, t, rid, vid, "after the bulk re-check")
    assert all(a["blocking"] == 0 for a in done["v"]["validation"]["agents"] if a["key"] not in NOT_BULK)
    # 3. Re-run every check from the PDF.
    assert client.post(f"{base}/rerun").status_code == 200
    drain()
    return _check_counts(client, t, rid, vid, "after re-running every check")["v"]


@pytest.mark.parametrize("name", ["acme_q2fy26_results_scanned.pdf", "acme_q2fy26_results.pdf",
                                  "acme_q2fy26_broken_text_layer.pdf", "borealis_annual_usd.pdf"])
def test_step_1_counts_add_up_before_and_after_every_decision(admin_client, name):
    client, t, _ = admin_client
    up = _upload_bytes(client, t, name, (CORPUS / name).read_bytes())
    _counts_hold_through_review(client, t, up["report_id"], up["version"]["id"])


_EXTRA = os.environ.get("STEP1_COUNTS_PDFS")


@pytest.mark.skipif(not _EXTRA, reason="set STEP1_COUNTS_PDFS to a folder of PDFs to run this on them")
@pytest.mark.parametrize("path", sorted(Path(_EXTRA).glob("*.pdf")) if _EXTRA else [], ids=lambda p: p.name)
def test_step_1_counts_add_up_on_real_pdfs(admin_client, path):
    client, t, _ = admin_client
    up = _upload_bytes(client, t, path.name, path.read_bytes())
    _counts_hold_through_review(client, t, up["report_id"], up["version"]["id"])
