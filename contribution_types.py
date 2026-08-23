"""Shared typed records for GitHub contribution collection and rendering."""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict


class RepositoryOwner(TypedDict):
    login: str


class DefaultBranchRef(TypedDict):
    name: str


class RepositoryInfo(TypedDict):
    nameWithOwner: str
    stargazerCount: int
    url: str
    visibility: str
    owner: RepositoryOwner
    defaultBranchRef: DefaultBranchRef | None


class PullRequestSummary(TypedDict):
    title: str
    url: str
    mergedAt: str
    baseRefName: str


class PullRequestInfo(PullRequestSummary):
    repository: RepositoryInfo
    number: NotRequired[int]
    mergeCommit: NotRequired[dict[str, Any] | None]
    commits: NotRequired[dict[str, Any]]


class ContributionDay(TypedDict):
    commitCount: int


class ContributionPageInfo(TypedDict):
    hasNextPage: bool


class ContributionConnection(TypedDict):
    nodes: list[ContributionDay]
    pageInfo: ContributionPageInfo


class RepositoryContribution(TypedDict):
    repository: RepositoryInfo
    contributions: ContributionConnection


class AccountInfo(TypedDict):
    id: str
    createdAt: str
    databaseId: NotRequired[int]


class AuthoredCommitInfo(TypedDict):
    oid: str
    url: str
    messageHeadline: str
    branches: list[str]
    coauthorOnly: NotRequired[bool]
    requiresDetail: NotRequired[bool]


class CommitRepositoryInfo(TypedDict):
    commits: list[AuthoredCommitInfo]
    stars: int
    url: str


class GroupedRepositoryInfo(CommitRepositoryInfo):
    prs: list[PullRequestSummary]
