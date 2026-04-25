import os
import asyncio
import httpx
import logging
from typing import List, Dict

LOGGER = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
POLL_INTERVAL = 30  # seconds

def get_watched_repos() -> List[str]:
    """Return list of repository full names to watch.
    Default pattern is all repos under the `saluca-llc` organization.
    """
    org = "saluca-llc"
    # In a real implementation we might query the GitHub API for org repos.
    # Here we assume a static list; environment can override.
    extra = os.getenv("GITHUB_WATCHED_REPOS")
    repos = []
    if extra:
        repos = [r.strip() for r in extra.split(",") if r.strip()]
    # Fallback to all repos in org – placeholder using org prefix
    repos.append(f"{org}/*")
    return repos

async def list_pull_requests(repo: str, client: httpx.AsyncClient) -> List[Dict]:
    """Fetch open pull requests for a repository.
    ``repo`` should be in the form ``owner/name``.
    """
    owner, name = repo.split("/")
    url = f"{GITHUB_API}/repos/{owner}/{name}/pulls"
    resp = await client.get(url, params={"state": "open"})
    resp.raise_for_status()
    return resp.json()

async def enqueue_pr_review(pr: Dict):
    """Placeholder for enqueuing a pr_review job.
    In production this would POST to the aletheia service endpoint.
    Here we simply log the action.
    """
    LOGGER.info("Enqueue pr_review job for PR #%s in %s", pr["number"], pr["base"]["repo"]["full_name"])
    # Simulate async call
    await asyncio.sleep(0)

async def poll_repo(repo: str, client: httpx.AsyncClient, processed: set):
    prs = await list_pull_requests(repo, client)
    for pr in prs:
        pr_id = pr["id"]
        if pr_id not in processed:
            await enqueue_pr_review(pr)
            processed.add(pr_id)

async def run_poller(stop_event: asyncio.Event):
    """Main poller loop. Runs until ``stop_event`` is set.
    """
    token = os.getenv("GITHUB_PAT_POLLER")
    if not token:
        LOGGER.error("GITHUB_PAT_POLLER environment variable not set")
        return
    headers = {"Authorization": f"token {token}"}
    async with httpx.AsyncClient(headers=headers) as client:
        processed = set()
        repos = get_watched_repos()
        while not stop_event.is_set():
            tasks = []
            for repo in repos:
                # Resolve wildcard pattern – for simplicity we ignore actual expansion.
                if repo.endswith("/*"):
                    # In a real system we would list org repos; here we skip.
                    continue
                tasks.append(poll_repo(repo, client, processed))
            if tasks:
                await asyncio.gather(*tasks)
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL)

if __name__ == "__main__":
    # Simple entry point for manual testing
    loop = asyncio.get_event_loop()
    stop = asyncio.Event()
    try:
        loop.run_until_complete(run_poller(stop))
    except KeyboardInterrupt:
        stop.set()
        loop.run_until_complete(asyncio.sleep(0))
