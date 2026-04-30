import requests
import hmac
import hashlib
import json

def post_audit(event: dict, soulkey: str, endpoint: str = "https://soul-svc.saluca.com/v1/audit/external-agent/event"):
    payload = json.dumps(event).encode()
    signature = hmac.new(soulkey.encode(), payload, hashlib.sha256).hexdigest()
    headers = {"Content-Type": "application/json", "X-Signature": signature}
    response = requests.post(endpoint, data=payload, headers=headers)
    response.raise_for_status()
    return response.json()
