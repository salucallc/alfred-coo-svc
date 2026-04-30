import asyncpg
from .models import ExternalAgent
from typing import List, Optional
from uuid import UUID

class ExternalAgentRepository:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def create(self, agent: ExternalAgent) -> ExternalAgent:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO external_agents (
                    tenant_id, plugin_id, plugin_version, direction,
                    framework_or_surface, framework_or_surface_version,
                    mode, manifest_only, capabilities, actions,
                    scope, policy, soulkey_kid, harness, registered_at,
                    last_health_check, status
                ) VALUES (
                    $1, $2, $3, $4,
                    $5, $6,
                    $7, $8, $9, $10,
                    $11, $12, $13, $14, now(),
                    $15, $16
                ) RETURNING *
                """,
                agent.tenant_id,
                agent.plugin_id,
                agent.plugin_version,
                agent.direction,
                agent.framework_or_surface,
                agent.framework_or_surface_version,
                agent.mode,
                agent.manifest_only,
                agent.capabilities,
                agent.actions,
                agent.scope,
                agent.policy,
                agent.soulkey_kid,
                agent.harness,
                agent.last_health_check,
                agent.status,
            )
            return ExternalAgent(**dict(row))

    async def get_by_id(self, agent_id: UUID) -> Optional[ExternalAgent]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM external_agents WHERE agent_id = $1", agent_id
            )
            if row:
                return ExternalAgent(**dict(row))
            return None

    async def list_by_tenant(self, tenant_id: UUID) -> List[ExternalAgent]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM external_agents WHERE tenant_id = $1", tenant_id
            )
            return [ExternalAgent(**dict(r)) for r in rows]

    async def delete(self, agent_id: UUID) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM external_agents WHERE agent_id = $1", agent_id
            )
