-- UUIDv7 TEXT identifiers throughout. Timestamps stored as UTC ISO-8601 TEXT.

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    policy_snapshot TEXT NOT NULL,
    stage TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_files (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    filename TEXT NOT NULL,
    source_type TEXT NOT NULL,
    row_count INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_columns (
    id TEXT PRIMARY KEY,
    source_file_id TEXT NOT NULL REFERENCES source_files(id),
    name TEXT NOT NULL,
    inferred_type TEXT,
    null_rate REAL,
    distinct_count INTEGER,
    sample_values TEXT
);

CREATE TABLE IF NOT EXISTS mappings (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    source_column_id TEXT NOT NULL REFERENCES source_columns(id),
    target_field TEXT,
    confidence REAL,
    rationale TEXT,
    alternatives TEXT,
    status TEXT NOT NULL DEFAULT 'proposed',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    source_file_id TEXT NOT NULL REFERENCES source_files(id),
    natural_key TEXT,
    data TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    blocked_on TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merges (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    survivor_record_id TEXT NOT NULL REFERENCES records(id),
    absorbed_record_id TEXT NOT NULL REFERENCES records(id),
    score REAL,
    field_survivorship TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS escalations (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    reason_code TEXT NOT NULL,
    scope TEXT NOT NULL,
    signature TEXT NOT NULL,
    entity_id TEXT,
    question TEXT NOT NULL,
    suggested_value TEXT,
    options TEXT,
    evidence TEXT,
    affected_count INTEGER NOT NULL DEFAULT 0,
    suggested_action TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    reason_code TEXT NOT NULL,
    signature TEXT NOT NULL,
    resolution TEXT NOT NULL,
    resolved_by TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (reason_code, signature)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    ts TEXT NOT NULL,
    actor TEXT NOT NULL CHECK (actor IN ('agent', 'llm', 'human', 'system')),
    stage TEXT,
    reason_code TEXT,
    entity_type TEXT,
    entity_id TEXT,
    field TEXT,
    before TEXT,
    after TEXT,
    confidence REAL,
    rule_id TEXT,
    model TEXT,
    prompt_hash TEXT,
    latency_ms INTEGER,
    cost_usd REAL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS llm_cache (
    id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    response TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (model, prompt_hash)
);

CREATE TABLE IF NOT EXISTS push_attempts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    record_id TEXT NOT NULL REFERENCES records(id),
    attempt_number INTEGER NOT NULL,
    status_code INTEGER,
    idempotency_key TEXT,
    outcome TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- target_employees lives in the external target process
-- (tools/mock_target_api.py), which owns its own store.

-- Append-only progress log, one row per thing the agent did. This is the source
-- of truth for the live view rather than a side channel: `seq` is monotonic, so
-- a browser that reconnects replays from its Last-Event-ID and misses nothing,
-- and a browser opening the page fresh replays the whole run from the start.
CREATE TABLE IF NOT EXISTS run_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    stage TEXT,
    message TEXT NOT NULL,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_run_events_run_seq ON run_events(run_id, seq);

-- What this agent last successfully pushed for each employee, so a re-import
-- can tell new/changed/unchanged apart by natural key and push only the delta.
CREATE TABLE IF NOT EXISTS pushed_state (
    employee_id TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    run_id TEXT NOT NULL,
    pushed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_records_run ON records(run_id);
CREATE INDEX IF NOT EXISTS idx_records_natural_key ON records(natural_key);
CREATE INDEX IF NOT EXISTS idx_escalations_run_status ON escalations(run_id, status);
CREATE INDEX IF NOT EXISTS idx_audit_events_run ON audit_events(run_id);
CREATE INDEX IF NOT EXISTS idx_push_attempts_run ON push_attempts(run_id);
