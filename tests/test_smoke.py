"""Smoke tests: app boots with a fake client, screens render, prompts build."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from perq.app import PerqApp
from perq.github import (
    CheckRun,
    Comment,
    Dashboard,
    PRDetail,
    PRSummary,
    ReviewThread,
    ThreadComment,
)
from perq.prompts import (
    MAX_DIFF_CHARS,
    MAX_LOG_CHARS,
    build_ci_diagnosis_prompt,
    build_review_prompt,
    build_summary_prompt,
)
from textual.widgets import Static, TextArea

from perq.screens.confirm import ConfirmModal
from perq.screens.dashboard import DashboardScreen, PRTable
from perq.screens.pr_detail import ChecksTable, PRDetailScreen
from perq.screens.text_input import TextInputModal

NOW = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)


def make_pr(number: int = 1, title: str = "Fix the widget") -> PRSummary:
    return PRSummary(
        repo="octo/spoon",
        number=number,
        title=title,
        author="josh",
        url=f"https://github.com/octo/spoon/pull/{number}",
        is_draft=False,
        review_decision="REVIEW_REQUIRED",
        ci_state="SUCCESS",
        additions=10,
        deletions=2,
        comment_count=3,
        updated_at=NOW,
        node_id="PR_kwDOA_fake",
    )


def make_checks() -> list[CheckRun]:
    return [
        CheckRun(
            name="build",
            bucket="success",
            raw_state="COMPLETED: SUCCESS",
            url="https://github.com/octo/spoon/runs/1",
            kind="check_run",
            job_id=101,
        ),
        CheckRun(
            name="tests",
            bucket="failure",
            raw_state="COMPLETED: FAILURE",
            url="https://github.com/octo/spoon/runs/2",
            kind="check_run",
            job_id=102,
        ),
        CheckRun(
            name="external-ci",
            bucket="failure",
            raw_state="FAILURE",
            url="https://ci.example.com/42",
            kind="status",
            job_id=None,
        ),
    ]


def make_detail(pr: PRSummary) -> PRDetail:
    return PRDetail(
        summary=pr,
        body="This fixes the widget.",
        state="OPEN",
        base_ref="main",
        head_ref="fix/widget",
        changed_files=2,
        created_at=NOW,
        labels=["bug"],
        conversation=[Comment(author="ana", body="Nice!", created_at=NOW)],
        review_threads=[
            ReviewThread(
                path="src/widget.py",
                line=42,
                is_resolved=False,
                is_outdated=False,
                diff_hunk="@@ -40,3 +40,4 @@\n-old\n+new",
                comments=[ThreadComment(author="ana", body="Why this?", created_at=NOW)],
            )
        ],
        checks=make_checks(),
    )


class FakeClient:
    def __init__(self) -> None:
        self.dashboards: list[Dashboard] | None = None
        self.closed_prs: list[str] = []
        self.posted_comments: list[tuple[str, str, int, str]] = []
        self.submitted_reviews: list[tuple[str, str, str]] = []

    async def fetch_dashboard(self) -> Dashboard:
        if self.dashboards:
            return self.dashboards.pop(0) if len(self.dashboards) > 1 else self.dashboards[0]
        return Dashboard(
            mine=[make_pr(1), make_pr(2, "Add tests")],
            review_requested=[make_pr(3, "Refactor parser")],
            involved=[],
        )

    async def fetch_pr_detail(self, owner: str, name: str, number: int) -> PRDetail:
        return make_detail(make_pr(number))

    async def fetch_diff(self, owner: str, name: str, number: int) -> str:
        return "diff --git a/src/widget.py b/src/widget.py\n@@ -1 +1 @@\n-old\n+new\n"

    async def fetch_job_logs(self, owner: str, name: str, job_id: int) -> str:
        return "2026-06-09T12:00:00.0000000Z ##[group]Run pytest\nFAILED tests/test_x.py\n"

    async def close_pr(self, node_id: str) -> None:
        self.closed_prs.append(node_id)

    async def comment_on_pr(self, owner: str, name: str, number: int, body: str) -> None:
        self.posted_comments.append((owner, name, number, body))

    async def submit_review(self, node_id: str, event: str, body: str = "") -> None:
        self.submitted_reviews.append((node_id, event, body))

    async def close(self) -> None:
        pass


async def test_dashboard_renders_sections():
    app = PerqApp(client=FakeClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
        mine = app.screen.query_one("#table-mine", PRTable)
        assert mine.row_count == 2
        assert app.screen.query_one("#table-review-requested", PRTable).row_count == 1
        # Empty section shows the note instead of the table.
        assert not app.screen.query_one("#table-involved", PRTable).display


async def test_open_pr_detail_from_dashboard():
    app = PerqApp(client=FakeClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PRDetailScreen)
        threads = app.screen.query_one("#threads")
        assert threads.children, "review threads should be mounted"
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)


async def test_quiet_refresh_preserves_cursor_and_focus():
    app = PerqApp(client=FakeClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        mine = screen.query_one("#table-mine", PRTable)
        await pilot.press("down")
        assert mine.cursor_row == 1
        screen.load_dashboard(quiet=True)
        await pilot.pause()
        assert mine.cursor_row == 1, "cursor should survive a quiet refresh"
        assert screen.focused is mine, "focus should survive a quiet refresh"
        assert screen.sub_title.startswith("updated ")


def test_parse_pr_ref():
    from perq.commands import parse_pr_ref

    for query in (
        "https://github.com/octo/spoon/pull/7",
        "https://github.com/octo/spoon/pull/7#discussion_r1",
        "http://www.github.com/octo/spoon/pull/7/",
        "octo/spoon#7",
        "octo/spoon/7",
    ):
        ref = parse_pr_ref(query)
        assert ref is not None, query
        assert (ref.repo, ref.number) == ("octo/spoon", 7), query
        assert ref.url == "https://github.com/octo/spoon/pull/7"

    for query in ("", "hello world", "octo/spoon", "https://github.com/octo/spoon"):
        assert parse_pr_ref(query) is None, query


async def test_open_pr_from_url_ref():
    from perq.commands import parse_pr_ref

    app = PerqApp(client=FakeClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        ref = parse_pr_ref("https://github.com/octo/spoon/pull/5")
        app.push_screen(PRDetailScreen(ref))
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PRDetailScreen)
        # The bare URL ref has no title; the fetch fills it in.
        assert screen.pr.title == "Fix the widget"
        header = screen.query_one("#pr-header", Static)
        assert "octo/spoon#5" in str(header.render())


async def test_checks_tab_lists_check_runs():
    app = PerqApp(client=FakeClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PRDetailScreen)
        table = screen.query_one("#checks-table", ChecksTable)
        assert table.row_count == 3
        assert {c.name for c in table.check_runs.values()} == {"build", "tests", "external-ci"}


async def test_diagnose_failed_check_builds_prompt(monkeypatch):
    app = PerqApp(client=FakeClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        table = screen.query_one("#checks-table", ChecksTable)
        table.focus()
        table.move_cursor(row=1)  # the failing "tests" check run

        pushed = []
        monkeypatch.setattr(app, "push_screen", lambda s, *a, **kw: pushed.append(s))
        await pilot.press("d")
        await pilot.pause()
        assert len(pushed) == 1
        assert pushed[0]._title == "CI diagnosis: tests"
        assert "FAILED tests/test_x.py" in pushed[0]._prompt


async def test_diagnose_external_check_degrades(monkeypatch):
    app = PerqApp(client=FakeClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        table = screen.query_one("#checks-table", ChecksTable)
        table.focus()
        table.move_cursor(row=2)  # the external StatusContext

        pushed = []
        monkeypatch.setattr(app, "push_screen", lambda s, *a, **kw: pushed.append(s))
        notices = []
        monkeypatch.setattr(screen, "notify", lambda msg, **kw: notices.append(msg))
        await pilot.press("d")
        await pilot.pause()
        assert not pushed
        assert any("external" in n for n in notices)


async def test_dashboard_changes_fire_notifications(monkeypatch):
    from dataclasses import replace

    first = Dashboard(mine=[make_pr(1)], review_requested=[], involved=[])
    second = Dashboard(
        mine=[replace(make_pr(1), ci_state="FAILURE")],
        review_requested=[make_pr(9, "New review ask")],
        involved=[],
    )
    client = FakeClient()
    client.dashboards = [first, second]

    app = PerqApp(client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        notices = []
        monkeypatch.setattr(
            screen, "notify", lambda msg, *, title="", **kw: notices.append(title)
        )
        screen.load_dashboard(quiet=True)
        await pilot.pause()
        assert "CI failed" in notices
        assert "Review requested" in notices


def test_ci_diagnosis_prompt_strips_timestamps_and_truncates():
    from perq.screens.pr_detail import ChecksTable  # noqa: F401  (import sanity)

    check = make_checks()[1]
    logs = "2026-06-09T12:00:00.0000000Z line one\n" + "x" * (MAX_LOG_CHARS + 1000)
    prompt = build_ci_diagnosis_prompt(make_pr(), check, logs)
    assert "octo/spoon#1" in prompt
    assert "tests (COMPLETED: FAILURE)" in prompt
    assert "2026-06-09T12:00:00" not in prompt
    assert "log truncated" in prompt
    assert len(prompt) < MAX_LOG_CHARS + 3000


def test_prompts_include_pr_context_and_truncate_diff():
    detail = make_detail(make_pr())
    long_diff = "x" * (MAX_DIFF_CHARS + 1000)

    summary = build_summary_prompt(detail, long_diff)
    assert "octo/spoon#1" in summary
    assert "diff truncated" in summary
    assert len(summary) < MAX_DIFF_CHARS + 5000

    review = build_review_prompt(detail, "small diff")
    assert "Why this?" in review
    assert "advisory" in review


async def _navigate_to_detail(pilot, app) -> PRDetailScreen:
    """Helper: navigate from dashboard to the first PR detail screen."""
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()
    assert isinstance(app.screen, PRDetailScreen)
    return app.screen


async def test_comment_action_posts_and_returns():
    client = FakeClient()
    app = PerqApp(client=client)
    async with app.run_test() as pilot:
        await _navigate_to_detail(pilot, app)
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, TextInputModal)

        # Type into the TextArea and submit.
        app.screen.query_one(TextArea).insert("Great PR!")
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.pause()  # wait for @work to complete

        assert isinstance(app.screen, PRDetailScreen)
        assert len(client.posted_comments) == 1
        assert client.posted_comments[0] == ("octo", "spoon", 1, "Great PR!")


async def test_comment_requires_body():
    client = FakeClient()
    app = PerqApp(client=client)
    async with app.run_test() as pilot:
        await _navigate_to_detail(pilot, app)
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, TextInputModal)

        # Submit with empty body — should stay on modal.
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, TextInputModal)
        assert client.posted_comments == []


async def test_approve_action_submits_review():
    client = FakeClient()
    app = PerqApp(client=client)
    async with app.run_test() as pilot:
        await _navigate_to_detail(pilot, app)
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, TextInputModal)

        # Empty body is allowed for approve.
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.pause()

        assert isinstance(app.screen, PRDetailScreen)
        assert len(client.submitted_reviews) == 1
        node_id, event, body = client.submitted_reviews[0]
        assert node_id == "PR_kwDOA_fake"
        assert event == "APPROVE"


async def test_request_changes_requires_body():
    client = FakeClient()
    app = PerqApp(client=client)
    async with app.run_test() as pilot:
        await _navigate_to_detail(pilot, app)
        await pilot.press("x")
        await pilot.pause()
        assert isinstance(app.screen, TextInputModal)

        # Submit with empty body — required for request changes.
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, TextInputModal)
        assert client.submitted_reviews == []

        # Type a body and submit.
        app.screen.query_one(TextArea).insert("Please fix the typo.")
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.pause()

        assert isinstance(app.screen, PRDetailScreen)
        assert len(client.submitted_reviews) == 1
        _, event, body = client.submitted_reviews[0]
        assert event == "REQUEST_CHANGES"
        assert body == "Please fix the typo."


async def test_close_pr_cancel_does_not_close():
    client = FakeClient()
    app = PerqApp(client=client)
    async with app.run_test() as pilot:
        await _navigate_to_detail(pilot, app)
        await pilot.press("C")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)

        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, PRDetailScreen)
        assert client.closed_prs == []


async def test_close_pr_confirm_closes():
    client = FakeClient()
    app = PerqApp(client=client)
    async with app.run_test() as pilot:
        await _navigate_to_detail(pilot, app)
        await pilot.press("C")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)

        await pilot.press("y")
        await pilot.pause()
        await pilot.pause()

        assert isinstance(app.screen, PRDetailScreen)
        assert client.closed_prs == ["PR_kwDOA_fake"]


async def test_mutation_actions_hidden_for_closed_pr():
    def make_closed_detail(pr: PRSummary) -> PRDetail:
        d = make_detail(pr)
        return PRDetail(
            summary=pr, body=d.body, state="MERGED",
            base_ref=d.base_ref, head_ref=d.head_ref,
            changed_files=d.changed_files, created_at=d.created_at,
        )

    class ClosedClient(FakeClient):
        async def fetch_pr_detail(self, owner, name, number):
            return make_closed_detail(make_pr(number))

    app = PerqApp(client=ClosedClient())
    async with app.run_test() as pilot:
        await _navigate_to_detail(pilot, app)
        screen = app.screen
        # check_action should disable all mutating bindings.
        for action in ("comment", "approve", "request_changes", "close_pr"):
            assert screen.check_action(action, ()) is False
