CREATE TABLE strategy_performance_observations (
    observation_key TEXT PRIMARY KEY,
    logical_decision_key TEXT NOT NULL,
    decision_semantic_sha256 TEXT NOT NULL CHECK (length(decision_semantic_sha256) = 64),
    schema_version TEXT NOT NULL CHECK (schema_version = 'PERFORMANCE_OBSERVATION_V1'),
    source_layer TEXT NOT NULL CHECK (source_layer IN ('HISTORICAL', 'FORWARD')),
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    family TEXT NOT NULL,
    registry_index INTEGER NOT NULL CHECK (registry_index >= 0),
    activation_status TEXT NOT NULL,
    activation_reason_code TEXT NOT NULL,
    market_id TEXT NOT NULL,
    market_date TEXT NOT NULL,
    evaluation_key TEXT,
    signal_identity_key TEXT UNIQUE,
    parent_strategy_id TEXT,
    source_decision_identity TEXT,
    checkpoint_minutes INTEGER NOT NULL CHECK (checkpoint_minutes > 0),
    horizon TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('YES', 'NO')),
    selected_buckets_json TEXT NOT NULL,
    selected_bucket_identity_sha256 TEXT NOT NULL CHECK (length(selected_bucket_identity_sha256) = 64),
    accepted INTEGER NOT NULL CHECK (accepted IN (0, 1)),
    emitted INTEGER NOT NULL CHECK (emitted IN (0, 1)),
    reason_code TEXT NOT NULL,
    reference_price_micros INTEGER CHECK (
        reference_price_micros IS NULL OR reference_price_micros BETWEEN 0 AND 1000000
    ),
    performance_price_micros INTEGER CHECK (
        performance_price_micros IS NULL OR performance_price_micros BETWEEN 0 AND 1000000
    ),
    performance_price_basis TEXT NOT NULL CHECK (performance_price_basis IN (
        'CONTRACT_STRESSED_REFERENCE_EXPLICIT',
        'CONTRACT_STRESSED_REFERENCE_RECONSTRUCTED',
        'CONTRACT_V2_STRESSED_Q_3C',
        'FORWARD_CAPTURED_CONTRACT',
        'NOT_APPLICABLE',
        'UNAVAILABLE'
    )),
    shares_micros INTEGER NOT NULL CHECK (shares_micros IN (0, 5000000)),
    scoring_status TEXT NOT NULL CHECK (
        scoring_status IN ('REJECTED', 'RESOLUTION_PENDING', 'UNSCORABLE')
    ),
    scoring_reason_code TEXT NOT NULL,
    observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms >= 0),
    source_created_at_ms INTEGER CHECK (source_created_at_ms IS NULL OR source_created_at_ms >= 0),
    provenance_run_id TEXT NOT NULL,
    source_result_sha256 TEXT NOT NULL CHECK (length(source_result_sha256) = 64),
    input_sha256 TEXT NOT NULL CHECK (length(input_sha256) = 64),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    UNIQUE (source_layer, logical_decision_key),
    CHECK (
        (accepted = 1 AND shares_micros = 5000000 AND scoring_status <> 'REJECTED')
        OR
        (accepted = 0 AND emitted = 0 AND signal_identity_key IS NULL
         AND shares_micros = 0 AND performance_price_micros IS NULL
         AND performance_price_basis = 'NOT_APPLICABLE'
         AND scoring_status = 'REJECTED')
    ),
    CHECK ((emitted = 1 AND accepted = 1 AND source_layer = 'FORWARD'
            AND signal_identity_key IS NOT NULL)
        OR (emitted = 0 AND signal_identity_key IS NULL)),
    CHECK (scoring_status <> 'RESOLUTION_PENDING' OR (
        performance_price_micros > 0
        AND performance_price_basis NOT IN ('NOT_APPLICABLE', 'UNAVAILABLE')
    )),
    FOREIGN KEY (signal_identity_key) REFERENCES signals(identity_key)
);

