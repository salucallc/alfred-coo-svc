import asyncio
import asyncpg
import pytest
from uuid import uuid4
from src.alfred_coo.agent_ingest.models import ExternalAgent
from src.alfred_coo.agent_ingest.repository import ExternalAgentRepository

@pytest.fixture(scope="module")
async def db_pool():
    pool = await asyncpg.create_pool(dsn="postgresql://postgres@localhost/test_db")
    async with pool.acquire() as conn:
        await conn.execute(open('migrations/external_agents/001_external_agents_table.sql').read())
    yield pool
    await pool.close()

@pytest.mark.asyncio
async def test_create_and_roundtrip(db_pool):
    repo = ExternalAgentRepository(db_pool)
    agent = ExternalAgent(
        tenant_id=uuid4(),
        plugin_id="myplugin",
        plugin_version="1.2.3",
        direction="inbound",
        framework_or_surface="cli",
        framework_or_surface_version="0.1",
        mode="cli",
        manifest_only=False,
        capabilities={"key": "value"},
        actions=None,
        scope={},
        policy={},
    )
    created = await repo.create(agent)
    fetched = await repo.get_by_id(created.agent_id)
    assert fetched is not None
    assert fetched.tenant_id == agent.tenant_id
    assert fetched.direction == "inbound"

@pytest.mark.asyncio
async def test_list_by_tenant(db_pool):
    repo = ExternalAgentRepository(db_pool)
    tenant = uuid4()
    for i in range(2):
        await repo.create(
            ExternalAgent(
                tenant_id=tenant,
                plugin_id=f"p{i}",
                plugin_version="1.0",
                direction="outbound",
                framework_or_surface="api",
                framework_or_surface_version="1",
                mode="api",
                manifest_only=False,
                capabilities={},
                actions={"run": []},
                scope={},
                policy={},
            )
        )
    agents = await repo.list_by_tenant(tenant)
    assert len(agents) == 2
