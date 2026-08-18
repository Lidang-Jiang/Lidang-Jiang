"""Fetch merged PRs via GitHub GraphQL API and update README.md."""

from __future__ import annotations

import json
import os
import re
import subprocess  # nosec B404
import sys
import time
from collections import defaultdict
from html import escape
from pathlib import Path


GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
USERNAME = "Lidang-Jiang"
README_PATH = Path(__file__).parent / "README.md"
GRAPHQL_MAX_ATTEMPTS = 3
GRAPHQL_INITIAL_RETRY_DELAY_SECONDS = 2

MERGED_PRS_QUERY = """
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
        }
      }
    }
  }
}
""" % USERNAME

OPEN_PRS_QUERY = """
{
  user(login: "%s") {
    pullRequests(first: 1, states: OPEN) {
      totalCount
    }
  }
}
""" % USERNAME


def _redact_token(message: str) -> str:
    """Redact the active GitHub token from diagnostic output."""
    if not GITHUB_TOKEN:
        return message
    return message.replace(GITHUB_TOKEN, "***")


def _run_graphql(query: str, variables: dict | None = None) -> dict:
    """Run a GitHub GraphQL query with bounded exponential backoff."""
    command = ["gh", "api", "graphql", "-f", f"query={query}"]
    if variables is not None:
        command.extend(["-f", f"variables={json.dumps(variables)}"])

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


def fetch_merged_prs() -> list[dict]:
    """Fetch all merged PRs using GitHub GraphQL API with pagination."""
    all_prs: list[dict] = []
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


def group_by_repo(prs: list[dict]) -> dict[str, dict]:
    """Group PRs by repository, sorted by star count descending."""
    repos: dict[str, dict] = defaultdict(lambda: {"prs": [], "stars": 0, "url": ""})

    for pr in prs:
        repo = pr["repository"]
        name = repo["nameWithOwner"]
        # Skip own profile repo
        if name == f"{USERNAME}/{USERNAME}":
            continue
        repos[name]["prs"].append(pr)
        repos[name]["stars"] = repo["stargazerCount"]
        repos[name]["url"] = repo["url"]

    # Sort by star count descending
    return dict(sorted(repos.items(), key=lambda x: x[1]["stars"], reverse=True))


def format_stars(count: int) -> str:
    """Format star count with K suffix for readability."""
    if count >= 1000:
        return f"{count / 1000:.1f}k".replace(".0k", "k")
    return str(count)


def repo_anchor(name: str) -> str:
    """Build a stable README anchor for a repository's PR details."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"merged-prs-{slug}"


def sorted_prs(info: dict) -> list[dict]:
    """Return repository PRs in newest-merged-first order."""
    return sorted(info["prs"], key=lambda p: p["mergedAt"], reverse=True)


def pr_number(pr: dict) -> str:
    """Extract the PR number from a pull request URL."""
    return pr["url"].split("/")[-1]


def markdown_text(text: str) -> str:
    """Escape text that will be embedded in generated Markdown."""
    return escape(text, quote=False).replace("|", "\\|")


def generate_table(repos: dict[str, dict]) -> str:
    """Generate markdown table from grouped PRs."""
    lines = [
        "| Repository | Stars | PRs Merged | Links |",
        "|:-----------|------:|:----------:|:------|",
    ]

    for name, info in repos.items():
        pr_count = len(info["prs"])
        stars = format_stars(info["stars"])
        # Keep large rows compact, but link to generated details instead of
        # GitHub search because search can omit older transferred PRs.
        if pr_count <= 3:
            pr_links = ", ".join(
                f"[#{pr_number(pr)}]({pr['url']})"
                for pr in sorted_prs(info)
            )
        else:
            pr_links = f"[View all](#{repo_anchor(name)})"

        repo_link = f"[{name}]({info['url']})"
        lines.append(f"| {repo_link} | {stars} | {pr_count} | {pr_links} |")

    return "\n".join(lines)


def generate_pr_details(repos: dict[str, dict]) -> str:
    """Generate complete PR details for repositories compacted in the table."""
    detail_repos = [
        (name, info)
        for name, info in repos.items()
        if len(info["prs"]) > 3
    ]
    if not detail_repos:
        return ""

    lines = ["### Merged PR Details", ""]
    for name, info in detail_repos:
        lines.extend([
            f'<a id="{repo_anchor(name)}"></a>',
            "<details>",
            (
                f"<summary><strong>{markdown_text(name)} "
                f"({len(info['prs'])} merged PRs)</strong></summary>"
            ),
            "",
        ])
        for pr in sorted_prs(info):
            lines.append(
                f"- [#{pr_number(pr)}]({pr['url']}) - {markdown_text(pr['title'])}"
            )
        lines.extend(["", "</details>", ""])

    return "\n".join(lines).rstrip()


def generate_summary(
    total_prs: int, repo_count: int, total_stars: int, open_prs: int,
) -> str:
    """Generate summary line above the table."""
    stars_str = format_stars(total_stars)
    return (
        f"> **{total_prs}** merged PRs across **{repo_count}** projects "
        f"({stars_str}+ combined stars)"
        f" · **{open_prs}** open PRs in review"
    )


def replace_section(readme: str, section: str, content: str) -> str:
    """Replace content between START/END markers for a given section."""
    pattern = (
        rf"(<!-- START_SECTION:{section} -->)\n"
        r".*?"
        rf"(<!-- END_SECTION:{section} -->)"
    )
    return re.sub(
        pattern,
        lambda match: f"{match.group(1)}\n{content}\n{match.group(2)}",
        readme,
        flags=re.DOTALL,
    )


def upsert_section_after(
    readme: str, section: str, content: str, after_section: str,
) -> str:
    """Replace a generated section or insert it after another generated section."""
    start_marker = f"<!-- START_SECTION:{section} -->"
    end_marker = f"<!-- END_SECTION:{after_section} -->"
    block = f"{start_marker}\n{content}\n<!-- END_SECTION:{section} -->"

    if start_marker in readme:
        return replace_section(readme, section, content)
    if end_marker not in readme:
        raise RuntimeError(f"Missing marker: {end_marker}")
    return readme.replace(end_marker, f"{end_marker}\n\n{block}", 1)


def update_readme(summary: str, table: str, pr_details: str) -> None:
    """Replace dynamic sections in README.md."""
    readme = README_PATH.read_text(encoding="utf-8")
    readme = replace_section(readme, "summary", summary)
    readme = replace_section(readme, "contributions", table)
    readme = upsert_section_after(
        readme, "pr_details", pr_details, "contributions",
    )
    README_PATH.write_text(readme.rstrip() + "\n", encoding="utf-8")


def main() -> None:
    prs = fetch_merged_prs()
    open_prs = fetch_open_pr_count()
    repos = group_by_repo(prs)
    total_stars = sum(info["stars"] for info in repos.values())
    table = generate_table(repos)
    pr_details = generate_pr_details(repos)
    summary = generate_summary(len(prs), len(repos), total_stars, open_prs)
    update_readme(summary, table, pr_details)
    print(
        f"Updated README with {len(repos)} repositories, "
        f"{len(prs)} merged PRs, {open_prs} open PRs."
    )


if __name__ == "__main__":
    main()
