# ALT-06: GitHub PR poller

## Target paths
- aletheia/app/watchers/__init__.py
- aletheia/app/watchers/github_poller.py
- aletheia/tests/test_github_poller.py

## Acceptance criteria
- APE/V: Poller logs new `pr_review` job enqueued within 45s of opening test PR. Integration test: throwaway PR → soul-svc verdict record within 3 min.

## Verification approach
- Run the ``run_poller`` coroutine in a test environment with a mocked GitHub API.
- Confirm via log capture that ``enqueue_pr_review`` is called for a newly opened PR within the 45‑second window.
- Verify that the integration test creates a temporary PR, triggers the poller, and asserts a ``pr_review`` record appears in ``soul-svc`` within three minutes.

## Risks
- **Rate limiting**: GitHub API rate limits may be hit during frequent polling; mitigate by using a personal access token with sufficient scopes.
- **Wildcard repo handling**: The placeholder ``saluca-llc/*`` pattern is not expanded in this minimal implementation; future work should enumerate org repos via the GitHub API.
- **Missing token**: If ``GITHUB_PAT_POLLER`` is not set, the poller will log an error and exit; deployment must ensure the variable is provided.
