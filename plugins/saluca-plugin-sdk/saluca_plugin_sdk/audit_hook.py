import json
import requests
from .dataclasses import AuditEvent

def post_audit(event: AuditEvent, soulkey: str, base_url: str = "https://soul-svc.internal"):
    url = f"{base_url}/v1/audit/external-agent/event"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"HMAC {soulkey}",
    }
    response = requests.post(url, headers=headers, data=json.dumps(event.__dict__))
    response.raise_for_status()
    return response.json()
