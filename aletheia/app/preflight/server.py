from fastapi import FastAPI, HTTPException

app = FastAPI()

@app.post("/v1/preflight")
def preflight(channel: str):
    """Simple preflight check that aborts if the Slack channel does not exist.
    For demonstration, any channel named 'nonexistent' is treated as invalid.
    """
    if channel == "nonexistent":
        # Log the failure (placeholder)
        print(f"Preflight failed: channel {channel} does not exist")
        raise HTTPException(status_code=412, detail="FAIL: non-existent Slack channel")
    # Otherwise succeed
    return {"verdict": "PASS", "detail": f"Channel {channel} exists"}
