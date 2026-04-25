from fastapi import FastAPI, HTTPException
from .verdict import VerdictRequest
from .soul_writer import write_verdict

app = FastAPI()

@app.get("/healthz")
def healthz():
    return {"status": "ok"}

@app.post("/v1/_debug/verdict")
def debug_verdict(v: VerdictRequest):
    try:
        write_verdict(v.dict())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "recorded", "verdict": v.verdict}
