CREATE TABLE source_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    natural_key TEXT NOT NULL UNIQUE,
    source_timestamp_ms INTEGER NOT NULL,
    received_timestamp_ms INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    recovery_origin TEXT NOT NULL,
    committed_at_ms INTEGER NOT NULL
);

CREATE TABLE source_cursors (
    source TEXT PRIMARY KEY,
    cursor_json TEXT NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE market_catalog (
    market_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE canonical_state (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_key TEXT NOT NULL UNIQUE,
    source_event_ids_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    recovery_origin TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE strategy_evaluations (
    evaluation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_key TEXT NOT NULL UNIQUE,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    status TEXT NOT NULL,
    input_snapshot_hash TEXT NOT NULL,
    evaluation_revision INTEGER NOT NULL,
    execution_eligible INTEGER NOT NULL CHECK (execution_eligible IN (0, 1)),
    evaluated_at_ms INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE signals (
    signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_key TEXT NOT NULL UNIQUE,
    evaluation_key TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE outbox_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL UNIQUE,
    topic TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    FOREIGN KEY (signal_id) REFERENCES signals(signal_id)
);

CREATE TABLE incidents (
    incident_id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_key TEXT NOT NULL UNIQUE,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);
