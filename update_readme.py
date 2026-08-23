"""Fetch authored commits and merged PRs, then update README.md."""

from __future__ import annotations

import json
import os
import re
import subprocess  # nosec B404
import sys
import time
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from contribution_types import (
    AccountInfo,
    AuthoredCommitInfo,
    CommitRepositoryInfo,
    PullRequestInfo,
    RepositoryContribution,
    RepositoryInfo,
    RepositoryOwner,
)
from github_queries import (
    ACCOUNT_QUERY,
    BRANCH_COAUTHOR_HISTORY_QUERY,
    BRANCH_HISTORY_QUERY,
    COMMIT_CONTRIBUTIONS_QUERY,
    MERGED_PRS_QUERY,
    OBJECT_HISTORY_QUERY,
    OPEN_PRS_QUERY,
)
from github_rest import is_commit_reachable, search_commits
from readme_render import (
    generate_commit_details,
    generate_pr_details,
    generate_summary,
    generate_table,
    group_by_repo,
)
from readme_sections import write_readme_sections
from request_budget import consume_github_request


GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
USERNAME = "Lidang-Jiang"
README_PATH = Path(__file__).parent / "README.md"
GRAPHQL_MAX_ATTEMPTS = 3
GRAPHQL_INITIAL_RETRY_DELAY_SECONDS = 2
CONTRIBUTION_WINDOW_DAYS = 90
UTC = timezone.utc
KNOWN_PROFILE_EMAILS = {"lidangjiang@gmail.com"}
MAX_COAUTHOR_CANDIDATES_PER_REPOSITORY = 25
MAX_HISTORY_PAGES_PER_BRANCH = 100


def _redact_token(message: str) -> str:
    """Redact the active GitHub token from diagnostic output."""
    redacted = message
    for token in (GH_TOKEN, GITHUB_TOKEN):
        if token:
            redacted = redacted.replace(token, "***")
    return redacted


def _run_graphql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run a GitHub GraphQL query with bounded exponential backoff."""
    command = ["gh", "api", "graphql", "-f", f"query={query}"]
    if variables is not None:
        for name, value in variables.items():
            if value is None:
                continue
            command.extend(["-F", f"{name}={value}"])

    for attempt in range(1, GRAPHQL_MAX_ATTEMPTS + 1):
        consume_github_request(f"GitHub GraphQL attempt {attempt}")
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

        loaded_response: object = json.loads(result.stdout)
        if not isinstance(loaded_response, dict):
            raise RuntimeError("GitHub GraphQL response was not an object")
        response = cast(dict[str, Any], loaded_response)
        if response.get("errors"):
            detail = _redact_token(json.dumps(response["errors"], ensure_ascii=False))
            raise RuntimeError(f"GitHub GraphQL response contained errors: {detail}")
        return response

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


def _fetch_account_info() -> AccountInfo:
    """Return the GitHub account ID and creation timestamp."""
    data = _run_graphql(ACCOUNT_QUERY)
    user = data.get("data", {}).get("user")
    if user is None:
        raise RuntimeError(f"GitHub user not found: {USERNAME}")
    return cast(AccountInfo, user)


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
    contributions = cast(
        list[RepositoryContribution],
        collection["commitContributionsByRepository"],
    )
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


def _discover_contribution_repositories(
    started_at: datetime,
    ended_at: datetime,
) -> dict[str, RepositoryInfo]:
    """Discover repositories from the contribution calendar without trusting counts."""
    repositories: dict[str, RepositoryInfo] = {}
    for window in contribution_windows(started_at, ended_at):
        for contribution in _fetch_commit_contribution_window(*window):
            repository = contribution["repository"]
            if not _is_external_public_repository(repository):
                continue
            repositories[repository["nameWithOwner"]] = repository

    return repositories


def _commit_has_user(commit: dict[str, Any]) -> bool:
    """Return whether a commit's author list includes the profile user."""
    authors = commit["authors"]
    if authors["pageInfo"]["hasNextPage"]:
        raise RuntimeError(f"Commit author list was truncated for {commit['oid']}")
    return any(
        (node.get("user") or {}).get("login", "").casefold() == USERNAME.casefold()
        for node in authors["nodes"]
    )


def _commit_has_user_as_coauthor(commit: dict[str, Any]) -> bool:
    """Return whether a real Co-authored-by trailer credits the profile user."""
    if not _commit_has_user(commit):
        return False
    user_emails = {
        node.get("email", "").casefold()
        for node in commit["authors"]["nodes"]
        if (node.get("user") or {}).get("login", "").casefold() == USERNAME.casefold()
        and node.get("email")
    }
    return bool(user_emails & _coauthor_trailer_emails(commit.get("message", "")))


