"""Modal screen for multi-line text input (comments, review bodies)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static, TextArea


class TextInputModal(ModalScreen[str | None]):
    BINDINGS = [
        Binding("ctrl+s", "submit", "Submit", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    def __init__(self, title: str, required: bool = False) -> None:
        super().__init__()
        self._title = title
        self._required = required

    def compose(self) -> ComposeResult:
        hint = "ctrl+s submit | esc cancel"
        if self._required:
            hint = f"body required · {hint}"
        with Vertical(id="text-input-modal"):
            yield Static(self._title, id="text-input-title")
            yield TextArea(id="text-body")
            yield Static(hint, id="text-input-hint")

    def on_mount(self) -> None:
        self.query_one("#text-body", TextArea).focus()

    def action_submit(self) -> None:
        body = self.query_one("#text-body", TextArea).text
        if self._required and not body.strip():
            self.notify("Body is required", severity="warning")
            return
        self.dismiss(body)

    def action_cancel(self) -> None:
        self.dismiss(None)
