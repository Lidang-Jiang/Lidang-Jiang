import unittest
from typing import Any, cast
from unittest.mock import patch

import update_readme
from contribution_types import (
    AccountInfo,
    AuthoredCommitInfo,
    PullRequestInfo,
    PullRequestSummary,
    RepositoryInfo,
)
from update_readme import (
    fetch_authored_commit_contributions,
)


def make_repository(
    name: str,
    *,
    owner: str,
    stars: int = 100,
    default_branch: str = "main",
) -> RepositoryInfo:
    return {
        "nameWithOwner": name,
        "stargazerCount": stars,
        "url": f"https://github.com/{name}",
        "visibility": "PUBLIC",
        "owner": {"login": owner},
        "defaultBranchRef": {"name": default_branch},
    }


def make_pr(
    number: int,
    repository: RepositoryInfo,
    *,
    base_branch: str = "main",
) -> PullRequestInfo:
    summary: PullRequestSummary = {
        "title": "Fix",
        "url": f"{repository['url']}/pull/{number}",
        "mergedAt": "2026-01-01T00:00:00Z",
        "baseRefName": base_branch,
    }
    return cast(PullRequestInfo, {**summary, "repository": repository})


def make_commit(oid: str, branch: str) -> AuthoredCommitInfo:
    return {
        "oid": oid,
        "url": f"https://github.com/example/project/commit/{oid}",
        "messageHeadline": "Fix",
        "branches": [branch],
    }


