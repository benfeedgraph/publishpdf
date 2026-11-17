"""Issue #7 and the sectioning found while testing it.

Footer: a page carries only the disclaimer its workspace saved (never the platform's
"DEVELOPMENT PLACEHOLDER" text); unfinished text can't be saved or published; drafts built with an
older disclaimer are rebuilt; the "Powered by PublishPDF" credit follows its setting.
Sections: a heading that wraps onto two lines is one heading; large-type passages and bullets
aren't headings; sections before the first chapter are on the site; a page whose text never
reaches the site is caught by the web page check."""

from __future__ import annotations

import pymupdf
import pytest
from sqlalchemy import select

from app import db, validation
from app.config import get_settings
from app.extraction.schema_builder import extract
from app.models import ReportVersion
from app.render import site
from app.render import theme as theming
from app.reports import unfinished_disclaimer
from app.tenancy import system_context
from tests.test_faq_story import BODY, META, _long_pdf_without_contents
from tests.test_pipeline_e2e import admin_client, drain, upload  # noqa: F401 (fixture)

PLACEHOLDER = "DEVELOPMENT PLACEHOLDER. The PDF filing is the official document. Replace this text after legal review."


# ------------------------------------------------------------------ the footer disclaimer


@pytest.mark.parametrize("text,found", [
    (PLACEHOLDER, "PLACEHOLDER"),
    ("Lorem ipsum dolor sit amet.", "Lorem ipsum"),
    ("TODO: legal to confirm wording", "TODO"),
    ("Disclaimer TBD", "TBD"),
    ("Please replace this with the final text.", "replace this"),
    ("[Company name] is not responsible for errors.", "[Company name]"),
    ("Copyright {{year}} Acme.", "{{year}}"),
    ("This page presents our results for convenience; the PDF filing is the official document.", None),
    ("Figures are as filed under Rule 33 [1] and audited.", None),          # a footnote marker is fine
    ("Today's photo of our todos-free placeholders-free board.", None),     # words that only contain the markers
])
def test_unfinished_disclaimer_text(text, found):
    assert unfinished_disclaimer(text) == found


def _footers(files: dict[str, bytes]) -> list[str]:
    return [d.decode()[d.decode().rfind('<footer class="site-footer">'):] for n, d in files.items() if n.endswith(".html")]


def test_pages_never_show_the_platform_placeholder(admin_client, monkeypatch):
    client, t, _ = admin_client
    monkeypatch.setattr(get_settings(), "disclaimer_default_text", PLACEHOLDER)
    up = upload(client, t, "acme_q2fy26_results.pdf")
    drain()
    base = f"/api/tenants/{t.id}/reports/{up['report_id']}/versions/{up['version']['id']}"
    page = client.get(base + "/preview/").text
    assert "PLACEHOLDER" not in page.upper()
    assert "The PDF filing is the official document." in page          # the page's own line stays
    assert client.get(f"/api/tenants/{t.id}/settings").json()["effective_disclaimer"] is None
    v = client.get(base).json()
    assert v["disclaimer_problem"] is None
    r = client.post(base + "/publish", json={"confirm_reviewed": True})
    assert r.status_code == 200, r.text                                  # no disclaimer needed to publish


def test_settings_refuse_unfinished_disclaimer_text(admin_client):
    client, t, _ = admin_client
    r = client.put(f"/api/tenants/{t.id}/settings", json={"disclaimer": PLACEHOLDER})
    assert r.status_code == 400 and "PLACEHOLDER" in r.json()["detail"]
    assert client.get(f"/api/tenants/{t.id}/settings").json()["disclaimer"] is None


