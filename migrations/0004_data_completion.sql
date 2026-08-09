CREATE TABLE data_import_runs (
    import_run_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_run_id TEXT NOT NULL UNIQUE,
    artifact_sha256 TEXT NOT NULL,
    source_path_fingerprint TEXT NOT NULL,
    schema_mapping_sha256 TEXT NOT NULL,
    dataset_start_ms INTEGER NOT NULL,
    dataset_end_ms INTEGER NOT NULL,
    declared_row_count INTEGER NOT NULL,
    inserted_row_count INTEGER NOT NULL,
    replayed_row_count INTEGER NOT NULL,
    conflict_row_count INTEGER NOT NULL CHECK (conflict_row_count = 0),
    dropped_row_count INTEGER NOT NULL CHECK (dropped_row_count = 0),
    status TEXT NOT NULL CHECK (status = 'COMPLETE'),
    created_at_ms INTEGER NOT NULL,
    CHECK (dataset_start_ms <= dataset_end_ms),
    CHECK (declared_row_count = inserted_row_count + replayed_row_count)
);

CREATE TABLE data_source_ranges (
    range_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    range_key TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    canonical_scope_json TEXT NOT NULL,
    canonical_scope_sha256 TEXT NOT NULL,
    requested_start_ms INTEGER NOT NULL,
    requested_end_ms INTEGER NOT NULL,
    granularity_ms INTEGER NOT NULL,
    contract_version TEXT NOT NULL,
    CHECK (requested_start_ms < requested_end_ms),
    CHECK (granularity_ms > 0)
);

CREATE TABLE data_source_range_assessments (
    assessment_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_key TEXT NOT NULL UNIQUE,
    range_key TEXT NOT NULL,
    import_run_id TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'COMPLETE','EXPECTED_ABSENT','SOURCE_UNAVAILABLE_RETRYABLE',
        'SOURCE_CONFLICT_FATAL','NOT_REQUIRED_BY_CONTRACT'
    )),
    expected_row_count INTEGER NOT NULL,
    observed_row_count INTEGER NOT NULL,
    missing_row_count INTEGER NOT NULL,
    reason_code TEXT,
    evidence_metadata_json TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    CHECK (expected_row_count >= 0),
    CHECK (observed_row_count >= 0),
    CHECK (missing_row_count >= 0),
    FOREIGN KEY (range_key) REFERENCES data_source_ranges(range_key),
    FOREIGN KEY (import_run_id) REFERENCES data_import_runs(import_run_id)
);

CREATE TABLE data_import_run_events (
    import_run_id TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('INSERTED','REPLAYED')),
    PRIMARY KEY (import_run_id,event_id),
    FOREIGN KEY (import_run_id) REFERENCES data_import_runs(import_run_id),
    FOREIGN KEY (event_id) REFERENCES source_events(event_id)
);

CREATE INDEX data_source_ranges_source_time_idx
    ON data_source_ranges(source, requested_start_ms, requested_end_ms);
CREATE INDEX data_source_range_assessments_range_idx
    ON data_source_range_assessments(range_key, assessment_row_id);
