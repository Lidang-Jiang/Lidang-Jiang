"""Fetch authored commits and merged PRs, then update README.md."""

from __future__ import annotations

import json
import os
import re
import subprocess  # nosec B404
import sys
import time
from datetime import datetime, time as datetime_time, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any, TypedDict


GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
USERNAME = "Lidang-Jiang"
README_PATH = Path(__file__).parent / "README.md"
GRAPHQL_MAX_ATTEMPTS = 3
GRAPHQL_INITIAL_RETRY_DELAY_SECONDS = 2
CONTRIBUTION_WINDOW_DAYS = 90
MARKDOWN_PUNCTUATION = "\\`*_{}[]()#+-.!|>~$"
UTC = timezone.utc


class RepositoryOwner(TypedDict):
    login: str


class RepositoryInfo(TypedDict):
    nameWithOwner: str
    stargazerCount: int
    url: str
    visibility: str
    owner: RepositoryOwner


class PullRequestSummary(TypedDict):
    title: str
    url: str
    mergedAt: str


class PullRequestInfo(PullRequestSummary):
    repository: RepositoryInfo


class ContributionDay(TypedDict):
    commitCount: int


class ContributionPageInfo(TypedDict):
    hasNextPage: bool


class ContributionConnection(TypedDict):
    nodes: list[ContributionDay]
    pageInfo: ContributionPageInfo


class RepositoryContribution(TypedDict):
    repository: RepositoryInfo
    contributions: ContributionConnection


class CommitRepositoryInfo(TypedDict):
    commits: int
    stars: int
    url: str


class GroupedRepositoryInfo(CommitRepositoryInfo):
    prs: list[PullRequestSummary]


ACCOUNT_QUERY = (
    """
query {
  user(login: "%s") {
    createdAt
  }
}
"""
    % USERNAME
)

COMMIT_CONTRIBUTIONS_QUERY = (
    """
query($from: DateTime!, $to: DateTime!) {
  user(login: "%s") {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      totalRepositoriesWithContributedCommits
      commitContributionsByRepository(maxRepositories: 100) {
        repository {
          nameWithOwner
          stargazerCount
          url
          visibility
          owner { login }
        }
        contributions(first: 100) {
          nodes { commitCount }
          pageInfo { hasNextPage }
        }
      }
    }
  }
}
"""
    % USERNAME
)

MERGED_PRS_QUERY = (
    """
query($cursor: String) {
  user(login: "%s") {
    pullRequests(
      first: 100
      states: MERGED
      orderBy: {field: CREATED_AT, direction: DESC}
      after: $cursor
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        title
        url
        mergedAt
        repository {
          nameWithOwner
          stargazerCount
          url
          visibility
          owner { login }
        }
      }
    }
  }
}
"""
    % USERNAME
)

OPEN_PRS_QUERY = (
    """
{
  user(login: "%s") {
    pullRequests(first: 1, states: OPEN) {
      totalCount
    }
  }
}
"""
    % USERNAME
)


def _redact_token(message: str) -> str:
    """Redact the active GitHub token from diagnostic output."""
    redacted = message
    for token in (GH_TOKEN, GITHUB_TOKEN):
        if token:
            redacted = redacted.replace(token, "***")
    return redacted


