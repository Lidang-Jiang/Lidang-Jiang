import unittest
from unittest.mock import patch

import request_budget


class GitHubRequestBudgetTest(unittest.TestCase):
    def test_shared_budget_fails_closed_before_excess_request(self) -> None:
        with (
            patch("request_budget.MAX_GITHUB_REQUESTS", 1),
            patch("request_budget._request_count", 0),
        ):
            request_budget.consume_github_request("first request")
            with self.assertRaisesRegex(RuntimeError, "request budget"):
                request_budget.consume_github_request("second request")


if __name__ == "__main__":
    unittest.main()
