CREATE TABLE IF NOT EXISTS task_events (
    id bigserial PRIMARY KEY,
    tenant text NOT NULL,
    task_id text NOT NULL,
    event_id text NOT NULL,
    event jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant, task_id, event_id)
);
CREATE INDEX IF NOT EXISTS task_events_tenant_task_idx
    ON task_events (tenant, task_id, id);