def _coauthor_trailer_emails(message: str) -> set[str]:
    """Extract case-folded emails from real Co-authored-by trailers."""
    return {
        match.group(1).casefold()
        for match in re.finditer(
            r"^Co-authored-by:.*<([^>]+)>\s*$",
            message,
            flags=re.IGNORECASE | re.MULTILINE,
        )
    }


def _account_identity_emails(account: AccountInfo) -> set[str]:
    """Return public and GitHub-generated emails that identify the profile user."""
    emails = {email.casefold() for email in KNOWN_PROFILE_EMAILS}
    emails.add(f"{USERNAME}@users.noreply.github.com".casefold())
    database_id = account.get("databaseId")
    if database_id is not None:
        emails.add(f"{database_id}+{USERNAME}@users.noreply.github.com".casefold())
    return emails


def _next_page_cursor(
    page_info: dict[str, Any],
    seen_cursors: set[str],
    context: str,
) -> str | None:
    """Return a progressing cursor, or None after the final page."""
    if not page_info["hasNextPage"]:
        return None
    cursor = page_info.get("endCursor")
    if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
        raise RuntimeError(f"{context} is missing or repeated end cursor")
    seen_cursors.add(cursor)
    return cursor


def _fetch_branch_authored_commits(
    repository: RepositoryInfo,
    branch: str,
    author_id: str,
) -> list[AuthoredCommitInfo] | None:
    """Fetch every user-authored commit reachable from one upstream branch."""
    owner, name = repository["nameWithOwner"].split("/", 1)
    cursor = None
    seen_cursors: set[str] = set()
    commits: list[AuthoredCommitInfo] = []
    page_count = 0

    while True:
        if page_count >= MAX_HISTORY_PAGES_PER_BRANCH:
            raise RuntimeError(
                "GitHub branch history exceeded the page budget for "
                f"{repository['nameWithOwner']}:{branch}"
            )
        page_count += 1
        data = _run_graphql(
            BRANCH_HISTORY_QUERY,
            {
                "owner": owner,
                "name": name,
                "qualifiedRef": f"refs/heads/{branch}",
                "authorId": author_id,
                "cursor": cursor,
            },
        )
        graph_repository = data["data"]["repository"]
        if graph_repository is None:
            raise RuntimeError(
                f"GitHub repository not found: {repository['nameWithOwner']}"
            )
        ref = graph_repository["ref"]
        if ref is None:
            return None
        target = ref["target"]
        if target["__typename"] != "Commit":
            raise RuntimeError(
                f"GitHub branch target is not a commit: "
                f"{repository['nameWithOwner']}:{branch}"
            )

        history = target["history"]
        for commit in history["nodes"]:
            converted = _to_authored_commit(commit, branch)
            if converted is not None:
                commits.append(converted)

        next_cursor = _next_page_cursor(
            history["pageInfo"],
            seen_cursors,
            f"GitHub branch history for {repository['nameWithOwner']}:{branch}",
        )
        if next_cursor is None:
            return commits
        cursor = next_cursor


def _fetch_default_branch_coauthored_commits(
    repository: RepositoryInfo,
    branch: str,
    account: AccountInfo,
) -> list[AuthoredCommitInfo]:
    """Find default-branch coauthor commits using repository-scoped search."""
    identity_emails = _account_identity_emails(account)
    context = f"coauthor search for {repository['nameWithOwner']}:{branch}"
    commits: list[AuthoredCommitInfo] = []
    candidate_oids: set[str] = set()

    for item in search_commits(repository["nameWithOwner"], identity_emails):
        oid = item.get("sha")
        raw_commit = item.get("commit")
        message = raw_commit.get("message") if isinstance(raw_commit, dict) else None
        if not isinstance(oid, str) or not isinstance(message, str):
            raise RuntimeError(f"GitHub commit search item was malformed: {context}")
        if _coauthor_trailer_emails(message) & identity_emails:
            candidate_oids.add(oid)

    if len(candidate_oids) > MAX_COAUTHOR_CANDIDATES_PER_REPOSITORY:
        raise RuntimeError(
            f"GitHub coauthor search exceeded the candidate budget for {context}"
        )

    for oid in sorted(candidate_oids):
        if not is_commit_reachable(repository["nameWithOwner"], branch, oid):
            continue
        history = _fetch_object_history(repository, oid, 1, context)
        if len(history) != 1 or history[0]["oid"] != oid:
            raise RuntimeError(f"GitHub commit search returned a stale OID: {context}")
        converted = _to_authored_commit(history[0], branch)
        if converted is not None and converted.get("coauthorOnly", False):
            commits.append(converted)

    return _deduplicate_commits(commits)


