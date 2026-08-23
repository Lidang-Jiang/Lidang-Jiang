"""Small, fail-closed GitHub REST helpers used by the profile updater."""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404
import sys
import time
from typing import Any, cast
from urllib.parse import quote

from request_budget import consume_github_request


GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
REST_MAX_ATTEMPTS = 3
REST_INITIAL_RETRY_DELAY_SECONDS = 2
REST_RATE_LIMIT_RETRY_DELAY_SECONDS = 65
REST_REQUEST_INTERVAL_SECONDS = 2.1
SEARCH_PAGE_SIZE = 100
SEARCH_RESULT_LIMIT = 1_000
_last_rest_request_at: float | None = None


def _redact_tokens(message: str) -> str:
    """Remove active GitHub tokens from command diagnostics."""
    redacted = message
    for token in (GH_TOKEN, GITHUB_TOKEN):
        if token:
            redacted = redacted.replace(token, "***")
    return redacted


def _run_rest(command: list[str]) -> dict[str, Any]:
    """Run one fixed-executable REST request with bounded retries."""
    for attempt in range(1, REST_MAX_ATTEMPTS + 1):
        _pace_rest_request()
        consume_github_request(f"GitHub REST attempt {attempt}")
        try:
            result = subprocess.run(  # nosec B603
                command,
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as error:
            detail = _redact_tokens(
                (error.stderr or "gh returned no error details").strip()
            )
            if attempt == REST_MAX_ATTEMPTS:
                raise RuntimeError(
                    "GitHub REST request failed after "
                    f"{REST_MAX_ATTEMPTS} attempts: {detail}"
                ) from error
            is_rate_limit = "rate limit" in detail.casefold() or "HTTP 429" in detail
            delay = (
                REST_RATE_LIMIT_RETRY_DELAY_SECONDS
                if is_rate_limit
                else REST_INITIAL_RETRY_DELAY_SECONDS * 2 ** (attempt - 1)
            )
            print(
                "GitHub REST request failed on "
                f"attempt {attempt}/{REST_MAX_ATTEMPTS}: {detail}. "
                f"Retrying in {delay} seconds.",
                file=sys.stderr,
            )
            time.sleep(delay)
            continue

        loaded_response: object = json.loads(result.stdout)
        if not isinstance(loaded_response, dict):
            raise RuntimeError("GitHub REST response was not an object")
        return cast(dict[str, Any], loaded_response)

    raise AssertionError("REST retry loop exited unexpectedly")


def _pace_rest_request() -> None:
    """Keep REST calls below GitHub Search's authenticated per-minute limit."""
    global _last_rest_request_at
    now = time.monotonic()
    if _last_rest_request_at is not None:
        delay = REST_REQUEST_INTERVAL_SECONDS - (now - _last_rest_request_at)
        if delay > 0:
            time.sleep(delay)
            now = time.monotonic()
    _last_rest_request_at = now


def search_commits(
    repository_name: str,
    identity_emails: set[str],
) -> list[dict[str, Any]]:
    """Find default-branch commits mentioning any profile identity email."""
    if not identity_emails:
        return []
    email_expression = " OR ".join(sorted(identity_emails))
    query = f"repo:{repository_name} ({email_expression})"
    items: list[dict[str, Any]] = []
    page = 1

    while True:
        response = _run_rest(
            [
                "gh",
                "api",
                "-X",
                "GET",
                "search/commits",
                "-f",
                f"q={query}",
                "-f",
                f"per_page={SEARCH_PAGE_SIZE}",
                "-f",
                f"page={page}",
            ]
        )
        if response.get("incomplete_results"):
            raise RuntimeError(
                f"GitHub commit search was incomplete for {repository_name}"
            )
        total_count = response.get("total_count")
        page_items = response.get("items")
        if not isinstance(total_count, int) or not isinstance(page_items, list):
            raise RuntimeError(
                f"GitHub commit search response was malformed for {repository_name}"
            )
        if total_count > SEARCH_RESULT_LIMIT:
            raise RuntimeError(
                "GitHub commit search exceeded the 1,000-result limit for "
                f"{repository_name}"
            )
        items.extend(cast(list[dict[str, Any]], page_items))
        if len(items) >= total_count:
            return items
        if not page_items:
            raise RuntimeError(
                f"GitHub commit search ended early for {repository_name}"
            )
        page += 1


def is_commit_reachable(
    repository_name: str,
    branch: str,
    oid: str,
) -> bool:
    """Return whether a commit is currently an ancestor of the named branch."""
    owner, name = repository_name.split("/", 1)
    endpoint = (
        f"repos/{quote(owner, safe='')}/{quote(name, safe='')}/compare/"
        f"{quote(oid, safe='')}...{quote(branch, safe='')}"
    )
    response = _run_rest(["gh", "api", "-X", "GET", endpoint])
    merge_base = response.get("merge_base_commit")
    status = response.get("status")
    return (
        isinstance(merge_base, dict)
        and merge_base.get("sha") == oid
        and status in {"ahead", "identical"}
    )
