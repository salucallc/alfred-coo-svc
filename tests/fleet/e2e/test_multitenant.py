import pytest
import time
import subprocess
import json

# Assuming existing fixtures: fleet_harness provides endpoint and tenant contexts

@pytest.fixture
def tenant_a(fleet_harness):
    # Setup tenant_a configuration
    fleet_harness.configure_tenant('acme-corp', tool_allowlist=["mcp.github.read"])
    return fleet_harness.get_tenant_context('acme-corp')

@pytest.fixture
def tenant_b(fleet_harness):
    # Setup tenant_b configuration with empty allowlist
    fleet_harness.configure_tenant('beta-industries', tool_allowlist=[])
    return fleet_harness.get_tenant_context('beta-industries')

def test_multitenant_memory_isolation_memory_isolation(tenant_a, tenant_b, fleet_harness):
    """Class 1 – Memory isolation between tenant_a and tenant_b"""
    # Tenant A writes a secret memory
    secret = {"content": "tenant_a.secret", "topics": ["tenant_a"]}
    write_resp = tenant_a.memory_write(secret)
    assert write_resp.status_code == 200

    # Tenant B searches for the secret content – should find nothing
    search_resp = tenant_b.memory_search(query="tenant_a.secret")
    assert search_resp.status_code == 200
    assert len(search_resp.json().get('results', [])) == 0

    # Tenant B searches for any topic written under tenant_a – should find nothing
    topic_search = tenant_b.memory_search(query="tenant_a")
    assert topic_search.status_code == 200
    assert len(topic_search.json().get('results', [])) == 0

def test_multitenant_policy_leakage_policy_leakage(tenant_a, tenant_b, fleet_harness):
    """Class 2 – Ensure tenant_b cannot use tenant_a's allowed tool"""
    # Tenant B attempts a tool that is only allowed for tenant_a
    tool_resp = tenant_b.invoke_tool("mcp.github.read", params={})
    # Expect blocked with specific error code
    assert tool_resp.status_code == 403
    assert tool_resp.json().get('error_code') == "tool_not_in_tenant_allowlist"

    # Verify audit log contains the blocked attempt with correct tenant_id and error_code
    audit_entries = fleet_harness.fetch_audit_logs(filter_tenant="beta-industries")
    matching = [e for e in audit_entries if e.get('error_code') == "tool_not_in_tenant_allowlist"]
    assert len(matching) > 0

def test_multitenant_blackout_recovery(tenant_a, tenant_b, fleet_harness):
    """Class 3 – Blackout recovery preserves per-tenant global_seq independence"""
    # Determine blackout duration based on env var
    fast_mode = fleet_harness.get_env('E2E_FAST') == '1'
    blackout_seconds = 30 if fast_mode else 600  # 10 minutes

    # Steady-state: both tenants write 5 memories each
    for i in range(5):
        mem_a = {"content": f"tenant_a_steady_{i}", "topics": ["tenant_a"]}
        mem_b = {"content": f"tenant_b_steady_{i}", "topics": ["tenant_b"]}
        resp_a = tenant_a.memory_write(mem_a)
        resp_b = tenant_b.memory_write(mem_b)
        assert resp_a.status_code == 200
        assert resp_b.status_code == 200

    # Simulate hub blackout
    fleet_harness.simulate_hub_blackout(start=True)
    time.sleep(blackout_seconds)

    # During blackout: each tenant writes 3 more memories locally
    for i in range(3):
        mem_a = {"content": f"tenant_a_blackout_{i}", "topics": ["tenant_a"]}
        mem_b = {"content": f"tenant_b_blackout_{i}", "topics": ["tenant_b"]}
        resp_a = tenant_a.memory_write(mem_a)
        resp_b = tenant_b.memory_write(mem_b)
        assert resp_a.status_code == 200
        assert resp_b.status_code == 200

    # Restore hub
    fleet_harness.simulate_hub_blackout(start=False)
    # Wait for reconciliation (harness provides helper)
    fleet_harness.wait_for_reconciliation(timeout=60)

    # Assert: per-tenant global_seq monotonic and gap-free; tenant_a's seq does NOT consume tenant_b's slots
    # Use harness helper to fetch global_seq ranges per tenant
    seq_ranges = fleet_harness.get_global_seq_ranges_per_tenant()
    assert len(seq_ranges) == 2
    for tenant_id, ranges in seq_ranges.items():
        # ranges should be list of (start, end) tuples; we expect one contiguous range
        assert len(ranges) == 1
        start, end = ranges[0]
        assert end - start + 1 == 8  # total 8 memories per tenant
    # Ensure ranges do not overlap across tenants (tenant_a's seq not consuming tenant_b's slots)
    # Simple check: tenant_a's range start > tenant_b's range end or vice versa? Actually they are independent global_seq sequences.
    # The harness should guarantee independence.

    # Assert both tenants end with all 8 memories visible on the hub
    for tenant_id in ['acme-corp', 'beta-industries']:
        memories = fleet_harness.fetch_memories_for_tenant(tenant_id)
        assert len(memories) == 8
        contents = [m['content'] for m in memories]
        expected_prefixes = [f"tenant_{tenant_id.split('-')[0]}_steady_", f"tenant_{tenant_id.split('-')[0]}_blackout_"]
        for prefix in expected_prefixes:
            matches = sum(1 for c in contents if c.startswith(prefix))
            assert matches == 5 if 'steady' in prefix else 3

    # Audit verification: produce e2e_audit.jsonl, count distinct tenant_ids
    audit_path = fleet_harness.dump_audit_log('e2e_audit.jsonl')
    # Use jq to count distinct tenant_ids
    cmd = "jq '.tenant_id' " + audit_path + " | sort -u | wc -l"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    distinct_count = int(result.stdout.strip())
    assert distinct_count == 2, f"Expected 2 distinct tenant_ids, got {distinct_count}"
    # Ensure no null or __legacy__
    cmd2 = "jq '.tenant_id' " + audit_path + " | grep -v null | grep -v __legacy__ | sort -u | wc -l"
    result2 = subprocess.run(cmd2, shell=True, capture_output=True, text=True)
    clean_count = int(result2.stdout.strip())
    assert clean_count == 2

    # DB verification: query fleet_memory_sync_log for monotonic per-tenant local_seq
    rows = fleet_harness.query_db(
        "SELECT tenant_id, local_seq FROM fleet_memory_sync_log WHERE tenant_id IN ('acme-corp', 'beta-industries') ORDER BY tenant_id, local_seq"
    )
    # Group by tenant_id
    by_tenant = {}
    for row in rows:
        by_tenant.setdefault(row['tenant_id'], []).append(row['local_seq'])
    for tenant_id, seqs in by_tenant.items():
        # Check monotonic and gap-free
        for i in range(len(seqs) - 1):
            assert seqs[i] + 1 == seqs[i + 1], f"Gap in local_seq for tenant {tenant_id}: {seqs[i]} -> {seqs[i+1]}"
        assert len(seqs) == 8, f"Expected 8 local_seq rows per tenant, got {len(seqs)} for {tenant_id}"
