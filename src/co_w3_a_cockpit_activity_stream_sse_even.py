from fastapi import APIRouter, Request, Depends
from sse_starlette.sse import EventSourceResponse
from typing import AsyncGenerator, Dict, Any
import asyncio
import logging

event_router = APIRouter()

# In-memory store of recent events (production impl would use Redis or similar)
recent_events: list = []
MAX_EVENTS_PER_CLIENT = 1000
EVENT_RATE_LIMIT = 100  # per second per client
clients: Dict[str, asyncio.Queue] = {}

@event_router.get("/cockpit/activity-stream")
async def activity_stream(request: Request):
    async def event_generator() -> AsyncGenerator[Dict[str, Any], None]:
        client_id = str(id(request))
        queue = asyncio.Queue()
        clients[client_id] = queue
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                    yield {
                        "event": event["event_type"],
                        "data": event["data"]
                    }
                except asyncio.TimeoutError:
                    continue
        finally:
            clients.pop(client_id, None)

    return EventSourceResponse(event_generator())

def broadcast_event(event_type: str, data: Dict[str, Any]) -> None:
    """Broadcasts an event to all connected clients."""
    event = {"event_type": event_type, "data": data}
    # Rate limiting: drop events if too many pending
    for client_queue in clients.values():
        if client_queue.qsize() < MAX_EVENTS_PER_CLIENT:
            client_queue.put_nowait(event)

# Placeholder handlers for pause/resume - to be enhanced with actual state tracking
paused_clients = set()

def pause_client(client_id: str) -> None:
    paused_clients.add(client_id)

def resume_client(client_id: str) -> None:
    paused_clients.discard(client_id)


# Sample usage inside tool execution path (e.g., co_w2_a_cockpit_live_activity_panel_all_.py)
# broadcast_event("tool_call_executed", {"tool": "sample_tool", "execution_time": "0.5s"})