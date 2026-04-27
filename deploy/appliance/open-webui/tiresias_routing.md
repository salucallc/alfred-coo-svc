# Open-WebUI Tiresias Routing

This document describes how the Open-WebUI service is configured to route all chat completion requests through the Tiresias policy proxy.

## Configuration
- Set `OPENAI_API_BASE_URL` to `http://tiresias-proxy:8840/proxy/openai` in the Docker compose environment.
- Ensure `TIRESIAS_URL` is set in the `.env.template` for services that need to contact the proxy.

## Security
All raw provider tokens have been removed from the environment; authentication is performed via Soulkey headers injected by the proxy.
