"""Step 1's review list and its filters (issue #5): a filter never offers a value that shows
nothing, "All checks" includes page notes such as Completeness so its count matches the
headline, decided items stop feeding the filters, and section names are headings, not OCR
fragments."""

from pathlib import Path

import pytest

from app import issue_filters
from app.issue_filters import section_labels, usable_heading
from app.validation import figure_states

CORPUS = Path(__file__).resolve().parents[2] / "corpus" / "synthetic"


def _i(n, check, severity, page, *, fid=None, bbox=None, status="open", section=None):
    """bbox: None means "at its figure" (a spot when there is a figure, none otherwise)."""
    spot = bool(fid) if bbox is None else bbox
    return {"id": f"i{n}", "check": check, "severity": severity, "page": page, "fid": fid,
            "bbox": [1, 2, 3, 4] if spot else None, "status": status, "section_id": section}


def build(issues, filters, *, figure_ids=None, **kw):
    """issue_filters.build with every figure the items name still in the document, unless
    the test says which are."""
    if figure_ids is None:
        figure_ids = {i["fid"] for i in issues if i["fid"]}
    return issue_filters.build(issues, filters, figure_ids=figure_ids, **kw)


# ITC JP Morgan, as on main: 2 blocking figures (p8, p25), 33 Period & unit notes on figures
# (p11), 4 Completeness notes with a page but no figure (p5, p20, p26, p28).
JPM = ([_i(1, "second_parser", "blocking", 8, fid="f1", section="s24"), _i(2, "second_parser", "blocking", 25, fid="f2", section="s30")]
       + [_i(10 + k, "period_unit", "warning", 11, fid=f"p{k}", section="s11") for k in range(33)]
       + [_i(60 + k, "completeness", "warning", p, bbox=False) for k, p in enumerate((5, 20, 26, 28))])


def _opts(out, facet):
    return {o["value"]: o["count"] for o in out["options"][facet]}


def test_page_filter_offers_only_pages_with_items_under_the_other_filters():
    out = build(JPM, {"severity": "blocking", "status": "open"})
    assert _opts(out, "page") == {"8": 1, "25": 1}           # not 5, 11, 20, 26, 28: nothing blocking there
    assert _opts(out, "check") == {"second_parser": 2}
    out = build(JPM, {"severity": None, "status": "open"})
    assert _opts(out, "page") == {"5": 1, "8": 1, "11": 33, "20": 1, "25": 1, "26": 1, "28": 1}
    # every option shows exactly as many items as it says
    for p, n in _opts(out, "page").items():
        assert build(JPM, {"status": "open", "page": p})["totals"]["rows"] == n > 0


def test_all_checks_includes_page_notes_so_it_matches_the_headline():
    out = build(JPM, {"severity": "warning", "status": "open"})
    headline_notes = sum(1 for i in JPM if i["severity"] == "warning" and i["status"] == "open")
    assert out["totals"] == {"rows": 37, "figures": 33, "notes": 4, "all_rows": 39}
    assert out["totals"]["rows"] == headline_notes                 # 37 notes, not 33
    assert {i["check"] for i in out["items"]} == {"period_unit", "completeness"}
    # the screen's side by side list is the items marked decidable: the 33 figures, not the notes
    assert {i["check"]: i["decidable"] for i in out["items"]} == {"period_unit": True, "completeness": False}


def test_only_an_open_item_on_a_figure_in_the_document_is_decidable_side_by_side():
    issues = [_i(1, "re_extraction", "blocking", 4, fid="f1"),
              _i(2, "re_extraction", "blocking", 4, fid="f2", status="resolved"),   # already decided
              _i(3, "re_extraction", "blocking", 4, fid="f3", bbox=False),          # a figure, even without a box
              _i(4, "reviewer_flag", "blocking", 4, fid="f4"),                      # a colleague's flag
              _i(5, "rendered_page", "blocking", None, fid="f5"),                   # a page-build fault
              _i(6, "completeness", "warning", 4),                                  # a page note
              _i(7, "period_unit", "warning", 4, bbox=True),                        # a whole table's note
              _i(8, "re_extraction", "warning", 4, fid="gone")]                     # marked "not a figure"
    out = build(issues, {}, figure_ids={"f1", "f2", "f3", "f4", "f5"})
    assert [i["id"] for i in out["items"] if i["decidable"]] == ["i1", "i3"]