def make_history_response(
    commits: list[AuthoredCommitInfo],
    has_next_page: bool,
    cursor: str | None,
) -> dict[str, Any]:
    nodes = [
        {
            "oid": commit["oid"],
            "url": commit["url"],
            "messageHeadline": commit["messageHeadline"],
            "authors": {
                "nodes": [{"user": {"login": "Lidang-Jiang"}}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        }
        for commit in commits
    ]
    return {
        "data": {
            "repository": {
                "ref": {
                    "target": {
                        "__typename": "Commit",
                        "history": {
                            "nodes": nodes,
                            "pageInfo": {
                                "hasNextPage": has_next_page,
                                "endCursor": cursor,
                            },
                        },
                    }
                }
            }
        }
    }


def make_graph_commit(
    oid: str,
    *,
    authors: tuple[str, ...] = ("Lidang-Jiang",),
    message: str | None = None,
    authored_at: str = "2026-01-01T00:00:00Z",
) -> dict[str, Any]:
    return {
        "oid": oid,
        "url": f"https://github.com/example/project/commit/{oid}",
        "messageHeadline": message or f"Fix {oid}",
        "message": message or f"Fix {oid}",
        "authoredDate": authored_at,
        "author": {"email": f"{authors[0]}@example.com"},
        "authors": {
            "nodes": [
                {
                    "email": f"{login}@example.com",
                    "user": {"login": login},
                }
                for login in authors
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        },
    }


def make_rich_pr(
    number: int,
    repository: RepositoryInfo,
    source_commits: list[dict[str, Any]],
    merge_commit: dict[str, Any],
    *,
    base_branch: str = "main",
    parent_count: int = 1,
) -> PullRequestInfo:
    merge_commit = {
        **merge_commit,
        "parents": {
            "totalCount": parent_count,
            "nodes": [
                {"oid": "base-parent"},
                *([{"oid": source_commits[-1]["oid"]}] if parent_count >= 2 else []),
            ],
        },
    }
    return {
        **make_pr(number, repository, base_branch=base_branch),
        "number": number,
        "mergeCommit": merge_commit,
        "commits": {
            "nodes": [{"commit": commit} for commit in source_commits],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "totalCount": len(source_commits),
        },
    }


class AuthoredCommitHistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.account: AccountInfo = {
            "id": "user-id",
            "databaseId": 119769478,
            "createdAt": "2022-12-04T02:00:55Z",
        }
        self.coauthor_patcher = patch(
            "update_readme._fetch_branch_coauthored_commits",
            return_value=[],
        )
        self.coauthor_patcher.start()
        self.addCleanup(self.coauthor_patcher.stop)

    def test_actual_history_overrides_calendar_count_for_openclaw(self) -> None:
        repository = make_repository(
            "openclaw/openclaw", owner="openclaw", stars=387_000
        )
        prs = [make_pr(56720, repository), make_pr(66285, repository)]

        with (
            patch("update_readme._fetch_account_info", return_value=self.account),
            patch(
                "update_readme._discover_contribution_repositories",
                return_value={"openclaw/openclaw": repository},
            ),
            patch(
                "update_readme._fetch_branch_authored_commits",
                return_value=[
                    make_commit("8acadc6990cd", "main"),
                    make_commit("6d539db011e4", "main"),
                ],
            ),
        ):
            repositories = fetch_authored_commit_contributions(prs)

        self.assertEqual(2, len(repositories["openclaw/openclaw"]["commits"]))
        self.assertEqual(2, len(prs))

    def test_non_default_pr_base_branch_counts_ros2_commit(self) -> None:
        repository = make_repository(
            "ros2/rclcpp", owner="ros2", default_branch="rolling"
        )
        pr = make_pr(3139, repository, base_branch="jazzy")

        def branch_commits(
            _repository: RepositoryInfo,
            branch: str,
            _author_id: str,
        ) -> list[AuthoredCommitInfo]:
            return [make_commit("b0c27e6d82d2", branch)] if branch == "jazzy" else []

        with (
            patch("update_readme._fetch_account_info", return_value=self.account),
            patch("update_readme._discover_contribution_repositories", return_value={}),
            patch(
                "update_readme._fetch_branch_authored_commits",
                side_effect=branch_commits,
            ),
        ):
            repositories = fetch_authored_commit_contributions([pr])

        commits = repositories["ros2/rclcpp"]["commits"]
        self.assertEqual(1, len(commits))
        self.assertEqual(["jazzy"], commits[0]["branches"])

    def test_merged_pr_does_not_imply_authored_commit(self) -> None:
        repository = make_repository("owner/repo", owner="owner")

        with (
            patch("update_readme._fetch_account_info", return_value=self.account),
            patch("update_readme._discover_contribution_repositories", return_value={}),
            patch("update_readme._fetch_branch_authored_commits", return_value=[]),
        ):
            repositories = fetch_authored_commit_contributions([make_pr(1, repository)])

        self.assertEqual([], repositories["owner/repo"]["commits"])

    def test_same_oid_on_multiple_branches_is_counted_once(self) -> None:
        repository = make_repository("owner/repo", owner="owner")

        def branch_commits(
            _repository: RepositoryInfo,
            branch: str,
            _author_id: str,
        ) -> list[AuthoredCommitInfo]:
            if branch == "main":
                return [make_commit("a", "main"), make_commit("b", "main")]
            return [make_commit("b", "release"), make_commit("c", "release")]

        with (
            patch("update_readme._fetch_account_info", return_value=self.account),
            patch(
                "update_readme._discover_contribution_repositories",
                return_value={"owner/repo": repository},
            ),
            patch(
                "update_readme._fetch_branch_authored_commits",
                side_effect=branch_commits,
            ),
        ):
            repositories = fetch_authored_commit_contributions(
                [make_pr(1, repository, base_branch="release")]
            )

        commits = repositories["owner/repo"]["commits"]
        self.assertEqual(["a", "b", "c"], [commit["oid"] for commit in commits])
        self.assertEqual(["main", "release"], commits[1]["branches"])

    def test_existing_branch_does_not_union_stale_pr_oid(self) -> None:
        repository = make_repository("owner/repo", owner="owner")
        pull_request = make_rich_pr(
            1,
            repository,
            [make_graph_commit("source")],
            make_graph_commit("stale-merge"),
        )

        with (
            patch("update_readme._fetch_account_info", return_value=self.account),
            patch("update_readme._discover_contribution_repositories", return_value={}),
            patch(
                "update_readme._fetch_branch_authored_commits",
                return_value=[make_commit("current", "main")],
            ),
        ):
            repositories = fetch_authored_commit_contributions([pull_request])

        self.assertEqual(
            ["current"],
            [commit["oid"] for commit in repositories["owner/repo"]["commits"]],
        )

    def test_third_party_pr_coauthor_is_found_without_calendar_days(self) -> None:
        repository = make_repository("owner/repo", owner="owner")
        coauthored: AuthoredCommitInfo = {
            **make_commit("coauthored", "main"),
            "coauthorOnly": True,
            "requiresDetail": True,
        }

        with (
            patch("update_readme._fetch_account_info", return_value=self.account),
            patch(
                "update_readme._discover_contribution_repositories",
                return_value={"owner/repo": repository},
            ),
            patch("update_readme._fetch_branch_authored_commits", return_value=[]),
            patch(
                "update_readme._fetch_branch_coauthored_commits",
                return_value=[coauthored],
            ) as fetch_coauthors,
        ):
            repositories = fetch_authored_commit_contributions([])

        self.assertEqual(
            ["coauthored"],
            [commit["oid"] for commit in repositories["owner/repo"]["commits"]],
        )
        fetch_coauthors.assert_called_once_with(
            repository,
            "main",
            self.account,
            is_default_branch=True,
        )

    def test_default_branch_search_validates_real_coauthor_trailer(self) -> None:
        self.coauthor_patcher.stop()
        repository = make_repository("owner/repo", owner="owner")
        signed_off = make_graph_commit(
            "signed",
            authors=("maintainer", "Lidang-Jiang"),
            message=("Release\n\nSigned-off-by: Lidang Jiang <lidangjiang@gmail.com>"),
        )
        coauthored = make_graph_commit(
            "coauthored",
            authors=("maintainer", "Lidang-Jiang"),
            message=(
                "Shared fix\n\nCo-authored-by: Lidang Jiang <lidangjiang@gmail.com>"
            ),
        )
        coauthored["authors"]["nodes"][1]["email"] = "lidangjiang@gmail.com"
        search_results = [
            {
                "sha": "signed",
                "commit": {"message": signed_off["message"]},
            },
            {
                "sha": "coauthored",
                "commit": {"message": coauthored["message"]},
            },
        ]

        with (
            patch(
                "update_readme.search_commits", return_value=search_results
            ) as search,
            patch(
                "update_readme.is_commit_reachable",
                return_value=True,
            ) as reachable,
            patch(
                "update_readme._fetch_object_history",
                return_value=[coauthored],
            ) as fetch_object,
        ):
            commits = update_readme._fetch_branch_coauthored_commits(
                repository,
                "main",
                self.account,
                is_default_branch=True,
            )

        self.assertEqual(["coauthored"], [commit["oid"] for commit in commits])
        search.assert_called_once_with(
            "owner/repo",
            {
                "119769478+lidang-jiang@users.noreply.github.com",
                "lidang-jiang@users.noreply.github.com",
                "lidangjiang@gmail.com",
            },
        )
        fetch_object.assert_called_once_with(
            repository,
            "coauthored",
            1,
            "coauthor search for owner/repo:main",
        )
        reachable.assert_called_once_with("owner/repo", "main", "coauthored")

    def test_default_branch_search_skips_detached_coauthor_commit(self) -> None:
        self.coauthor_patcher.stop()
        repository = make_repository("owner/repo", owner="owner")
        coauthored = make_graph_commit(
            "detached",
            authors=("maintainer", "Lidang-Jiang"),
            message=(
                "Shared fix\n\nCo-authored-by: Lidang Jiang <lidangjiang@gmail.com>"
            ),
        )
        search_result = {
            "sha": "detached",
            "commit": {"message": coauthored["message"]},
        }

        with (
            patch("update_readme.search_commits", return_value=[search_result]),
            patch("update_readme.is_commit_reachable", return_value=False),
            patch("update_readme._fetch_object_history") as fetch_object,
        ):
            commits = update_readme._fetch_branch_coauthored_commits(
                repository,
                "main",
                self.account,
                is_default_branch=True,
            )

        self.assertEqual([], commits)
        fetch_object.assert_not_called()

    def test_default_branch_search_enforces_candidate_budget(self) -> None:
        self.coauthor_patcher.stop()
        repository = make_repository("owner/repo", owner="owner")
        message = "Shared fix\n\nCo-authored-by: Lidang Jiang <lidangjiang@gmail.com>"
        search_results = [
            {"sha": f"candidate-{index}", "commit": {"message": message}}
            for index in range(2)
        ]

        with (
            patch("update_readme.MAX_COAUTHOR_CANDIDATES_PER_REPOSITORY", 1),
            patch("update_readme.search_commits", return_value=search_results),
            patch("update_readme.is_commit_reachable") as reachable,
            self.assertRaisesRegex(RuntimeError, "candidate budget"),
        ):
            update_readme._fetch_branch_coauthored_commits(
                repository,
                "main",
                self.account,
                is_default_branch=True,
            )

        reachable.assert_not_called()

    def test_nondefault_coauthor_history_scans_from_account_creation(self) -> None:
        self.coauthor_patcher.stop()
        repository = make_repository(
            "owner/repo",
            owner="owner",
            default_branch="rolling",
        )
        coauthored = make_graph_commit(
            "coauthored",
            authors=("maintainer", "Lidang-Jiang"),
            message=(
                "Shared fix\n\nCo-authored-by: Lidang Jiang <Lidang-Jiang@example.com>"
            ),
        )
        response = {
            "data": {
                "repository": {
                    "ref": {
                        "target": {
                            "__typename": "Commit",
                            "history": {
                                "nodes": [coauthored],
                                "pageInfo": {
                                    "hasNextPage": False,
                                    "endCursor": None,
                                },
                            },
                        }
                    }
                }
            }
        }

        with patch("update_readme._run_graphql", return_value=response) as run:
            commits = update_readme._fetch_branch_coauthored_commits(
                repository,
                "jazzy",
                self.account,
                is_default_branch=False,
            )

        self.assertEqual(["coauthored"], [commit["oid"] for commit in commits])
        variables = run.call_args.args[1]
        self.assertEqual("refs/heads/jazzy", variables["qualifiedRef"])
        self.assertEqual("2022-12-04T02:00:55Z", variables["since"])

    def test_nondefault_coauthor_history_enforces_page_budget(self) -> None:
        self.coauthor_patcher.stop()
        repository = make_repository("owner/repo", owner="owner")
        response = {
            "data": {
                "repository": {
                    "ref": {
                        "target": {
                            "__typename": "Commit",
                            "history": {
                                "nodes": [],
                                "pageInfo": {
                                    "hasNextPage": True,
                                    "endCursor": "next-page",
                                },
                            },
                        }
                    }
                }
            }
        }

        with (
            patch("update_readme.MAX_HISTORY_PAGES_PER_BRANCH", 1),
            patch("update_readme._run_graphql", return_value=response) as run,
            self.assertRaisesRegex(RuntimeError, "page budget"),
        ):
            update_readme._fetch_branch_coauthored_commits(
                repository,
                "release",
                self.account,
                is_default_branch=False,
            )

        self.assertEqual(1, run.call_count)

    def test_deleted_branch_fallback_does_not_leak_into_other_repository(self) -> None:
        missing_repository = make_repository("owner/missing", owner="owner")
        existing_repository = make_repository("owner/existing", owner="owner")
        pull_request = make_rich_pr(
            1,
            missing_repository,
            [make_graph_commit("source")],
            make_graph_commit("fallback"),
            base_branch="deleted",
        )

        def branch_commits(
            repository: RepositoryInfo,
            branch: str,
            _author_id: str,
        ) -> list[AuthoredCommitInfo] | None:
            if repository["nameWithOwner"] == "owner/missing" and branch == "deleted":
                return None
            if repository["nameWithOwner"] == "owner/existing":
                return [make_commit("existing", branch)]
            return []

        with (
            patch("update_readme._fetch_account_info", return_value=self.account),
            patch(
                "update_readme._discover_contribution_repositories",
                return_value={
                    "owner/missing": missing_repository,
                    "owner/existing": existing_repository,
                },
            ),
            patch(
                "update_readme._fetch_branch_authored_commits",
                side_effect=branch_commits,
            ),
        ):
            repositories = fetch_authored_commit_contributions([pull_request])

        self.assertEqual(
            ["fallback"],
            [commit["oid"] for commit in repositories["owner/missing"]["commits"]],
        )
        self.assertEqual(
            ["existing"],
            [commit["oid"] for commit in repositories["owner/existing"]["commits"]],
        )

    def test_branch_history_paginates_and_rejects_incomplete_cursor(self) -> None:
        repository = make_repository("owner/repo", owner="owner")
        page_one = make_history_response([make_commit("a", "main")], True, "cursor-1")
        page_two = make_history_response([make_commit("b", "main")], False, None)

        with patch(
            "update_readme._run_graphql", side_effect=[page_one, page_two]
        ) as run:
            commits = update_readme._fetch_branch_authored_commits(
                repository, "main", "user-id"
            )

        self.assertIsNotNone(commits)
        assert commits is not None
        self.assertEqual(["a", "b"], [commit["oid"] for commit in commits])
        self.assertEqual("cursor-1", run.call_args_list[1].args[1]["cursor"])

        incomplete = make_history_response([], True, None)
        with (
            patch("update_readme._run_graphql", return_value=incomplete),
            self.assertRaisesRegex(RuntimeError, "missing or repeated end cursor"),
        ):
            update_readme._fetch_branch_authored_commits(repository, "main", "user-id")

    def test_deleted_branch_uses_single_commit_pr_fallback(self) -> None:
        repository = make_repository("owner/repo", owner="owner")
        merge_commit = make_graph_commit("merged")
        pr = make_rich_pr(1, repository, [make_graph_commit("source")], merge_commit)

        with (
            patch("update_readme._fetch_account_info", return_value=self.account),
            patch("update_readme._discover_contribution_repositories", return_value={}),
            patch("update_readme._fetch_branch_authored_commits", return_value=None),
        ):
            repositories = fetch_authored_commit_contributions([pr])

        commits = repositories["owner/repo"]["commits"]
        self.assertEqual(["merged"], [commit["oid"] for commit in commits])

    def test_coauthor_only_squash_commit_is_counted(self) -> None:
        repository = make_repository("owner/repo", owner="owner")
        merge_commit = make_graph_commit(
            "squash",
            authors=("maintainer", "lidang-jiang"),
            message=(
                "Shared fix\n\nCo-authored-by: Lidang Jiang <lidang-jiang@example.com>"
            ),
        )
        pr = make_rich_pr(1, repository, [make_graph_commit("source")], merge_commit)

        commits = update_readme._reconstruct_pr_authored_commits(pr)

        self.assertEqual(["squash"], [commit["oid"] for commit in commits])

    def test_regular_merge_counts_preserved_authored_source_commits(self) -> None:
        repository = make_repository("owner/repo", owner="owner")
        sources = [make_graph_commit("a"), make_graph_commit("b")]
        merge_commit = make_graph_commit("merge", authors=("maintainer",))
        pr = make_rich_pr(
            1,
            repository,
            sources,
            merge_commit,
            parent_count=2,
        )

        with patch(
            "update_readme._fetch_object_history",
            return_value=list(reversed(sources)),
        ):
            commits = update_readme._reconstruct_pr_authored_commits(pr)

        self.assertEqual(["a", "b"], [commit["oid"] for commit in commits])

    def test_multi_commit_squash_and_rebase_use_actual_output_oids(self) -> None:
        repository = make_repository("owner/repo", owner="owner")
        sources = [
            make_graph_commit("source-a", message="First"),
            make_graph_commit("source-b", message="Second"),
        ]
        squash = make_graph_commit("squash", message="Combined")
        squash_pr = make_rich_pr(1, repository, sources, squash)
        unrelated_parent = make_graph_commit("parent", message="Unrelated")

        with patch(
            "update_readme._fetch_merge_history",
            return_value=[squash, unrelated_parent],
        ):
            squash_commits = update_readme._reconstruct_pr_authored_commits(squash_pr)

        self.assertEqual(["squash"], [commit["oid"] for commit in squash_commits])

        rebased = [
            {**sources[0], "oid": "rebased-a"},
            {**sources[1], "oid": "rebased-b"},
        ]
        rebase_pr = make_rich_pr(2, repository, sources, rebased[-1])
        with patch(
            "update_readme._fetch_merge_history",
            return_value=list(reversed(rebased)),
        ):
            rebase_commits = update_readme._reconstruct_pr_authored_commits(rebase_pr)

        self.assertEqual(
            ["rebased-a", "rebased-b"],
            [commit["oid"] for commit in rebase_commits],
        )

    def test_merge_history_accepts_an_exact_prefix_with_older_history(self) -> None:
        repository = make_repository("owner/repo", owner="owner")
        merge_commit = make_graph_commit("merge")
        pull_request = make_rich_pr(
            1,
            repository,
            [make_graph_commit("source-a"), make_graph_commit("source-b")],
            merge_commit,
        )
        nodes = [merge_commit, make_graph_commit("parent")]
        response = {
            "data": {
                "repository": {
                    "object": {
                        "__typename": "Commit",
                        "history": {
                            "nodes": nodes,
                            "pageInfo": {
                                "hasNextPage": True,
                                "endCursor": "older-history",
                            },
                        },
                    }
                }
            }
        }

        with patch("update_readme._run_graphql", return_value=response):
            history = update_readme._fetch_merge_history(pull_request, 2)

        self.assertEqual(nodes, history)

    def test_reconstruction_rejects_incomplete_pr_data_and_counts(self) -> None:
        repository = make_repository("owner/repo", owner="owner")
        incomplete = make_pr(1, repository)

        with self.assertRaisesRegex(RuntimeError, "incomplete merge data"):
            update_readme._reconstruct_pr_authored_commits(incomplete)

        rich = make_rich_pr(
            2,
            repository,
            [make_graph_commit("source")],
            make_graph_commit("merge"),
        )
        rich["commits"]["totalCount"] = 2
        with self.assertRaisesRegex(RuntimeError, "incomplete commit list"):
            update_readme._reconstruct_pr_authored_commits(rich)

    def test_rebase_matching_handles_nullable_author_email(self) -> None:
        source = make_graph_commit("source")
        actual = make_graph_commit("actual", message=source["message"])
        source["author"] = {"email": None}
        actual["author"] = {"email": None}

        self.assertTrue(update_readme._same_authored_change(source, actual))

    def test_coauthor_detection_excludes_signed_off_only_trailer(self) -> None:
        signed_off = make_graph_commit(
            "signed",
            authors=("maintainer", "Lidang-Jiang"),
            message=(
                "Release aggregation\n\n"
                "Signed-off-by: Lidang Jiang <Lidang-Jiang@example.com>"
            ),
        )
        coauthored = make_graph_commit(
            "coauthored",
            authors=("maintainer", "Lidang-Jiang"),
            message=(
                "Shared fix\n\nCo-authored-by: Lidang Jiang <Lidang-Jiang@example.com>"
            ),
        )

        self.assertFalse(update_readme._commit_has_user_as_coauthor(signed_off))
        self.assertTrue(update_readme._commit_has_user_as_coauthor(coauthored))


if __name__ == "__main__":
    unittest.main()