def _fetch_nondefault_branch_coauthored_commits(
    repository: RepositoryInfo,
    branch: str,
    account: AccountInfo,
) -> list[AuthoredCommitInfo]:
    """Scan complete non-default history since account creation for coauthors."""
    owner, name = repository["nameWithOwner"].split("/", 1)
    commits: list[AuthoredCommitInfo] = []
    cursor = None
    seen_cursors: set[str] = set()
    page_count = 0

    while True:
        if page_count >= MAX_HISTORY_PAGES_PER_BRANCH:
            raise RuntimeError(
                "GitHub coauthor history exceeded the page budget for "
                f"{repository['nameWithOwner']}:{branch}"
            )
        page_count += 1
        data = _run_graphql(
            BRANCH_COAUTHOR_HISTORY_QUERY,
            {
                "owner": owner,
                "name": name,
                "qualifiedRef": f"refs/heads/{branch}",
                "since": _format_github_datetime(
                    _parse_github_datetime(account["createdAt"])
                ),
                "cursor": cursor,
            },
        )
        graph_repository = data["data"]["repository"]
        if graph_repository is None:
            raise RuntimeError(
                f"GitHub repository not found: {repository['nameWithOwner']}"
            )
        ref = graph_repository["ref"]
        if ref is None or ref["target"]["__typename"] != "Commit":
            raise RuntimeError(
                f"GitHub branch disappeared during history scan: "
                f"{repository['nameWithOwner']}:{branch}"
            )
        history = ref["target"]["history"]
        for raw_commit in history["nodes"]:
            converted = _to_authored_commit(raw_commit, branch)
            if converted is not None and converted.get("coauthorOnly", False):
                commits.append(converted)
        next_cursor = _next_page_cursor(
            history["pageInfo"],
            seen_cursors,
            f"GitHub coauthor history for {repository['nameWithOwner']}:{branch}",
        )
        if next_cursor is None:
            return _deduplicate_commits(commits)
        cursor = next_cursor


def _fetch_branch_coauthored_commits(
    repository: RepositoryInfo,
    branch: str,
    account: AccountInfo,
    *,
    is_default_branch: bool,
) -> list[AuthoredCommitInfo]:
    """Find coauthor-only commits independently from contribution-day data."""
    if is_default_branch:
        return _fetch_default_branch_coauthored_commits(repository, branch, account)
    return _fetch_nondefault_branch_coauthored_commits(repository, branch, account)


def _to_authored_commit(
    commit: dict[str, Any],
    branch: str,
    *,
    reconstructed: bool = False,
) -> AuthoredCommitInfo | None:
    """Convert a GraphQL commit only when the profile user authored it."""
    if commit["authors"]["pageInfo"]["hasNextPage"]:
        raise RuntimeError(f"Commit author list was truncated for {commit['oid']}")
    first_author = cast(dict[str, Any], next(iter(commit["authors"]["nodes"]), {}))
    primary_login = (first_author.get("user") or {}).get("login", "")
    coauthor_only = primary_login.casefold() != USERNAME.casefold()
    if coauthor_only and not _commit_has_user_as_coauthor(commit):
        return None
    return {
        "oid": commit["oid"],
        "url": commit["url"],
        "messageHeadline": commit["messageHeadline"],
        "branches": [branch],
        "coauthorOnly": coauthor_only,
        "requiresDetail": reconstructed or coauthor_only,
    }


def _fetch_object_history(
    repository: RepositoryInfo,
    oid: str,
    count: int,
    context: str,
) -> list[dict[str, Any]]:
    """Fetch a fixed history prefix starting from one commit object."""
    owner, name = repository["nameWithOwner"].split("/", 1)
    data = _run_graphql(
        OBJECT_HISTORY_QUERY,
        {
            "owner": owner,
            "name": name,
            "oid": oid,
            "count": count,
        },
    )
    graph_repository = data["data"]["repository"]
    if graph_repository is None:
        raise RuntimeError(
            f"GitHub repository not found: {repository['nameWithOwner']}"
        )
    graph_object = graph_repository["object"]
    if graph_object is None or graph_object["__typename"] != "Commit":
        raise RuntimeError(f"Commit history object not found: {context}")
    return cast(list[dict[str, Any]], graph_object["history"]["nodes"])