def _run_graphql(
    query: str, variables: dict[str, str | None] | None = None
) -> dict[str, Any]:
    """Run a GitHub GraphQL query with bounded exponential backoff."""
    command = ["gh", "api", "graphql", "-f", f"query={query}"]
    if variables is not None:
        for name, value in variables.items():
            if value is None:
                continue
            command.extend(["-F", f"{name}={value}"])

    for attempt in range(1, GRAPHQL_MAX_ATTEMPTS + 1):
        try:
            # The command always starts with the fixed gh executable.
            result = subprocess.run(  # nosec B603
                command,
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as error:
            detail = _redact_token(
                (error.stderr or "gh returned no error details").strip()
            )
            if attempt == GRAPHQL_MAX_ATTEMPTS:
                raise RuntimeError(
                    "GitHub GraphQL request failed after "
                    f"{GRAPHQL_MAX_ATTEMPTS} attempts: {detail}"
                ) from error

            delay = GRAPHQL_INITIAL_RETRY_DELAY_SECONDS * 2 ** (attempt - 1)
            print(
                "GitHub GraphQL request failed on "
                f"attempt {attempt}/{GRAPHQL_MAX_ATTEMPTS}: {detail}. "
                f"Retrying in {delay} seconds.",
                file=sys.stderr,
            )
            time.sleep(delay)
            continue

        return json.loads(result.stdout)

    raise AssertionError("GraphQL retry loop exited unexpectedly")


def _parse_github_datetime(value: str) -> datetime:
    """Parse a GitHub UTC timestamp into a timezone-aware datetime."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _format_github_datetime(value: datetime) -> str:
    """Format a datetime for GitHub GraphQL DateTime variables."""
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def contribution_windows(
    started_at: datetime, ended_at: datetime
) -> list[tuple[datetime, datetime]]:
    """Return non-overlapping UTC calendar-day windows for contribution queries."""
    if started_at.tzinfo is None or ended_at.tzinfo is None:
        raise ValueError("Contribution range datetimes must be timezone-aware")
    if ended_at < started_at:
        raise ValueError("Contribution range end cannot be before its start")

    first_day = started_at.astimezone(UTC).date()
    last_day = ended_at.astimezone(UTC).date()
    current = datetime.combine(first_day, datetime_time.min, tzinfo=UTC)
    final = datetime.combine(last_day, datetime_time(23, 59, 59), tzinfo=UTC)
    windows: list[tuple[datetime, datetime]] = []

    while current <= final:
        window_end = min(
            current + timedelta(days=CONTRIBUTION_WINDOW_DAYS) - timedelta(seconds=1),
            final,
        )
        windows.append((current, window_end))
        current = window_end + timedelta(seconds=1)

    return windows


def _fetch_account_created_at() -> datetime:
    """Return the GitHub account creation timestamp."""
    data = _run_graphql(ACCOUNT_QUERY)
    user = data.get("data", {}).get("user")
    if user is None:
        raise RuntimeError(f"GitHub user not found: {USERNAME}")
    return _parse_github_datetime(user["createdAt"])


def _repository_commit_count(contribution: RepositoryContribution) -> int:
    """Count authored commits across a repository's contribution days."""
    return sum(node["commitCount"] for node in contribution["contributions"]["nodes"])


def _split_calendar_window(
    started_at: datetime, ended_at: datetime
) -> tuple[tuple[datetime, datetime], tuple[datetime, datetime]]:
    """Split a multi-day inclusive window at a UTC calendar-day boundary."""
    day_count = (ended_at.date() - started_at.date()).days + 1
    left_day_count = day_count // 2
    left_end_date = started_at.date() + timedelta(days=left_day_count - 1)
    left_end = datetime.combine(left_end_date, datetime_time(23, 59, 59), tzinfo=UTC)
    right_start = left_end + timedelta(seconds=1)
    return (started_at, left_end), (right_start, ended_at)


def _fetch_commit_contribution_window(
    started_at: datetime, ended_at: datetime
) -> list[RepositoryContribution]:
    """Fetch one complete contribution window, splitting if GitHub truncates it."""
    data = _run_graphql(
        COMMIT_CONTRIBUTIONS_QUERY,
        {
            "from": _format_github_datetime(started_at),
            "to": _format_github_datetime(ended_at),
        },
    )
    collection = data["data"]["user"]["contributionsCollection"]
    contributions = collection["commitContributionsByRepository"]
    enumerated_total = sum(_repository_commit_count(item) for item in contributions)
    enumerated_repositories = len(contributions)
    has_more = any(
        item["contributions"]["pageInfo"]["hasNextPage"] for item in contributions
    )

    is_complete = (
        enumerated_total == collection["totalCommitContributions"]
        and enumerated_repositories
        == collection["totalRepositoriesWithContributedCommits"]
        and not has_more
    )
    if is_complete:
        return contributions
    if started_at.date() == ended_at.date():
        raise RuntimeError(
            "GitHub returned incomplete commit contributions for "
            f"{started_at.date().isoformat()}"
        )

    left_window, right_window = _split_calendar_window(started_at, ended_at)
    return [
        *_fetch_commit_contribution_window(*left_window),
        *_fetch_commit_contribution_window(*right_window),
    ]


def _is_external_public_repository(repository: RepositoryInfo) -> bool:
    """Return whether a repository belongs in the public external-project list."""
    owner: RepositoryOwner = repository["owner"]
    return (
        repository.get("visibility") == "PUBLIC"
        and owner.get("login", "").casefold() != USERNAME.casefold()
    )


def fetch_authored_commit_contributions(
    now: datetime | None = None,
) -> dict[str, CommitRepositoryInfo]:
    """Fetch all public external default-branch commits authored by the user."""
    ended_at = now or datetime.now(UTC)
    started_at = _fetch_account_created_at()
    repositories: dict[str, CommitRepositoryInfo] = {}

    for window in contribution_windows(started_at, ended_at):
        for contribution in _fetch_commit_contribution_window(*window):
            repository = contribution["repository"]
            if not _is_external_public_repository(repository):
                continue
            name = repository["nameWithOwner"]
            previous = repositories.get(name)
            previous_commits = previous["commits"] if previous is not None else 0
            repositories[name] = {
                "commits": previous_commits + _repository_commit_count(contribution),
                "stars": repository["stargazerCount"],
                "url": repository["url"],
            }

    return repositories


def fetch_merged_prs() -> list[PullRequestInfo]:
    """Fetch all merged PRs using GitHub GraphQL API with pagination."""
    all_prs: list[PullRequestInfo] = []
    cursor = None

    while True:
        data = _run_graphql(MERGED_PRS_QUERY, {"cursor": cursor})
        pr_data = data["data"]["user"]["pullRequests"]
        all_prs.extend(pr_data["nodes"])

        if not pr_data["pageInfo"]["hasNextPage"]:
            break
        cursor = pr_data["pageInfo"]["endCursor"]

    return all_prs


def fetch_open_pr_count() -> int:
    """Fetch total count of open PRs."""
    data = _run_graphql(OPEN_PRS_QUERY)
    return data["data"]["user"]["pullRequests"]["totalCount"]


def group_by_repo(
    prs: list[PullRequestInfo],
    commit_repositories: dict[str, CommitRepositoryInfo],
) -> dict[str, GroupedRepositoryInfo]:
    """Union merged PR and authored commit contributions by repository."""
    repos: dict[str, GroupedRepositoryInfo] = {
        name: {
            "prs": [],
            "commits": info["commits"],
            "stars": info["stars"],
            "url": info["url"],
        }
        for name, info in commit_repositories.items()
    }

    for pr in prs:
        repo = pr["repository"]
        if not _is_external_public_repository(repo):
            continue
        name = repo["nameWithOwner"]
        previous = repos.get(
            name,
            {"prs": [], "commits": 0, "stars": 0, "url": ""},
        )
        repos[name] = {
            "prs": [*previous["prs"], pr],
            "commits": previous["commits"],
            "stars": repo["stargazerCount"],
            "url": repo["url"],
        }

    # Sort by star count descending, then by name for deterministic ties.
    return dict(
        sorted(
            repos.items(),
            key=lambda item: (-item[1]["stars"], item[0].casefold()),
        )
    )


def format_stars(count: int) -> str:
    """Format star count with K suffix for readability."""
    if count >= 1000:
        return f"{count / 1000:.1f}k".replace(".0k", "k")
    return str(count)


def repo_anchor(name: str) -> str:
    """Build a stable README anchor for a repository's PR details."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"merged-prs-{slug}"


def sorted_prs(info: GroupedRepositoryInfo) -> list[PullRequestSummary]:
    """Return repository PRs in newest-merged-first order."""
    return sorted(info["prs"], key=lambda p: p["mergedAt"], reverse=True)


def pr_number(pr: PullRequestSummary) -> str:
    """Extract the PR number from a pull request URL."""
    return pr["url"].split("/")[-1]


def html_text(text: str) -> str:
    """Normalize and escape untrusted text for a native HTML text node."""
    return escape(" ".join(text.split()), quote=False)


def markdown_text(text: str) -> str:
    """Render untrusted text without allowing Markdown formatting."""
    escaped = html_text(text)
    for punctuation in MARKDOWN_PUNCTUATION:
        escaped = escaped.replace(punctuation, f"\\{punctuation}")
    return escaped


def generate_table(repos: dict[str, GroupedRepositoryInfo]) -> str:
    """Generate markdown table from grouped PRs."""
    lines = [
        "| Repository | Stars | Authored Commits | Merged PRs | PR Links |",
        "|:-----------|------:|:----------------:|:----------:|:---------|",
    ]

    for name, info in repos.items():
        pr_count = len(info["prs"])
        stars = format_stars(info["stars"])
        commits_url = f"{info['url']}/commits?author={USERNAME}"
        commit_link = f"[{info['commits']}]({commits_url})"
        # Keep large rows compact, but link to generated details instead of
        # GitHub search because search can omit older transferred PRs.
        if pr_count == 0:
            pr_links = "—"
        elif pr_count <= 3:
            pr_links = ", ".join(
                f"[#{pr_number(pr)}]({pr['url']})" for pr in sorted_prs(info)
            )
        else:
            pr_links = f"[View all](#{repo_anchor(name)})"

        repo_link = f"[{name}]({info['url']})"
        lines.append(
            f"| {repo_link} | {stars} | {commit_link} | {pr_count} | {pr_links} |"
        )

    return "\n".join(lines)


def generate_pr_details(repos: dict[str, GroupedRepositoryInfo]) -> str:
    """Generate complete PR details for repositories compacted in the table."""
    detail_repos = [
        (name, info) for name, info in repos.items() if len(info["prs"]) > 3
    ]
    if not detail_repos:
        return ""

    lines = ["### Merged PR Details", ""]
    for name, info in detail_repos:
        lines.extend(
            [
                f'<a id="{repo_anchor(name)}"></a>',
                "<details>",
                (
                    f"<summary><strong>{html_text(name)} "
                    f"({len(info['prs'])} merged PRs)</strong></summary>"
                ),
                "",
            ]
        )
        for pr in sorted_prs(info):
            lines.append(
                f"- [#{pr_number(pr)}]({pr['url']}) - {markdown_text(pr['title'])}"
            )
        lines.extend(["", "</details>", ""])

    return "\n".join(lines).rstrip()


def generate_summary(
    total_commits: int,
    total_prs: int,
    repo_count: int,
    total_stars: int,
    open_prs: int,
) -> str:
    """Generate summary line above the table."""
    stars_str = format_stars(total_stars)
    return (
        f"> **{total_commits}** authored commits and **{total_prs}** merged PRs "
        f"across **{repo_count}** external projects "
        f"({stars_str}+ combined stars)"
        f" · **{open_prs}** open PRs in review"
    )


def replace_section(readme: str, section: str, content: str) -> str:
    """Replace content between START/END markers for a given section."""
    start_marker = f"<!-- START_SECTION:{section} -->"
    end_marker = f"<!-- END_SECTION:{section} -->"
    if readme.count(start_marker) != 1 or readme.count(end_marker) != 1:
        raise RuntimeError(f"Missing or duplicate generated section: {section}")
    pattern = (
        rf"({re.escape(start_marker)})\n"
        r".*?"
        rf"({re.escape(end_marker)})"
    )

    def replacement(match: re.Match[str]) -> str:
        return f"{match.group(1)}\n{content}\n{match.group(2)}"

    updated, replacement_count = re.subn(
        pattern,
        replacement,
        readme,
        flags=re.DOTALL,
    )
    if replacement_count != 1:
        raise RuntimeError(f"Missing or duplicate generated section: {section}")
    return updated


def upsert_section_after(
    readme: str,
    section: str,
    content: str,
    after_section: str,
) -> str:
    """Replace a generated section or insert it after another generated section."""
    start_marker = f"<!-- START_SECTION:{section} -->"
    end_marker = f"<!-- END_SECTION:{after_section} -->"
    block = f"{start_marker}\n{content}\n<!-- END_SECTION:{section} -->"

    if start_marker in readme:
        return replace_section(readme, section, content)
    if readme.count(end_marker) != 1:
        raise RuntimeError(f"Missing marker or duplicate marker: {end_marker}")
    return readme.replace(end_marker, f"{end_marker}\n\n{block}", 1)


def update_readme(summary: str, table: str, pr_details: str) -> None:
    """Replace dynamic sections in README.md."""
    readme = README_PATH.read_text(encoding="utf-8")
    readme = replace_section(readme, "summary", summary)
    readme = replace_section(readme, "contributions", table)
    readme = upsert_section_after(
        readme,
        "pr_details",
        pr_details,
        "contributions",
    )
    README_PATH.write_text(readme.rstrip() + "\n", encoding="utf-8")


def main() -> None:
    prs = fetch_merged_prs()
    commit_repositories = fetch_authored_commit_contributions()
    open_prs = fetch_open_pr_count()
    repos = group_by_repo(prs, commit_repositories)
    total_commits = sum(info["commits"] for info in repos.values())
    total_prs = sum(len(info["prs"]) for info in repos.values())
    total_stars = sum(info["stars"] for info in repos.values())
    table = generate_table(repos)
    pr_details = generate_pr_details(repos)
    summary = generate_summary(
        total_commits,
        total_prs,
        len(repos),
        total_stars,
        open_prs,
    )
    update_readme(summary, table, pr_details)
    print(
        f"Updated README with {len(repos)} repositories, "
        f"{total_commits} authored commits, {total_prs} merged PRs, "
        f"{open_prs} open PRs."
    )


if __name__ == "__main__":
    main()