def test_a_draft_built_with_the_placeholder_is_held_back_and_rebuilt(admin_client):
    client, t, _ = admin_client
    up = upload(client, t, "acme_q2fy26_results.pdf")
    drain()
    rid, vid = up["report_id"], up["version"]["id"]
    base = f"/api/tenants/{t.id}/reports/{rid}/versions/{vid}"
    # a draft built before this fix: its footer carries the placeholder
    with db.session(system_context()) as s:
        v = s.get(ReportVersion, vid)
        v.theme_snapshot = {**(v.theme_snapshot or {}), "disclaimer": PLACEHOLDER}
    problem = client.get(base).json()["disclaimer_problem"]
    assert problem and "PLACEHOLDER" in problem
    r = client.post(base + "/publish", json={"confirm_reviewed": True})
    assert r.status_code == 409 and r.json()["detail"] == problem
    # saving the workspace's disclaimer rebuilds the draft with it
    own = "These pages present our results for convenience; the PDF filing is the official document."
    assert client.put(f"/api/tenants/{t.id}/settings", json={"disclaimer": own}).status_code == 200
    assert client.get(base).json()["status"] == "processing"
    drain()
    assert client.get(base).json()["disclaimer_problem"] is None
    assert own in client.get(base + "/preview/").text
    assert client.post(base + "/publish", json={"confirm_reviewed": True}).status_code == 200


def test_a_draft_with_an_older_disclaimer_than_settings_is_held_back(admin_client):
    client, t, _ = admin_client
    up = upload(client, t, "acme_q2fy26_results.pdf")
    drain()
    base = f"/api/tenants/{t.id}/reports/{up['report_id']}/versions/{up['version']['id']}"
    with db.session(system_context()) as s:          # the setting changed, the page wasn't rebuilt yet
        v = s.get(ReportVersion, up["version"]["id"])
        v.theme_snapshot = {**(v.theme_snapshot or {}), "disclaimer": "An earlier disclaimer."}
    problem = client.get(base).json()["disclaimer_problem"]
    assert problem and "earlier disclaimer" in problem
    assert client.post(base + "/publish", json={"confirm_reviewed": True}).status_code == 409


@pytest.mark.parametrize("on", [True, False])
def test_powered_by_credit_follows_its_setting(on):
    pdf = _long_pdf_without_contents(["Chairman Statement", "Business Review"], pages_each=1)
    schema, _ = extract(pdf, {**META, "report_type": "annual_report"})
    theme = theming.validate({"footer": {"show_powered_by": on}})
    files = site.render_report(schema, theme=theme, disclaimer=None, pdf_bytes=pdf)
    footers = _footers(files)
    assert footers and all(("Powered by PublishPDF" in f) == on for f in footers)
    assert all('class="disclaimer"' not in f for f in footers)          # no disclaimer saved: none shown


def test_design_preview_shows_the_credit_when_ticked(admin_client):
    client, t, _ = admin_client
    upload(client, t, "acme_q2fy26_results.pdf")
    drain()
    theme = client.get(f"/api/tenants/{t.id}/settings").json()["theme"]
    for on in (True, False):
        html = client.post(f"/api/tenants/{t.id}/theme/preview",
                           json={"theme": {**theme, "footer": {"show_powered_by": on}}}).text
        footer = html[html.rfind('<footer class="site-footer">'):]
        assert ("Powered by PublishPDF" in footer) == on


# ------------------------------------------------------------------ sections


def _page_pdf(draw) -> bytes:
    doc = pymupdf.open()
    draw(doc.new_page())
    doc.new_page().insert_textbox(pymupdf.Rect(72, 72, 520, 300), BODY, fontname="helv", fontsize=11)
    return doc.tobytes()


def _headings(pdf: bytes) -> list[str]:
    """Every heading the sections carry, sub-headings included (a heading-only section is folded
    into the next one as its sub-heading)."""
    schema, _ = extract(pdf, META)
    return [h for s in schema["sections"] for h in (s.get("heading_text"), s.get("subheading_text")) if h]


def test_a_heading_that_wraps_is_one_heading():
    def draw(p):
        p.insert_text((72, 100), "Manufactured", fontname="hebo", fontsize=22)
        p.insert_text((72, 126), "Capital", fontname="hebo", fontsize=22)
        p.insert_textbox(pymupdf.Rect(72, 150, 520, 300), BODY, fontname="helv", fontsize=11)
        p.insert_text((72, 340), "Approach and Business Model", fontname="hebo", fontsize=16)
        p.insert_textbox(pymupdf.Rect(72, 356, 520, 500), BODY, fontname="helv", fontsize=11)
    heads = _headings(_page_pdf(draw))
    assert "Manufactured Capital" in heads and "Capital" not in heads and "Manufactured" not in heads
    assert "Approach and Business Model" in heads              # a heading of another size stays separate


