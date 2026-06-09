"""Prompt templates for Claude Code summaries and reviews."""

from __future__ import annotations

import re

from perq.github import CheckRun, PRDetail, PRSummary

MAX_DIFF_CHARS = 120_000
MAX_COMMENTS = 30
MAX_LOG_CHARS = 60_000

# GitHub Actions prefixes every log line with an ISO timestamp.
_LOG_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ?", re.MULTILINE)


def _truncate_diff(diff: str) -> str:
    if len(diff) <= MAX_DIFF_CHARS:
        return diff
    return diff[:MAX_DIFF_CHARS] + "\n\n[... diff truncated for length ...]"


def _pr_context(detail: PRDetail, diff: str) -> str:
    pr = detail.summary
    lines = [
        f"# Pull request: {pr.repo}#{pr.number} — {pr.title}",
        f"Author: {pr.author} | State: {detail.state}"
        + (" (draft)" if pr.is_draft else ""),
        f"Branch: {detail.head_ref} -> {detail.base_ref}",
        f"Changes: {detail.changed_files} files, +{pr.additions} -{pr.deletions}",
    ]
    if detail.labels:
        lines.append(f"Labels: {', '.join(detail.labels)}")
    lines.append("\n## Description\n")
    lines.append(detail.body or "(no description)")

    if detail.conversation:
        lines.append("\n## Existing conversation\n")
        for comment in detail.conversation[:MAX_COMMENTS]:
            kind = f" [{comment.kind}]" if comment.kind != "comment" else ""
            lines.append(f"### {comment.author}{kind}\n{comment.body}\n")

    if detail.review_threads:
        lines.append("\n## Existing code review threads\n")
        for thread in detail.review_threads[:MAX_COMMENTS]:
            status = "resolved" if thread.is_resolved else "open"
            lines.append(f"### {thread.path}:{thread.line or '?'} ({status})")
            for comment in thread.comments:
                lines.append(f"- {comment.author}: {comment.body}")

    lines.append("\n## Diff\n")
    lines.append("```diff")
    lines.append(_truncate_diff(diff))
    lines.append("```")
    return "\n".join(lines)


def _tail_logs(logs: str) -> str:
    logs = _LOG_TIMESTAMP.sub("", logs)
    if len(logs) <= MAX_LOG_CHARS:
        return logs
    return "[... log truncated, showing tail ...]\n" + logs[-MAX_LOG_CHARS:]


def build_ci_diagnosis_prompt(pr: PRSummary, check: CheckRun, logs: str) -> str:
    return f"""You are diagnosing a failed CI check on a GitHub pull request. All the \
information you need is below — do not use any tools.

# Pull request: {pr.repo}#{pr.number} — {pr.title}
Failed check: {check.name} ({check.raw_state})

## Job logs

```
{_tail_logs(logs)}
```

Write a concise markdown diagnosis with these sections:
1. **What failed** — the failing step and the actual error, quoted briefly.
2. **Root cause** — why it failed, as specifically as the logs allow.
3. **Suggested fix** — concrete next steps, at the file/command level where possible.
4. **Flaky or real?** — whether this looks like an infrastructure/flake issue or a \
genuine problem with the change, with your confidence.

Be specific and quote the relevant log lines sparingly. Output only the diagnosis."""


def build_summary_prompt(detail: PRDetail, diff: str) -> str:
    return f"""You are summarising a GitHub pull request for a busy engineer. All the \
information you need is below — do not use any tools.

{_pr_context(detail, diff)}

Write a concise markdown summary with these sections:
1. **What it does** — one or two sentences.
2. **Key changes** — bullet points grouped by area of the codebase.
3. **Risk & size** — how risky/large is this change, what could break.
4. **Open questions** — anything unclear or worth asking the author (omit if none).

Be specific and reference file names where helpful. Output only the summary."""


def build_review_prompt(detail: PRDetail, diff: str) -> str:
    return f"""You are performing a local, advisory code review of a GitHub pull request. \
Your review is displayed in a terminal app and never posted to GitHub. All the \
information you need is below — do not use any tools.

{_pr_context(detail, diff)}

Write a thorough markdown code review:
1. **Verdict** — approve / request changes / needs discussion, with a one-line reason.
2. **Findings** — bugs, correctness issues, and edge cases, each with a `file:line` \
reference to the diff and a severity (critical/major/minor).
3. **Design & style** — structural or readability concerns worth raising.
4. **Security** — any security implications (omit if none).

Don't repeat points already raised in the existing conversation; build on them instead. \
Be direct and specific. Output only the review."""
