import pytest
import websockets
import asyncio

@pytest.mark.asyncio
async def test_invalid_key():
    uri = "ws://localhost:8090/v1/fleet/link"
    async with websockets.connect(uri, extra_headers={"Authorization": "Bearer invalid"}) as ws:
        with pytest.raises(websockets.exceptions.ConnectionClosedError):
            await ws.send("test")
