# MVP Interface Freeze

Interface version: `BTC_DAILY_RANGE_MVP_V1`.

This is the shared Worker/Manager boundary. Workers may implement task-local
types with these exact wire fields, but only the Manager may add shared models,
storage wiring, runtime orchestration, REST fields, or outbox topics. A field or
semantic change requires a new interface version and Manager decision.

Canonical anchors: `src/strategy_dispatch.py`, `src/strategy_v1.py`,
`src/models.py`, `src/fixed_point.py`, `src/polymarket_provider.py`,
`src/storage.py`, `src/api.py`, `migrations/0001_core.sql` through
`migrations/0004_data_completion.sql`,
`strategy_sources/frozen/contracts/STRICT_A_STRATEGY_FROZEN.json`, and
`strategy_sources/frozen/contracts/STRICT_A_MODEL_FROZEN.json`.

## Common rules

- JSON field names are `snake_case`; timestamps are nonnegative UTC Unix epoch
  milliseconds (`*_at_ms` or `*_timestamp_ms`).
- Probability/price is integer `micros` in `[0, 1_000_000]`; shares are
  nonnegative integer `shares_micros`; USD is nonnegative integer `usd_micros`.
- Five shares = `5_000_000 shares_micros`; default bankroll USD 1000 =
  `1_000_000_000 usd_micros`.
- Hashes are lowercase SHA-256 hex of canonical JSON/bytes. Keys are nonempty
  strings and unique at their named persistence boundary.
- `origin` is `CURRENT_LIVE_REEVALUATION` for executable MVP inputs/signals.
  `RECOVERED_AFTER_DOWNTIME` can be evaluated but must never create an intent.
- Unknown, missing, stale, conflicting, or non-finite input fails closed with a
  reason code and zero new fill.

## Existing canonical types to reuse

- `V1ExecutableCheckpointInput`: evaluator input; exactly 11 `BucketInput`
  rows, checkpoint 60 or 30, prior-position flag, Strict price-history evidence,
  and no PF1 evidence for Strict A.
- `V1Evaluation`: canonical Strict A evaluator output.
- `SignalRecord`: persisted signal identity; its canonical payload carries
  `StrictASignalV1`.
- `MarketBook`: current bids/asks in micros and current provider book hash.
- `ProbabilityMicros`, `SharesMicros`, `UsdMicros`: storage arithmetic types for
  nonnegative prices, shares, balances, and costs; signed PnL remains an integer
  count of USD micros.
- Existing outbox WS envelope: `event_id`, `topic`, `event_type`, `payload`,
  `created_at_ms`; replay remains strictly after `after_event_id`.

## `CurrentExecutionEvidenceV1`

Immutable contemporaneous evidence produced by Worker A.

| Field | Type / unit |
|---|---|
| `schema_version` | literal `CURRENT_EXECUTION_EVIDENCE_V1` |
| `evidence_key` | unique string: SHA-256 of the canonical evidence excluding this field |
| `market_id`, `market_date`, `token_id` | string; `market_date` is `YYYY-MM-DD` in the canonical market identity |
| `checkpoint_minutes` | integer `60` or `30` |
| `candle_first_open_time_ms`, `candle_last_open_time_ms` | epoch ms |
| `closed_candle_count` | literal integer `181` |
| `candles_sha256`, `model_bundle_sha256`, `book_sha256`, `fee_provenance_sha256` | SHA-256 hex |
| `bucket_index` | integer `0..10` |
| `model_probability_micros`, `market_q_micros`, `vwap5_micros` | probability/price micros |
| `available_depth_shares_micros` | shares micros; must be at least `5_000_000` for full fill readiness |
| `fee_per_share_usd_micros` | USD micros per share |
| `book_source_timestamp_ms`, `observed_at_ms` | epoch ms; freshness is checked at capture |
| `price_history_source_sha256` | source hash used by canonical `StrictPriceHistoryEvidence` |

The same capture provides the existing `V1ExecutableCheckpointInput`. It uses
the frozen Strict A model, 11 current bucket rows, `prior_position` derived from
logical intent/position state, and `pf1_snapshot_evidence=None`.

## `StrictASignalV1`

Canonical payload inside an existing `SignalRecord`.

| Field | Type / unit |
|---|---|
| `schema_version` | literal `STRICT_A_SIGNAL_V1` |
| `signal_key`, `evaluation_key` | unique strings; `signal_key` is the `SignalRecord.identity_key` |
| `strategy_id` | literal `YES_STRICT_A_OPERATIONAL` |
| `market_id`, `market_date` | string |
| `checkpoint_minutes` | integer `60` or `30` |
| `side` | literal `YES` |
| `bucket_index`, `token_id` | integer `0..10`, nonempty string |
| `model_probability_micros`, `market_q_micros`, `vwap5_micros` | probability/price micros |
| `fee_per_share_usd_micros` | USD micros per share |
| `evidence_key`, `input_snapshot_hash` | nonempty string, SHA-256 hex |
| `execution_eligible` | boolean; true only when `V1Evaluation.accepted` and canonical execution eligibility are true |
| `reason_code` | canonical evaluator/block reason string |
| `origin`, `evaluated_at_ms` | origin enum, epoch ms |

