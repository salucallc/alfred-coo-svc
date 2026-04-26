# Fleet Endpoint Threat Model

## Overview
The endpoint runs in potentially hostile customer networks. Threats include:

1. **Credential theft** – protect `api_key` with short TTL and rotation.
2. **Man‑in‑the‑middle on WS** – TLS 1.3 ensures confidentiality.
3. **Denial of service** – degraded mode limits impact; queue caps prevent overload.
4. **Policy leakage** – per‑tenant policy bundles isolate permissions.
5. **Data exfiltration** – append‑only memory sync prevents overwriting historic data.