def test_a_tables_unit_note_is_a_note_not_a_figure():
    """A Period & unit note about a whole table ("This table's numbers don't line up in rows
    and columns…") has a page and a box but no figure: nothing to confirm side by side, and
    the headline counts it as another item, not a figure."""
    issues = [_i(1, "period_unit", "warning", 3, fid="f1"),
              _i(2, "period_unit", "warning", 3, bbox=True), _i(3, "period_unit", "warning", 7, bbox=True)]
    out = build(issues, {"status": "open"})
    assert out["totals"] == {"rows": 3, "figures": 1, "notes": 2, "all_rows": 3}
    assert {i["id"]: i["decidable"] for i in out["items"]} == {"i1": True, "i2": False, "i3": False}


# Every kind of item Step 1 can hold: figures flagged once and twice, blocking and notes, a
# figure since marked "not a figure", table and page notes, a colleague's flag, a page fault,
# and items already decided.
MIXED = [_i(1, "re_extraction", "blocking", 2, fid="f1"), _i(2, "low_confidence", "warning", 2, fid="f1"),
         _i(3, "period_unit", "warning", 2, fid="f2"), _i(4, "second_parser", "blocking", 3, fid="f3", bbox=False),
         _i(5, "reviewer_flag", "blocking", 3, fid="f4"), _i(6, "rendered_page", "blocking", None, fid="f5"),
         _i(7, "re_extraction", "blocking", 4, fid="gone1"), _i(8, "period_unit", "warning", 4, fid="gone2"),
         _i(9, "period_unit", "warning", 5, bbox=True), _i(10, "period_unit", "blocking", 5, bbox=True),
         _i(11, "completeness", "warning", 6), _i(12, "rendered_page", "blocking", None),
         _i(13, "re_extraction", "blocking", 2, fid="f6", status="resolved"),
         _i(14, "completeness", "warning", 6, status="resolved")]
IN_DOCUMENT = {"f1", "f2", "f3", "f4", "f5", "f6", "f7"}


@pytest.mark.parametrize("severity", [None, "blocking", "warning"])
def test_the_lists_figures_and_notes_add_up_to_the_headline(severity):
    """The same items, counted by the list and by the headline: the list's figures are the
    figures that need attention or have notes, and its notes are the other items."""
    mine = [i for i in MIXED if not severity or i["severity"] == severity]
    h = figure_states(IN_DOCUMENT, [(i["fid"], i["check"], i["severity"]) for i in mine if i["status"] == "open"])
    out = build(MIXED, {"status": "open", "severity": severity}, figure_ids=IN_DOCUMENT)
    assert out["totals"]["figures"] == h["need_attention"] + h["notes"]
    assert out["totals"]["notes"] == h["other_need_attention"] + h["other_notes"]
    assert out["totals"]["rows"] == out["totals"]["figures"] + out["totals"]["notes"]
    # every item the screen shows side by side is on one of the headline's figures
    assert {i["fid"] for i in out["items"] if i["decidable"]} <= IN_DOCUMENT
    if severity is None:
        assert (h["need_attention"], h["notes"], h["other_need_attention"], h["other_notes"]) == (4, 1, 3, 3)
        assert out["totals"] == {"rows": 11, "figures": 5, "notes": 6, "all_rows": 13}


def test_a_decided_item_stops_feeding_the_filters():
    issues = [_i(1, "re_extraction", "blocking", 5, fid="f1", section="s21", status="resolved"),
              _i(2, "completeness", "warning", 5), _i(3, "re_extraction", "blocking", 9, fid="f2", section="s30")]
    out = build(issues, {"severity": "blocking", "status": "open"})
    assert _opts(out, "page") == {"9": 1} and _opts(out, "section") == {}   # no "s21": its item was confirmed
    # the current choice is kept with 0, so the screen can put the filter back to "All"
    out = build(issues, {"severity": "blocking", "status": "open", "page": "5"})
    assert _opts(out, "page") == {"5": 0, "9": 1} and out["totals"]["rows"] == 0
    out = build(issues, {"severity": "blocking", "status": "open", "section": "s21"},
                sections=[{"id": "s21", "heading_text": "Compliance", "source": {"page": 3}}])
    assert [(o["value"], o["count"]) for o in out["options"]["section"]] == [("s21", 0)]
    assert out["totals"]["rows"] == 0


def test_one_figure_flagged_by_two_checks_is_one_row():
    issues = [_i(1, "re_extraction", "blocking", 4, fid="f1"), _i(2, "low_confidence", "blocking", 4, fid="f1")]
    out = build(issues, {"status": "open"})
    assert out["totals"]["rows"] == 1 and _opts(out, "page") == {"4": 1}
    assert _opts(out, "check") == {"low_confidence": 1, "re_extraction": 1}


