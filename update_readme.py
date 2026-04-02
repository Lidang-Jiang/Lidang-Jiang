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

GRAPHQL_QUERY = """
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


def fetch_merged_prs() -> list[dict]:
    """Fetch all merged PRs using GitHub GraphQL API with pagination."""
    all_prs: list[dict] = []
    cursor = None

    while True:
        variables = json.dumps({"cursor": cursor})
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={GRAPHQL_QUERY}",
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


def generate_table(repos: dict[str, dict]) -> str:
    """Generate markdown table from grouped PRs."""
    lines = [
        "| Repository | PRs Merged | Links |",
        "|:-----------|:----------:|:------|",
    ]

    for name, info in repos.items():
        pr_count = len(info["prs"])
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
        lines.append(f"| {repo_link} | {pr_count} | {pr_links} |")

    return "\n".join(lines)


def update_readme(table: str) -> None:
    """Replace content between markers in README.md."""
    readme = README_PATH.read_text(encoding="utf-8")
    pattern = (
        r"(<!-- START_SECTION:contributions -->)\n"
        r".*?"
        r"(<!-- END_SECTION:contributions -->)"
    )
    replacement = f"\\1\n{table}\n\\2"
    updated = re.sub(pattern, replacement, readme, flags=re.DOTALL)
    README_PATH.write_text(updated, encoding="utf-8")


def main() -> None:
    prs = fetch_merged_prs()
    repos = group_by_repo(prs)
    table = generate_table(repos)
    update_readme(table)
    print(f"Updated README with {len(repos)} repositories, {len(prs)} merged PRs.")


if __name__ == "__main__":
    main()
