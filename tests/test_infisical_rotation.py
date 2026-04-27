import time
import os
import requests

def test_infisical_rotation():
    base = os.getenv('INFISICAL_HOST', 'http://localhost:3000')
    secret_id = os.getenv('INFISICAL_TEST_SECRET_ID', 'test-key')

    # Get original value
    resp = requests.get(f"{base}/api/v3/secrets/{secret_id}")
    resp.raise_for_status()
    orig = resp.json().get('value')

    # Rotate
    rot = requests.post(f"{base}/api/v3/secrets/{secret_id}/rotate")
    assert rot.status_code == 200
    new = rot.json().get('value')
    assert new != orig

    # Poll for service to pick up new value (simulate 60s poll + buffer)
    for _ in range(12):
        time.sleep(5)
        svc_resp = requests.get(f"{base}/service/secret/{secret_id}")
        if svc_resp.ok and svc_resp.json().get('value') == new:
            break
    else:
        assert False, "Service did not pick up rotated secret within expected time"