CREATE TABLE strategy_performance_resolutions (
    resolution_key TEXT PRIMARY KEY,
    observation_key TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    supersedes_resolution_key TEXT UNIQUE,
    settlement_identity TEXT NOT NULL,
    settlement_source_event_id INTEGER,
    winning_bucket_identity TEXT NOT NULL,
    won INTEGER NOT NULL CHECK (won IN (0, 1)),
    shares_micros INTEGER NOT NULL CHECK (shares_micros = 5000000),
    cost_usd_micros INTEGER NOT NULL CHECK (cost_usd_micros > 0),
    gross_payout_usd_micros INTEGER NOT NULL CHECK (
        gross_payout_usd_micros IN (0, 5000000)
    ),
    pnl_usd_micros INTEGER NOT NULL,
    turnover_usd_micros INTEGER NOT NULL CHECK (turnover_usd_micros > 0),
    resolution_date TEXT NOT NULL,
    resolved_at_ms INTEGER CHECK (resolved_at_ms IS NULL OR resolved_at_ms >= 0),
    provenance_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    UNIQUE (observation_key, revision),
    CHECK ((revision = 1 AND supersedes_resolution_key IS NULL)
        OR (revision > 1 AND supersedes_resolution_key IS NOT NULL)),
    CHECK (gross_payout_usd_micros = CASE WHEN won = 1 THEN 5000000 ELSE 0 END),
    CHECK (turnover_usd_micros = cost_usd_micros),
    CHECK (pnl_usd_micros = gross_payout_usd_micros - cost_usd_micros),
    FOREIGN KEY (observation_key) REFERENCES strategy_performance_observations(observation_key),
    FOREIGN KEY (supersedes_resolution_key) REFERENCES strategy_performance_resolutions(resolution_key),
    FOREIGN KEY (settlement_source_event_id) REFERENCES source_events(event_id)
);

CREATE TABLE strategy_performance_ingest_runs (
    ingest_run_key TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL CHECK (schema_version = 'PERFORMANCE_INGEST_RUN_V1'),
    mode TEXT NOT NULL CHECK (mode IN ('HISTORICAL_BOOTSTRAP', 'FORWARD_INCREMENTAL')),
    source_artifact_class TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64),
    acceptance_sha256 TEXT CHECK (acceptance_sha256 IS NULL OR length(acceptance_sha256) = 64),
    source_schema_version TEXT NOT NULL,
    input_count INTEGER NOT NULL CHECK (input_count >= 0),
    inserted_count INTEGER NOT NULL CHECK (inserted_count >= 0),
    replayed_count INTEGER NOT NULL CHECK (replayed_count >= 0),
    rejected_count INTEGER NOT NULL CHECK (rejected_count >= 0),
    accepted_count INTEGER NOT NULL CHECK (accepted_count >= 0),
    unscorable_count INTEGER NOT NULL CHECK (unscorable_count >= 0),
    conflict_count INTEGER NOT NULL CHECK (conflict_count >= 0),
    first_source_identity TEXT,
    last_source_identity TEXT,
    first_cursor_json TEXT,
    last_cursor_json TEXT,
    status TEXT NOT NULL CHECK (status IN ('STARTED', 'COMPLETE', 'FAILED')),
    started_at_ms INTEGER NOT NULL CHECK (started_at_ms >= 0),
    completed_at_ms INTEGER CHECK (completed_at_ms IS NULL OR completed_at_ms >= started_at_ms),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    CHECK (accepted_count + rejected_count = input_count),
    CHECK (unscorable_count <= accepted_count),
    CHECK ((status = 'COMPLETE' AND completed_at_ms IS NOT NULL)
        OR (status <> 'COMPLETE' AND completed_at_ms IS NULL))
);

CREATE TABLE strategy_performance_cursors (
    cursor_name TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL CHECK (schema_version = 'PERFORMANCE_CURSOR_V1'),
    cursor_json TEXT NOT NULL,
    cursor_sha256 TEXT NOT NULL CHECK (length(cursor_sha256) = 64),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0)
);

CREATE TABLE strategy_performance_catchup (
    catchup_key TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL CHECK (schema_version = 'PERFORMANCE_CATCHUP_V1'),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    supersedes_catchup_key TEXT UNIQUE,
    market_date TEXT NOT NULL,
    classification TEXT NOT NULL CHECK (
        classification IN ('RESOLVED', 'PENDING', 'EXPECTED_ABSENT', 'DATA_GAP')
    ),
    reason_code TEXT NOT NULL,
    market_id TEXT,
    source_event_identity TEXT,
    classified_at_ms INTEGER NOT NULL CHECK (classified_at_ms >= 0),
    provenance_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    UNIQUE (market_date, revision),
    CHECK ((revision = 1 AND supersedes_catchup_key IS NULL)
        OR (revision > 1 AND supersedes_catchup_key IS NOT NULL)),
    FOREIGN KEY (supersedes_catchup_key)
        REFERENCES strategy_performance_catchup(catchup_key)
);

