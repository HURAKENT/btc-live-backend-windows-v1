CREATE TABLE strategy_checkpoint_schedules (
    schedule_id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_key TEXT NOT NULL UNIQUE,
    evaluation_key TEXT NOT NULL,
    market_id TEXT NOT NULL,
    market_identity_sha256 TEXT NOT NULL,
    resolution_ms INTEGER NOT NULL,
    registry_index INTEGER NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    rule_spec_sha256 TEXT NOT NULL,
    input_schema_version TEXT NOT NULL,
    activation_status TEXT NOT NULL,
    activation_source_commit TEXT NOT NULL,
    c5_acceptance_sha256 TEXT NOT NULL,
    c5_status_matrix_sha256 TEXT NOT NULL,
    checkpoint_minutes INTEGER NOT NULL,
    due_at_ms INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('PENDING', 'CLAIMED', 'CAPTURED', 'COMPLETED', 'BLOCKED')
    ),
    claim_owner TEXT,
    claimed_at_ms INTEGER,
    capture_origin TEXT CHECK (
        capture_origin IS NULL OR
        capture_origin IN ('LIVE', 'RECOVERED_AFTER_DOWNTIME')
    ),
    input_snapshot_hash TEXT,
    input_payload_json TEXT,
    historical_depth_available INTEGER CHECK (
        historical_depth_available IS NULL OR
        historical_depth_available IN (0, 1)
    ),
    evaluation_id INTEGER,
    current_reevaluation_required INTEGER NOT NULL DEFAULT 0 CHECK (
        current_reevaluation_required IN (0, 1)
    ),
    current_input_snapshot_hash TEXT,
    current_input_payload_json TEXT,
    current_claim_owner TEXT,
    current_claimed_at_ms INTEGER,
    current_reevaluation_evaluation_id INTEGER,
    blocked_reason TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    FOREIGN KEY (market_id) REFERENCES market_catalog(market_id),
    FOREIGN KEY (evaluation_id) REFERENCES strategy_evaluations(evaluation_id),
    FOREIGN KEY (current_reevaluation_evaluation_id)
        REFERENCES strategy_evaluations(evaluation_id),
    UNIQUE (market_id, strategy_id, checkpoint_minutes)
);

CREATE INDEX strategy_checkpoint_due_idx
    ON strategy_checkpoint_schedules(state, due_at_ms, registry_index);

CREATE INDEX strategy_checkpoint_market_strategy_idx
    ON strategy_checkpoint_schedules(market_id, strategy_id, checkpoint_minutes);

ALTER TABLE strategy_evaluations
    ADD COLUMN checkpoint_group_key TEXT;

ALTER TABLE strategy_evaluations
    ADD COLUMN rule_spec_sha256 TEXT;

ALTER TABLE strategy_evaluations
    ADD COLUMN origin TEXT NOT NULL DEFAULT 'LIVE' CHECK (
        origin IN ('LIVE', 'RECOVERED_AFTER_DOWNTIME')
    );

ALTER TABLE strategy_evaluations
    ADD COLUMN historical_signal_is_current_live_signal INTEGER NOT NULL
        DEFAULT 0 CHECK (historical_signal_is_current_live_signal IN (0, 1));

ALTER TABLE strategy_evaluations
    ADD COLUMN current_reevaluation_required INTEGER NOT NULL DEFAULT 0 CHECK (
        current_reevaluation_required IN (0, 1)
    );

CREATE UNIQUE INDEX strategy_evaluations_checkpoint_group_key_idx
    ON strategy_evaluations(checkpoint_group_key)
    WHERE checkpoint_group_key IS NOT NULL;

ALTER TABLE signals
    ADD COLUMN origin TEXT NOT NULL DEFAULT 'LIVE' CHECK (
        origin IN ('LIVE', 'RECOVERED_AFTER_DOWNTIME')
    );

ALTER TABLE signals
    ADD COLUMN execution_eligible INTEGER NOT NULL DEFAULT 0 CHECK (
        execution_eligible IN (0, 1)
    );

ALTER TABLE signals
    ADD COLUMN infrastructure_only INTEGER NOT NULL DEFAULT 0 CHECK (
        infrastructure_only IN (0, 1)
    );
