"""perq application entry point."""

from __future__ import annotations

import sys

from textual.app import App

from perq.commands import OpenPRCommands
from perq.github import GitHubAuthError, GitHubClient, PRSummary, discover_token
from perq.screens.dashboard import DashboardScreen


class PerqApp(App):
    """A dashboard for your GitHub pull requests."""

    TITLE = "perq"
    CSS_PATH = "perq.tcss"
    COMMANDS = App.COMMANDS | {OpenPRCommands}

    def __init__(self, token: str | None = None, client: GitHubClient | None = None):
        super().__init__()
        self._token = token
        self.client = client
        self.all_prs: list[PRSummary] = []

    def on_mount(self) -> None:
        if self.client is None:
            self.client = GitHubClient(self._token)
        self.push_screen(DashboardScreen())

    async def on_unmount(self) -> None:
        if self.client is not None:
            await self.client.close()


def main() -> None:
    try:
        token = discover_token()
    except GitHubAuthError as exc:
        sys.exit(str(exc))
    PerqApp(token=token).run()


if __name__ == "__main__":
    main()
