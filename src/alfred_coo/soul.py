"""
Async HTTP client for soul-svc memory endpoints.
"""

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential


class SoulClient:
    def __init__(self, base_url: str, api_key: str, session_id: str, fallback_urls: list[str] | None = None, timeout: float = 30.0):
        self.base_url = base_url
        self.api_key = api_key
        self.session_id = session_id
        self.fallback_urls = fallback_urls or []
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
    async def recent_memories(self, limit: int = 20, topics: list[str] | None = None) -> list[dict]:
        url = f"{self.base_url}/v1/memory/recent"
        params = {"session_id": self.session_id, "limit": limit}
        if topics:
            params["topics"] = ",".join(topics)
        
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            response = await self._client.get(url, params=params, headers=headers)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            if self.fallback_urls:
                for fallback_url in self.fallback_urls:
                    try:
                        fallback_client = httpx.AsyncClient(timeout=self.timeout)
                        fallback_url_full = f"{fallback_url}/v1/memory/recent"
                        response = await fallback_client.get(fallback_url_full, params=params, headers=headers)
                        await fallback_client.aclose()
                        response.raise_for_status()
                        return response.json()
                    except:
                        continue
            raise e

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
    async def write_memory(self, content: str, topics: list[str] | None = None) -> dict:
        url = f"{self.base_url}/v1/memory/write"
        data = {"session_id": self.session_id, "content": content}
        if topics:
            data["topics"] = topics
            
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            response = await self._client.post(url, json=data, headers=headers)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            if self.fallback_urls:
                for fallback_url in self.fallback_urls:
                    try:
                        fallback_client = httpx.AsyncClient(timeout=self.timeout)
                        fallback_url_full = f"{fallback_url}/v1/memory/write"
                        response = await fallback_client.post(fallback_url_full, json=data, headers=headers)
                        await fallback_client.aclose()
                        response.raise_for_status()
                        return response.json()
                    except:
                        continue
            raise e

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
    async def search_memories(self, query: str, limit: int = 10) -> list[dict]:
        url = f"{self.base_url}/v1/memory/search"
        data = {"query": query, "limit": limit}
        
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            response = await self._client.post(url, json=data, headers=headers)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            if self.fallback_urls:
                for fallback_url in self.fallback_urls:
                    try:
                        fallback_client = httpx.AsyncClient(timeout=self.timeout)
                        fallback_url_full = f"{fallback_url}/v1/memory/search"
                        response = await fallback_client.post(fallback_url_full, json=data, headers=headers)
                        await fallback_client.aclose()
                        response.raise_for_status()
                        return response.json()
                    except:
                        continue
            raise e

    async def close(self):
        await self._client.aclose()
