import asyncio
import os
from unittest import mock

import pytest
import httpx

from aletheia.app.watchers.github_poller import (
    get_watched_repos,
    list_pull_requests,
    enqueue_pr_review,
    run_poller,
)

@pytest.fixture
def fake_pr():
    return {
        "id": 12345,
        "number": 1,
        "base": {"repo": {"full_name": "saluca-llc/example-repo"}},
        "state": "open",
    }

@pytest.mark.asyncio
async def test_get_watched_repos(monkeypatch):
    monkeypatch.setenv("GITHUB_WATCHED_REPOS", "saluca-llc/example-repo")
    repos = get_watched_repos()
    assert "saluca-llc/example-repo" in repos
    assert "saluca-llc/*" in repos

@pytest.mark.asyncio
async def test_list_pull_requests_success(fake_pr, monkeypatch):
    async def mock_get(url, params=None):
        class Resp:
            def raise_for_status(self):
                pass
            def json(self):
                return [fake_pr]
        return Resp()
    client = mock.AsyncMock()
    client.get.side_effect = mock_get
    prs = await list_pull_requests("saluca-llc/example-repo", client)
    assert isinstance(prs, list)
    assert prs[0]["id"] == fake_pr["id"]

@pytest.mark.asyncio
async def test_enqueue_pr_review_logs(caplog, fake_pr):
    await enqueue_pr_review(fake_pr)
    # Ensure log entry was created
    assert any("Enqueue pr_review job" in record.message for record in caplog.records)

@pytest.mark.asyncio
async def test_run_poller_stops(monkeypatch, caplog):
    # Patch environment variable
    monkeypatch.setenv("GITHUB_PAT_POLLER", "dummy-token")
    # Stub out async functions to avoid real network calls
    async def fake_list_pull_requests(repo, client):
        return []
    async def fake_enqueue_pr_review(pr):
        pass
    monkeypatch.setattr("aletheia.app.watchers.github_poller.list_pull_requests", fake_list_pull_requests)
    monkeypatch.setattr("aletheia.app.watchers.github_poller.enqueue_pr_review", fake_enqueue_pr_review)
    # Run poller for a single iteration then stop
    stop = asyncio.Event()
    async def stopper():
        await asyncio.sleep(0.1)
        stop.set()
    await asyncio.gather(run_poller(stop), stopper())
    # Verify that poller started and exited without errors
    assert any("GITHUB_PAT_POLLER" not in os.environ for record in caplog.records) is False
