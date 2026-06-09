"""Unit tests for dashboard snapshot diffing and notification helpers."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from perq.events import ChangeEvent, MAX_EVENTS, diff_dashboards, osascript_args
from perq.github import Dashboard, PRSummary

NOW = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)


def make_pr(number: int = 1, **overrides) -> PRSummary:
    pr = PRSummary(
        repo="octo/spoon",
        number=number,
        title="Fix the widget",
        author="josh",
        url=f"https://github.com/octo/spoon/pull/{number}",
        is_draft=False,
        review_decision="REVIEW_REQUIRED",
        ci_state="PENDING",
        additions=10,
        deletions=2,
        comment_count=3,
        updated_at=NOW,
    )
    return replace(pr, **overrides) if overrides else pr


def test_first_load_is_silent():
    new = Dashboard(mine=[make_pr(ci_state="FAILURE")])
    assert diff_dashboards(None, new) == []


def test_ci_failure_on_my_pr_fires_error():
    old = Dashboard(mine=[make_pr()])
    new = Dashboard(mine=[make_pr(ci_state="FAILURE")])
    events = diff_dashboards(old, new)
    assert len(events) == 1
    assert events[0].title == "CI failed"
    assert events[0].severity == "error"
    assert "octo/spoon#1" in events[0].message


def test_ci_recovery_fires_information():
    old = Dashboard(mine=[make_pr(ci_state="FAILURE")])
    new = Dashboard(mine=[make_pr(ci_state="SUCCESS")])
    events = diff_dashboards(old, new)
    assert [e.title for e in events] == ["CI green"]
    assert events[0].severity == "information"


def test_transition_to_pending_is_silent():
    old = Dashboard(mine=[make_pr(ci_state="SUCCESS")])
    new = Dashboard(mine=[make_pr(ci_state="PENDING")])
    assert diff_dashboards(old, new) == []


def test_unchanged_ci_is_silent():
    old = Dashboard(mine=[make_pr(ci_state="FAILURE")])
    new = Dashboard(mine=[make_pr(ci_state="FAILURE")])
    assert diff_dashboards(old, new) == []


def test_review_decision_transitions():
    old = Dashboard(mine=[make_pr()])
    approved = Dashboard(mine=[make_pr(review_decision="APPROVED")])
    events = diff_dashboards(old, approved)
    assert [e.title for e in events] == ["Approved"]
    assert events[0].severity == "information"

    changes = Dashboard(mine=[make_pr(review_decision="CHANGES_REQUESTED")])
    events = diff_dashboards(old, changes)
    assert [e.title for e in events] == ["Changes requested"]
    assert events[0].severity == "warning"


def test_new_comments_fire_once_per_pr():
    old = Dashboard(mine=[make_pr(comment_count=3)])
    new = Dashboard(mine=[make_pr(comment_count=5)])
    events = diff_dashboards(old, new)
    assert len(events) == 1
    assert events[0].title == "2 new comments"


def test_comment_decrease_is_silent():
    old = Dashboard(mine=[make_pr(comment_count=5)])
    new = Dashboard(mine=[make_pr(comment_count=3)])
    assert diff_dashboards(old, new) == []


def test_comments_on_review_requested_prs_fire():
    old = Dashboard(review_requested=[make_pr(comment_count=0)])
    new = Dashboard(review_requested=[make_pr(comment_count=1)])
    events = diff_dashboards(old, new)
    assert [e.title for e in events] == ["1 new comment"]


def test_new_review_request_fires():
    old = Dashboard(involved=[make_pr()])
    new = Dashboard(review_requested=[make_pr()])
    events = diff_dashboards(old, new)
    assert [e.title for e in events] == ["Review requested"]
    assert events[0].severity == "warning"


def test_existing_review_request_is_silent():
    old = Dashboard(review_requested=[make_pr()])
    new = Dashboard(review_requested=[make_pr()])
    assert diff_dashboards(old, new) == []


def test_brand_new_pr_is_silent():
    old = Dashboard()
    new = Dashboard(mine=[make_pr(ci_state="FAILURE")])
    assert diff_dashboards(old, new) == []


def test_section_move_without_changes_is_silent():
    old = Dashboard(involved=[make_pr()])
    new = Dashboard(mine=[make_pr()])
    assert diff_dashboards(old, new) == []


def test_storm_guard_collapses_to_summary():
    old = Dashboard(mine=[make_pr(n) for n in range(1, 8)])
    new = Dashboard(mine=[make_pr(n, ci_state="FAILURE") for n in range(1, 8)])
    events = diff_dashboards(old, new)
    assert len(events) == 1
    assert events[0].title == "Dashboard updated"
    assert "7 PR updates" in events[0].message


def test_storm_guard_threshold_exact():
    old = Dashboard(mine=[make_pr(n) for n in range(1, MAX_EVENTS + 1)])
    new = Dashboard(mine=[make_pr(n, ci_state="FAILURE") for n in range(1, MAX_EVENTS + 1)])
    events = diff_dashboards(old, new)
    assert len(events) == MAX_EVENTS  # at the limit, no collapse
    assert all(e.title == "CI failed" for e in events)


def test_osascript_args_escapes_quotes_and_backslashes():
    args = osascript_args('He said "hi"', "path\\to\\thing")
    assert args[0] == "osascript"
    assert args[1] == "-e"
    assert '\\"hi\\"' in args[2]
    assert "path\\\\to\\\\thing" in args[2]