def test_labels_come_from_the_api():
    out = build(JPM, {"status": "open"}, check_labels={"second_parser": "Second PDF parser"},
                sections=[{"id": "s11", "heading_text": "ITC FMCG – well positioned for rapid growth", "source": {"page": 11}},
                          {"id": "s24", "heading_text": "drivers", "source": {"page": 8}},
                          {"id": "s30", "heading_text": "drivers", "source": {"page": 8}}])
    assert {o["value"]: o["label"] for o in out["options"]["check"]}["second_parser"] == "Second PDF parser"
    assert [o["label"] for o in out["options"]["section"]] == [
        "Section on page 8", "Section on page 8 · 2", "p11 · ITC FMCG – well positioned for rapid growth"]


@pytest.mark.parametrize("heading,ocr,usable", [
    ("Financial Highlights", False, "Financial Highlights"),
    ("ESG RATINGS", True, "ESG RATINGS"),
    ("(c) Compliance with the Companies Act, 2013", False, "(c) Compliance with the Companies Act, 2013"),
    ("1. Tourism in mission mode", False, "1. Tourism in mission mode"),
    ("— Auf einen Blick", False, "Auf einen Blick"),
    ("drivers", False, None),                      # starts mid-sentence
    ("or Bandra-Kurla Complex", True, None),
    ("*for Gestated", True, None),
    ("= 51% of Energy coneum", False, None),
    ("…amped up by few addon", False, None),
    ("FYSOE", True, None),                         # a lone all-caps word on a scanned page
    ("EBITDA", False, "EBITDA"),                   # the same on a text page is a heading
    ("MM ENA", True, None),
])
def test_section_names_are_headings_not_fragments(heading, ocr, usable):
    assert usable_heading(heading, ocr=ocr) == usable


def test_scanned_page_rule_uses_the_page_method():
    labels = section_labels([{"id": "s1", "heading_text": "FYSOE", "source": {"page": 15}},
                             {"id": "s2", "heading_text": "FYSOE", "source": {"page": 16}}],
                            pages=[{"page": 15, "method": "ocr"}, {"page": 16, "method": "text_layer"}])
    assert labels == {"s1": "Section on page 15", "s2": "p16 · FYSOE"}


def test_issues_endpoint_returns_the_list_with_its_filters(client):
    """Through the API on a processed report: every option the endpoint offers shows exactly
    that many items, and "All checks" counts every open item."""
    from tests.conftest import add_member, make_tenant, make_user, sign_in
    from tests.test_pipeline_e2e import drain
    from app.tenancy import Role
    t, u = make_tenant(), make_user()
    add_member(t, u, Role.client_admin)
    sign_in(client, u)
    data = (CORPUS / "acme_q2fy26_results_scanned.pdf").read_bytes()
    ins = client.post(f"/api/tenants/{t.id}/reports/inspect", files={"file": ("scan.pdf", data, "application/pdf")}).json()
    layout = next(o for o in ins["build_options"]["options"] if o["mode"] == "layout")
    r = client.post(f"/api/tenants/{t.id}/reports/create", json={
        "source_file_id": ins["source_file_id"], "company_name": "Acme Industries Limited", "report_type": "quarterly_results",
        "fiscal_year": 2026, "period": "q2", "currency": "INR", "reporting_unit": "₹ crore",
        "build_mode": "layout", "credits_shown": layout["max_credits"]})
    assert r.status_code == 201, r.text
    drain()
    V = f"/api/tenants/{t.id}/reports/{r.json()['report_id']}/versions/{r.json()['version']['id']}/issues"
    everything = client.get(V).json()
    assert everything["issues"], "the scanned sample should leave something to review"
    assert {"options", "totals"} <= everything.keys()
    assert all(isinstance(i["decidable"], bool) for i in everything["issues"])
    open_ = client.get(V, params={"status": "open"}).json()
    assert open_["totals"]["rows"] == len({(i["fid"] and f"fig:{i['fid']}") or i["id"] for i in open_["issues"]})
    for facet, param in (("page", "page"), ("check", "check"), ("section", "section_id"), ("severity", "severity")):
        for o in open_["options"][facet]:
            got = client.get(V, params={"status": "open", param: o["value"]}).json()
            assert got["totals"]["rows"] == o["count"] > 0, (facet, o)