T60 is primary. T30 may be executable only when no logical Strict A intent or
position exists for the same `market_date`. Maximum one logical Strict A paper
position per `market_date`.

## Paper contracts

`PaperIntentV1` fields: `schema_version=PAPER_INTENT_V1`, unique `intent_key`
(`strict-a:<market_date>`), `signal_key`, `evidence_key`, `market_id`,
`market_date`, `token_id`, `side=YES`, `checkpoint_minutes`,
`requested_shares_micros=5_000_000`, `status` (`PENDING`, `PARTIAL`, `FILLED`,
`BLOCKED`, `SETTLED`), `reason_code`, `created_at_ms`, `updated_at_ms`.

`PaperFillV1` fields: `schema_version=PAPER_FILL_V1`, unique `fill_key`
(`<intent_key>:<fill_sequence>`), `intent_key`, positive `fill_sequence`,
`shares_micros`, `price_micros`, `fee_usd_micros`, `gross_cost_usd_micros`,
`evidence_key`, `book_sha256`, `filled_at_ms`. A fill is local simulation from
the contemporaneous book only; cumulative shares cannot exceed the request.

`PaperPositionV1` fields: `schema_version=PAPER_POSITION_V1`, unique
`position_key` equal to `strict-a:<market_date>`, `intent_key`, `market_id`,
`market_date`, `token_id`, `side=YES`, `status` (`OPEN`, `PARTIAL`, `SETTLED`),
`filled_shares_micros`, `average_price_micros`, `cost_basis_usd_micros`,
`fees_paid_usd_micros`, `settlement_value_usd_micros` (nullable until settled),
`realized_pnl_usd_micros`, `unrealized_pnl_usd_micros`, `opened_at_ms`,
`updated_at_ms`, `settled_at_ms` (nullable).

`PaperAccountV1` fields: `schema_version=PAPER_ACCOUNT_V1`, unique
`account_key=default`, `starting_bankroll_usd_micros=1_000_000_000`,
`cash_usd_micros`, `open_cost_basis_usd_micros`, `equity_usd_micros`,
`realized_pnl_usd_micros`, `unrealized_pnl_usd_micros`, and `updated_at_ms`.
PnL values may be signed integers; all balances/costs remain nonnegative.
For a fill, `gross_cost_usd_micros = round_half_up(price_micros *
shares_micros / 1_000_000)` and `fee_usd_micros =
round_half_up(fee_per_share_usd_micros * shares_micros / 1_000_000)` using
integer arithmetic. Position cost basis is the sum of fill gross costs and
fees. On settlement, `realized_pnl_usd_micros = settlement_value_usd_micros -
cost_basis_usd_micros`; while open, unrealized PnL uses the latest current book
mark and the same integer rule. Account equity equals cash plus open marked
value.

`PaperExecutionReadinessV1` fields: `schema_version=PAPER_READINESS_V1`, unique
`readiness_key` (`strict-a:<market_date>:<checkpoint_minutes>`), `market_id`,
`market_date`, `checkpoint_minutes`, nullable `signal_key` and `evidence_key`,
`ready` boolean, `reason_code`, `requested_shares_micros=5_000_000`, and
`checked_at_ms`. `ready=false` never creates a fill.

Frozen readiness/block reasons are: `READY`, `NO_CURRENT_MARKET`,
`SOURCE_NOT_LIVE`, `MISSING_181_CLOSED_CANDLES`, `MODEL_INPUT_INVALID`,
`INCOMPLETE_MARKET_SET`, `MISSING_CURRENT_BOOK`, `STALE_CURRENT_BOOK`,
`INSUFFICIENT_FIVE_SHARE_DEPTH`, `MISSING_FEE_PROVENANCE`,
`NO_STRICT_A_SIGNAL`, `T30_BLOCKED_EXISTING_LOGICAL_EXECUTION`,
`DUPLICATE_LOGICAL_EXECUTION`, `RECOVERED_EXECUTION_FORBIDDEN`,
`INSUFFICIENT_PAPER_CASH`, and `SETTLEMENT_PENDING`.

## `DashboardBootstrapV1`

The existing `GET /api/v1/bootstrap` response is extended, not replaced. It
retains `current_market_identity`, `health`, `sources`, `signals`, `incidents`,
and `last_event_id`, and adds:

- `interface_version`: literal `BTC_DAILY_RANGE_MVP_V1`;
- `strict_a_signal`: latest `StrictASignalV1` or null;
- `execution_readiness`: latest `PaperExecutionReadinessV1` or null;
- `paper_account`: `PaperAccountV1`;
- `paper_positions`: array of `PaperPositionV1`;
- `paper_fills`: array of `PaperFillV1`.

Paper commits publish through the existing transactional outbox using topics
`paper.intent`, `paper.fill`, `paper.position`, `paper.account`, and
`paper.readiness`. Their payloads carry the corresponding frozen contract and
the existing resumable WS envelope supplies ordering and timestamps.