def _fetch_merge_history(
    pull_request: PullRequestInfo,
    count: int,
) -> list[dict[str, Any]]:
    """Fetch actual destination history ending at a PR's merge commit."""
    merge_commit = pull_request.get("mergeCommit")
    if merge_commit is None:
        raise RuntimeError(f"Merged PR has no merge commit: {pull_request['url']}")
    history = _fetch_object_history(
        pull_request["repository"],
        merge_commit["oid"],
        count,
        pull_request["url"],
    )
    if not history or history[0]["oid"] != merge_commit["oid"]:
        raise RuntimeError(f"PR merge history has wrong head: {pull_request['url']}")
    return history


def _same_authored_change(source: dict[str, Any], actual: dict[str, Any]) -> bool:
    """Match a rebased commit using author metadata preserved by Git."""
    source_author = source.get("author") or {}
    actual_author = actual.get("author") or {}
    return (
        source.get("message") == actual.get("message")
        and source.get("authoredDate") == actual.get("authoredDate")
        and (source_author.get("email") or "").casefold()
        == (actual_author.get("email") or "").casefold()
    )


def _reconstruct_pr_authored_commits(
    pull_request: PullRequestInfo,
) -> list[AuthoredCommitInfo]:
    """Map one merged PR to the commits that actually landed upstream."""
    merge_commit = pull_request.get("mergeCommit")
    commit_connection = pull_request.get("commits")
    if merge_commit is None or commit_connection is None:
        raise RuntimeError(f"PR has incomplete merge data: {pull_request['url']}")
    if (
        commit_connection["pageInfo"]["hasNextPage"]
        or len(commit_connection["nodes"]) != commit_connection["totalCount"]
    ):
        raise RuntimeError(f"PR has incomplete commit list: {pull_request['url']}")

    source_commits = [node["commit"] for node in commit_connection["nodes"]]
    if not source_commits:
        raise RuntimeError(f"Merged PR has no source commits: {pull_request['url']}")

    parent_count = merge_commit["parents"]["totalCount"]
    if parent_count >= 2:
        parent_nodes = merge_commit["parents"].get("nodes", [])
        if len(parent_nodes) != parent_count:
            raise RuntimeError(f"PR has incomplete parent list: {pull_request['url']}")
        newest_first = _fetch_object_history(
            pull_request["repository"],
            parent_nodes[-1]["oid"],
            len(source_commits),
            pull_request["url"],
        )
        if len(newest_first) != len(source_commits):
            raise RuntimeError(f"Incomplete PR parent history: {pull_request['url']}")
        oldest_first = list(reversed(newest_first))
        if not all(
            source["oid"] == actual["oid"] or _same_authored_change(source, actual)
            for source, actual in zip(source_commits, oldest_first, strict=True)
        ):
            raise RuntimeError(
                f"Ambiguous PR merge parent history: {pull_request['url']}"
            )
        actual_commits = [*oldest_first, merge_commit]
    elif len(source_commits) == 1:
        actual_commits = [merge_commit]
    else:
        newest_first = _fetch_merge_history(pull_request, len(source_commits))
        if len(newest_first) != len(source_commits):
            raise RuntimeError(f"Incomplete PR merge history: {pull_request['url']}")
        oldest_first = list(reversed(newest_first))
        matches = [
            _same_authored_change(source, actual)
            for source, actual in zip(source_commits, oldest_first, strict=True)
        ]
        if all(matches):
            actual_commits = oldest_first
        elif any(matches):
            raise RuntimeError(f"Ambiguous PR merge strategy: {pull_request['url']}")
        else:
            actual_commits = [merge_commit]

    branch = pull_request["baseRefName"]
    authored = [
        converted
        for commit in actual_commits
        if (converted := _to_authored_commit(commit, branch, reconstructed=True))
        is not None
    ]
    return _deduplicate_commits(authored)


def _deduplicate_commits(
    commits: list[AuthoredCommitInfo],
) -> list[AuthoredCommitInfo]:
    """Deduplicate commits by OID while preserving every containing branch."""
    by_oid: dict[str, AuthoredCommitInfo] = {}
    for commit in commits:
        previous = by_oid.get(commit["oid"])
        if previous is None:
            by_oid[commit["oid"]] = {
                **commit,
                "branches": sorted(set(commit["branches"])),
            }
            continue
        by_oid[commit["oid"]] = {
            **previous,
            "branches": sorted({*previous["branches"], *commit["branches"]}),
            "coauthorOnly": previous.get("coauthorOnly", False)
            and commit.get("coauthorOnly", False),
            "requiresDetail": previous.get("requiresDetail", False)
            or commit.get("requiresDetail", False),
        }
    return [by_oid[oid] for oid in sorted(by_oid)]


