# OPS-09: Rotation endpoint + docs

## Target paths
- deploy/appliance/infisical/rotation_endpoint.md
- tests/test_infisical_rotation.py
- plans/v1-ga/OPS-09.md

## Acceptance criteria
- `POST rotate → new value; services pick up within 90s`

## Verification approach
- Manual curl test of rotation endpoint verifies new value.
- Automated pytest `test_infisical_rotation.py` checks value change and service poll within 90 seconds.
- CI runs `ruff` and `pytest` ensuring all checks green.

## Risks
- Timing assumptions: poll interval 60s may cause flaky test if service restart delayed.
- Endpoint authentication not covered; test assumes local dev environment.
