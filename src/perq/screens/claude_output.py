"""Modal screen that streams Claude Code output."""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Markdown, Static

from perq.claude import ClaudeError, ClaudeNotFoundError, ClaudeRunner
from perq.clipboard import copy_to_system_clipboard


class ClaudeOutputScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("c", "copy", "Copy to clipboard"),
    ]

    def __init__(self, title: str, prompt: str) -> None:
        super().__init__()
        self._title = title
        self._prompt = prompt
        self._runner = ClaudeRunner()
        self._text = ""
        self._dirty = False
        self._streaming = True

    def compose(self) -> ComposeResult:
        with Vertical(id="claude-modal"):
            yield Static(self._title, id="claude-title")
            with VerticalScroll(id="claude-scroll"):
                yield Markdown("", id="claude-md")
            yield Static("⠋ Asking Claude…", id="claude-status")

    def on_mount(self) -> None:
        self.set_interval(0.25, self._refresh)
        self.run_claude()

    @work(exclusive=True)
    async def run_claude(self) -> None:
        status = self.query_one("#claude-status", Static)
        try:
            async for chunk in self._runner.stream(self._prompt):
                self._text += chunk
                self._dirty = True
        except ClaudeNotFoundError as exc:
            status.update(f"[red]{exc}[/red]")
            self._streaming = False
            return
        except ClaudeError as exc:
            status.update(f"[red]Claude failed:[/red] {exc}")
            self._streaming = False
            return
        self._streaming = False
        self._dirty = True
        status.update("[green]Done[/green] · c copy · esc close")

    async def _refresh(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        await self.query_one("#claude-md", Markdown).update(self._text)
        scroll = self.query_one("#claude-scroll", VerticalScroll)
        if self._streaming and scroll.allow_vertical_scroll:
            scroll.scroll_end(animate=False)

    def action_close(self) -> None:
        self._runner.stop()
        self.workers.cancel_node(self)
        self.dismiss()

    def action_copy(self) -> None:
        if not self._text:
            return
        # OSC 52 for terminals that support it (remote sessions etc.).
        self.app.copy_to_clipboard(self._text)
        if copy_to_system_clipboard(self._text):
            self.notify("Copied to clipboard")
        else:
            self.notify(
                "No clipboard tool found — sent OSC 52, which your terminal may ignore",
                severity="warning",
            )
