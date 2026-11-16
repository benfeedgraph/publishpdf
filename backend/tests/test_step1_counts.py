"""Step 1's counts add up (issue #3): one status per figure in the headline, items that aren't
about one figure counted on their own, agent cards that follow people's decisions, and
"Every figure matches the PDF" only when every figure was confirmed."""

from __future__ import annotations

from app import validation
from app.validation import Issue
from tests.test_checks import clean
from tests.test_pipeline_e2e import _version, admin_client, drain, upload  # noqa: F401 (fixture)


def _adds_up(h: dict) -> bool:
    return h["passed"] + h["need_attention"] + h["notes"] == h["figures"]


def test_headline_gives_each_figure_one_status():
    h = validation.figure_headline(10, [
        ("f1", "blocking"), ("f1", "warning"),        # blocking wins; counted once
        ("f2", "warning"), ("f2", "warning"),         # two notes on one figure: one figure
        ("f3", "blocking"),
        (None, "warning"), (None, "warning"),         # a table's unit label, a page's count
        (None, "blocking"),                           # the web page
    ])
    assert h == {"figures": 10, "passed": 7, "need_attention": 2, "notes": 1,
                 "other_need_attention": 1, "other_notes": 2}
    assert _adds_up(h)


def test_cards_count_only_items_still_open():
    agents = [{"key": "re_extraction", "blocking": 3, "warnings": 1}, {"key": "period_unit", "blocking": 0, "warnings": 2}]
    issues = [Issue("re_extraction", "blocking", "m", "f1", status="resolved"), Issue("re_extraction", "blocking", "m", "f2"),
              Issue("re_extraction", "warning", "m", "f3"), Issue("period_unit", "warning", "m", None, status="resolved")]
    out = validation.recount_agents(agents, issues)
    assert [(a["blocking"], a["warnings"]) for a in out] == [(1, 1), (0, 0)]
    assert validation.recount_agents(None, issues) is None


def test_not_confirmed_counts_figures_no_check_and_no_person_confirmed():
    data, schema, artifact = clean()
    f = next(f for f in schema["figures"].values() if f["kind"] not in validation.REEXTRACT_KINDS)
    f["edited"] = True                                 # the trace and the second parser skip it now
    _, summary = validation.run_all(schema, data, artifact, None, threshold=0.95)
    assert summary["unconfirmed_fids"] == [f["id"]]
    assert validation.count_not_confirmed(schema, summary["unconfirmed_fids"]) == 1
    f["review"] = {"action": "confirm", "raw": f["raw"]}   # a person confirmed it
    assert validation.count_not_confirmed(schema, summary["unconfirmed_fids"]) == 0
    assert validation.count_not_confirmed(schema, None) is None


def test_step_1_counts_add_up_before_during_and_after_review(admin_client):
    client, t, _ = admin_client
    up = upload(client, t, "acme_q2fy26_results_scanned.pdf", period="q1")
    rid, vid = up["report_id"], up["version"]["id"]
    base = f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}"
    drain()
    v = _version(client, t, rid, vid)
    h, s = v["headline"], v["validation"]
    assert h["figures"] == s["figures_checked"] and _adds_up(h)
    assert sum(s["consensus"].values()) == s["figures_checked"]
    open_items = [i for i in client.get(f"{base}/issues", params={"status": "open"}).json()["issues"]]
    assert h["need_attention"] == len({i["fid"] for i in open_items if i["fid"] and i["severity"] == "blocking"})
    assert h["other_notes"] == sum(1 for i in open_items if not i["fid"] and i["severity"] == "warning")
    blocking = [i for i in open_items if i["severity"] == "blocking" and i["fid"]]
    assert len(blocking) >= 2, "the scanned sample should have figures to decide"

    # A decision counts at once, and the counts still add up before the re-check finishes.
    assert client.post(f"{base}/issues/{blocking[0]['id']}/resolve", json={"action": "confirm"}).status_code == 200
    mid = _version(client, t, rid, vid)["headline"]
    assert _adds_up(mid) and mid["need_attention"] == h["need_attention"] - 1 and mid["passed"] >= h["passed"]
    drain()

    # Confirm everything left: the cards follow (no "8 blocking" next to "0 need your attention"),
    # and every figure is confirmed by a check or a person, so "Every figure matches the PDF" holds.
    rest = [i["id"] for i in client.get(f"{base}/issues", params={"status": "open", "severity": "blocking"}).json()["issues"]
            if i["check"] not in ("rendered_page", "schema", "reviewer_flag")]
    assert client.post(f"{base}/issues/resolve-bulk", json={"issue_ids": rest, "action": "confirm"}).status_code == 200
    drain()
    v = _version(client, t, rid, vid)
    h, s = v["headline"], v["validation"]
    assert v["open_blocking"] == 0 and h["need_attention"] == 0 and _adds_up(h)
    assert all(a["blocking"] == 0 for a in s["agents"])
    assert s["not_confirmed"] == 0
    assert sum(s["consensus"].values()) == s["figures_checked"]
