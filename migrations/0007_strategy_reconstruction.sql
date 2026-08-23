CREATE TABLE strategy_reconstruction_status (
    reconstruction_key TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL CHECK (
        schema_version = 'STRATEGY_RECONSTRUCTION_STATUS_V1'
    ),
    market_date TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('RECOVERED', 'EXPECTED_ABSENT', 'DATA_GAP')
    ),
    reason_code TEXT NOT NULL,
    missing_input TEXT,
    checkpoint_minutes_json TEXT NOT NULL,
    observation_count INTEGER NOT NULL CHECK (observation_count >= 0),
    evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256) = 64),
    input_sha256 TEXT NOT NULL CHECK (length(input_sha256) = 64),
    reconstructed_at_ms INTEGER NOT NULL CHECK (reconstructed_at_ms >= 0),
    provenance_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    UNIQUE (market_date, strategy_id),
    CHECK ((status = 'RECOVERED' AND observation_count > 0 AND missing_input IS NULL)
        OR (status = 'EXPECTED_ABSENT' AND observation_count = 0 AND missing_input IS NULL)
        OR (status = 'DATA_GAP' AND observation_count = 0 AND missing_input IS NOT NULL))
);

CREATE INDEX strategy_reconstruction_status_date_idx
    ON strategy_reconstruction_status(market_date, status, strategy_id);
