import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = FastAPI()

VALID_KEY = os.getenv("FLEET_GATEWAY_KEY", "valid-key")

@app.websocket("/v1/fleet/link")
async def fleet_link(ws: WebSocket):
    await ws.accept()
    auth = ws.headers.get("authorization")
    if auth != f"Bearer {VALID_KEY}":
        await ws.close(code=1008)
        return
    try:
        while True:
            data = await ws.receive_text()
            await ws.send_text(f"echo: {data}")
    except WebSocketDisconnect:
        pass
