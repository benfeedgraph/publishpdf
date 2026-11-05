"""Review-flow fixes: one item per misread figure, honest re-check timing, upload guesses."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.api.report_routes import _processing_elapsed
from app.extraction.detect import company_from_filename
from app.validation import Issue, merge_figure_duplicates


def test_same_figure_flagged_by_two_checks_is_one_item():
    issues = [
        Issue("low_confidence", "blocking", "Read with low confidence (62%). A person must confirm it.", "p3f1", 3,
              actual="8888"),
        Issue("re_extraction", "blocking", "A second, independent reading disagrees.", "p3f1", 3,
              expected="8888", actual="888"),
        Issue("re_extraction", "blocking", "A second, independent reading disagrees.", "p3f2", 3),
        Issue("arithmetic", "blocking", "Total doesn't add up.", "p3f1", 3),
    ]
    out = merge_figure_duplicates(issues)
    by_fid = [(i.fid, i.check) for i in out]
    assert by_fid.count(("p3f1", "re_extraction")) == 1
    assert ("p3f1", "low_confidence") not in by_fid
    assert ("p3f1", "arithmetic") in by_fid          # a different kind of problem stays separate
    assert ("p3f2", "re_extraction") in by_fid
    kept = next(i for i in out if i.fid == "p3f1" and i.check == "re_extraction")
    assert "low reading confidence" in kept.message


def test_blocking_wins_over_warning_when_merging():
    issues = [Issue("re_extraction", "warning", "Re-read was unclear.", "f1", 1),
              Issue("low_confidence", "blocking", "A person must confirm it.", "f1", 1)]
    out = merge_figure_duplicates(issues)
    assert len(out) == 1 and out[0].severity == "blocking" and out[0].check == "low_confidence"


def _job(kind, created, started=None, finished=None):
    return SimpleNamespace(kind=kind, created_at=created, started_at=started, finished_at=finished)


def test_recheck_is_timed_from_its_own_start_not_the_upload():
    now = datetime.now(timezone.utc)
    upload = now - timedelta(hours=3)
    jobs = [
        _job("pipeline.extract", upload, upload, upload + timedelta(minutes=2)),
        _job("pipeline.render", upload + timedelta(minutes=2), upload + timedelta(minutes=2), upload + timedelta(minutes=4)),
        _job("pipeline.validate", upload + timedelta(minutes=4), upload + timedelta(minutes=4), upload + timedelta(minutes=6)),
        # a reviewer's decision hours later starts a new run
        _job("pipeline.render", now - timedelta(seconds=40), now - timedelta(seconds=30)),
    ]
    elapsed = _processing_elapsed(jobs)
    assert elapsed is not None and 25 <= elapsed <= 60


def test_chained_stages_count_as_one_run():
    now = datetime.now(timezone.utc)
    t0 = now - timedelta(seconds=100)
    jobs = [
        _job("pipeline.render", t0, t0, t0 + timedelta(seconds=60)),
        _job("pipeline.validate", t0 + timedelta(seconds=60), t0 + timedelta(seconds=61)),
    ]
    assert 95 <= _processing_elapsed(jobs) <= 110


def test_company_guess_from_filename():
    assert company_from_filename("ITC_Annual_Report_2025.pdf") == "ITC"
    assert company_from_filename("Acme-Industries-Q2-FY26-Results.pdf") == "Acme Industries"
    assert company_from_filename("annual-report-2024-25.pdf") is None
    assert company_from_filename(None) is None
