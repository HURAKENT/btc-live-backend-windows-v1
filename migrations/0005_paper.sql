CREATE TABLE paper_execution_readiness (
    readiness_key TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL CHECK (schema_version = 'PAPER_READINESS_V1'),
    market_id TEXT NOT NULL,
    market_date TEXT NOT NULL,
    checkpoint_minutes INTEGER NOT NULL CHECK (checkpoint_minutes IN (30, 60)),
    signal_key TEXT,
    evidence_key TEXT,
    ready INTEGER NOT NULL CHECK (ready IN (0, 1)),
    reason_code TEXT NOT NULL CHECK (reason_code IN (
        'READY', 'NO_CURRENT_MARKET', 'SOURCE_NOT_LIVE',
        'MISSING_181_CLOSED_CANDLES', 'MODEL_INPUT_INVALID',
        'INCOMPLETE_MARKET_SET', 'MISSING_CURRENT_BOOK',
        'STALE_CURRENT_BOOK', 'INSUFFICIENT_FIVE_SHARE_DEPTH',
        'MISSING_FEE_PROVENANCE', 'NO_STRICT_A_SIGNAL',
        'T30_BLOCKED_EXISTING_LOGICAL_EXECUTION',
        'DUPLICATE_LOGICAL_EXECUTION', 'RECOVERED_EXECUTION_FORBIDDEN',
        'INSUFFICIENT_PAPER_CASH', 'SETTLEMENT_PENDING'
    )),
    requested_shares_micros INTEGER NOT NULL CHECK (
        requested_shares_micros = 5000000
    ),
    checked_at_ms INTEGER NOT NULL CHECK (checked_at_ms >= 0),
    CHECK ((ready = 1 AND reason_code = 'READY') OR
           (ready = 0 AND reason_code <> 'READY'))
);

CREATE TABLE paper_intents (
    intent_key TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL CHECK (schema_version = 'PAPER_INTENT_V1'),
    signal_key TEXT NOT NULL,
    evidence_key TEXT NOT NULL,
    market_id TEXT NOT NULL,
    market_date TEXT NOT NULL UNIQUE,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side = 'YES'),
    checkpoint_minutes INTEGER NOT NULL CHECK (checkpoint_minutes IN (30, 60)),
    requested_shares_micros INTEGER NOT NULL CHECK (
        requested_shares_micros = 5000000
    ),
    status TEXT NOT NULL CHECK (
        status IN ('PENDING', 'PARTIAL', 'FILLED', 'BLOCKED', 'SETTLED')
    ),
    reason_code TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
    CHECK (intent_key = 'strict-a:' || market_date)
);

CREATE TABLE paper_fills (
    fill_key TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL CHECK (schema_version = 'PAPER_FILL_V1'),
    intent_key TEXT NOT NULL,
    fill_sequence INTEGER NOT NULL CHECK (fill_sequence > 0),
    shares_micros INTEGER NOT NULL CHECK (
        shares_micros > 0 AND shares_micros <= 5000000
    ),
    price_micros INTEGER NOT NULL CHECK (price_micros BETWEEN 0 AND 1000000),
    fee_usd_micros INTEGER NOT NULL CHECK (fee_usd_micros >= 0),
    gross_cost_usd_micros INTEGER NOT NULL CHECK (gross_cost_usd_micros >= 0),
    evidence_key TEXT NOT NULL,
    book_sha256 TEXT NOT NULL,
    filled_at_ms INTEGER NOT NULL CHECK (filled_at_ms >= 0),
    UNIQUE (intent_key, fill_sequence),
    CHECK (fill_key = intent_key || ':' || fill_sequence),
    FOREIGN KEY (intent_key) REFERENCES paper_intents(intent_key)
);

CREATE TABLE paper_positions (
    position_key TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL CHECK (schema_version = 'PAPER_POSITION_V1'),
    intent_key TEXT NOT NULL UNIQUE,
    market_id TEXT NOT NULL,
    market_date TEXT NOT NULL UNIQUE,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side = 'YES'),
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'PARTIAL', 'SETTLED')),
    filled_shares_micros INTEGER NOT NULL CHECK (
        filled_shares_micros > 0 AND filled_shares_micros <= 5000000
    ),
    average_price_micros INTEGER NOT NULL CHECK (
        average_price_micros BETWEEN 0 AND 1000000
    ),
    cost_basis_usd_micros INTEGER NOT NULL CHECK (cost_basis_usd_micros >= 0),
    fees_paid_usd_micros INTEGER NOT NULL CHECK (fees_paid_usd_micros >= 0),
    settlement_value_usd_micros INTEGER CHECK (
        settlement_value_usd_micros IS NULL OR settlement_value_usd_micros >= 0
    ),
    realized_pnl_usd_micros INTEGER NOT NULL,
    unrealized_pnl_usd_micros INTEGER NOT NULL,
    opened_at_ms INTEGER NOT NULL CHECK (opened_at_ms >= 0),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= opened_at_ms),
    settled_at_ms INTEGER CHECK (settled_at_ms IS NULL OR settled_at_ms >= opened_at_ms),
    CHECK (position_key = 'strict-a:' || market_date),
    CHECK ((status = 'SETTLED' AND settlement_value_usd_micros IS NOT NULL
            AND settled_at_ms IS NOT NULL AND unrealized_pnl_usd_micros = 0)
        OR (status <> 'SETTLED' AND settlement_value_usd_micros IS NULL
            AND settled_at_ms IS NULL AND realized_pnl_usd_micros = 0)),
    FOREIGN KEY (intent_key) REFERENCES paper_intents(intent_key)
);

CREATE TABLE paper_accounts (
    account_key TEXT PRIMARY KEY CHECK (account_key = 'default'),
    schema_version TEXT NOT NULL CHECK (schema_version = 'PAPER_ACCOUNT_V1'),
    starting_bankroll_usd_micros INTEGER NOT NULL CHECK (
        starting_bankroll_usd_micros = 1000000000
    ),
    cash_usd_micros INTEGER NOT NULL CHECK (cash_usd_micros >= 0),
    open_cost_basis_usd_micros INTEGER NOT NULL CHECK (
        open_cost_basis_usd_micros >= 0
    ),
    equity_usd_micros INTEGER NOT NULL CHECK (equity_usd_micros >= 0),
    realized_pnl_usd_micros INTEGER NOT NULL,
    unrealized_pnl_usd_micros INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0)
);

CREATE INDEX paper_readiness_market_date_idx
    ON paper_execution_readiness(market_date, checkpoint_minutes);
CREATE INDEX paper_fills_intent_idx ON paper_fills(intent_key, fill_sequence);
CREATE INDEX paper_positions_status_date_idx ON paper_positions(status, market_date);
