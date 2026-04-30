-- migrations/external_agents/001_external_agents_table.sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE external_agents (
    agent_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL,
    plugin_id TEXT NOT NULL,
    plugin_version TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('inbound','outbound','bidirectional')),
    framework_or_surface TEXT NOT NULL,
    framework_or_surface_version TEXT NOT NULL,
    mode TEXT NOT NULL,
    manifest_only BOOLEAN NOT NULL DEFAULT false,
    capabilities JSONB NOT NULL,
    actions JSONB,
    scope JSONB NOT NULL,
    policy JSONB NOT NULL,
    soulkey_kid TEXT,
    harness TEXT,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_health_check TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('active','paused','error','revoked')) DEFAULT 'active'
);

CREATE INDEX idx_external_agents_tenant_id ON external_agents (tenant_id);
CREATE INDEX idx_external_agents_plugin_id ON external_agents (plugin_id);
CREATE INDEX idx_external_agents_direction ON external_agents (direction);
CREATE UNIQUE INDEX uq_external_agents_tenant_agent ON external_agents (tenant_id, agent_id) WHERE status <> 'revoked';
