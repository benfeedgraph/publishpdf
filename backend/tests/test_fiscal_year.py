"""The fiscal year as companies write it (issue #6): FY2021, FY26, FY'26, FY2025-26, 2025-26
are all read, stored as the year the fiscal year ends, and written on the page the way the
company wrote them. Anything else gets a message that says what to type, next to the field."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app import db
from app.extraction.dates import FiscalYearError, end_year, fiscal_year_of, parse_fiscal_year, parse_period
from app.extraction.schema_builder import period_label
from app.tenancy import system_context
from tests.test_pipeline_e2e import CORPUS, admin_client, drain, upload  # noqa: F401 (fixture)


@pytest.mark.parametrize("typed,year,label", [
    ("2021", 2021, "FY2021"),
    ("FY2021", 2021, "FY2021"),
    ("FY 2021", 2021, "FY2021"),
    ("fy2026", 2026, "FY2026"),
    ("FY21", 2021, "FY21"),
    ("FY'26", 2026, "FY'26"),
    ("FY’26", 2026, "FY'26"),
    ("FY2025-26", 2026, "FY2025-26"),
    ("FY 2025-26", 2026, "FY2025-26"),
    ("FY2025–26", 2026, "FY2025-26"),             # en dash
    ("FY2025/26", 2026, "FY2025-26"),
    ("FY2025-2026", 2026, "FY2025-26"),
    ("F.Y. 2025-26", 2026, "FY2025-26"),
    ("2025-26", 2026, "2025-26"),
    ("2024-2025", 2025, "2024-25"),
    ("fiscal 2025", 2025, "FY2025"),
    ("fiscal year 2025-26", 2026, "FY2025-26"),
    ("financial year 2024-25", 2025, "FY2024-25"),
    ("  2021 ", 2021, "FY2021"),
    ("２０２１", 2021, "FY2021"),                    # full-width digits
    (2026, 2026, "FY2026"),                         # what older clients send
    ("1999-00", 2000, "1999-00"),
])
def test_years_as_companies_write_them(typed, year, label):
    fy = parse_fiscal_year(typed)
    assert (fy.year, fy.label) == (year, label)
    assert parse_fiscal_year(fy.label).year == year          # the label reads back as the same year


@pytest.mark.parametrize("typed,message", [
    ("", "Enter the fiscal year, like 2026 or FY2025-26."),
    ("   ", "Enter the fiscal year, like 2026 or FY2025-26."),
    (None, "Enter the fiscal year, like 2026 or FY2025-26."),
    ("21", "Enter the full year, like 2021, or write it as FY21."),
    ("20211", "Enter a year like 2026 or FY2025-26."),
    ("twenty twenty", "Enter a year like 2026 or FY2025-26."),
    ("'26", "Enter a year like 2026 or FY2025-26."),
    ("2026.0", "Enter a year like 2026 or FY2025-26."),
    ("1989", "Enter a year between 1990 and 2100."),
    ("0000", "Enter a year between 1990 and 2100."),
    ("2101", "Enter a year between 1990 and 2100."),
    ("2025-27", "2025-27 isn't one fiscal year. Enter it like FY2025-26."),
    ("2025-24", "2025-24 isn't one fiscal year. Enter it like FY2025-26."),
    ("FY 25-26", "Enter it like FY2025-26."),
    ("Q3 2025", "Enter only the year here, like FY2025, and choose Q3 under Period."),
    ("Q2 FY26", "Enter only the year here, like FY26, and choose Q2 under Period."),
    ("3Q25", "Enter only the year here, like FY25, and choose Q3 under Period."),
    ("9M FY26", "Enter only the year here, like FY26, and choose Nine months under Period."),
])
def test_what_isnt_a_year_says_what_to_type(typed, message):
    with pytest.raises(FiscalYearError) as e:
        parse_fiscal_year(typed)
    assert str(e.value) == message


def test_one_convention_for_the_table_headers_and_the_form():
    assert [end_year("2026"), end_year("26"), end_year("2025", "26"), end_year("2024", "2025")] == [2026, 2026, 2026, 2025]
    assert [fiscal_year_of(x) for x in ("26", "'26", "2025", "2025-26", "24-25", "2024-2025")] == [2026, 2026, 2025, 2026, 2025, 2025]
    assert parse_period("Q2 FY2025-26").fiscal_year == parse_fiscal_year("FY2025-26").year == 2026


def test_the_page_writes_the_year_the_company_way():
    assert period_label("q2", 2026) == "Q2 FY2026"                 # no label: as before
    assert period_label("q2", 2026, "FY26") == "Q2 FY26"
    assert period_label("fy", 2026, "2025-26") == "2025-26"
    assert period_label("fy", 2026, "FY2025-26") == "FY2025-26"


# ------------------------------------------------------------------ the upload form (API)


def _inspect(client, t, name="acme_q2fy26_results.pdf"):
    r = client.post(f"/api/tenants/{t.id}/reports/inspect",
                    files={"file": (name, (CORPUS / name).read_bytes(), "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()


def _create(client, t, ins, fiscal_year, period="q2"):
    credits = next(o["max_credits"] for o in ins["build_options"]["options"] if o["mode"] == "layout")
    return client.post(f"/api/tenants/{t.id}/reports/create", json={
        "source_file_id": ins["source_file_id"], "company_name": "Acme Industries Limited",
        "report_type": "quarterly_results", "fiscal_year": fiscal_year, "period": period,
        "currency": "INR", "reporting_unit": "₹ crore", "build_mode": "layout", "credits_shown": credits})


def test_the_form_takes_the_year_as_typed_and_files_it_under_the_year_it_ends(admin_client):
    client, t, _ = admin_client
    r = _create(client, t, _inspect(client, t), "FY26")
    assert r.status_code == 201, r.text
    rid = r.json()["report_id"]
    rep = client.get(f"/api/tenants/{t.id}/reports/{rid}").json()
    assert (rep["fiscal_year"], rep["fiscal_year_label"], rep["period_label"]) == (2026, "FY26", "Q2 FY26")
    assert rep["path"] == "/fy2026/q2/results/"                     # URLs keep the year it ends

    # The same period written another way is the next version of the same report; the
    # newest upload's way of writing the year is the one shown (as with the company name).
    again = _create(client, t, _inspect(client, t), "2025-26")
    assert again.status_code == 201, again.text
    assert again.json()["report_id"] == rid and again.json()["version"]["version_no"] == 2
    rep = client.get(f"/api/tenants/{t.id}/reports/{rid}").json()
    assert (rep["fiscal_year_label"], rep["period_label"]) == ("2025-26", "Q2 2025-26")
    listed = client.get(f"/api/tenants/{t.id}/reports").json()["reports"]
    assert [(x["fiscal_year"], x["period_label"]) for x in listed] == [(2026, "Q2 2025-26")]

    assert _create(client, t, _inspect(client, t), 2026).status_code == 201       # a number, from older clients


@pytest.mark.parametrize("typed,message", [
    ("Q3 2025", "Enter only the year here, like FY2025, and choose Q3 under Period."),
    ("21", "Enter the full year, like 2021, or write it as FY21."),
    (None, "Enter the fiscal year, like 2026 or FY2025-26."),
    ("1989", "Enter a year between 1990 and 2100."),
])
def test_a_year_that_cant_be_read_is_answered_for_the_field(admin_client, typed, message):
    client, t, _ = admin_client
    r = _create(client, t, _inspect(client, t), typed)
    assert r.status_code == 400
    assert r.json() == {"detail": message, "field": "fiscal_year"}
    assert client.get(f"/api/tenants/{t.id}/reports").json()["reports"] == []


def test_the_form_previews_how_the_page_will_write_the_year(admin_client):
    client, t, _ = admin_client
    check = lambda **q: client.get(f"/api/tenants/{t.id}/reports/fiscal-year", params=q)  # noqa: E731
    assert check(value="FY2025-26", period="fy").json() == {"fiscal_year": 2026, "fiscal_year_label": "FY2025-26", "shown": "FY2025-26"}
    assert check(value="FY'26", period="q2").json()["shown"] == "Q2 FY'26"
    assert check(value="2026").json() == {"fiscal_year": 2026, "fiscal_year_label": "FY2026", "shown": "FY2026"}
    bad = check(value="20211", period="q2")
    assert bad.status_code == 400 and bad.json() == {"detail": "Enter a year like 2026 or FY2025-26.", "field": "fiscal_year"}


def test_reports_from_before_the_label_read_as_they_always_did(admin_client):
    client, t, _ = admin_client
    rid = _create(client, t, _inspect(client, t), "FY26").json()["report_id"]
    with db.session(system_context()) as s:                         # a report made before migration 0010
        s.execute(text("UPDATE reports SET fiscal_year_label = NULL WHERE id = :id"), {"id": rid})
    rep = client.get(f"/api/tenants/{t.id}/reports/{rid}").json()
    assert (rep["fiscal_year"], rep["fiscal_year_label"], rep["period_label"]) == (2026, None, "Q2 FY2026")


def test_the_published_page_writes_the_year_as_typed(admin_client):
    client, t, _ = admin_client
    up = upload(client, t, "acme_q2fy26_results.pdf", fiscal_year="FY26")      # the older one-step upload too
    rid, vid = up["report_id"], up["version"]["id"]
    drain()
    meta = client.get(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/schema").json()["metadata"]
    assert (meta["fiscal_year"], meta["fiscal_year_label"], meta["period_label"]) == (2026, "FY26", "Q2 FY26")
    page = client.get(f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}/preview/").text
    assert "<title>Q2 FY26 results · Acme Industries Limited" in page
