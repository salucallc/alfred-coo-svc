# External Agents Migration

This migration introduces the `external_agents` table used by the agent_ingest service.

## Schema

- `agent_id` UUID PK
- `tenant_id` UUID
- `plugin_id` TEXT
- `plugin_version` TEXT
- `direction` ENUM (`inbound`, `outbound`, `bidirectional`)
- `framework_or_surface` TEXT
- `framework_or_surface_version` TEXT
- `mode` TEXT (`cli`, `mcp`, `api`)
- `manifest_only` BOOLEAN default false
- `capabilities` JSONB
- `actions` JSONB (only for outbound)
- `scope` JSONB
- `policy` JSONB
- `soulkey_kid` TEXT
- `harness` TEXT
- `registered_at` TIMESTAMPTZ
- `last_health_check` TIMESTAMPTZ nullable
- `status` TEXT enum (`active`, `paused`, `error`, `revoked`)

Indexes are created for tenant, plugin, direction, and a partial unique index on `(tenant_id, agent_id)` where status is not revoked.
