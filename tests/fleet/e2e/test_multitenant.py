import pytest

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
