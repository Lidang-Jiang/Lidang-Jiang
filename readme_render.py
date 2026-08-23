"""Render GitHub contribution statistics as safe Markdown."""

from __future__ import annotations

import re
from html import escape
from collections.abc import Sequence
from urllib.parse import quote

from contribution_types import (
    CommitRepositoryInfo,
    GroupedRepositoryInfo,
    PullRequestInfo,
    PullRequestSummary,
)


USERNAME = "Lidang-Jiang"
MARKDOWN_PUNCTUATION = "\\`*_{}[]()#+-.!|>~$"


def group_by_repo(
    prs: Sequence[PullRequestInfo],
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
        if (
            repo["visibility"] != "PUBLIC"
            or repo["owner"]["login"].casefold() == USERNAME.casefold()
        ):
            continue
        name = repo["nameWithOwner"]
        previous = repos.get(
            name,
            {"prs": [], "commits": [], "stars": 0, "url": ""},
        )
        repos[name] = {
            "prs": [*previous["prs"], pr],
            "commits": previous["commits"],
            "stars": repo["stargazerCount"],
            "url": repo["url"],
        }

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


def commit_anchor(name: str) -> str:
    """Build a stable README anchor for a repository's commit details."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"authored-commits-{slug}"


def sorted_prs(info: GroupedRepositoryInfo) -> list[PullRequestSummary]:
    """Return repository PRs in newest-merged-first order."""
    return sorted(
        info["prs"], key=lambda pull_request: pull_request["mergedAt"], reverse=True
    )


def pr_number(pull_request: PullRequestSummary) -> str:
    """Extract the PR number from a pull request URL."""
    return pull_request["url"].split("/")[-1]


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
    """Generate a Markdown table from grouped commits and PRs."""
    lines = [
        "| Repository | Stars | Authored / Co-authored Commits | Merged PRs | PR Links |",
        "|:-----------|------:|:----------------:|:----------:|:---------|",
    ]

    for name, info in repos.items():
        pr_count = len(info["prs"])
        commit_count = len(info["commits"])
        commit_branches = sorted(
            {branch for commit in info["commits"] for branch in commit["branches"]}
        )
        requires_detail = any(
            commit.get("requiresDetail", False) for commit in info["commits"]
        )
        if commit_count == 0:
            commit_link = "0"
        elif len(commit_branches) == 1 and not requires_detail:
            branch = quote(commit_branches[0], safe="")
            commits_url = f"{info['url']}/commits/{branch}?author={USERNAME}"
            commit_link = f"[{commit_count}]({commits_url})"
        else:
            commit_link = f"[{commit_count}](#{commit_anchor(name)})"

        if pr_count == 0:
            pr_links = "—"
        elif pr_count <= 3:
            pr_links = ", ".join(
                f"[#{pr_number(pull_request)}]({pull_request['url']})"
                for pull_request in sorted_prs(info)
            )
        else:
            pr_links = f"[View all](#{repo_anchor(name)})"

        repo_link = f"[{name}]({info['url']})"
        lines.append(
            f"| {repo_link} | {format_stars(info['stars'])} | {commit_link} | "
            f"{pr_count} | {pr_links} |"
        )

    return "\n".join(lines)


def generate_commit_details(repos: dict[str, GroupedRepositoryInfo]) -> str:
    """Generate exact commit links when contributions span multiple branches."""
    detail_repos = [
        (name, info)
        for name, info in repos.items()
        if (
            len({branch for commit in info["commits"] for branch in commit["branches"]})
            > 1
            or any(commit.get("requiresDetail", False) for commit in info["commits"])
        )
    ]
    if not detail_repos:
        return ""

    lines = ["### Authored Commit Details", ""]
    for name, info in detail_repos:
        lines.extend(
            [
                f'<a id="{commit_anchor(name)}"></a>',
                "<details>",
                (
                    f"<summary><strong>{html_text(name)} "
                    f"({len(info['commits'])} authored/co-authored commits)"
                    "</strong></summary>"
                ),
                "",
            ]
        )
        for commit in info["commits"]:
            branches = ", ".join(sorted(set(commit["branches"])))
            lines.append(
                f"- [{commit['oid'][:7]}]({commit['url']}) - "
                f"{markdown_text(commit['messageHeadline'])} "
                f"_(branches: {markdown_text(branches)})_"
            )
        lines.extend(["", "</details>", ""])

    return "\n".join(lines).rstrip()


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
        for pull_request in sorted_prs(info):
            lines.append(
                f"- [#{pr_number(pull_request)}]({pull_request['url']}) - "
                f"{markdown_text(pull_request['title'])}"
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
    return (
        f"> **{total_commits}** authored/co-authored commits and "
        f"**{total_prs}** merged PRs "
        f"across **{repo_count}** external projects "
        f"({format_stars(total_stars)}+ combined stars)"
        f" · **{open_prs}** open PRs in review"
    )
