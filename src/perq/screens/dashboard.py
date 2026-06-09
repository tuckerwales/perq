"""Dashboard screen: sections of PRs you own or are involved in."""

from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from datetime import datetime, timezone

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from perq.events import ChangeEvent, diff_dashboards, osascript_args
from perq.github import Dashboard, PRSummary

# Dashboard auto-refresh interval in seconds; set PERQ_REFRESH=0 to disable.
AUTO_REFRESH_SECONDS = float(os.environ.get("PERQ_REFRESH", "") or 60)

# macOS desktop banners for change events; set PERQ_DESKTOP_NOTIFY=0 to disable.
DESKTOP_NOTIFY = os.environ.get("PERQ_DESKTOP_NOTIFY", "1").lower() not in ("0", "false")

SECTIONS = [
    ("mine", "My open PRs"),
    ("review-requested", "Review requested"),
    ("involved", "Involved"),
]

COLUMNS = ("PR", "Title", "Author", "CI", "Review", "+/-", "💬", "Updated")


def relative_time(then: datetime) -> str:
    delta = datetime.now(timezone.utc) - then
    seconds = int(delta.total_seconds())
    for unit, span in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= span:
            return f"{seconds // span}{unit} ago"
    return "just now"


def ci_cell(state: str | None) -> Text:
    return {
        "SUCCESS": Text("✓", style="green"),
        "FAILURE": Text("✗", style="red"),
        "ERROR": Text("✗", style="red"),
        "PENDING": Text("●", style="yellow"),
        "EXPECTED": Text("●", style="yellow"),
    }.get(state or "", Text("-", style="dim"))


def review_cell(pr: PRSummary) -> Text:
    if pr.is_draft:
        return Text("draft", style="dim")
    return {
        "APPROVED": Text("approved", style="green"),
        "CHANGES_REQUESTED": Text("changes", style="red"),
        "REVIEW_REQUIRED": Text("pending", style="yellow"),
    }.get(pr.review_decision or "", Text("-", style="dim"))


class PRTable(DataTable):
    """A DataTable whose rows map to PRSummary objects."""

    def __init__(self, **kwargs) -> None:
        super().__init__(cursor_type="row", zebra_stripes=True, **kwargs)
        self.prs: dict[str, PRSummary] = {}

    def on_mount(self) -> None:
        self.add_columns(*COLUMNS)

    def set_prs(self, prs: list[PRSummary]) -> None:
        cursor_key = None
        if self.row_count:
            row_key, _ = self.coordinate_to_cell_key(self.cursor_coordinate)
            cursor_key = row_key.value
        self.clear()
        self.prs.clear()
        for pr in prs:
            key = f"{pr.repo}#{pr.number}"
            self.prs[key] = pr
            self.add_row(
                Text(key, style="bold cyan"),
                pr.title,
                pr.author,
                ci_cell(pr.ci_state),
                review_cell(pr),
                Text.assemble((f"+{pr.additions}", "green"), " ", (f"-{pr.deletions}", "red")),
                str(pr.comment_count) if pr.comment_count else "-",
                relative_time(pr.updated_at),
                key=key,
            )
        if cursor_key in self.prs:
            self.move_cursor(row=self.get_row_index(cursor_key))

    def pr_at_cursor(self) -> PRSummary | None:
        if not self.row_count:
            return None
        row_key, _ = self.coordinate_to_cell_key(self.cursor_coordinate)
        return self.prs.get(row_key.value or "")


class DashboardScreen(Screen):
    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("o", "open_browser", "Open in browser"),
        Binding("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="dashboard"):
            for section_id, title in SECTIONS:
                yield Static(title, classes="section-title")
                yield PRTable(id=f"table-{section_id}")
                yield Static("No pull requests", classes="empty-note", id=f"empty-{section_id}")
        yield Footer()

    def on_mount(self) -> None:
        self._loaded_once = False
        self._last_snapshot: Dashboard | None = None
        if AUTO_REFRESH_SECONDS > 0:
            self.set_interval(AUTO_REFRESH_SECONDS, self._refresh_tick)

    def on_screen_resume(self) -> None:
        # Fires on first push and whenever we return from a PR detail screen.
        quiet = self._loaded_once
        self._loaded_once = True
        self.load_dashboard(quiet=quiet)

    def _refresh_tick(self) -> None:
        if self.is_current:
            self.load_dashboard(quiet=True)

    @work(exclusive=True)
    async def load_dashboard(self, quiet: bool = False) -> None:
        container = self.query_one("#dashboard")
        if not quiet:
            container.loading = True
        try:
            dashboard: Dashboard = await self.app.client.fetch_dashboard()
        except Exception as exc:
            if quiet:
                self.sub_title = "refresh failed"
            else:
                self.notify(f"Failed to load dashboard: {exc}", severity="error", timeout=10)
            return
        finally:
            if not quiet:
                container.loading = False

        events = diff_dashboards(self._last_snapshot, dashboard)
        self._last_snapshot = dashboard
        self._emit_events(events)

        for section_id, prs in [
            ("mine", dashboard.mine),
            ("review-requested", dashboard.review_requested),
            ("involved", dashboard.involved),
        ]:
            table = self.query_one(f"#table-{section_id}", PRTable)
            note = self.query_one(f"#empty-{section_id}", Static)
            table.set_prs(prs)
            table.display = bool(prs)
            note.display = not prs

        self.app.all_prs = [
            *dashboard.mine,
            *dashboard.review_requested,
            *dashboard.involved,
        ]
        self.sub_title = f"updated {datetime.now():%H:%M:%S}"
        if not quiet:
            for table in self.query(PRTable):
                if table.row_count:
                    table.focus()
                    break

    def _emit_events(self, events: list[ChangeEvent]) -> None:
        desktop = DESKTOP_NOTIFY and sys.platform == "darwin" and not self.app.app_focus
        for event in events:
            self.notify(event.message, title=event.title, severity=event.severity, timeout=8)
            if desktop:
                subprocess.Popen(
                    osascript_args(event.title, event.message),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

    def _focused_pr(self) -> PRSummary | None:
        focused = self.focused
        if isinstance(focused, PRTable):
            return focused.pr_at_cursor()
        return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        pr = (
            event.data_table.prs.get(event.row_key.value or "")
            if isinstance(event.data_table, PRTable)
            else None
        )
        if pr is not None:
            from perq.screens.pr_detail import PRDetailScreen

            self.app.push_screen(PRDetailScreen(pr))

    def action_refresh(self) -> None:
        self.load_dashboard()

    def action_open_browser(self) -> None:
        pr = self._focused_pr()
        if pr is None:
            self.notify("No pull request selected", severity="warning")
            return
        webbrowser.open(pr.url)

    def action_quit(self) -> None:
        self.app.exit()