CREATE TABLE strategy_performance_materialization_revisions (
    revision_key TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL CHECK (
        schema_version = 'PERFORMANCE_MATERIALIZATION_REVISION_V1'
    ),
    strategy_id TEXT NOT NULL,
    source_view TEXT NOT NULL CHECK (source_view IN ('HISTORICAL', 'FORWARD', 'COMBINED')),
    calculation_version TEXT NOT NULL,
    source_ledger_revision INTEGER NOT NULL CHECK (source_ledger_revision >= 0),
    source_ledger_sha256 TEXT NOT NULL CHECK (length(source_ledger_sha256) = 64),
    generated_at_ms INTEGER NOT NULL CHECK (generated_at_ms >= 0),
    aggregate_count INTEGER NOT NULL CHECK (aggregate_count > 0),
    timeseries_count INTEGER NOT NULL CHECK (timeseries_count >= 0),
    children_sha256 TEXT NOT NULL CHECK (length(children_sha256) = 64),
    status TEXT NOT NULL CHECK (status = 'COMPLETE'),
    is_current INTEGER NOT NULL CHECK (is_current IN (0, 1)),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    UNIQUE (strategy_id, source_view, calculation_version, source_ledger_revision)
);

CREATE UNIQUE INDEX strategy_performance_one_current_revision_idx
    ON strategy_performance_materialization_revisions(
        strategy_id, source_view, calculation_version
    ) WHERE is_current = 1;

CREATE TABLE strategy_performance_aggregates (
    aggregate_key TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL CHECK (schema_version = 'PERFORMANCE_AGGREGATE_V1'),
    revision_key TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    source_view TEXT NOT NULL CHECK (source_view IN ('HISTORICAL', 'FORWARD', 'COMBINED')),
    window_kind TEXT NOT NULL,
    window_key TEXT NOT NULL,
    calculation_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    UNIQUE (revision_key, window_kind, window_key),
    FOREIGN KEY (revision_key) REFERENCES strategy_performance_materialization_revisions(revision_key)
);

CREATE TABLE strategy_performance_timeseries (
    timeseries_key TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL CHECK (schema_version = 'PERFORMANCE_TIMESERIES_V1'),
    revision_key TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    source_view TEXT NOT NULL CHECK (source_view IN ('HISTORICAL', 'FORWARD', 'COMBINED')),
    series_kind TEXT NOT NULL CHECK (series_kind IN (
        'CUMULATIVE', 'MONTHLY', 'ROLLING_30D', 'ROLLING_90D', 'ROLLING_365D'
    )),
    period_key TEXT NOT NULL,
    calculation_version TEXT NOT NULL,
    observation_count INTEGER NOT NULL CHECK (observation_count >= 0),
    observation_sha256 TEXT NOT NULL CHECK (length(observation_sha256) = 64),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    UNIQUE (revision_key, series_kind, period_key),
    FOREIGN KEY (revision_key) REFERENCES strategy_performance_materialization_revisions(revision_key)
);

CREATE INDEX strategy_performance_observations_strategy_view_idx
    ON strategy_performance_observations(strategy_id, source_layer, market_date, checkpoint_minutes);
CREATE INDEX strategy_performance_observations_logical_idx
    ON strategy_performance_observations(logical_decision_key, source_layer);
CREATE INDEX strategy_performance_resolutions_observation_idx
    ON strategy_performance_resolutions(observation_key, revision DESC);
CREATE INDEX strategy_performance_catchup_date_idx
    ON strategy_performance_catchup(market_date, classification);
CREATE INDEX strategy_performance_aggregates_revision_idx
    ON strategy_performance_aggregates(revision_key, window_kind, window_key);
CREATE INDEX strategy_performance_timeseries_revision_idx
    ON strategy_performance_timeseries(revision_key, series_kind, period_key);
