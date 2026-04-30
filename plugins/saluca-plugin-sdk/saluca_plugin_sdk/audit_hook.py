import json
import requests
from .dataclasses import AuditEvent


def post_audit(event: AuditEvent, url: str = "https://soul-svc.internal/v1/audit/external-agent/event") -> requests.Response:
    """POST an audit event to the soul service.
    The payload is HMAC‑signed using the plugin's soulkey (placeholder).
    """
    headers = {
        "Content-Type": "application/json",
        # Placeholder for HMAC signature – in real code, compute with secret key.
        "X-Signature": "placeholder-signature",
    }
    data = json.dumps({
        "event_type": event.event_type,
        "payload": event.payload,
        "timestamp": event.timestamp.isoformat(),
    })
    response = requests.post(url, headers=headers, data=data, timeout=5)
    response.raise_for_status()
    return response