def test_large_type_running_text_is_not_a_heading():
    def draw(p):
        for i, line in enumerate(["We see India as a land of", "opportunity for the nation. We see India",
                                  "as a reservoir of tremendous talent"]):
            p.insert_text((72, 100 + 26 * i), line, fontname="helv", fontsize=20)
        p.insert_textbox(pymupdf.Rect(72, 200, 520, 400), BODY, fontname="helv", fontsize=11)
    heads = _headings(_page_pdf(draw))
    assert not any("India" in h or "talent" in h for h in heads)


def test_bullets_are_not_headings():
    def draw(p):
        p.insert_text((72, 90), "Highlights", fontname="hebo", fontsize=20)
        for i, line in enumerate(["• Revenue grew strongly", "• Margins widened", "– Operating leverage of 5.6%"]):
            p.insert_text((72, 130 + 22 * i), line, fontname="hebo", fontsize=15)
        p.insert_textbox(pymupdf.Rect(72, 220, 520, 400), BODY, fontname="helv", fontsize=11)
    heads = _headings(_page_pdf(draw))
    assert heads == ["Highlights"]                  # the bullets are the section's text, not headings of their own


def test_sections_before_the_first_chapter_are_on_the_site():
    # AI-picked chapters start at "Business Review": "Chairman Statement" comes before it and was
    # in no chapter, so its page never reached the site.
    pdf = _long_pdf_without_contents(["Chairman Statement", "Business Review", "Strategy", "Governance"])

    def llm(prompt: str, system: str | None = None, feature: str | None = None) -> str:
        import json
        if feature != "chapters":
            return "{}"
        ids = [s["id"] for s in json.loads(prompt)["sections"] if s["heading"] in ("Business Review", "Strategy")]
        return json.dumps({"chapters": ids})

    schema, _ = extract(pdf, {**META, "report_type": "annual_report"}, llm=llm)
    files = site.render_report(schema, theme=theming.validate({}), disclaimer=None, pdf_bytes=pdf)
    text = " ".join(d.decode() for n, d in files.items() if n.endswith(".html"))
    assert "Chairman Statement" in text
    assert validation.check_text_on_site(schema, pdf, files) == []


TOPICS = ["rural distribution and the reach of our brands into smaller towns",
          "capital expenditure on new plants and the timing of their commissioning",
          "working capital, receivables and the cash conversion cycle over the year",
          "sustainability targets for water, energy and packaging across our sites"]


def _distinct_pages_pdf() -> bytes:
    doc = pymupdf.open()
    for i, topic in enumerate(TOPICS, start=1):
        p = doc.new_page()
        p.insert_text((72, 100), f"Section {'ABCD'[i - 1]}", fontname="hebo", fontsize=20)
        p.insert_textbox(pymupdf.Rect(72, 130, 520, 500), (f"This page discusses {topic}. " * 6).strip(), fontname="helv", fontsize=11)
    return doc.tobytes()


def test_web_page_check_catches_a_page_left_out():
    pdf = _distinct_pages_pdf()
    schema, _ = extract(pdf, META)
    files = site.render_report(schema, theme=theming.validate({}), disclaimer=None, pdf_bytes=pdf)
    assert validation.check_text_on_site(schema, pdf, files) == []
    # page 3's text taken out of the site, as the old build did with sections before the first chapter
    gone = {n: (d.decode().replace(TOPICS[2], "").replace(TOPICS[2].replace(",", ""), "").encode() if n.endswith(".html") else d)
            for n, d in files.items()}
    issues = validation.check_text_on_site(schema, pdf, gone)
    assert [(i.page, i.severity, i.check) for i in issues] == [(3, "warning", "completeness")]


def test_a_report_split_into_chapters_shows_its_cover_on_the_first_page():
    pdf = _long_pdf_without_contents(["Chairman Statement", "Business Review", "Strategy", "Governance"])

    def llm(prompt: str, system: str | None = None, feature: str | None = None) -> str:
        import json
        if feature != "chapters":
            return "{}"
        return json.dumps({"chapters": [s["id"] for s in json.loads(prompt)["sections"]
                                        if s["heading"] in ("Business Review", "Strategy")]})

    schema, _ = extract(pdf, {**META, "report_type": "annual_report"}, llm=llm)
    files = site.render_report(schema, theme=theming.validate({}), disclaimer=None, pdf_bytes=pdf)
    base = site.report_base_path(schema["metadata"]).lstrip("/")
    home, other = files[base + "index.html"].decode(), files[base + "strategy/index.html"].decode()
    assert 'class="web-hero' in home and 'class="web-cover"' in home and "pages/p0001" in home
    assert home.count("<h1") == 1 and 'id="start"' in home                 # one page title; "Read the report" lands on the text
    assert 'class="web-hero' not in other and other.count("<h1") == 1        # later chapter pages are unchanged
    assert validation.check_text_on_site(schema, pdf, files) == []          # page 1 is on the site now


