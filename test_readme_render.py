import unittest

from contribution_types import AuthoredCommitInfo, GroupedRepositoryInfo
from readme_render import generate_summary, generate_table


def make_commit(oid: str, branch: str = "main") -> AuthoredCommitInfo:
    return {
        "oid": oid,
        "url": f"https://github.com/example/project/commit/{oid}",
        "messageHeadline": "Fix",
        "branches": [branch],
    }


class ReadmeRenderTest(unittest.TestCase):
    def test_commit_only_repository_uses_dash_for_pr_links(self) -> None:
        repos: dict[str, GroupedRepositoryInfo] = {
            "NousResearch/hermes-agent": {
                "prs": [],
                "commits": [make_commit("commit-1")],
                "stars": 233_000,
                "url": "https://github.com/NousResearch/hermes-agent",
            }
        }

        table = generate_table(repos)

        self.assertIn("| 0 | — |", table)
        self.assertIn(
            "https://github.com/NousResearch/hermes-agent/commits/main"
            "?author=Lidang-Jiang",
            table,
        )

    def test_generate_summary_formats_counts(self) -> None:
        summary = generate_summary(
            total_commits=75,
            total_prs=49,
            repo_count=21,
            total_stars=859_000,
            open_prs=60,
        )

        self.assertEqual(
            "> **75** authored commits and **49** merged PRs across "
            "**21** external projects "
            "(859k+ combined stars) · **60** open PRs in review",
            summary,
        )


if __name__ == "__main__":
    unittest.main()
