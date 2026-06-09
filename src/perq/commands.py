"""Command palette commands for opening pull requests."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from functools import partial

from textual.command import DiscoveryHit, Hit, Hits, Provider

from perq.github import PRSummary

# https://github.com/owner/repo/pull/123, owner/repo#123 or owner/repo/123
PR_REF_RE = re.compile(
    r"""^(?:https?://(?:www\.)?github\.com/)?
        (?P<owner>[\w.-]+)/(?P<name>[\w.-]+)
        (?:/pull/|\#|/)(?P<number>\d+)
        (?:/?(?:[?#].*)?)?$""",
    re.VERBOSE,
)


def parse_pr_ref(query: str) -> PRSummary | None:
    """Parse a PR URL or owner/repo#123 shorthand into a placeholder PRSummary."""
    match = PR_REF_RE.match(query.strip())
    if match is None:
        return None
    owner, name, number = match["owner"], match["name"], int(match["number"])
    return PRSummary(
        repo=f"{owner}/{name}",
        number=number,
        title="",
        author="",
        url=f"https://github.com/{owner}/{name}/pull/{number}",
        is_draft=False,
        review_decision=None,
        ci_state=None,
        additions=0,
        deletions=0,
        comment_count=0,
        updated_at=datetime.now(timezone.utc),
    )


class OpenPRCommands(Provider):
    """Open a PR from a pasted URL, or jump to one already on the dashboard."""

    def _open(self, pr: PRSummary) -> None:
        from perq.screens.pr_detail import PRDetailScreen

        self.app.push_screen(PRDetailScreen(pr))

    def _dashboard_prs(self) -> list[PRSummary]:
        return getattr(self.app, "all_prs", [])

    async def discover(self) -> Hits:
        yield DiscoveryHit(
            "Open PR by URL…",
            lambda: None,
            help="Type or paste a GitHub PR URL or owner/repo#123 here",
        )
        for pr in self._dashboard_prs()[:10]:
            yield DiscoveryHit(
                f"Open {pr.repo}#{pr.number}: {pr.title}",
                partial(self._open, pr),
                help=f"by {pr.author}",
            )

    async def search(self, query: str) -> Hits:
        ref = parse_pr_ref(query)
        if ref is not None:
            yield Hit(
                1.0,
                f"Open {ref.repo}#{ref.number}",
                partial(self._open, ref),
                help="Fetch and view this pull request",
            )
            return

        matcher = self.matcher(query)
        for pr in self._dashboard_prs():
            text = f"Open {pr.repo}#{pr.number}: {pr.title}"
            score = matcher.match(text)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(text),
                    partial(self._open, pr),
                    help=f"by {pr.author}",
                )
