CREATE TABLE IF NOT EXISTS receipts (
    id uuid PRIMARY KEY,
    tenant text NOT NULL,
    idempotency_key text,
    request_hash text NOT NULL,
    status text NOT NULL CHECK (status IN ('pending', 'completed', 'failed', 'rejected', 'unknown')),
    decision jsonb NOT NULL,
    outcome jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant, idempotency_key)
);
CREATE INDEX IF NOT EXISTS receipts_tenant_created_idx ON receipts (tenant, created_at DESC);

CREATE TABLE IF NOT EXISTS attempts (
    id bigserial PRIMARY KEY,
    receipt_id uuid NOT NULL REFERENCES receipts(id),
    target_id text NOT NULL,
    status text NOT NULL,
    elapsed_ms integer NOT NULL CHECK (elapsed_ms >= 0),
    http_status integer,
    observation jsonb NOT NULL,
    error_code text
);
CREATE INDEX IF NOT EXISTS attempts_receipt_idx ON attempts (receipt_id, id);
