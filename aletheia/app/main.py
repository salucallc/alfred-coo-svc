from fastapi import FastAPI, Body

app = FastAPI()

@app.get("/healthz")
def healthz():
    return {"status": "ok"}

@app.post("/v1/_debug/verdict")
async def debug_verdict(payload: dict = Body(...)):
    # Placeholder call to soul_writer
    from .soul_writer import write_verdict
    await write_verdict(payload)
    return {"result": "written"}
