"""Headless Alfred COO daemon."""
__version__ = "0.1.0"

import os
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, Response
import httpx

app = FastAPI()

TIRESIAS_URL = os.getenv("TIRESIAS_URL", "http://localhost:8840")
SOULKEY_HEADER = "Authorization"

def _validate_soulkey(request: Request):
    if SOULKEY_HEADER not in request.headers:
        raise HTTPException(status_code=401, detail="Missing Soulkey")
    # Additional validation could be added here.

@app.post("/v1/messages")
async def proxy_messages(request: Request):
    _validate_soulkey(request)
    body = await request.json()
    stream = body.get("stream", False)
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{TIRESIAS_URL}/v1/messages",
            json=body,
            headers={k: v for k, v in request.headers.items()},
            timeout=30.0,
            stream=stream,
        )
        if stream:
            async def generator():
                async for chunk in resp.aiter_bytes():
                    yield chunk
            return StreamingResponse(generator(), status_code=resp.status_code, headers=resp.headers)
        else:
            content = await resp.aread()
            return Response(content=content, status_code=resp.status_code, headers=resp.headers)