def fetch_authored_commit_contributions(
    prs: list[PullRequestInfo],
    now: datetime | None = None,
) -> dict[str, CommitRepositoryInfo]:
    """Count actual user-authored commits on relevant upstream branches."""
    account = _fetch_account_info()
    ended_at = now or datetime.now(UTC)
    started_at = _parse_github_datetime(account["createdAt"])
    candidates = _discover_contribution_repositories(started_at, ended_at)
    branches: dict[str, set[str]] = {}

    for name, repository in candidates.items():
        default_ref = repository["defaultBranchRef"]
        if default_ref is not None:
            branches.setdefault(name, set()).add(default_ref["name"])

    for pr in prs:
        repository = pr["repository"]
        if not _is_external_public_repository(repository):
            continue
        name = repository["nameWithOwner"]
        candidates[name] = repository
        repo_branches = branches.setdefault(name, set())
        default_ref = repository["defaultBranchRef"]
        if default_ref is not None:
            repo_branches.add(default_ref["name"])
        repo_branches.add(pr["baseRefName"])

    repositories: dict[str, CommitRepositoryInfo] = {}
    missing_branches: set[tuple[str, str]] = set()
    for name, repository in candidates.items():
        authored_commits: list[AuthoredCommitInfo] = []
        default_ref = repository["defaultBranchRef"]
        default_branch = default_ref["name"] if default_ref is not None else None
        for branch in sorted(branches.get(name, set())):
            branch_commits = _fetch_branch_authored_commits(
                repository, branch, account["id"]
            )
            if branch_commits is None:
                missing_branches.add((name, branch))
                continue
            authored_commits.extend(branch_commits)
            authored_commits.extend(
                _fetch_branch_coauthored_commits(
                    repository,
                    branch,
                    account,
                    is_default_branch=branch == default_branch,
                )
            )
        authored_commits.extend(
            commit
            for pull_request in prs
            if pull_request["repository"]["nameWithOwner"] == name
            if (
                pull_request["repository"]["nameWithOwner"],
                pull_request["baseRefName"],
            )
            in missing_branches
            for commit in _reconstruct_pr_authored_commits(pull_request)
        )
        repositories[name] = {
            "commits": _deduplicate_commits(authored_commits),
            "stars": repository["stargazerCount"],
            "url": repository["url"],
        }

    return repositories


def fetch_merged_prs() -> list[PullRequestInfo]:
    """Fetch all merged PRs using GitHub GraphQL API with pagination."""
    all_prs: list[PullRequestInfo] = []
    cursor = None
    seen_cursors: set[str] = set()

    while True:
        data = _run_graphql(MERGED_PRS_QUERY, {"cursor": cursor})
        pr_data = data["data"]["user"]["pullRequests"]
        all_prs.extend(pr_data["nodes"])

        next_cursor = _next_page_cursor(
            pr_data["pageInfo"], seen_cursors, "GitHub merged PR pagination"
        )
        if next_cursor is None:
            break
        cursor = next_cursor

    return all_prs


def fetch_open_pr_count() -> int:
    """Fetch total count of open PRs."""
    data = _run_graphql(OPEN_PRS_QUERY)
    return cast(int, data["data"]["user"]["pullRequests"]["totalCount"])


def update_readme(
    summary: str,
    table: str,
    commit_details: str,
    pr_details: str,
) -> None:
    """Replace dynamic sections in README.md."""
    write_readme_sections(README_PATH, summary, table, commit_details, pr_details)


def main() -> None:
    prs = fetch_merged_prs()
    commit_repositories = fetch_authored_commit_contributions(prs)
    open_prs = fetch_open_pr_count()
    repos = group_by_repo(prs, commit_repositories)
    total_commits = sum(len(info["commits"]) for info in repos.values())
    total_prs = sum(len(info["prs"]) for info in repos.values())
    total_stars = sum(info["stars"] for info in repos.values())
    table = generate_table(repos)
    commit_details = generate_commit_details(repos)
    pr_details = generate_pr_details(repos)
    summary = generate_summary(
        total_commits,
        total_prs,
        len(repos),
        total_stars,
        open_prs,
    )
    update_readme(summary, table, commit_details, pr_details)
    print(
        f"Updated README with {len(repos)} repositories, "
        f"{total_commits} authored commits, {total_prs} merged PRs, "
        f"{open_prs} open PRs."
    )


if __name__ == "__main__":
    main()
