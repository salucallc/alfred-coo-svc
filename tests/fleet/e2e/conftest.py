"""Fleet E2E test fixtures.

The full multi-tenant fleet harness from plan C §5 F21 (SAL-2629) needs
1 hub + 3 endpoints on 3 isolated Docker networks, plus a real
fleet_memory_sync_log database. That infrastructure is not wired into
the default CI runner, so the ``fleet_harness`` fixture skips by default
and only activates when ``FLEET_E2E_DOCKER_UP=1`` is set in the env.

SAL-3672: every PR pushed to alfred-coo-svc was tripping the Fleet E2E
job because the fixture didn't exist at all (fixture-not-found errors,
not skips). The fix is a skip-with-clear-reason path so the workflow
passes-by-skip until the harness is properly wired.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope='session')
def docker_compose_file():
    return 'tests/fleet/e2e/docker-compose.fleet-e2e.yml'


@pytest.fixture
def fleet_harness():
    """Multi-tenant fleet harness — full implementation pending SAL-2629.

    Skips when ``FLEET_E2E_DOCKER_UP=1`` is not set in the env so PRs
    don't all fail on the missing infra. When set, the fixture raises
    ``NotImplementedError`` instead of silently passing — that signals
    to whoever stood up the harness that this stub still needs to be
    replaced with the real implementation against the running stack.

    See test_multitenant.py for the full method surface this harness
    must expose: configure_tenant, get_tenant_context (with
    memory_write / memory_search / invoke_tool methods), fetch_audit_logs,
    simulate_hub_blackout, wait_for_reconciliation,
    get_global_seq_ranges_per_tenant, fetch_memories_for_tenant,
    dump_audit_log, query_db, get_env.
    """
    if os.environ.get("FLEET_E2E_DOCKER_UP") != "1":
        pytest.skip(
            "fleet_harness skipped: full multi-tenant harness needs "
            "1 hub + 3 endpoints on Docker networks. "
            "Set FLEET_E2E_DOCKER_UP=1 after standing up the docker-compose "
            "stack at tests/fleet/e2e/docker-compose.fleet-e2e.yml. "
            "Tracking: SAL-2629 (full impl), SAL-3672 (this skip path)."
        )
    raise NotImplementedError(
        "FLEET_E2E_DOCKER_UP=1 is set but the in-process fleet_harness "
        "stub is not implemented. Replace this fixture body with the "
        "real harness driving the docker-compose stack — see SAL-2629."
    )
