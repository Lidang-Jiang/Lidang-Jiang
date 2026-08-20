import io
import json
import subprocess  # nosec B404
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import call, patch

import update_readme
from update_readme import (
    CommitRepositoryInfo,
    GroupedRepositoryInfo,
    PullRequestInfo,
    PullRequestSummary,
    RepositoryContribution,
    RepositoryInfo,
    contribution_windows,
    fetch_authored_commit_contributions,
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


UTC = timezone.utc


def make_pr(number: int, title: str, merged_at: str) -> PullRequestSummary:
    return {
        "title": title,
        "url": f"https://github.com/example/project/pull/{number}",
        "mergedAt": merged_at,
    }


def make_repository(
    name: str = "example/project",
    *,
    owner: str = "example",
    stars: int = 100,
    visibility: str = "PUBLIC",
) -> RepositoryInfo:
    return {
        "nameWithOwner": name,
        "stargazerCount": stars,
        "url": f"https://github.com/{name}",
        "visibility": visibility,
        "owner": {"login": owner},
    }


def make_commit_repository(
    name: str,
    commit_counts: list[int],
    *,
    owner: str,
    stars: int = 100,
    visibility: str = "PUBLIC",
    has_next_page: bool = False,
) -> RepositoryContribution:
    return {
        "repository": make_repository(
            name,
            owner=owner,
            stars=stars,
            visibility=visibility,
        ),
        "contributions": {
            "nodes": [{"commitCount": count} for count in commit_counts],
            "pageInfo": {"hasNextPage": has_next_page},
        },
    }


def make_commit_response(
    total: int,
    repositories: list[RepositoryContribution],
    *,
    repository_total: int | None = None,
) -> dict[str, Any]:
    return {
        "data": {
            "user": {
                "contributionsCollection": {
                    "totalCommitContributions": total,
                    "totalRepositoriesWithContributedCommits": (
                        len(repositories)
                        if repository_total is None
                        else repository_total
                    ),
                    "commitContributionsByRepository": repositories,
                }
            }
        }
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
    def test_contribution_windows_cover_range_without_overlap(self) -> None:
        started_at = datetime(2022, 12, 4, 2, 0, 55, tzinfo=UTC)
        ended_at = started_at + timedelta(days=95, seconds=17)

        windows = contribution_windows(started_at, ended_at)

        self.assertEqual(2, len(windows))
        self.assertEqual(datetime(2022, 12, 4, tzinfo=UTC), windows[0][0])
        self.assertEqual(
            datetime(2023, 3, 3, 23, 59, 59, tzinfo=UTC),
            windows[0][1],
        )
        self.assertEqual(windows[0][1] + timedelta(seconds=1), windows[1][0])
        self.assertEqual(
            datetime(2023, 3, 9, 23, 59, 59, tzinfo=UTC),
            windows[-1][1],
        )

    def test_contribution_windows_reject_invalid_or_naive_ranges(self) -> None:
        aware = datetime(2026, 1, 1, tzinfo=UTC)
        naive = datetime(2026, 1, 1)

        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            contribution_windows(naive, aware)
        with self.assertRaisesRegex(ValueError, "before"):
            contribution_windows(aware, aware - timedelta(seconds=1))

    def test_fetch_authored_commits_sums_days_and_filters_repositories(self) -> None:
        account_response = {"data": {"user": {"createdAt": "2026-01-01T00:00:00Z"}}}
        contribution_response = make_commit_response(
            8,
            [
                make_commit_repository(
                    "NousResearch/hermes-agent",
                    [2, 1],
                    owner="NousResearch",
                    stars=233_000,
                ),
                make_commit_repository(
                    "Lidang-Jiang/private-work",
                    [1],
                    owner="Lidang-Jiang",
                    visibility="PRIVATE",
                ),
                make_commit_repository(
                    "Lidang-Jiang/public-work",
                    [3],
                    owner="Lidang-Jiang",
                ),
                make_commit_repository(
                    "enterprise/internal-work",
                    [1],
                    owner="enterprise",
                    visibility="INTERNAL",
                ),
            ],
        )

        with patch(
            "update_readme._run_graphql",
            side_effect=[account_response, contribution_response],
        ):
            repositories = fetch_authored_commit_contributions(
                now=datetime(2026, 1, 31, tzinfo=UTC)
            )

        self.assertEqual(["NousResearch/hermes-agent"], list(repositories))
        self.assertEqual(3, repositories["NousResearch/hermes-agent"]["commits"])
        self.assertEqual(233_000, repositories["NousResearch/hermes-agent"]["stars"])

    def test_fetch_authored_commits_splits_truncated_window(self) -> None:
        account_response = {"data": {"user": {"createdAt": "2026-01-01T00:00:00Z"}}}
        truncated_response = make_commit_response(
            2,
            [
                make_commit_repository(
                    "example/project",
                    [1],
                    owner="example",
                    has_next_page=True,
                )
            ],
        )
        complete_left = make_commit_response(
            1,
            [make_commit_repository("example/project", [1], owner="example")],
        )
        complete_right = make_commit_response(
            1,
            [make_commit_repository("example/project", [1], owner="example")],
        )

        with patch(
            "update_readme._run_graphql",
            side_effect=[
                account_response,
                truncated_response,
                complete_left,
                complete_right,
            ],
        ) as run_graphql:
            repositories = fetch_authored_commit_contributions(
                now=datetime(2026, 1, 3, tzinfo=UTC)
            )

        self.assertEqual(4, run_graphql.call_count)
        self.assertEqual(2, repositories["example/project"]["commits"])
        parent_variables = run_graphql.call_args_list[1].args[1]
        left_variables = run_graphql.call_args_list[2].args[1]
        right_variables = run_graphql.call_args_list[3].args[1]
        self.assertEqual("2026-01-01T00:00:00Z", parent_variables["from"])
        self.assertEqual("2026-01-03T23:59:59Z", parent_variables["to"])
        self.assertEqual("2026-01-01T23:59:59Z", left_variables["to"])
        self.assertEqual("2026-01-02T00:00:00Z", right_variables["from"])

    def test_fetch_authored_commits_splits_repository_cap(self) -> None:
        account_response = {"data": {"user": {"createdAt": "2026-01-01T00:00:00Z"}}}
        capped_response = make_commit_response(
            2,
            [make_commit_repository("example/first", [1], owner="example")],
            repository_total=2,
        )
        complete_left = make_commit_response(
            1,
            [make_commit_repository("example/first", [1], owner="example")],
        )
        complete_right = make_commit_response(
            1,
            [make_commit_repository("example/second", [1], owner="example")],
        )

        with patch(
            "update_readme._run_graphql",
            side_effect=[
                account_response,
                capped_response,
                complete_left,
                complete_right,
            ],
        ):
            repositories = fetch_authored_commit_contributions(
                now=datetime(2026, 1, 3, tzinfo=UTC)
            )

        self.assertEqual(["example/first", "example/second"], list(repositories))

    def test_fetch_authored_commits_fails_if_single_day_window_is_incomplete(
        self,
    ) -> None:
        account_response = {"data": {"user": {"createdAt": "2026-01-01T00:00:00Z"}}}
        incomplete_response = make_commit_response(1, [])

        with (
            patch(
                "update_readme._run_graphql",
                side_effect=[account_response, incomplete_response],
            ),
            self.assertRaisesRegex(RuntimeError, "incomplete commit contributions"),
        ):
            fetch_authored_commit_contributions(now=datetime(2026, 1, 1, tzinfo=UTC))

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

    def test_graphql_failure_redacts_gh_token(self) -> None:
        redaction_value = "not-a-real-gh-token"
        failure = subprocess.CalledProcessError(
            returncode=1,
            cmd=["gh", "api", "graphql"],
            stderr=f"request failed with token {redaction_value}",
        )

        with (
            patch("update_readme.GH_TOKEN", redaction_value),
            patch("update_readme.subprocess.run", side_effect=[failure] * 3),
            patch("update_readme.time.sleep"),
            self.assertRaises(RuntimeError) as caught,
        ):
            fetch_open_pr_count()

        self.assertNotIn(redaction_value, str(caught.exception))
        self.assertIn("***", str(caught.exception))

    def test_graphql_variables_use_typed_fields_and_omit_none(self) -> None:
        response = SimpleNamespace(stdout='{"data": {}}')

        with patch("update_readme.subprocess.run", return_value=response) as run:
            update_readme._run_graphql(
                "query($from: DateTime!, $cursor: String) { viewer { login } }",
                {
                    "from": "2026-01-01T00:00:00Z",
                    "cursor": None,
                },
            )

        command = run.call_args.args[0]
        self.assertIn("-F", command)
        self.assertIn("from=2026-01-01T00:00:00Z", command)
        self.assertNotIn("cursor=None", command)
        self.assertFalse(any(item.startswith("variables=") for item in command))

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
        self.assertNotIn("cursor=None", first_command)
        self.assertIn("cursor=cursor-1", second_command)

    def test_fetch_open_pr_count_reads_total_count(self) -> None:
        response = SimpleNamespace(
            stdout=json.dumps({"data": {"user": {"pullRequests": {"totalCount": 7}}}})
        )

        with patch("update_readme.subprocess.run", return_value=response):
            self.assertEqual(7, fetch_open_pr_count())

    def test_group_by_repo_unions_pr_and_commit_repositories(self) -> None:
        prs: list[PullRequestInfo] = [
            {
                **make_pr(1, "Low", "2026-01-01T00:00:00Z"),
                "repository": make_repository("owner/low", owner="owner", stars=10),
            },
            {
                **make_pr(2, "Profile", "2026-01-02T00:00:00Z"),
                "repository": make_repository(
                    "Lidang-Jiang/Lidang-Jiang",
                    owner="Lidang-Jiang",
                    stars=999,
                ),
            },
            {
                **make_pr(3, "High", "2026-01-03T00:00:00Z"),
                "repository": make_repository("owner/high", owner="owner", stars=100),
            },
        ]
        commit_repositories: dict[str, CommitRepositoryInfo] = {
            "owner/high": {
                "commits": 2,
                "stars": 100,
                "url": "https://github.com/owner/high",
            },
            "NousResearch/hermes-agent": {
                "commits": 1,
                "stars": 233_000,
                "url": "https://github.com/NousResearch/hermes-agent",
            },
        }

        repos = group_by_repo(prs, commit_repositories)

        self.assertEqual(
            ["NousResearch/hermes-agent", "owner/high", "owner/low"],
            list(repos),
        )
        self.assertEqual([prs[2]], repos["owner/high"]["prs"])
        self.assertEqual(2, repos["owner/high"]["commits"])
        self.assertEqual([], repos["NousResearch/hermes-agent"]["prs"])
        self.assertEqual(1, repos["NousResearch/hermes-agent"]["commits"])
        self.assertEqual("https://github.com/owner/low", repos["owner/low"]["url"])

    def test_group_by_repo_sorts_star_ties_by_name(self) -> None:
        commit_repositories: dict[str, CommitRepositoryInfo] = {
            "example/zeta": {
                "commits": 1,
                "stars": 10,
                "url": "https://github.com/example/zeta",
            },
            "example/alpha": {
                "commits": 1,
                "stars": 10,
                "url": "https://github.com/example/alpha",
            },
        }

        repos = group_by_repo([], commit_repositories)

        self.assertEqual(["example/alpha", "example/zeta"], list(repos))

    def test_small_repo_uses_inline_pr_links_without_details(self) -> None:
        repos: dict[str, GroupedRepositoryInfo] = {
            "example/small": {
                "prs": [
                    make_pr(1, "First fix", "2026-01-01T00:00:00Z"),
                    make_pr(2, "Second fix", "2026-01-02T00:00:00Z"),
                    make_pr(3, "Third fix", "2026-01-03T00:00:00Z"),
                ],
                "commits": 4,
                "stars": 1200,
                "url": "https://github.com/example/small",
            }
        }

        table = generate_table(repos)
        details = generate_pr_details(repos)

        self.assertIn("[#3](https://github.com/example/project/pull/3)", table)
        self.assertIn("[#2](https://github.com/example/project/pull/2)", table)
        self.assertIn("[#1](https://github.com/example/project/pull/1)", table)
        self.assertIn(
            "[4](https://github.com/example/small/commits?author=Lidang-Jiang)",
            table,
        )
        self.assertNotIn("View all", table)
        self.assertEqual("", details)

    def test_large_repo_uses_collapsed_details_with_matching_anchor(self) -> None:
        repo_name = "Example/Big Project"
        repos: dict[str, GroupedRepositoryInfo] = {
            repo_name: {
                "prs": [
                    make_pr(1, "First fix", "2026-01-01T00:00:00Z"),
                    make_pr(2, "Second fix", "2026-01-02T00:00:00Z"),
                    make_pr(3, "Third fix", "2026-01-03T00:00:00Z"),
                    make_pr(4, "Fourth fix", "2026-01-04T00:00:00Z"),
                ],
                "commits": 6,
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
        repos: dict[str, GroupedRepositoryInfo] = {
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
                "commits": 4,
                "stars": 1,
                "url": "https://github.com/owner/repo-pipe",
            }
        }

        details = generate_pr_details(repos)

        self.assertIn("owner/repo|pipe (4 merged PRs)", details)
        self.assertNotIn("owner/repo\\|pipe", details)
        self.assertIn("Fix value &lt; 3 and table \\| pipe", details)

    def test_details_render_untrusted_pr_titles_as_plain_text(self) -> None:
        malicious_title = (
            "![image](https://evil.example/image.png) "
            "[click](https://evil.example) `code` *bold*\\payload\nnext"
        )
        repos: dict[str, GroupedRepositoryInfo] = {
            "owner/repo": {
                "prs": [
                    make_pr(1, malicious_title, "2026-01-01T00:00:00Z"),
                    make_pr(2, "Second", "2026-01-02T00:00:00Z"),
                    make_pr(3, "Third", "2026-01-03T00:00:00Z"),
                    make_pr(4, "Fourth", "2026-01-04T00:00:00Z"),
                ],
                "commits": 4,
                "stars": 1,
                "url": "https://github.com/owner/repo",
            }
        }

        details = generate_pr_details(repos)

        self.assertNotIn("![image]", details)
        self.assertNotIn("[click](", details)
        self.assertNotIn("`code`", details)
        self.assertNotIn("*bold*", details)
        self.assertIn("next", details)
        self.assertNotIn("payload\nnext", details)

    def test_commit_only_repository_uses_dash_for_pr_links(self) -> None:
        repos: dict[str, GroupedRepositoryInfo] = {
            "NousResearch/hermes-agent": {
                "prs": [],
                "commits": 1,
                "stars": 233_000,
                "url": "https://github.com/NousResearch/hermes-agent",
            }
        }

        table = generate_table(repos)

        self.assertIn("| 0 | — |", table)
        self.assertIn(
            "https://github.com/NousResearch/hermes-agent/commits?author=Lidang-Jiang",
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
        with self.assertRaisesRegex(RuntimeError, "Missing or duplicate.*summary"):
            replace_section("no marker", "summary", "content")
        with self.assertRaisesRegex(RuntimeError, "Missing or duplicate.*summary"):
            replace_section(f"{readme}\n{readme}", "summary", "content")
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
        prs: list[PullRequestInfo] = [
            {
                **make_pr(1, "Fix", "2026-01-01T00:00:00Z"),
                "repository": make_repository("owner/repo", owner="owner", stars=1500),
            }
        ]
        commit_repositories: dict[str, CommitRepositoryInfo] = {
            "owner/repo": {
                "commits": 3,
                "stars": 1500,
                "url": "https://github.com/owner/repo",
            }
        }

        with (
            patch("update_readme.fetch_merged_prs", return_value=prs),
            patch(
                "update_readme.fetch_authored_commit_contributions",
                return_value=commit_repositories,
            ),
            patch("update_readme.fetch_open_pr_count", return_value=2),
            patch("update_readme.update_readme") as write_readme,
            patch("builtins.print") as print_output,
        ):
            update_readme.main()

        write_readme.assert_called_once()
        summary, table, details = write_readme.call_args.args
        self.assertIn("**3** authored commits", summary)
        self.assertIn("**1** merged PRs", summary)
        self.assertIn("(1.5k+ combined stars)", summary)
        self.assertIn("[#1](https://github.com/example/project/pull/1)", table)
        self.assertEqual("", details)
        print_output.assert_called_once_with(
            "Updated README with 1 repositories, 3 authored commits, "
            "1 merged PRs, 2 open PRs."
        )

    def test_main_does_not_write_readme_when_remote_fetch_fails(self) -> None:
        with (
            patch("update_readme.fetch_merged_prs", return_value=[]),
            patch(
                "update_readme.fetch_authored_commit_contributions",
                side_effect=RuntimeError("GitHub unavailable"),
            ),
            patch("update_readme.update_readme") as write_readme,
            self.assertRaisesRegex(RuntimeError, "GitHub unavailable"),
        ):
            update_readme.main()

        write_readme.assert_not_called()


if __name__ == "__main__":
    unittest.main()
