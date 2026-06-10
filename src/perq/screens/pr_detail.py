"""PR detail screen: overview, conversation, code comments, checks, and diff."""

from __future__ import annotations

import asyncio
import webbrowser

import httpx
from rich.markup import escape
from rich.syntax import Syntax
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Markdown,
    Static,
    TabbedContent,
    TabPane,
)

from perq.github import CheckRun, Comment, PRDetail, PRSummary, ReviewThread
from perq.prompts import build_ci_diagnosis_prompt, build_review_prompt, build_summary_prompt
from perq.screens.claude_output import ClaudeOutputScreen
from perq.screens.confirm import ConfirmModal
from perq.screens.text_input import TextInputModal


def _diff_syntax(code: str) -> Syntax:
    return Syntax(code, "diff", theme="ansi_dark", word_wrap=True)


def check_cell(bucket: str) -> Text:
    return {
        "success": Text("✓", style="green"),
        "failure": Text("✗", style="red"),
        "pending": Text("●", style="yellow"),
    }.get(bucket, Text("-", style="dim"))


class ChecksTable(DataTable):
    """A DataTable whose rows map to CheckRun objects."""

    def __init__(self, **kwargs) -> None:
        super().__init__(cursor_type="row", zebra_stripes=True, **kwargs)
        self.check_runs: dict[str, CheckRun] = {}

    def on_mount(self) -> None:
        self.add_columns("", "Check", "State")

    def set_checks(self, checks: list[CheckRun]) -> None:
        self.clear()
        self.check_runs.clear()
        for index, check in enumerate(checks):
            key = f"{index}:{check.name}"
            self.check_runs[key] = check
            self.add_row(
                check_cell(check.bucket),
                check.name,
                check.raw_state,
                key=key,
            )

    def check_at_cursor(self) -> CheckRun | None:
        if not self.row_count:
            return None
        row_key, _ = self.coordinate_to_cell_key(self.cursor_coordinate)
        return self.check_runs.get(row_key.value or "")


class CommentPanel(Vertical):
    def __init__(self, comment: Comment) -> None:
        super().__init__(classes="comment")
        self.comment = comment

    def compose(self) -> ComposeResult:
        comment = self.comment
        kind = f" · {comment.kind}" if comment.kind != "comment" else ""
        when = comment.created_at.strftime("%Y-%m-%d %H:%M")
        yield Static(f"[b]{comment.author}[/b]{kind} · {when}", classes="comment-header")
        if comment.body:
            yield Markdown(comment.body)


class ThreadPanel(Vertical):
    def __init__(self, thread: ReviewThread) -> None:
        super().__init__(classes="thread")
        self.thread = thread

    def compose(self) -> ComposeResult:
        thread = self.thread
        badges = ["[green]resolved[/green]" if thread.is_resolved else "[yellow]open[/yellow]"]
        if thread.is_outdated:
            badges.append("[dim]outdated[/dim]")
        location = f"{thread.path}:{thread.line}" if thread.line else thread.path
        yield Static(f"[b cyan]{location}[/b cyan] · {' · '.join(badges)}", classes="thread-header")
        if thread.diff_hunk:
            yield Static(_diff_syntax(thread.diff_hunk), classes="diff-hunk")
        for comment in thread.comments:
            yield Static(
                f"[b]{comment.author}[/b] · {comment.created_at.strftime('%Y-%m-%d %H:%M')}",
                classes="comment-header",
            )
            yield Markdown(comment.body)


class PRDetailScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("s", "summarise", "Summarise (Claude)"),
        Binding("R", "review", "Review (Claude)"),
        Binding("d", "diagnose", "Diagnose check (Claude)"),
        Binding("o", "open_browser", "Open in browser"),
        Binding("c", "comment", "Comment"),
        Binding("a", "approve", "Approve"),
        Binding("x", "request_changes", "Request changes"),
        Binding("C", "close_pr", "Close PR"),
    ]

    def __init__(self, pr: PRSummary) -> None:
        super().__init__()
        self.pr = pr
        self.detail: PRDetail | None = None
        self.diff: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            f"[b]{self.pr.repo}#{self.pr.number}[/b] {escape(self.pr.title)}", id="pr-header"
        )
        with TabbedContent(id="pr-tabs"):
            with TabPane("Overview", id="tab-overview"):
                with VerticalScroll():
                    yield Static(id="pr-meta")
                    yield Markdown(id="pr-body")
            with TabPane("Conversation", id="tab-conversation"):
                yield VerticalScroll(id="conversation")
            with TabPane("Code comments", id="tab-threads"):
                yield VerticalScroll(id="threads")
            with TabPane("Checks", id="tab-checks"):
                with VerticalScroll():
                    yield ChecksTable(id="checks-table")
                    yield Static("No checks reported", classes="empty-note", id="checks-empty")
            with TabPane("Files", id="tab-files"):
                with VerticalScroll():
                    yield Static(id="diff-view")
        yield Footer()

    def on_mount(self) -> None:
        self.load_pr()

    @work(exclusive=True)
    async def load_pr(self) -> None:
        tabs = self.query_one("#pr-tabs")
        tabs.loading = True
        try:
            self.detail, self.diff = await asyncio.gather(
                self.app.client.fetch_pr_detail(self.pr.owner, self.pr.name, self.pr.number),
                self.app.client.fetch_diff(self.pr.owner, self.pr.name, self.pr.number),
            )
        except Exception as exc:
            self.notify(f"Failed to load PR: {exc}", severity="error", timeout=10)
            return
        finally:
            tabs.loading = False
        await self._populate()

    async def _populate(self) -> None:
        # TabbedContent mounts its panes asynchronously; if the fetch finishes
        # first, the target containers may not exist yet.
        for selector in (
            "#pr-meta",
            "#pr-body",
            "#conversation",
            "#threads",
            "#checks-table",
            "#diff-view",
        ):
            while not self.query(selector):
                await asyncio.sleep(0.05)

        detail, diff = self.detail, self.diff
        assert detail is not None and diff is not None
        pr = detail.summary

        # Refresh the header: PRs opened from a URL start with a bare reference.
        self.pr = pr
        self.query_one("#pr-header", Static).update(
            f"[b]{pr.repo}#{pr.number}[/b] {escape(pr.title)}"
        )

        state = detail.state + (" · draft" if pr.is_draft else "")
        meta = [
            f"[b]{pr.author}[/b] wants to merge [cyan]{detail.head_ref}[/cyan]"
            f" into [cyan]{detail.base_ref}[/cyan]",
            f"State: {state} · {detail.changed_files} files ·"
            f" [green]+{pr.additions}[/green] [red]-{pr.deletions}[/red]"
            f" · opened {detail.created_at.strftime('%Y-%m-%d')}",
        ]
        if detail.labels:
            meta.append("Labels: " + ", ".join(detail.labels))
        if detail.assignees:
            meta.append("Assignees: " + ", ".join(detail.assignees))
        if detail.reviewers:
            meta.append("Review requested: " + ", ".join(detail.reviewers))
        if detail.checks:
            counts = {"success": 0, "failure": 0, "pending": 0}
            for check in detail.checks:
                if check.bucket in counts:
                    counts[check.bucket] += 1
            parts = []
            if counts["success"]:
                parts.append(f"[green]{counts['success']} ✓[/green]")
            if counts["failure"]:
                parts.append(f"[red]{counts['failure']} ✗[/red]")
            if counts["pending"]:
                parts.append(f"[yellow]{counts['pending']} running[/yellow]")
            if parts:
                meta.append("Checks: " + " · ".join(parts))
        self.query_one("#pr-meta", Static).update("\n".join(meta))
        self.query_one("#pr-body", Markdown).update(detail.body or "*No description*")

        conversation = self.query_one("#conversation", VerticalScroll)
        await conversation.remove_children()
        if detail.conversation:
            await conversation.mount_all(CommentPanel(c) for c in detail.conversation)
        else:
            await conversation.mount(Static("No comments yet", classes="empty-note"))

        threads = self.query_one("#threads", VerticalScroll)
        await threads.remove_children()
        if detail.review_threads:
            await threads.mount_all(ThreadPanel(t) for t in detail.review_threads)
        else:
            await threads.mount(Static("No code review comments", classes="empty-note"))

        checks_table = self.query_one("#checks-table", ChecksTable)
        checks_table.set_checks(detail.checks)
        checks_table.display = bool(detail.checks)
        self.query_one("#checks-empty", Static).display = not detail.checks

        self.query_one("#diff-view", Static).update(
            _diff_syntax(diff) if diff else "No diff available"
        )

    def _claude_ready(self) -> bool:
        if self.detail is None or self.diff is None:
            self.notify("Still loading the PR — try again in a moment", severity="warning")
            return False
        return True

    def action_summarise(self) -> None:
        if self._claude_ready():
            prompt = build_summary_prompt(self.detail, self.diff)
            self.app.push_screen(
                ClaudeOutputScreen(f"Summary of {self.pr.repo}#{self.pr.number}", prompt)
            )

    def action_review(self) -> None:
        if self._claude_ready():
            prompt = build_review_prompt(self.detail, self.diff)
            self.app.push_screen(
                ClaudeOutputScreen(f"Review of {self.pr.repo}#{self.pr.number}", prompt)
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if not isinstance(event.data_table, ChecksTable):
            return
        check = event.data_table.check_runs.get(event.row_key.value or "")
        if check is None:
            return
        if check.url:
            webbrowser.open(check.url)
        else:
            self.notify("This check has no details URL", severity="warning")

    def action_diagnose(self) -> None:
        focused = self.focused
        if not isinstance(focused, ChecksTable):
            self.notify("Open the Checks tab and select a check first", severity="warning")
            return
        check = focused.check_at_cursor()
        if check is None:
            self.notify("No check selected", severity="warning")
            return
        if not check.is_failed:
            self.notify(f"{check.name} did not fail — nothing to diagnose")
            return
        if check.job_id is None:
            self.notify(
                "No logs available for external checks — press Enter to open in browser",
                severity="warning",
            )
            return
        self.diagnose_check(check)

    @work(exclusive=True)
    async def diagnose_check(self, check: CheckRun) -> None:
        self.notify(f"Fetching logs for {check.name}…", timeout=4)
        try:
            logs = await self.app.client.fetch_job_logs(
                self.pr.owner, self.pr.name, check.job_id
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (404, 410):
                self.notify(
                    f"Logs for {check.name} have expired or are unavailable",
                    severity="warning",
                )
            else:
                self.notify(f"Failed to fetch logs: {exc}", severity="error", timeout=10)
            return
        except Exception as exc:
            self.notify(f"Failed to fetch logs: {exc}", severity="error", timeout=10)
            return
        prompt = build_ci_diagnosis_prompt(self.pr, check, logs)
        self.app.push_screen(ClaudeOutputScreen(f"CI diagnosis: {check.name}", prompt))

    def action_open_browser(self) -> None:
        webbrowser.open(self.pr.url)

    def action_back(self) -> None:
        self.app.pop_screen()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        mutating = {"comment", "approve", "request_changes", "close_pr"}
        if action in mutating:
            return self.detail is not None and self.detail.state == "OPEN"
        return True

    def _mutation_ready(self) -> bool:
        if self.detail is None:
            self.notify("Still loading the PR — try again in a moment", severity="warning")
            return False
        if self.detail.state != "OPEN":
            self.notify(f"PR is {self.detail.state.lower()} — cannot modify", severity="warning")
            return False
        return True

    def action_comment(self) -> None:
        if not self._mutation_ready():
            return
        self.app.push_screen(
            TextInputModal("Add comment", required=True),
            self._on_comment_result,
        )

    def _on_comment_result(self, body: str | None) -> None:
        if body is None:
            return
        self._post_comment(body)

    @work(exclusive=True, group="pr-mutation")
    async def _post_comment(self, body: str) -> None:
        try:
            await self.app.client.comment_on_pr(
                self.pr.owner, self.pr.name, self.pr.number, body
            )
        except Exception as exc:
            self.notify(f"Failed to post comment: {exc}", severity="error", timeout=10)
            return
        self.notify("Comment posted")
        self.load_pr()

    def action_approve(self) -> None:
        if not self._mutation_ready():
            return
        self.app.push_screen(
            TextInputModal("Approve — add a comment (optional)"),
            self._on_approve_result,
        )

    def _on_approve_result(self, body: str | None) -> None:
        if body is None:
            return
        self._submit_review("APPROVE", body)

    def action_request_changes(self) -> None:
        if not self._mutation_ready():
            return
        self.app.push_screen(
            TextInputModal("Request changes", required=True),
            self._on_request_changes_result,
        )

    def _on_request_changes_result(self, body: str | None) -> None:
        if body is None:
            return
        self._submit_review("REQUEST_CHANGES", body)

    @work(exclusive=True, group="pr-mutation")
    async def _submit_review(self, event: str, body: str) -> None:
        assert self.detail is not None
        label = event.lower().replace("_", " ")
        try:
            await self.app.client.submit_review(self.detail.summary.node_id, event, body)
        except Exception as exc:
            self.notify(f"Failed to submit review: {exc}", severity="error", timeout=10)
            return
        self.notify(f"Review submitted: {label}")
        self.load_pr()

    def action_close_pr(self) -> None:
        if not self._mutation_ready():
            return
        self.app.push_screen(
            ConfirmModal(
                "Close PR",
                f"Close {self.pr.repo}#{self.pr.number}?",
            ),
            self._on_close_confirmed,
        )

    def _on_close_confirmed(self, confirmed: bool) -> None:
        if not confirmed:
            return
        self._close_pr()

    @work(exclusive=True, group="pr-mutation")
    async def _close_pr(self) -> None:
        assert self.detail is not None
        try:
            await self.app.client.close_pr(self.detail.summary.node_id)
        except Exception as exc:
            self.notify(f"Failed to close PR: {exc}", severity="error", timeout=10)
            return
        self.notify("PR closed")
        self.load_pr()
