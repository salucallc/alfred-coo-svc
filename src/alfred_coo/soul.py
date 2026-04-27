"""
Async HTTP client for soul-svc memory endpoints.
"""

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential


# When a topic filter is active, fetch a generous window of recent memories
# from the session and filter client-side. The session has many memories of
# varied topics so a small ``limit`` would otherwise miss the topic we care
# about. 200 covers ~10 wave kickoffs worth of state+wave_pass+gate_ack
# entries, which is well past any realistic horizon for the consumers.
_TOPIC_FILTER_FETCH_FLOOR = 200


def _normalize_memory(mem: dict) -> dict:
    """Best-effort coerce a soul-svc memory record into the shape the
    autonomous_build callers expect.

    soul-svc v2.0.0 ``GET /v1/memory/{session_id}`` returns each entry with
    ``full_context`` (and ``topic_id``); the older response shape — and our
    callers — expect ``content``. Map ``full_context`` → ``content`` if
    ``content`` is absent, and ensure ``topics`` is a list (some endpoints
    only return ``topic_id``).
    """
    if not isinstance(mem, dict):
        return mem
    out = dict(mem)
    if not out.get("content") and out.get("full_context") is not None:
        out["content"] = out["full_context"]
    topics = out.get("topics")
    topic_id = out.get("topic_id")
    if not isinstance(topics, list):
        topics = []
    if topic_id and topic_id not in topics:
        topics = [topic_id, *topics]
    out["topics"] = topics
    return out


def _matches_topic(mem: dict, topics: list[str]) -> bool:
    if not topics:
        return True
    mem_topics = mem.get("topics") or []
    topic_id = mem.get("topic_id")
    haystack = set(mem_topics)
    if topic_id:
        haystack.add(topic_id)
    return any(t in haystack for t in topics)


class SoulClient:
    def __init__(self, base_url: str, api_key: str, session_id: str, fallback_urls: list[str] | None = None, timeout: float = 30.0):
        self.base_url = base_url
        self.api_key = api_key
        self.session_id = session_id
        self.fallback_urls = fallback_urls or []
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)

    async def _get_session_memories(self, base_url: str, fetch_limit: int) -> list[dict]:
        """Fetch raw memories for ``self.session_id`` from a single base URL.

        Uses ``GET /v1/memory/{session_id}`` — the actual soul-svc v2.0.0
        endpoint. The previous implementation called ``/v1/memory/recent``,
        which does NOT exist on soul-svc and was being silently routed to
        ``/v1/memory/{session_id}`` with ``session_id="recent"``, returning
        an empty list every time and breaking every cache lookup
        (``restore``, ``lookup_wave_pass``, ``lookup_gate_ack``).
        """
        url = f"{base_url}/v1/memory/{self.session_id}"
        params = {"limit": fetch_limit}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        response = await self._client.get(url, params=params, headers=headers)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            mems = payload.get("memories") or []
        elif isinstance(payload, list):
            mems = payload
        else:
            mems = []
        return mems

    async def _get_session_memories_with_fallback(self, fetch_limit: int) -> list[dict]:
        try:
            return await self._get_session_memories(self.base_url, fetch_limit)
        except Exception as e:
            if not self.fallback_urls:
                raise e
            for fallback_url in self.fallback_urls:
                try:
                    fallback_client = httpx.AsyncClient(timeout=self.timeout)
                    try:
                        url = f"{fallback_url}/v1/memory/{self.session_id}"
                        params = {"limit": fetch_limit}
                        headers = {"Authorization": f"Bearer {self.api_key}"}
                        response = await fallback_client.get(url, params=params, headers=headers)
                        response.raise_for_status()
                        payload = response.json()
                    finally:
                        await fallback_client.aclose()
                    if isinstance(payload, dict):
                        return payload.get("memories") or []
                    if isinstance(payload, list):
                        return payload
                    return []
                except Exception:
                    continue
            raise e

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
    async def recent_memories(self, limit: int = 20, topics: list[str] | None = None) -> list[dict]:
        """Return the most recent memories for ``self.session_id``, optionally
        filtered by ``topics``.

        Result shape: ``list[dict]``. Each dict is normalized so that
        ``content`` is populated (mapped from ``full_context`` when soul-svc
        only returned the latter) and ``topics`` is always a list. soul-svc
        returns memories in reverse-chronological order; the filter and
        ``limit`` truncation are applied after normalization.
        """
        # When a topic filter is active, fetch a generous window of recent
        # memories from the session so the filter has enough material to
        # match against. soul-svc has no per-topic ``recent`` endpoint, so
        # we fetch and filter client-side.
        if topics:
            fetch_limit = max(_TOPIC_FILTER_FETCH_FLOOR, limit)
        else:
            fetch_limit = limit
        mems = await self._get_session_memories_with_fallback(fetch_limit)
        normalized = [_normalize_memory(m) for m in mems if isinstance(m, dict)]
        if topics:
            normalized = [m for m in normalized if _matches_topic(m, topics)]
        return normalized[:limit]

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
