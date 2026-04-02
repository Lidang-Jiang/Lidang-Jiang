"""Fetch merged PRs via GitHub GraphQL API and update README.md."""

from __future__ import annotations

import os
import re
import subprocess
import json
from collections import defaultdict
from pathlib import Path


GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
USERNAME = "Lidang-Jiang"
README_PATH = Path(__file__).parent / "README.md"

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


def fetch_merged_prs() -> list[dict]:
    """Fetch all merged PRs using GitHub GraphQL API with pagination."""
    all_prs: list[dict] = []
    cursor = None

    while True:
        variables = json.dumps({"cursor": cursor})
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={MERGED_PRS_QUERY}",
             "-f", f"variables={variables}"],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
        pr_data = data["data"]["user"]["pullRequests"]
        all_prs.extend(pr_data["nodes"])

        if not pr_data["pageInfo"]["hasNextPage"]:
            break
        cursor = pr_data["pageInfo"]["endCursor"]

    return all_prs


def fetch_open_pr_count() -> int:
    """Fetch total count of open PRs."""
    result = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={OPEN_PRS_QUERY}"],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
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


def generate_table(repos: dict[str, dict]) -> str:
    """Generate markdown table from grouped PRs."""
    lines = [
        "| Repository | Stars | PRs Merged | Links |",
        "|:-----------|------:|:----------:|:------|",
    ]

    for name, info in repos.items():
        pr_count = len(info["prs"])
        stars = format_stars(info["stars"])
        # Build PR links: show individual titles for repos with <= 3 PRs,
        # otherwise just show count with search link
        if pr_count <= 3:
            pr_links = ", ".join(
                f"[#{pr['url'].split('/')[-1]}]({pr['url']})"
                for pr in sorted(info["prs"], key=lambda p: p["mergedAt"], reverse=True)
            )
        else:
            search_url = (
                f"https://github.com/{name}/pulls"
                f"?q=is%3Apr+is%3Amerged+author%3A{USERNAME}"
            )
            pr_links = f"[View all]({search_url})"

        repo_link = f"[{name}]({info['url']})"
        lines.append(f"| {repo_link} | {stars} | {pr_count} | {pr_links} |")

    return "\n".join(lines)


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
    return re.sub(pattern, rf"\1\n{content}\n\2", readme, flags=re.DOTALL)


def update_readme(summary: str, table: str) -> None:
    """Replace dynamic sections in README.md."""
    readme = README_PATH.read_text(encoding="utf-8")
    readme = replace_section(readme, "summary", summary)
    readme = replace_section(readme, "contributions", table)
    README_PATH.write_text(readme.rstrip() + "\n", encoding="utf-8")


def main() -> None:
    prs = fetch_merged_prs()
    open_prs = fetch_open_pr_count()
    repos = group_by_repo(prs)
    total_stars = sum(info["stars"] for info in repos.values())
    table = generate_table(repos)
    summary = generate_summary(len(prs), len(repos), total_stars, open_prs)
    update_readme(summary, table)
    print(f"Updated README with {len(repos)} repositories, {len(prs)} merged PRs, {open_prs} open PRs.")


if __name__ == "__main__":
    main()
