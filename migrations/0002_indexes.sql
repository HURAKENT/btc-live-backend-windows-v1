CREATE INDEX source_events_source_timestamp_idx
    ON source_events(source, source_timestamp_ms);

CREATE INDEX strategy_evaluations_strategy_idx
    ON strategy_evaluations(strategy_id, evaluated_at_ms);

CREATE INDEX signals_created_idx
    ON signals(created_at_ms);

CREATE INDEX outbox_events_created_idx
    ON outbox_events(created_at_ms);

CREATE INDEX incidents_status_idx
    ON incidents(status, created_at_ms);
