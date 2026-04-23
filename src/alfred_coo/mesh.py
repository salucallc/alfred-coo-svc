"""
Async HTTP client for the soul-svc mesh-tasks API.
"""

import re
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

class MeshClient:
    def __init__(self, base_url: str, api_key: str, fallback_urls: list[str] | None = None, timeout: float = 30.0):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.fallback_urls = [url.rstrip('/') for url in (fallback_urls or [])]
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)
    
    def _get_auth_header(self):
        return {"Authorization": f"Bearer {self.api_key}"}
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, max=10),
        retry=retry_if_exception_type((httpx.NetworkError, httpx.TimeoutException))
    )
    async def _make_request(self, method: str, url: str, **kwargs):
        urls_to_try = [self.base_url] + self.fallback_urls
        last_exception = None
        
        for base in urls_to_try:
            try:
                full_url = f"{base}{url}"
                response = await self._client.request(method, full_url, **kwargs)
                response.raise_for_status()
                return response.json()
            except Exception as e:
                last_exception = e
                continue
                
        raise last_exception
    
    async def list_pending(self, limit: int = 50) -> list[dict]:
        # soul-svc returns {"tasks": [...], "count": N}; unwrap here.
        url = f"/v1/mesh/tasks?status=pending&limit={limit}"
        headers = self._get_auth_header()
        resp = await self._make_request("GET", url, headers=headers)
        if isinstance(resp, dict):
            return resp.get("tasks", [])
        return resp

    async def list_tasks(self, status: str | None = None, limit: int = 50) -> list[dict]:
        """Generic mesh tasks listing. Used by the autonomous_build orchestrator
        to poll for completed child tasks (status="completed") after dispatch.

        Mirrors the unwrap behaviour of `list_pending`.
        """
        qs = f"limit={limit}"
        if status:
            qs += f"&status={status}"
        url = f"/v1/mesh/tasks?{qs}"
        headers = self._get_auth_header()
        resp = await self._make_request("GET", url, headers=headers)
        if isinstance(resp, dict):
            return resp.get("tasks", [])
        return resp

    async def create_task(
        self,
        title: str,
        description: str = "",
        from_session_id: str | None = None,
    ) -> dict:
        """POST /v1/mesh/tasks. Returns the created task record.

        Used by the autonomous_build orchestrator to fan out per-ticket child
        tasks (tagged `[persona:alfred-coo-a]` by the caller). Mirrors the
        standalone `mesh_task_create` tool handler in tools.py but reuses the
        shared httpx client + failover chain rather than urllib.
        """
        url = "/v1/mesh/tasks"
        headers = self._get_auth_header()
        data = {
            "from_session_id": from_session_id or "alfred-coo",
            "title": title,
            "description": description or "",
        }
        return await self._make_request("POST", url, headers=headers, json=data)

    async def claim(self, task_id: str, session_id: str, node_id: str) -> dict:
        url = f"/v1/mesh/tasks/{task_id}/claim"
        headers = self._get_auth_header()
        data = {"session_id": session_id, "node_id": node_id}
        return await self._make_request("PATCH", url, headers=headers, json=data)

    async def complete(
        self,
        task_id: str,
        session_id: str,
        result: dict,
        status: str = "completed",
    ) -> dict:
        url = f"/v1/mesh/tasks/{task_id}/complete"
        headers = self._get_auth_header()
        data = {
            "session_id": session_id,
            "status": status,
            "result": result,
        }
        return await self._make_request("PATCH", url, headers=headers, json=data)
    
    async def heartbeat(self, session_id: str, node_id: str, harness: str, current_task: str = "", metadata: dict | None = None) -> dict:
        url = "/v1/mesh/heartbeat"
        headers = self._get_auth_header()
        data = {
            "session_id": session_id,
            "node_id": node_id,
            "harness": harness
        }
        if current_task:
            data["current_task"] = current_task
        if metadata:
            data["metadata"] = metadata
        return await self._make_request("POST", url, headers=headers, json=data)

def parse_persona_tag(title: str) -> str | None:
    match = re.search(r'\[persona:(.*?)\]', title, re.IGNORECASE)
    return match.group(1) if match else None
