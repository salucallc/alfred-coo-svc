import asyncpg
import pytest
import os

@pytest.fixture(scope="module")
async def conn():
    conn = await asyncpg.connect(dsn="postgresql://postgres@localhost/test_db")
    yield conn
    await conn.close()

@pytest.mark.asyncio
async def test_migration_applies(conn):
    migration_path = os.path.join('migrations', 'external_agents', '001_external_agents_table.sql')
    sql = open(migration_path).read()
    await conn.execute(sql)
    table = await conn.fetchrow("SELECT to_regclass('public.external_agents') as tbl")
    assert table['tbl'] == 'external_agents'
    idx = await conn.fetch("SELECT indexname FROM pg_indexes WHERE tablename='external_agents'")
    index_names = {r['indexname'] for r in idx}
    assert 'idx_external_agents_tenant_id' in index_names
    assert 'idx_external_agents_plugin_id' in index_names
    assert 'idx_external_agents_direction' in index_names
    assert any('uq_external_agents_tenant_agent' in name for name in index_names)
