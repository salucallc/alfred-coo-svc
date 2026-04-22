"""
Health check endpoint for Alfred Coo service.
"""

import time
from fastapi import FastAPI, Response

__version__ = "1.0.0"

_last_loop_tick: float = 0.0

def mark_alive() -> None:
    """Update the last loop tick timestamp to current epoch seconds."""
    global _last_loop_tick
    _last_loop_tick = time.time()

def make_app() -> FastAPI:
    """Create and return a FastAPI app with a health check endpoint."""
    app = FastAPI()
    
    @app.get("/healthz")
    def health_check(response: Response):
        current_time = time.time()
        uptime_seconds = int(current_time)
        last_loop_age_seconds = int(current_time - _last_loop_tick)
        
        status = "ok" if last_loop_age_seconds < 120 else "service degraded"
        
        if last_loop_age_seconds >= 120:
            response.status_code = 503
            
        return {
            "status": status,
            "version": __version__,
            "uptime_seconds": uptime_seconds,
            "last_loop_age_seconds": last_loop_age_seconds
        }
    
    return app
