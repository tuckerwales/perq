"""Async GitHub API client backed by the `gh` CLI's auth token."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

API_URL = "https://api.github.com"


class GitHubAuthError(Exception):
    """Raised when no usable GitHub token can be found."""


def discover_token() -> str:
    """Get a token from `gh auth token`, falling back to $GITHUB_TOKEN."""
    gh = shutil.which("gh")
    if gh:
        result = subprocess.run(
            [gh, "auth", "token"], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    raise GitHubAuthError(
        "No GitHub token found. Run `gh auth login` or set GITHUB_TOKEN."
    )


@dataclass
class PRSummary:
    repo: str  # "owner/name"
    number: int
    title: str
    author: str
    url: str
    is_draft: bool
    review_decision: str | None  # APPROVED / CHANGES_REQUESTED / REVIEW_REQUIRED
    ci_state: str | None  # SUCCESS / FAILURE / PENDING / ERROR / EXPECTED
    additions: int
    deletions: int
    comment_count: int
    updated_at: datetime

    @property
    def owner(self) -> str:
        return self.repo.split("/")[0]

    @property
    def name(self) -> str:
        return self.repo.split("/")[1]


@dataclass
class CheckRun:
    name: str
    bucket: str  # success / failure / pending / skipped / neutral
    raw_state: str  # original status/conclusion/state, e.g. "COMPLETED: FAILURE"
    url: str | None
    kind: str  # "check_run" or "status"
    job_id: int | None  # Actions job id (CheckRun databaseId); None for StatusContext

    @property
    def is_failed(self) -> bool:
        return self.bucket == "failure"


@dataclass
class Comment:
    author: str
    body: str
    created_at: datetime
    kind: str = "comment"  # "comment" or a review state like "APPROVED"


@dataclass
class ThreadComment:
    author: str
    body: str
    created_at: datetime


@dataclass
class ReviewThread:
    path: str
    line: int | None
    is_resolved: bool
    is_outdated: bool
    diff_hunk: str
    comments: list[ThreadComment] = field(default_factory=list)


@dataclass
class PRDetail:
    summary: PRSummary
    body: str
    state: str  # OPEN / CLOSED / MERGED
    base_ref: str
    head_ref: str
    changed_files: int
    created_at: datetime
    labels: list[str] = field(default_factory=list)
    assignees: list[str] = field(default_factory=list)
    reviewers: list[str] = field(default_factory=list)
    conversation: list[Comment] = field(default_factory=list)
    review_threads: list[ReviewThread] = field(default_factory=list)
    checks: list[CheckRun] = field(default_factory=list)


@dataclass
class Dashboard:
    mine: list[PRSummary] = field(default_factory=list)
    review_requested: list[PRSummary] = field(default_factory=list)
    involved: list[PRSummary] = field(default_factory=list)


PR_FRAGMENT = """
fragment prFields on PullRequest {
  number
  title
  url
  isDraft
  reviewDecision
  additions
  deletions
  updatedAt
  author { login }
  repository { nameWithOwner }
  comments { totalCount }
  commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
}
"""

DASHBOARD_QUERY = (
    """
