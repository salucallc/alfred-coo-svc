import asyncio
import json
import time
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse

router = APIRouter()

# Mapping of client identifier → pause event. The client identifier is derived from the request object's id.
_client_pause_events: dict[int, asyncio.Event] = {}

# Maximum emission rate per client (events per second).
MAX_EVENTS_PER_SEC = 100

async def _event_generator(event_type: str | None, pause_event: asyncio.Event):
    """Yield SSE‑formatted event strings respecting pause state and rate limit.

    Args:
        event_type: Optional filter applied to the generated payload.
        pause_event: An ``asyncio.Event`` that is cleared when the stream is paused.
    """
    interval = 1.0 / MAX_EVENTS_PER_SEC
    while True:
        # Wait until the client has resumed the stream.
        await pause_event.wait()
        payload = {
            "event_type": event_type or "default",
            "timestamp": time.time(),
            "data": {"msg": "activity"},
        }
        # SSE format: "data: <json>\n\n"
        yield f"data: {json.dumps(payload)}\n\n"
        await asyncio.sleep(interval)

@router.get("/sse/activities")
async def activity_stream(request: Request, event_type: str | None = None):
    """SSE endpoint delivering activity events.

    The client can later pause/resume the stream via the ``/pause`` and ``/resume``
    endpoints using the ``client_id`` returned in the ``X-Client-Id`` header.
    """
    client_id = id(request)
    # Initialise the pause event for this client (un‑paused by default).
    pause_event = asyncio.Event()
    pause_event.set()
    client_pause_events[client_id] = pause_event

    async def generator():
        try:
            async for chunk in _event_generator(event_type, pause_event):
                if await request.is_disconnected():
                    break
                yield chunk
        finally:
            # Clean up state when the client disconnects.
            client_pause_events.pop(client_id, None)

    # Expose the client identifier so that pause/resume calls can target this stream.
    headers = {"X-Client-Id": str(client_id)}
    return StreamingResponse(generator(), media_type="text/event-stream", headers=headers)

@router.post("/sse/activities/{client_id}/pause")
async def pause_stream(client_id: int):
    """Pause a specific client stream.

    Returns a JSON object indicating the new state.
    """
    pause_event = client_pause_events.get(client_id)
    if pause_event is None:
        raise HTTPException(status_code=404, detail="Stream not found")
    pause_event.clear()
    return {"status": "paused"}

@router.post("/sse/activities/{client_id}/resume")
async def resume_stream(client_id: int):
    """Resume a paused client stream.
    """
    pause_event = client_pause_events.get(client_id)
    if pause_event is None:
        raise HTTPException(status_code=404, detail="Stream not found")
    pause_event.set()
    return {"status": "resumed"}
