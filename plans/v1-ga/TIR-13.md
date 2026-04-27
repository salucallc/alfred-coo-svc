# TIR-13: Update open-webui to route through tiresias

## Target paths
- deploy/appliance/docker-compose.yml
- deploy/appliance/.env.template
- deploy/appliance/open-webui/tiresias_routing.md

## Acceptance criteria
- chat completion via browser works
- `tiresias_audit` row increments per turn

## Verification approach
- Run integration test that triggers a chat completion from Open-WebUI and asserts a successful response.
- Query the `tiresias_audit` table to confirm a new row was added for the request.

## Risks
- Removing raw provider keys may affect other services if not fully migrated.
- Misconfiguration of `OPENAI_API_BASE_URL` could break chat completions.