query Dashboard($mine: String!, $reviewRequested: String!, $involved: String!) {
  mine: search(query: $mine, type: ISSUE, first: 30) {
    nodes { ...prFields }
  }
  reviewRequested: search(query: $reviewRequested, type: ISSUE, first: 30) {
    nodes { ...prFields }
  }
  involved: search(query: $involved, type: ISSUE, first: 30) {
    nodes { ...prFields }
  }
}
"""
    + PR_FRAGMENT
)

DETAIL_QUERY = """
query PRDetail($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number
      title
      body
      url
      state
      isDraft
      baseRefName
      headRefName
      additions
      deletions
      changedFiles
      createdAt
      updatedAt
      reviewDecision
      author { login }
      repository { nameWithOwner }
      labels(first: 20) { nodes { name } }
      assignees(first: 10) { nodes { login } }
      reviewRequests(first: 10) {
        nodes {
          requestedReviewer {
            ... on User { login }
            ... on Team { name }
          }
        }
      }
      comments(first: 100) {
        nodes { author { login } body createdAt }
      }
      reviews(first: 50) {
        nodes { author { login } body state submittedAt }
      }
      reviewThreads(first: 100) {
        nodes {
          path
          line
          isResolved
          isOutdated
          comments(first: 50) {
            nodes { author { login } body createdAt diffHunk }
          }
        }
      }
      commits(last: 1) {
        nodes {
          commit {
            statusCheckRollup {
              state
              contexts(first: 100) {
                nodes {
                  __typename
                  ... on CheckRun {
                    databaseId
                    name
                    status
                    conclusion
                    detailsUrl
                  }
                  ... on StatusContext {
                    context
                    state
                    targetUrl
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _author(node: dict | None) -> str:
    return (node or {}).get("login") or "ghost"


def _ci_state(node: dict) -> str | None:
    commits = node.get("commits", {}).get("nodes") or []
    if not commits:
        return None
    rollup = (commits[0].get("commit") or {}).get("statusCheckRollup")
    return rollup.get("state") if rollup else None


_CHECK_RUN_BUCKETS = {
    "SUCCESS": "success",
    "FAILURE": "failure",
    "TIMED_OUT": "failure",
    "CANCELLED": "failure",
    "ACTION_REQUIRED": "failure",
    "STARTUP_FAILURE": "failure",
    "SKIPPED": "skipped",
    "NEUTRAL": "neutral",
}

_STATUS_CONTEXT_BUCKETS = {
    "SUCCESS": "success",
    "FAILURE": "failure",
    "ERROR": "failure",
    "PENDING": "pending",
    "EXPECTED": "pending",
}


def _parse_checks(node: dict) -> list[CheckRun]:
    commits = node.get("commits", {}).get("nodes") or []
    if not commits:
        return []
    rollup = (commits[0].get("commit") or {}).get("statusCheckRollup") or {}
    checks: list[CheckRun] = []
    for ctx in (rollup.get("contexts") or {}).get("nodes") or []:
        if ctx.get("__typename") == "CheckRun":
            status = ctx.get("status") or "?"
            conclusion = ctx.get("conclusion")
            if status != "COMPLETED":
                bucket = "pending"
            else:
                bucket = _CHECK_RUN_BUCKETS.get(conclusion or "", "neutral")
            checks.append(
                CheckRun(
                    name=ctx.get("name") or "?",
                    bucket=bucket,
                    raw_state=f"{status}: {conclusion}" if conclusion else status,
                    url=ctx.get("detailsUrl"),
                    kind="check_run",
                    job_id=ctx.get("databaseId"),
                )
            )
        elif ctx.get("__typename") == "StatusContext":
            state = ctx.get("state") or "?"
            checks.append(
                CheckRun(
                    name=ctx.get("context") or "?",
                    bucket=_STATUS_CONTEXT_BUCKETS.get(state, "neutral"),
                    raw_state=state,
                    url=ctx.get("targetUrl"),
                    kind="status",
                    job_id=None,
                )
            )
    return checks


def _parse_summary(node: dict) -> PRSummary:
    return PRSummary(
        repo=node["repository"]["nameWithOwner"],
        number=node["number"],
        title=node["title"],
        author=_author(node.get("author")),
        url=node["url"],
        is_draft=node["isDraft"],
        review_decision=node.get("reviewDecision"),
        ci_state=_ci_state(node),
        additions=node.get("additions", 0),
        deletions=node.get("deletions", 0),
        comment_count=node.get("comments", {}).get("totalCount", 0),
        updated_at=_parse_dt(node["updatedAt"]),
    )


class GitHubClient:
    def __init__(self, token: str | None = None) -> None:
        self._token = token or discover_token()
        self._client = httpx.AsyncClient(
            base_url=API_URL,
            headers={
                "Authorization": f"Bearer {self._token}",
                "X-Github-Next-Global-ID": "1",
            },
            timeout=30.0,
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _graphql(self, query: str, variables: dict) -> dict:
        response = await self._client.post(
            "/graphql", json={"query": query, "variables": variables}
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            messages = "; ".join(e.get("message", "?") for e in payload["errors"])
            raise RuntimeError(f"GitHub GraphQL error: {messages}")
        return payload["data"]

    async def fetch_dashboard(self) -> Dashboard:
        base = "is:pr is:open archived:false sort:updated-desc"
        data = await self._graphql(
            DASHBOARD_QUERY,
            {
                "mine": f"{base} author:@me",
                "reviewRequested": f"{base} review-requested:@me",
                "involved": f"{base} involves:@me -author:@me -review-requested:@me",
            },
        )

        def parse(key: str) -> list[PRSummary]:
            return [_parse_summary(n) for n in data[key]["nodes"] if n]

        return Dashboard(
            mine=parse("mine"),
            review_requested=parse("reviewRequested"),
            involved=parse("involved"),
        )

    async def fetch_pr_detail(self, owner: str, name: str, number: int) -> PRDetail:
        data = await self._graphql(
            DETAIL_QUERY, {"owner": owner, "name": name, "number": number}
        )
        pr = data["repository"]["pullRequest"]

        conversation: list[Comment] = [
            Comment(
                author=_author(c.get("author")),
                body=c["body"],
                created_at=_parse_dt(c["createdAt"]),
            )
            for c in pr["comments"]["nodes"]
        ]
        for review in pr["reviews"]["nodes"]:
            # Skip empty COMMENTED shells: their content lives in review threads.
            if not review["body"] and review["state"] == "COMMENTED":
                continue
            conversation.append(
                Comment(
                    author=_author(review.get("author")),
                    body=review["body"],
                    created_at=_parse_dt(review["submittedAt"]),
                    kind=review["state"],
                )
            )
        conversation.sort(key=lambda c: c.created_at)

        threads: list[ReviewThread] = []
        for t in pr["reviewThreads"]["nodes"]:
            comments = t["comments"]["nodes"]
            if not comments:
                continue
            threads.append(
                ReviewThread(
                    path=t["path"],
                    line=t.get("line"),
                    is_resolved=t["isResolved"],
                    is_outdated=t["isOutdated"],
                    diff_hunk=comments[0].get("diffHunk") or "",
                    comments=[
                        ThreadComment(
                            author=_author(c.get("author")),
                            body=c["body"],
                            created_at=_parse_dt(c["createdAt"]),
                        )
                        for c in comments
                    ],
                )
            )

        reviewers = []
        for req in pr["reviewRequests"]["nodes"]:
            reviewer = req.get("requestedReviewer") or {}
            reviewers.append(reviewer.get("login") or reviewer.get("name") or "?")

        return PRDetail(
            summary=_parse_summary(pr),
            body=pr["body"] or "",
            state=pr["state"],
            base_ref=pr["baseRefName"],
            head_ref=pr["headRefName"],
            changed_files=pr["changedFiles"],
            created_at=_parse_dt(pr["createdAt"]),
            labels=[l["name"] for l in pr["labels"]["nodes"]],
            assignees=[a["login"] for a in pr["assignees"]["nodes"]],
            reviewers=reviewers,
            conversation=conversation,
            review_threads=threads,
            checks=_parse_checks(pr),
        )

    async def fetch_job_logs(self, owner: str, name: str, job_id: int) -> str:
        response = await self._client.get(
            f"/repos/{owner}/{name}/actions/jobs/{job_id}/logs"
        )
        response.raise_for_status()
        return response.text

    async def fetch_diff(self, owner: str, name: str, number: int) -> str:
        response = await self._client.get(
            f"/repos/{owner}/{name}/pulls/{number}",
            headers={"Accept": "application/vnd.github.diff"},
        )
        response.raise_for_status()
        return response.text


async def _smoke() -> None:
    client = GitHubClient()
    try:
        dashboard = await client.fetch_dashboard()
        for label, prs in [
            ("My open PRs", dashboard.mine),
            ("Review requested", dashboard.review_requested),
            ("Involved", dashboard.involved),
        ]:
            print(f"{label} ({len(prs)}):")
            for pr in prs[:5]:
                print(f"  {pr.repo}#{pr.number} [{pr.ci_state}] {pr.title}")
        first = next(
            iter(dashboard.mine or dashboard.review_requested or dashboard.involved),
            None,
        )
        if first:
            detail = await client.fetch_pr_detail(first.owner, first.name, first.number)
            print(f"Checks for {first.repo}#{first.number} ({len(detail.checks)}):")
            for check in detail.checks:
                print(f"  [{check.bucket}] {check.name} ({check.raw_state}) job={check.job_id}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(_smoke())
