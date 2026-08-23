"""Shared fail-closed request budget for one profile update process."""

MAX_GITHUB_REQUESTS = 300
_request_count = 0


def consume_github_request(context: str) -> None:
    """Reserve one GitHub request or fail before exceeding the process budget."""
    global _request_count
    if _request_count >= MAX_GITHUB_REQUESTS:
        raise RuntimeError(
            "GitHub request budget exhausted before "
            f"{context} ({MAX_GITHUB_REQUESTS} requests)"
        )
    _request_count += 1