def test_a_contents_entry_set_like_a_heading_never_starts_a_chapter():
    # The contents page lists the chapters in large bold type, so its entries read as headings
    # with the same words as the chapters themselves: a chapter must start at its own page.
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 300), "ACME ANNUAL REPORT 2026", fontname="hebo", fontsize=22)
    toc = doc.new_page()
    toc.insert_text((72, 80), "Contents", fontname="hebo", fontsize=18)
    rows = ["Chairman Statement", "Business Review", "Strategy", "Governance"]
    for i, lbl in enumerate(rows):
        toc.insert_text((72, 140 + 60 * i), lbl, fontname="hebo", fontsize=16)
    for lbl in rows:
        for k in range(3):
            p = doc.new_page()
            p.insert_text((72, 80), lbl if k == 0 else f"{lbl} notes {k}", fontname="hebo", fontsize=16 if k == 0 else 13)
            p.insert_textbox(pymupdf.Rect(72, 110, 520, 400), f"{lbl}: " + BODY, fontname="helv", fontsize=11)
    pdf = doc.tobytes()
    schema, _ = extract(pdf, {**META, "report_type": "annual_report"})
    files = site.render_report(schema, theme=theming.validate({}), disclaimer=None, pdf_bytes=pdf)
    pages = [d.decode() for n, d in files.items() if n.endswith("index.html")]
    titled = [p for p in pages if '<h1 class="web-sec-title">Strategy</h1>' in p]
    # the "Strategy" chapter page holds the chapter itself, not the contents entry
    assert len(titled) == 1 and "Strategy: The Company continued" in titled[0]
    assert validation.check_text_on_site(schema, pdf, files) == []


def test_the_page_after_a_cover_is_not_hidden_with_the_cover():
    # ICICI: the cover's last line doesn't end a sentence, so the notice on page 2 was joined to
    # it as one paragraph across the page break — and hidden with the cover, which is a picture.
    notice = ("Certain definitions in this release relating to a future period of time are forward-looking "
              "statements intended to qualify for the safe harbor under applicable securities laws")
    doc = pymupdf.open()
    cover = doc.new_page(width=960, height=540)
    cover.insert_text((60, 200), "Q2-2027:", fontname="hebo", fontsize=40)
    cover.insert_text((60, 260), "Performance review", fontname="hebo", fontsize=28)
    cover.insert_text((60, 320), "October 17, 2026", fontname="helv", fontsize=14)
    p2 = doc.new_page(width=960, height=540)
    p2.insert_textbox(pymupdf.Rect(60, 60, 900, 300), notice + ".", fontname="helv", fontsize=12)
    for title in ("Highlights", "Financial performance", "Asset quality", "Capital"):
        for k in range(2):
            p = doc.new_page(width=960, height=540)
            p.insert_text((60, 70), title if k == 0 else f"{title} detail", fontname="hebo", fontsize=24 if k == 0 else 16)
            p.insert_textbox(pymupdf.Rect(60, 100, 900, 400), f"{title}: " + BODY + " " + BODY, fontname="helv", fontsize=12)
    pdf = doc.tobytes()

    def llm(prompt: str, system: str | None = None, feature: str | None = None) -> str:
        import json
        if feature != "chapters":
            return "{}"
        return json.dumps({"chapters": [s["id"] for s in json.loads(prompt)["sections"] if s["heading"] in ("Highlights", "Asset quality")]})

    schema, _ = extract(pdf, META, llm=llm)
    files = site.render_report(schema, theme=theming.validate({}), disclaimer=None, pdf_bytes=pdf)
    text = " ".join(d.decode() for n, d in files.items() if n.endswith(".html"))
    assert "safe harbor under applicable securities laws" in text
    assert validation.check_text_on_site(schema, pdf, files) == []
