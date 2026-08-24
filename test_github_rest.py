import json
import subprocess  # nosec B404
import unittest
from types import SimpleNamespace
from unittest.mock import call, patch

import github_rest


def make_search_page(
    total_count: int,
    items: list[dict[str, object]],
    *,
    incomplete: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        stdout=json.dumps(
            {
                "total_count": total_count,
                "incomplete_results": incomplete,
                "items": items,
            }
        )
    )


class GitHubCommitSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        interval_patcher = patch("github_rest.REST_REQUEST_INTERVAL_SECONDS", 0)
        last_request_patcher = patch("github_rest._last_rest_request_at", None)
        interval_patcher.start()
        last_request_patcher.start()
        self.addCleanup(interval_patcher.stop)
        self.addCleanup(last_request_patcher.stop)

    def test_search_commits_builds_scoped_query_and_paginates(self) -> None:
        pages = [
            make_search_page(101, [{"sha": str(index)} for index in range(100)]),
            make_search_page(101, [{"sha": "100"}]),
        ]

        with patch("github_rest.subprocess.run", side_effect=pages) as run:
            items = github_rest.search_commits(
                "owner/repo",
                {"person@example.com", "123+person@users.noreply.github.com"},
            )

        self.assertEqual(101, len(items))
        first_command = run.call_args_list[0].args[0]
        second_command = run.call_args_list[1].args[0]
        self.assertIn(
            "q=repo:owner/repo "
            "(123+person@users.noreply.github.com OR person@example.com)",
            first_command,
        )
        self.assertIn("page=1", first_command)
        self.assertIn("page=2", second_command)

    def test_search_commits_retries_incomplete_results_without_keeping_them(
        self,
    ) -> None:
        incomplete_page = make_search_page(
            1,
            [{"sha": "partial"}],
            incomplete=True,
        )
        complete_page = make_search_page(1, [{"sha": "complete"}])

        with (
            patch(
                "github_rest.subprocess.run",
                side_effect=[incomplete_page, complete_page],
            ) as run,
            patch("github_rest.time.sleep") as sleep,
        ):
            items = github_rest.search_commits(
                "owner/repo",
                {"person@example.com"},
            )

        self.assertEqual([{"sha": "complete"}], items)
        self.assertEqual(2, run.call_count)
        self.assertEqual(run.call_args_list[0].args[0], run.call_args_list[1].args[0])
        sleep.assert_called_once_with(2)

    def test_search_commits_retries_an_incomplete_later_page_once(self) -> None:
        first_page_items: list[dict[str, object]] = [
            {"sha": str(index)} for index in range(100)
        ]
        pages = [
            make_search_page(101, first_page_items),
            make_search_page(101, [{"sha": "partial"}], incomplete=True),
            make_search_page(101, [{"sha": "100"}]),
        ]

        with (
            patch("github_rest.subprocess.run", side_effect=pages) as run,
            patch("github_rest.time.sleep") as sleep,
        ):
            items = github_rest.search_commits(
                "owner/repo",
                {"person@example.com"},
            )

        self.assertEqual(
            [str(index) for index in range(101)],
            [item["sha"] for item in items],
        )
        self.assertEqual(3, run.call_count)
        self.assertIn("page=2", run.call_args_list[1].args[0])
        self.assertEqual(run.call_args_list[1].args[0], run.call_args_list[2].args[0])
        sleep.assert_called_once_with(2)

    def test_search_commits_rejects_persistently_incomplete_results(self) -> None:
        incomplete_page = make_search_page(1, [], incomplete=True)

        with (
            patch(
                "github_rest.subprocess.run",
                return_value=incomplete_page,
            ) as run,
            patch("github_rest.time.sleep") as sleep,
            self.assertRaisesRegex(
                RuntimeError,
                "incomplete for owner/repo page 1 after 3 attempts",
            ),
        ):
            github_rest.search_commits("owner/repo", {"person@example.com"})

        self.assertEqual(3, run.call_count)
        self.assertEqual([call(2), call(4)], sleep.call_args_list)

    def test_search_commits_rejects_capped_results(self) -> None:
        with (
            patch(
                "github_rest.subprocess.run",
                return_value=make_search_page(1001, []),
            ),
            self.assertRaisesRegex(RuntimeError, "1,000-result"),
        ):
            github_rest.search_commits("owner/repo", {"person@example.com"})

    def test_search_commits_retries_and_redacts_token(self) -> None:
        secret = "not-a-real-token"
        failure = subprocess.CalledProcessError(
            1,
            ["gh"],
            stderr=f"request failed with {secret}",
        )

        with (
            patch("github_rest.GH_TOKEN", secret),
            patch("github_rest.REST_INITIAL_RETRY_DELAY_SECONDS", 0),
            patch(
                "github_rest.subprocess.run", side_effect=[failure, failure, failure]
            ),
            self.assertRaises(RuntimeError) as raised,
        ):
            github_rest.search_commits("owner/repo", {"person@example.com"})

        self.assertNotIn(secret, str(raised.exception))
        self.assertIn("***", str(raised.exception))

    def test_search_commits_uses_long_bounded_delay_for_rate_limit(self) -> None:
        rate_limit = subprocess.CalledProcessError(
            1,
            ["gh"],
            stderr="gh: API rate limit exceeded (HTTP 403)",
        )

        with (
            patch("github_rest.REST_RATE_LIMIT_RETRY_DELAY_SECONDS", 61),
            patch(
                "github_rest.subprocess.run",
                side_effect=[rate_limit, make_search_page(0, [])],
            ),
            patch("github_rest.time.sleep") as sleep,
        ):
            items = github_rest.search_commits(
                "owner/repo",
                {"person@example.com"},
            )

        self.assertEqual([], items)
        sleep.assert_called_once_with(61)

    def test_compare_checks_current_branch_ancestry_and_encodes_branch(self) -> None:
        response = SimpleNamespace(
            stdout=json.dumps(
                {
                    "status": "ahead",
                    "merge_base_commit": {"sha": "abc123"},
                }
            )
        )
        with patch("github_rest.subprocess.run", return_value=response) as run:
            reachable = github_rest.is_commit_reachable(
                "owner/repo",
                "release/1.x",
                "abc123",
            )

        self.assertTrue(reachable)
        self.assertIn(
            "repos/owner/repo/compare/abc123...release%2F1.x",
            run.call_args.args[0],
        )

        detached_response = SimpleNamespace(
            stdout=json.dumps(
                {
                    "status": "diverged",
                    "merge_base_commit": {"sha": "older"},
                }
            )
        )
        with patch("github_rest.subprocess.run", return_value=detached_response):
            self.assertFalse(
                github_rest.is_commit_reachable("owner/repo", "main", "abc123")
            )

    def test_rest_requests_are_paced_below_search_rate_limit(self) -> None:
        with (
            patch("github_rest.REST_REQUEST_INTERVAL_SECONDS", 2.1),
            patch("github_rest._last_rest_request_at", 10.0),
            patch("github_rest.time.monotonic", side_effect=[11.0, 13.1]),
            patch("github_rest.time.sleep") as sleep,
        ):
            github_rest._pace_rest_request()

        sleep.assert_called_once_with(1.1)


if __name__ == "__main__":
    unittest.main()
