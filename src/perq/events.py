"""Diff dashboard snapshots into notification-worthy change events."""

from __future__ import annotations

from dataclasses import dataclass

from perq.github import Dashboard, PRSummary

MAX_EVENTS = 5


@dataclass(frozen=True)
class ChangeEvent:
    key: str  # "owner/repo#123"
    title: str
    message: str
    severity: str  # "information" / "warning" / "error"


def _index(dashboard: Dashboard) -> dict[str, PRSummary]:
    prs: dict[str, PRSummary] = {}
    for section in (dashboard.mine, dashboard.review_requested, dashboard.involved):
        for pr in section:
            prs.setdefault(f"{pr.repo}#{pr.number}", pr)
    return prs


def diff_dashboards(old: Dashboard | None, new: Dashboard) -> list[ChangeEvent]:
    """Return events for meaningful transitions between two dashboard snapshots.

    Only transitions fire — never absolute states — so the first load (old=None)
    and PRs that merely appear or move between sections stay silent.
    """
    if old is None:
        return []

    old_index = _index(old)
    events: list[ChangeEvent] = []

    for pr in new.mine:
        key = f"{pr.repo}#{pr.number}"
        previous = old_index.get(key)
        if previous is None:
            continue
        label = f"{key}: {pr.title}"

        if pr.ci_state != previous.ci_state:
            if pr.ci_state in ("FAILURE", "ERROR"):
                events.append(ChangeEvent(key, "CI failed", label, "error"))
            elif pr.ci_state == "SUCCESS":
                events.append(ChangeEvent(key, "CI green", label, "information"))

        if pr.review_decision != previous.review_decision:
            if pr.review_decision == "APPROVED":
                events.append(ChangeEvent(key, "Approved", label, "information"))
            elif pr.review_decision == "CHANGES_REQUESTED":
                events.append(ChangeEvent(key, "Changes requested", label, "warning"))

    for section in (new.mine, new.review_requested):
        for pr in section:
            key = f"{pr.repo}#{pr.number}"
            previous = old_index.get(key)
            if previous is None or pr.comment_count <= previous.comment_count:
                continue
            count = pr.comment_count - previous.comment_count
            plural = "s" if count > 1 else ""
            events.append(
                ChangeEvent(
                    key,
                    f"{count} new comment{plural}",
                    f"{key}: {pr.title}",
                    "information",
                )
            )

    old_requested = {f"{pr.repo}#{pr.number}" for pr in old.review_requested}
    for pr in new.review_requested:
        key = f"{pr.repo}#{pr.number}"
        if key not in old_requested:
            events.append(
                ChangeEvent(key, "Review requested", f"{key}: {pr.title}", "warning")
            )

    if len(events) > MAX_EVENTS:
        return [
            ChangeEvent(
                "",
                "Dashboard updated",
                f"{len(events)} PR updates since last refresh",
                "information",
            )
        ]
    return events


def osascript_args(title: str, message: str) -> list[str]:
    """Build a macOS `display notification` command with quoted strings escaped."""

    def esc(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    return [
        "osascript",
        "-e",
        f'display notification "{esc(message)}" with title "{esc(title)}"',
    ]
