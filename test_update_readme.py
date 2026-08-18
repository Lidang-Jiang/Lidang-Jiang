import io
import json
import subprocess  # nosec B404
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

import update_readme
from update_readme import (
    fetch_merged_prs,
    fetch_open_pr_count,
    generate_pr_details,
    generate_summary,
    generate_table,
    group_by_repo,
    replace_section,
    repo_anchor,
    upsert_section_after,
)


def make_pr(number: int, title: str, merged_at: str) -> dict:
    return {
        "title": title,
        "url": f"https://github.com/example/project/pull/{number}",
        "mergedAt": merged_at,
    }


def make_graphql_page(nodes: list[dict], has_next: bool, cursor: str | None) -> object:
    return SimpleNamespace(
        stdout=json.dumps(
            {
                "data": {
                    "user": {
                        "pullRequests": {
                            "pageInfo": {
                                "hasNextPage": has_next,
                                "endCursor": cursor,
                            },
                            "nodes": nodes,
                        }
                    }
                }
            }
        )
    )


class ReadmeGenerationTest(unittest.TestCase):
    def test_fetch_merged_prs_retries_transient_graphql_failure(self) -> None:
        failure = subprocess.CalledProcessError(
            returncode=1,
            cmd=["gh", "api", "graphql"],
            stderr="temporary GraphQL failure",
        )
        response = make_graphql_page([{"title": "recovered"}], False, None)
        stderr = io.StringIO()

        with (
            patch(
                "update_readme.subprocess.run",
                side_effect=[failure, response],
            ) as run,
            patch("update_readme.time.sleep") as sleep,
            redirect_stderr(stderr),
        ):
            prs = fetch_merged_prs()

        self.assertEqual([{"title": "recovered"}], prs)
        self.assertEqual(2, run.call_count)
        sleep.assert_called_once_with(2)
        self.assertIn("attempt 1/3", stderr.getvalue())
        self.assertIn("temporary GraphQL failure", stderr.getvalue())

    def test_graphql_failure_reports_redacted_error_after_retries(self) -> None:
        redaction_value = "not-a-real-credential"
        failure = subprocess.CalledProcessError(
            returncode=1,
            cmd=["gh", "api", "graphql"],
            stderr=f"request failed with token {redaction_value}",
        )
        stderr = io.StringIO()

        with (
            patch("update_readme.GITHUB_TOKEN", redaction_value),
            patch(
                "update_readme.subprocess.run",
                side_effect=[failure, failure, failure],
            ) as run,
            patch("update_readme.time.sleep") as sleep,
            redirect_stderr(stderr),
            self.assertRaisesRegex(
                RuntimeError,
                "GitHub GraphQL request failed after 3 attempts",
            ) as caught,
        ):
            fetch_open_pr_count()

        self.assertEqual(3, run.call_count)
        self.assertEqual([call(2), call(4)], sleep.call_args_list)
        self.assertNotIn(redaction_value, str(caught.exception))
        self.assertNotIn(redaction_value, stderr.getvalue())
        self.assertIn("***", str(caught.exception))

    def test_fetch_merged_prs_uses_graphql_pagination(self) -> None:
        responses = [
            make_graphql_page([{"title": "first"}], True, "cursor-1"),
            make_graphql_page([{"title": "second"}], False, None),
        ]

        with patch("update_readme.subprocess.run", side_effect=responses) as run:
            prs = fetch_merged_prs()

        self.assertEqual([{"title": "first"}, {"title": "second"}], prs)
        self.assertEqual(2, run.call_count)
        first_command = run.call_args_list[0].args[0]
        second_command = run.call_args_list[1].args[0]
        self.assertIn('"cursor": null', first_command[-1])
        self.assertIn('"cursor": "cursor-1"', second_command[-1])

    def test_fetch_open_pr_count_reads_total_count(self) -> None:
        response = SimpleNamespace(
            stdout=json.dumps(
                {"data": {"user": {"pullRequests": {"totalCount": 7}}}}
            )
        )

        with patch("update_readme.subprocess.run", return_value=response):
            self.assertEqual(7, fetch_open_pr_count())

    def test_group_by_repo_skips_profile_repo_and_sorts_by_stars(self) -> None:
        prs = [
            {
                **make_pr(1, "Low", "2026-01-01T00:00:00Z"),
                "repository": {
                    "nameWithOwner": "owner/low",
                    "stargazerCount": 10,
                    "url": "https://github.com/owner/low",
                },
            },
            {
                **make_pr(2, "Profile", "2026-01-02T00:00:00Z"),
                "repository": {
                    "nameWithOwner": "Lidang-Jiang/Lidang-Jiang",
                    "stargazerCount": 999,
                    "url": "https://github.com/Lidang-Jiang/Lidang-Jiang",
                },
            },
            {
                **make_pr(3, "High", "2026-01-03T00:00:00Z"),
                "repository": {
                    "nameWithOwner": "owner/high",
                    "stargazerCount": 100,
                    "url": "https://github.com/owner/high",
                },
            },
        ]

        repos = group_by_repo(prs)

        self.assertEqual(["owner/high", "owner/low"], list(repos))
        self.assertEqual([prs[2]], repos["owner/high"]["prs"])
        self.assertEqual("https://github.com/owner/low", repos["owner/low"]["url"])

    def test_small_repo_uses_inline_pr_links_without_details(self) -> None:
        repos = {
            "example/small": {
                "prs": [
                    make_pr(1, "First fix", "2026-01-01T00:00:00Z"),
                    make_pr(2, "Second fix", "2026-01-02T00:00:00Z"),
                    make_pr(3, "Third fix", "2026-01-03T00:00:00Z"),
                ],
                "stars": 1200,
                "url": "https://github.com/example/small",
            }
        }

        table = generate_table(repos)
        details = generate_pr_details(repos)

        self.assertIn("[#3](https://github.com/example/project/pull/3)", table)
        self.assertIn("[#2](https://github.com/example/project/pull/2)", table)
        self.assertIn("[#1](https://github.com/example/project/pull/1)", table)
        self.assertNotIn("View all", table)
        self.assertEqual("", details)

    def test_large_repo_uses_collapsed_details_with_matching_anchor(self) -> None:
        repo_name = "Example/Big Project"
        repos = {
            repo_name: {
                "prs": [
                    make_pr(1, "First fix", "2026-01-01T00:00:00Z"),
                    make_pr(2, "Second fix", "2026-01-02T00:00:00Z"),
                    make_pr(3, "Third fix", "2026-01-03T00:00:00Z"),
                    make_pr(4, "Fourth fix", "2026-01-04T00:00:00Z"),
                ],
                "stars": 900,
                "url": "https://github.com/example/big-project",
            }
        }

        anchor = repo_anchor(repo_name)
        table = generate_table(repos)
        details = generate_pr_details(repos)

        self.assertIn(f"[View all](#{anchor})", table)
        self.assertIn(f'<a id="{anchor}"></a>', details)
        self.assertIn("<details>", details)
        self.assertNotIn("<details open>", details)
        self.assertIn(
            "<summary><strong>Example/Big Project (4 merged PRs)</strong></summary>",
            details,
        )
        self.assertLess(details.index("#4"), details.index("#1"))

    def test_details_escape_markdown_and_html_sensitive_text(self) -> None:
        repos = {
            "owner/repo|pipe": {
                "prs": [
                    make_pr(
                        1,
                        "Fix value < 3 and table | pipe",
                        "2026-01-01T00:00:00Z",
                    ),
                    make_pr(2, "Second", "2026-01-02T00:00:00Z"),
                    make_pr(3, "Third", "2026-01-03T00:00:00Z"),
                    make_pr(4, "Fourth", "2026-01-04T00:00:00Z"),
                ],
                "stars": 1,
                "url": "https://github.com/owner/repo-pipe",
            }
        }

        details = generate_pr_details(repos)

        self.assertIn("owner/repo\\|pipe (4 merged PRs)", details)
        self.assertIn("Fix value &lt; 3 and table \\| pipe", details)

    def test_generate_summary_formats_counts(self) -> None:
        summary = generate_summary(
            total_prs=49,
            repo_count=21,
            total_stars=859_000,
            open_prs=60,
        )

        self.assertEqual(
            "> **49** merged PRs across **21** projects "
            "(859k+ combined stars) · **60** open PRs in review",
            summary,
        )

    def test_section_helpers_replace_insert_and_raise_on_missing_marker(self) -> None:
        readme = (
            "before\n"
            "<!-- START_SECTION:summary -->\n"
            "old\n"
            "<!-- END_SECTION:summary -->\n"
            "<!-- END_SECTION:contributions -->\n"
            "after"
        )

        replaced = replace_section(readme, "summary", "new")
        inserted = upsert_section_after(
            replaced,
            "pr_details",
            "details",
            "contributions",
        )
        updated = upsert_section_after(
            inserted,
            "pr_details",
            "new details",
            "contributions",
        )

        self.assertIn("<!-- START_SECTION:summary -->\nnew\n", replaced)
        self.assertIn("<!-- START_SECTION:pr_details -->\ndetails\n", inserted)
        self.assertIn("<!-- START_SECTION:pr_details -->\nnew details\n", updated)
        with self.assertRaisesRegex(RuntimeError, "Missing marker"):
            upsert_section_after("no marker", "new", "content", "missing")

    def test_update_readme_rewrites_generated_sections(self) -> None:
        original = (
            "<!-- START_SECTION:summary -->\n"
            "old summary\n"
            "<!-- END_SECTION:summary -->\n"
            "<!-- START_SECTION:contributions -->\n"
            "old table\n"
            "<!-- END_SECTION:contributions -->\n"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            readme_path = Path(tmpdir) / "README.md"
            readme_path.write_text(original, encoding="utf-8")

            with patch("update_readme.README_PATH", readme_path):
                update_readme.update_readme("summary", "table", "details")

            content = readme_path.read_text(encoding="utf-8")

        self.assertIn("<!-- START_SECTION:summary -->\nsummary\n", content)
        self.assertIn("<!-- START_SECTION:contributions -->\ntable\n", content)
        self.assertIn("<!-- START_SECTION:pr_details -->\ndetails\n", content)

    def test_main_fetches_generates_and_updates_readme(self) -> None:
        prs = [
            {
                **make_pr(1, "Fix", "2026-01-01T00:00:00Z"),
                "repository": {
                    "nameWithOwner": "owner/repo",
                    "stargazerCount": 1500,
                    "url": "https://github.com/owner/repo",
                },
            }
        ]

        with (
            patch("update_readme.fetch_merged_prs", return_value=prs),
            patch("update_readme.fetch_open_pr_count", return_value=2),
            patch("update_readme.update_readme") as write_readme,
            patch("builtins.print") as print_output,
        ):
            update_readme.main()

        write_readme.assert_called_once()
        summary, table, details = write_readme.call_args.args
        self.assertIn("**1** merged PRs", summary)
        self.assertIn("(1.5k+ combined stars)", summary)
        self.assertIn("[#1](https://github.com/example/project/pull/1)", table)
        self.assertEqual("", details)
        print_output.assert_called_once_with(
            "Updated README with 1 repositories, 1 merged PRs, 2 open PRs."
        )


if __name__ == "__main__":
    unittest.main()
