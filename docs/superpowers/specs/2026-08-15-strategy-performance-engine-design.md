# Strategy Performance Engine Design

**Date:** 2026-08-15
**Status:** Design complete; awaiting user review
**Implementation status:** Not started
**Scope:** Autonomous strategy-level performance accounting and query contracts

## 1. Context and authority

The successful autonomous AHR run
`reports/historical_revalidation/20260814T205244105513Z/` is the historical
engineering baseline for this stage. Its acceptance receipt reports `PASS`,
170/170 dates, 47/47 strategies, four strategy families, 4,888/4,888 completed
evaluation units, 5,063 semantic parity comparisons, zero input or dispatch
failures, zero lookahead violations, zero network requests, and no production
database or scheduler modification.

The historical bootstrap input is:

- `STRATEGY_RESULTS.parquet` from run `20260814T205244105513Z`;
- file SHA-256
  `26fad48c7d705bc4954fe65dfaefa7b3b87e1af77a7e94dfffe79c383aa38d84`;
- 4,994 result records: 4,300 V1 and 694 V2;
- 1,850 accepted and 3,144 rejected result records;
- canonical settlements with SHA-256
  `ea9ed11c1aa7a975c499bcd27332fdbfa0dd927cf2ef4323e89b372516c4e8e1`.

The difference between 4,888 evaluation units and 4,994 result records is
intentional evidence that an AHR unit, a decision result, an accepted signal,
and a resolved performance observation are different concepts. The Performance
Engine must preserve those distinctions.

Frozen Registry47 aggregate metrics are not primary input. They may be used
only as secondary verification evidence after new observations and aggregates
have been calculated from current-system results.

## 2. Goal

Build an autonomous, deterministic Strategy Performance Engine that:

1. bootstraps historical observations from the successful current-system AHR
   replay;
2. observes new persisted forward evaluations and signals;
3. attaches canonical market resolutions when they become available;
4. computes strategy-level economics at the canonical five-share unit;
5. rebuilds historical, forward, and combined aggregates from raw facts;
6. exposes computed data through a backend query contract;
7. requires no ChatGPT or Codex involvement during normal calculation,
   reconciliation, refresh, or reads.

This is analytics infrastructure. It does not change strategy evaluation,
strategy eligibility, execution, scheduling, provider recovery, or market
acquisition.

## 3. Existing conventions and architectural decision

The current project already provides:

- numbered SQLite migrations under `migrations/`;
- one WAL-mode `SqliteStore` writer with explicit transactions;
- immutable natural keys, canonical JSON, SHA-256 provenance, idempotent replay,
  and conflict-on-different-payload behavior;
- persisted `strategy_evaluations` and `signals` with unique evaluation and
  signal identities;
- captured checkpoint inputs in `strategy_checkpoint_schedules`;
- append-only source evidence in `source_events`;
- query-only API access through `SqliteReadStore`;
- idempotent settlement/conflict behavior in the separate paper ledger that can
  inform, but must not be reused as strategy-performance truth.

Three storage approaches were considered:

1. **A separate analytics SQLite database.** It isolates writes but creates a
   second migration, backup, lifecycle, consistency, and writer boundary.
2. **Compute every metric on API reads.** It avoids caches but makes expensive
   rolling series and rebuild verification part of request handling and leaves
   no durable autonomous aggregate revision.
3. **An auditable performance ledger plus rebuildable materializations in the
   existing SQLite database.** This follows current migrations, transaction,
   conflict, backup, and read-store patterns.

The design selects option 3. All writes flow through the existing single-writer
boundary. API and dashboard paths remain read-only. Aggregate and timeseries
tables are caches, never primary truth.

## 4. Invariants

- Primary truth is event-level or signal-level evidence, not stored WR/ROI/PnL
  totals.
- `HISTORICAL`, `FORWARD`, and `COMBINED` are explicit query dimensions.
- Historical and forward observations remain independently filterable forever.
- Every observation has a source-independent `logical_decision_key` in addition
  to its source-specific observation identity.
- `COMBINED` is a deterministic cross-layer composition, not a destructive
  merge and not a blind concatenation.
- The same logical decision may remain present in both raw layers for audit, but
  it can contribute at most once to `COMBINED`.
- Five shares (`5_000_000` share-micros) is the canonical reporting unit.
- A rejected evaluation is an opportunity but is not an accepted signal or a
  trade/performance observation.
- An accepted signal without a settlement is unresolved, not a loss.
- An accepted signal without a usable contract price is explicitly unscorable;
  no price is fabricated.
- Historical depth marked `UNAVAILABLE` cannot produce claims of realized
  execution performance.
- Strategy performance and operational/trading eligibility are separate.
- Strategy-level observations may overlap; strategy PnL is never automatically
  summed into a portfolio total.
- All economics use fixed-point integers for storage and deterministic decimal
  rules for ratios. Binary floating-point is not canonical state.
- Reprocessing identical evidence is a no-op. Reusing an identity with different
  canonical content is a conflict and fails closed.
- `trading_approval=false`, `real_orders=false`, `wallet=false`,
  `signing=false`, and `authenticated_CLOB_writes=false` remain invariant.

## 5. Canonical terminology and denominators

### 5.1 Opportunity

One contract-applicable evaluator result record. For historical data this is a
validated AHR result record, not every date × strategy pair and not merely every
AHR unit. For forward data it is one current `LIVE` evaluator result
reconstructed from `strategy_evaluations` and its captured checkpoint input.
`RECOVERED_AFTER_DOWNTIME` evaluations remain non-live audit evidence and are
excluded; a subsequent persisted current reevaluation is independently eligible.

`opportunity_count = count(all valid observations, accepted or rejected)`.

### 5.2 Accepted signal

An opportunity whose current evaluator result has `accepted=true`. Historical
replay does not emit a runtime `signals` row, so historical accepted signals are
counterfactual signal decisions, not claims that a message was emitted live.

`accepted_signal_count = count(observations where accepted=true)`.

### 5.3 Emitted signal

An accepted forward observation linked to a unique persisted row in `signals`.
Historical observations have `emitted=false` by definition.

`emitted_signal_count = count(observations where emitted=true)`.

The dashboard label **signals** maps to `accepted_signal_count`; actual emission
is exposed separately so historical and forward displays are not misleading.

### 5.4 Priced, resolved, unresolved, and unscorable

- **Priced:** accepted and has a validated canonical performance price basis.
- **Resolved:** priced and has an effective canonical settlement that maps to a
  unique win/loss outcome.
- **Unresolved:** accepted but not resolved, including settlement-pending and
  explicitly unscorable observations.
- **Unscorable:** accepted but missing or ambiguous price, bucket identity,
  settlement mapping, or another required fact. It carries a reason code and no
  invented economics.

`resolved_signal_count` is the denominator for wins, losses, win rate, resolved
PnL, resolved turnover, ROI, drawdown, and streaks.

`unresolved_signal_count = accepted_signal_count - resolved_signal_count`.

`wins + losses = resolved_signal_count` must always hold.

## 6. Component boundaries

### 6.1 PerformanceObservationBuilder

**Responsibility:** Convert source evaluation evidence into canonical immutable
performance observations without reading settlements.

**Inputs:**

- historical AHR result record plus verified AHR run/manifest identity;
- or current `LIVE` forward `strategy_evaluations`, optional linked non-
  infrastructure `signals`, and captured checkpoint input;
- current dispatcher/rule-pack binding and canonical status metadata.

**Outputs:** Validated `PerformanceObservation` records with deterministic keys,
source classification, decision facts, performance price basis, and provenance.

**Dependencies:** Existing strategy registry/dispatcher metadata, existing
canonical input encodings, fixed-point helpers, SHA-256 utilities, and
`PerformanceRepository`.

**Failure behavior:** Missing identity, version/family mismatch, noncanonical
JSON, hash mismatch, duplicate identity with different content, invalid price,
or incomplete accepted decision fails closed. A missing usable performance price
may be persisted only as `UNSCORABLE` with a specific reason; it cannot be
replaced with a guessed value.

The builder never reads settlement, W/L, old PnL, or Registry47 aggregate fields
while constructing the decision observation.

### 6.2 SettlementReconciler

**Responsibility:** Attach canonical resolution evidence to accepted
observations and produce append-only resolution revisions.

**Inputs:** Unresolved accepted observations and canonical stored settlement
events. Historical input uses the canonical 170-date settlement artifact.
Forward input uses resolution evidence already persisted by the provider/source
layer; the reconciler itself performs no provider network request.

**Outputs:** `PerformanceResolution` revisions containing the effective winning
bucket/outcome, deterministic win/loss, payout, cost, PnL, turnover, resolution
timestamp, provenance, and content hash.

**Dependencies:** Strategy-side/bucket scoring rules, canonical market identity
mapping, fixed-point economics, and `PerformanceRepository`.

**Failure behavior:** Missing resolution produces `RESOLUTION_PENDING` and no
economic guess. Ambiguous settlement, impossible bucket mapping, settlement
regression, or same resolution key with changed content fails closed. A genuine
correction is a new revision that explicitly supersedes its predecessor.

### 6.3 PerformanceAggregator

**Responsibility:** Deterministically fold observations plus effective
resolutions into scalar aggregates, monthly buckets, rolling windows, and
cumulative PnL series.

**Inputs:** Repository snapshots filtered by strategy and source layer.

**Outputs:** Canonical aggregate payloads and timeseries rows for `HISTORICAL`,
`FORWARD`, and `COMBINED`, each with input revision/hash and calculation schema
version.

**Dependencies:** Pure fixed-point metric functions and `PerformanceRepository`.

**Failure behavior:** Broken invariants, duplicate effective observations,
non-finite ratios, unknown status, or aggregate/hash disagreement aborts the
refresh. The prior complete aggregate revision remains readable.

### 6.4 PerformanceRepository

**Responsibility:** Own migrations, natural-key conflict checks, append-only
observation/resolution writes, reconciliation cursors, atomic materialization
replacement, and read snapshots.

**Inputs/outputs:** Typed domain records and query filters; it does not calculate
strategy decisions or financial metrics.

**Dependencies:** Existing `SqliteStore` single-writer connection and
`SqliteReadStore` query-only connection.

**Failure behavior:** Exact replay returns the existing identity; payload drift
under an existing identity raises a conflict. A materialization refresh is one
transaction and cannot expose a partial revision.

### 6.5 PerformanceQueryService

**Responsibility:** Return already-calculated strategy metadata, aggregates,
timeseries, recent resolved observations, and unresolved observations.

**Inputs:** Validated strategy/view/window query parameters.

**Outputs:** Versioned backend response objects.

**Dependencies:** `SqliteReadStore`/`PerformanceRepository` read methods only.

**Failure behavior:** Unknown strategy/view/window is a bounded client error;
missing aggregate revision reports `NOT_READY`, never zero-filled fabricated
performance. It performs no economics in API or dashboard code.

## 7. Logical storage model

Names below are the selected logical entities. Exact migration numbering is an
implementation-plan decision, but the implementation must use the existing
numbered SQL migration mechanism rather than a new framework.

### 7.1 `strategy_performance_observations` — primary decision ledger

One immutable row per evaluator result:

| Field | Contract |
| --- | --- |
| `observation_key` | Deterministic source-specific primary key |
| `logical_decision_key` | Deterministic source-independent strategy-decision identity used for cross-layer composition |
| `decision_semantic_sha256` | Hash of source-independent canonical decision semantics used for overlap equality |
| `schema_version` | Exact performance observation schema version |
| `source_layer` | `HISTORICAL` or `FORWARD` |
| `strategy_id`, `strategy_version`, `family` | Reused from current registry/dispatcher conventions |
| `registry_index` | Current canonical registry identity |
| `activation_status`, `activation_reason_code` | Status snapshot; never used to alter scoring |
| `market_id`, `market_date` | Canonical market identity and UTC market date |
| `evaluation_key`, `signal_identity_key` | Source evaluation and optional emitted-signal identity |
| `parent_strategy_id`, `source_decision_identity` | Nullable V2/derived-decision lineage |
| `checkpoint_minutes`, `horizon` | Canonical checkpoint/horizon |
| `side` | `YES` or `NO` |
| `selected_buckets_json`, `selected_bucket_identity_sha256` | Ordered canonical selection and hash |
| `accepted`, `emitted` | Independent exact booleans |
| `reason_code` | Evaluator reason, including rejection |
| `reference_price_micros` | Nullable raw decision reference price; provenance only when a stressed performance price exists |
| `performance_price_micros` | Nullable contract price used for five-share analytics |
| `performance_price_basis` | Exact enum defined in section 11.1; never unlabeled |
| `shares_micros` | Exactly `5_000_000` for accepted performance observations; rejected rows carry zero economics |
| `scoring_status`, `scoring_reason_code` | `REJECTED`, `RESOLUTION_PENDING`, or `UNSCORABLE` before effective resolution |
| `observed_at_ms`, `source_created_at_ms` | Engine and source timestamps |
| `provenance_run_id` | AHR run ID or forward runtime/run identity |
| `source_result_sha256`, `input_sha256` | Source result and input audit identities |
| `payload_json`, `payload_sha256` | Complete canonical observation and content hash |

The performance engine does not create a second 47-strategy registry. Family,
version, parent, registry index, and activation status are validated against the
current dispatcher/rule pack and canonical status metadata. Stored status is a
provenance snapshot; current eligibility shown by queries comes from current
canonical status and may differ from the historical snapshot.

### 7.2 `strategy_performance_resolutions` — append-only settlement revisions

One row per observation and settlement revision:

- `resolution_key` unique primary identity;
- `observation_key` foreign key;
- `revision` positive integer;
- `supersedes_resolution_key` nullable explicit predecessor;
- canonical market/settlement identity and settlement source-event identity;
- winning bucket/outcome;
- `won` exact boolean;
- `shares_micros = 5_000_000`;
- `cost_usd_micros`, `gross_payout_usd_micros`, `pnl_usd_micros`, and
  `turnover_usd_micros`;
- required `resolution_date`, taken from the canonical settlement market/date
  identity;
- nullable `resolved_at_ms`, settlement provenance, canonical payload, and
  SHA-256.

`resolved_at_ms` is audit metadata and is present only when canonical settlement
evidence supplies a trustworthy timestamp. Historical resolution must not
manufacture a millisecond timestamp from `market_date`, event-end assumptions,
ingest time, or file metadata.

For BTC Daily Range, `resolution_date` is the canonical settlement's market
date. It is a date identity, not a timestamp reconstructed at an assumed hour.

Only one valid terminal revision may be effective for an observation. Exact
replay is a no-op. A different result under the same key is a conflict. A
correction must use the next revision, name the superseded key, preserve the old
row, and trigger a complete deterministic aggregate rebuild.

The resolved performance row exposed to aggregators and APIs is a deterministic
join of the immutable observation and its effective resolution. It contains all
raw signal facts plus settlement, win/loss, cost, payout, PnL, turnover,
timestamps, run provenance, and source-result audit identity.

### 7.3 `strategy_performance_ingest_runs` and cursors

An ingest-run record stores:

- deterministic run key and mode (`HISTORICAL_BOOTSTRAP` or
  `FORWARD_INCREMENTAL`);
- source artifact/path class, source SHA-256, AHR acceptance SHA-256, and source
  schema version;
- input, inserted, replayed, rejected, accepted, unscorable, and conflict counts;
- first/last source identity and cursor;
- `COMPLETE` status only after all writes and invariant checks succeed;
- generated manifest hash and timestamps.

Forward ingestion keeps an explicit cursor over committed evaluation/signal
identities. Cursor advance and observation inserts occur in the same writer
transaction. A crash replays the last boundary safely.

### 7.4 `strategy_performance_aggregates`

Rebuildable cache keyed by:

`(strategy_id, source_view, window_kind, window_key, calculation_version)`.

It stores the metric payload, source ledger revision/hash, generated timestamp,
and aggregate SHA-256. `source_view` is exactly `HISTORICAL`, `FORWARD`, or
`COMBINED`. A new complete revision atomically replaces the readable current
revision; partial output is never visible.

### 7.5 `strategy_performance_timeseries`

Rebuildable cache for cumulative, monthly, and rolling series. The natural key
contains strategy, source view, series kind, period/window identity,
calculation version, and aggregate revision. Points store fixed-point values and
the underlying observation count/hash.

Neither aggregate table is authoritative. Deleting both and rebuilding from the
observation and resolution ledgers must reproduce byte-equivalent canonical
payloads and hashes.

## 8. Identity, idempotency, and conflict rules

Historical observation identity is derived from stable source coordinates:

`HISTORICAL + AHR mode + strategy_id + market identity/date + checkpoint + unit_key`.

`source_result_sha256` is content, not part of the natural key. Therefore a
changed result under the same logical coordinates is detected as a conflict
instead of being inserted as a second observation.

Forward observation identity is derived from:

`FORWARD + evaluation_key + evaluator-result/checkpoint identity`.

The source-independent `logical_decision_key` is derived from canonical logical
coordinates shared by both layers:

`strategy_id + strategy_version + canonical market identity/date + checkpoint/horizon + side + canonical parent/source-decision lineage where applicable`.

It excludes source layer, AHR/runtime run ID, ingest/evaluation timestamp,
source-specific observation key, and source-result hash. The canonical semantic
decision payload used for overlap comparison includes strategy/version, market,
checkpoint/horizon, side, ordered selected bucket set, accepted/reason,
reference/performance prices and basis, and parent/source-decision semantics. A
SHA-256 of that canonical payload is stored as `decision_semantic_sha256`.

`signal_identity_key` is linked separately and cannot be reused by another
observation. Including `source_layer` prevents historical and forward facts from
colliding while preserving their independent audit trails. The repository
enforces at most one observation per `(source_layer, logical_decision_key)`;
the same logical key may exist once in each layer.

For every natural key:

1. insert when absent;
2. return `REPLAYED` when canonical payload and SHA-256 are identical;
3. raise a conflict when canonical content differs;
4. never repair a conflict by overwrite or `INSERT OR REPLACE`.

Duplicate AHR rows, duplicate signals, repeated reconciliation, process restart,
or aggregate refresh cannot double-count observations, wins, turnover, or PnL.

Cross-layer duplicates are deliberately not rejected at raw-ledger ingest. They
remain independently queryable under their original `source_layer`. During
`COMBINED` composition:

1. a `logical_decision_key` present in only one layer contributes that
   observation;
2. the same key in both layers with identical `decision_semantic_sha256`
   contributes exactly once, using `FORWARD` as the effective/preferred
   observation;
3. the same key with differing canonical semantic content aborts the aggregate
   refresh with `COMBINED_CROSS_LAYER_CONFLICT`;
4. no tie-break, latest-write rule, or silent double-count is permitted.

Any future expanded historical baseline must run this overlap check against all
already accumulated forward observations before a new `COMBINED` revision can
become current.

## 9. Historical bootstrap

Historical bootstrap is a bounded offline administrative engine operation, not
an AHR replay and not provider acquisition.

The bootstrap sequence is:

1. verify the pinned AHR acceptance and result-file hashes;
2. require AHR `status=PASS`, `exit_code=0`, 170 dates, 47 strategies, 4,888
   completed units, 5,063 parity comparisons, zero failures/violations/network,
   and no production DB/scheduler modification;
3. verify result parquet schema and record-level `result_sha256` values;
4. validate all 47 identities against the current registry/dispatcher and
   canonical status metadata;
5. convert every valid AHR result into one observation before reading settlement
   for that observation;
6. count rejected records as opportunities but never as accepted signals,
   resolved trades, turnover, payout, or PnL;
7. load and verify canonical settlements;
8. reconcile accepted, priced observations to settlement revisions;
9. persist an ingest receipt and rebuild all three views;
10. optionally compare newly calculated aggregates with frozen Registry47
    metrics as secondary evidence only.

The bootstrap performs no market/provider request and does not call evaluators.
It consumes the already accepted replay output. A different AHR run requires an
explicitly approved source pin and a new ingest run; it cannot silently replace
the pinned baseline.

## 10. Forward autonomous lifecycle

```text
Signal Engine
  -> strategy evaluation persisted
  -> accepted signal persisted when applicable
  -> PerformanceObservationBuilder appends FORWARD observation
  -> observation is RESOLUTION_PENDING or UNSCORABLE
  -> provider/source layer later persists canonical resolution evidence
  -> SettlementReconciler appends resolution revision
  -> PerformanceAggregator atomically refreshes affected materializations
  -> PerformanceQueryService returns the new revision
  -> dashboard reflects backend-computed values
```

The Performance Engine may be triggered by committed outbox events or bounded
cursor polling, but either mechanism consumes stored evidence only. Provider
network acquisition remains outside this engine. The engine must recover after
restart from persisted cursors and natural keys without user or AI assistance.

Current forward signal payloads are deliberately small. Observation building
therefore joins non-infrastructure `LIVE` signals to `strategy_evaluations` and
the corresponding captured checkpoint input. It must not infer selected
buckets, price, side, or horizon from strategy names. Recovered-after-downtime
evaluations do not become forward signals. If the persisted evidence is
insufficient, the observation is explicitly unscorable or ingestion fails
according to whether the missing fact is optional or contract-required.

## 11. Settlement and five-share scoring

### 11.1 Price basis

The canonical performance basis is signal-contract/reference analytics, not a
claim of actual historical execution.

For every **accepted V1 historical AHR observation**:

```text
if stressed_reference_cost_micros is not null:
    performance_price_micros = stressed_reference_cost_micros
    performance_price_basis = CONTRACT_STRESSED_REFERENCE_EXPLICIT
else:
    require market_probability_micros
    require selected_leg_count >= 1
    performance_price_micros = min(
        1_000_000,
        market_probability_micros + 30_000 * selected_leg_count,
    )
    performance_price_basis = CONTRACT_STRESSED_REFERENCE_RECONSTRUCTED
```

`selected_leg_count` is the length of the validated canonical selected bucket
set. It is never inferred from a strategy name. Raw
`market_probability_micros` is not a performance price when reconstruction is
required.

The authoritative example `NO_A0 / 2026-01-03` has raw market probability
`665_000`, one selected leg, and null explicit stressed cost. Its performance
price is therefore `min(1_000_000, 665_000 + 30_000) = 695_000`, matching frozen
parity evidence.

For every canonical **V2 performance observation**:

```text
performance_price_micros = stressed_q_3c_micros
performance_price_basis = CONTRACT_V2_STRESSED_Q_3C
```

V2 `actual_price_micros` remains provenance/reference only and cannot silently
replace `stressed_q_3c_micros`. V2 retains its V1 source-decision lineage.

Forward observations use the equivalent captured evaluator contract price and
basis. Paper fills and real execution are separate ledgers and are not
substituted into combined strategy performance.

Every observation exposes `performance_price_basis`. Historical depth
`UNAVAILABLE` remains visible in provenance. No slippage, depth, or fee is
fabricated.

### 11.2 Economics

For a priced accepted observation with integer
`performance_price_micros` and exactly five whole shares:

```text
cost_usd_micros = performance_price_micros * 5
gross_payout_usd_micros = 5_000_000 if won else 0
pnl_usd_micros = gross_payout_usd_micros - cost_usd_micros
turnover_usd_micros = cost_usd_micros
```

This multiplication is exact integer arithmetic. It uses no binary floating
point and requires no rounding. For the authoritative `NO_A0 / 2026-01-03`
winning example, cost is `695_000 * 5 = 3_475_000`, payout is `5_000_000`, and
PnL is `1_525_000` USD micros.

Per-observation return is `pnl_usd_micros / cost_usd_micros` only when cost is
positive.

Stored monetary values are USD micros. Ratios are derived deterministically from
integer numerators/denominators using fixed-point/decimal arithmetic and
serialized in one declared decimal format.
Zero-cost accepted observations are unscorable for ROI and must not create an
infinite or fabricated ratio.

### 11.3 Win/loss mapping

The reconciler uses canonical bucket identities and the strategy's side:

- YES wins when the resolved winner is in the selected set;
- NO wins when the resolved winner is outside the selected set;
- V2 scores its own accepted overlay observation while preserving parent/source
  decision lineage;
- ambiguous or non-exclusive settlement mapping is unscorable and fails closed.

Settlement is never used to decide whether the source evaluator accepted the
signal.

## 12. Metric definitions

Each payload includes raw numerator/denominator counts as well as ratios.

| Metric | Exact definition |
| --- | --- |
| Opportunities | All valid applicable observations, accepted plus rejected |
| Accepted signals | Observations with `accepted=true` |
| Emitted signals | Accepted FORWARD observations linked to persisted `signals` |
| Resolved signals | Accepted, priced observations with one effective resolution |
| Unresolved signals | Accepted signals minus resolved signals |
| Unscorable signals | Accepted observations with an explicit non-pending scoring defect |
| Wins / losses | Effective resolved outcomes; sum equals resolved signals |
| Win rate | `wins / resolved_signal_count`; null when denominator is zero |
| Turnover | Sum of resolved `cost_usd_micros` |
| Gross payout | Sum of resolved `gross_payout_usd_micros` |
| PnL @ 5 shares | Sum of resolved `pnl_usd_micros` |
| ROI | `resolved PnL / resolved turnover`; null when turnover is zero |
| Average entry/reference price | Mean `performance_price_micros` across all priced accepted observations; payload includes `priced_signal_count` |
| Average winner return | Arithmetic mean of per-observation `pnl/cost` over resolved wins with positive cost |
| Average loser return | Arithmetic mean of per-observation `pnl/cost` over resolved losses with positive cost |
| Max drawdown | Largest peak-to-trough decline in cumulative resolved PnL micros, high-water mark initialized at zero |
| Current drawdown | Current cumulative resolved PnL minus its prior high-water mark, reported as a nonnegative decline magnitude |
| Longest win/loss streak | Maximum consecutive resolved outcomes in deterministic resolution order |
| Monthly performance | Resolved counts/economics grouped by UTC `market_date` calendar month |
| Rolling performance | Same resolved metrics over trailing 30, 90, and 365 UTC calendar days ending at explicit `as_of_date` |
| First/last signal date | Min/max `market_date` over accepted observations |
| Last resolved date | Maximum `resolution_date` over effective resolved observations; independent of `resolved_at_ms` |
| Decision coverage | `accepted_signal_count / opportunity_count` |
| Resolution coverage | `resolved_signal_count / accepted_signal_count` |
| Price coverage | `priced_signal_count / accepted_signal_count` |

The canonical financial sequence for cumulative PnL, max/current drawdown, and
win/loss streaks is:

```text
market_date ASC
checkpoint_minutes DESC
observation_key ASC
```

This order uses causal fields available in both layers. `resolved_at_ms` is not
part of financial ordering. An actual forward resolution timestamp remains
audit metadata and may be displayed by the API. Historical observations with
null `resolved_at_ms` remain fully scoreable and deterministically rebuildable.
Ratio values are null, not zero, when their denominator is zero.

## 13. Historical, forward, and combined views

### HISTORICAL

Filters only `source_layer=HISTORICAL`. Its bootstrap provenance names the
pinned AHR run and settlement artifact.

### FORWARD

Filters only `source_layer=FORWARD`. It grows as current evaluations/signals are
persisted and later resolved. Strategies not evaluated operationally have valid
zero-opportunity forward views, not synthetic observations.

### COMBINED

Groups the union of historical and forward observations by
`logical_decision_key`, applies the cross-layer overlap rule in section 8, and
recomputes all ratios, drawdowns, streaks, rolling windows, and dates over the
effective deduplicated set. Additive fields equal historical plus forward only
after subtracting identical cross-layer duplicates. Non-additive fields are
recomputed, never added or averaged from precomputed ratios.

Every combined response includes the historical and forward input revision/hash,
raw component counts, identical-overlap count, effective observation count, and
preferred-source rule, so it is fully auditable and decomposable. No raw row
loses its original source layer.

## 14. Identity overlap and portfolio boundary

All 47 identities receive independent statistics, including operational,
T60/T30, U1/U2, component, parent, and overlay identities. These are analytical
views of overlapping decisions, not independent capital allocations.

Therefore the engine provides no automatic all-strategy PnL, portfolio ROI,
portfolio drawdown, bankroll curve, capital allocation, or leaderboard score
that implies additivity. Portfolio statistics require an explicit deduplication
and allocation policy and are out of scope.

## 15. V2 status and eligibility

All 13 Volatility V2 identities receive historical, forward, and combined
statistics when observations exist. Query responses must expose their canonical:

- `activation_status=DISABLED_RESEARCH_ONLY`;
- `activation_reason_code=V2_FROZEN_POLICY_NOT_ROBUST_RESEARCH_ONLY`;
- user-facing labels `RESEARCH_ONLY` and `NOT_ROBUST`.

Attractive performance cannot change activation status, paper eligibility, or
operational eligibility. The Performance Engine never feeds metrics into the
dispatcher or activation decision.

## 16. Aggregate rebuild and refresh

The aggregator reads a stable ledger snapshot/revision, calculates all views in
one deterministic order, validates invariants, writes a new materialization
revision in one transaction, and only then marks it current.

Refresh triggers are:

- completion of historical bootstrap;
- insertion of new forward observations;
- insertion of settlement revisions;
- explicit administrative rebuild after calculation-version migration.

Incremental refresh may optimize an implementation later, but a full rebuild is
the correctness oracle. Incremental and full rebuild output hashes must match.

## 17. Backend/API contract for a later dashboard

No dashboard implementation is part of this stage. The future API should expose
versioned read-only resources equivalent to:

- `GET /api/v1/performance/strategies` — identity, family, version, status,
  eligibility labels, and compact three-view summary;
- `GET /api/v1/performance/strategies/{strategy_id}` — complete HISTORICAL,
  FORWARD, and COMBINED metrics with numerators/denominators and provenance;
- `GET /api/v1/performance/strategies/{strategy_id}/timeseries` — cumulative,
  monthly, and rolling series selected by explicit view;
- `GET /api/v1/performance/strategies/{strategy_id}/observations` — bounded
  recent resolved or unresolved observations with cursor pagination;
- `GET /api/v1/performance/status` — current ledger/materialization revisions,
  last refresh, bootstrap identity, and reconciliation health.

Response contracts include:

- schema/calculation version;
- strategy identity/family/version and current canonical status;
- performance view and five-share basis;
- signals, W/L, WR, ROI, PnL, turnover, max/current drawdown;
- cumulative PnL, monthly and rolling series;
- recent resolved and unresolved observations;
- first/last signal and last resolution;
- price/resolution/decision coverage;
- input revision/hash and generated timestamp.

The query service reads materialized canonical values. API handlers and
dashboard JavaScript may format currency, percentages, and dates, but must not
calculate PnL, ROI, WR, drawdown, streaks, coverage, or combined statistics.

## 18. Failure and missing-data policy

| Condition | Required behavior |
| --- | --- |
| Missing/failed AHR acceptance or hash mismatch | Block historical bootstrap |
| Unknown strategy or registry/status mismatch | Fail closed |
| Rejected AHR result | Persist opportunity; zero trade economics |
| Missing settlement | `RESOLUTION_PENDING`; exclude from resolved metrics |
| Missing usable price | `UNSCORABLE_MISSING_PRICE`; no PnL/ROI guess |
| Historical depth unavailable | Preserve `UNAVAILABLE`; use labeled contract-reference basis only |
| Ambiguous bucket/settlement mapping | Unscorable/fail closed; never choose a winner arbitrarily |
| Duplicate identical source identity | Idempotent replay |
| Duplicate identity with different content | Conflict; no overwrite |
| Identical cross-layer logical decision | Keep both raw rows; count FORWARD once in `COMBINED` |
| Conflicting cross-layer logical decision | `COMBINED_CROSS_LAYER_CONFLICT`; retain prior complete aggregate revision |
| Corrected settlement | Append explicit superseding revision and rebuild |
| Partial aggregate refresh | Roll back; previous complete revision stays current |
| Missing current materialization | API returns `NOT_READY`, not fabricated zero metrics |
| Provider/network unavailable | Historical bootstrap unaffected; engine performs no acquisition |

## 19. Migration, writer, and operational boundaries

Future implementation uses one additive numbered migration and updates the
existing store's allowlisted table/read methods. It does not rewrite existing
signal, evaluation, paper, source-event, or scheduler tables.

Historical bootstrap and forward reconciliation submit writes through the
single-writer boundary. Read APIs use query-only connections. The design adds no
Task Scheduler task, Windows Service, provider downloader, wallet, key, signing,
authenticated CLOB write, order placement, or trading approval.

Backups and database integrity checks must include the new tables through the
existing operational backup/schema-report conventions before implementation is
accepted.

## 20. Verification and acceptance criteria

Future implementation is accepted only when tests prove:

1. the pinned successful AHR result bootstraps autonomously and offline;
2. all 47 strategy identities are represented;
3. all 4,994 result records are classified deterministically and rejected AHR
   records are never counted as trades;
4. historical and forward observations remain separately queryable;
5. an identical `logical_decision_key` and semantic payload in both layers is
   retained in each raw view, counted once in combined, and uses FORWARD as the
   effective combined observation;
6. a cross-layer duplicate with different semantic content fails closed with
   `COMBINED_CROSS_LAYER_CONFLICT` and cannot publish a new combined revision;
7. combined additive metrics equal the deduplicated effective set and
   non-additive metrics equal deterministic recomputation over that set;
8. an accepted V1 row with explicit stress preserves
   `stressed_reference_cost_micros` and the explicit basis enum;
9. an accepted V1 row with null stress reconstructs
   `min(1_000_000, market_probability_micros + 30_000 * selected_leg_count)`,
   including multi-leg coverage and the `NO_A0 / 2026-01-03 = 695_000` case;
10. V2 uses `stressed_q_3c_micros` with
    `CONTRACT_V2_STRESSED_Q_3C`, never `actual_price_micros` as its performance
    price;
11. cost, payout, turnover, and PnL use the exact five-whole-share integer
    formulas with no binary floating point or multiplication rounding;
12. a historical resolved observation with null `resolved_at_ms` remains fully
    scoreable and produces byte-identical rebuilds under
    `market_date ASC, checkpoint_minutes DESC, observation_key ASC`;
13. `last_resolved_date` is derived without a synthetic millisecond timestamp;
14. repeated settlement reconciliation is idempotent;
15. a duplicate evaluation or signal cannot double-count a signal, trade,
   turnover, payout, or PnL;
16. unresolved and unscorable observations are excluded from resolved WR, ROI,
   drawdown, and streak denominators;
17. deleting materializations and rebuilding produces identical canonical
    payloads and hashes;
18. V2 statistics preserve `RESEARCH_ONLY`/`NOT_ROBUST` status and never imply
    operational eligibility;
19. historical bootstrap makes zero network requests;
20. static and behavioral safety checks prove no wallet, order, signing,
    authenticated-write, or trading-approval path was introduced;
21. API and dashboard layers return/format engine results without independently
    recalculating economics;
22. old Registry47 metrics are consumed only by an optional secondary parity
    check after new calculation;
23. same-key/same-payload replay is a no-op and same-key/different-payload is a
    fail-closed conflict;
24. settlement correction retains the original revision, selects one explicit
    successor, and deterministically changes affected aggregates;
25. crash/restart at each ingest or refresh transaction boundary produces no
    partial current state;
26. `wins + losses = resolved`, resolved and unresolved observations form an
    exact partition of accepted observations, and every published ratio includes
    its denominator;
27. overlapping identities cannot be queried as an implicit portfolio total.

## 21. Non-goals

- strategy optimization, parameter search, or model refit;
- new strategies or evaluator identities;
- evaluator, threshold, registry, dispatcher, or opportunity-map changes;
- live-candidate selection or risk approval;
- portfolio construction, cross-strategy deduplication, capital allocation, or
  bankroll sizing;
- actual order execution, wallet, signing, authenticated provider writes, or
  real-money accounting;
- provider downloading inside the Performance Engine;
- historical replay or a new AHR run;
- dashboard visual implementation;
- manual ChatGPT/Codex leaderboard or recurring AI calculation;
- treating paper fills or old frozen aggregate tables as primary strategy truth.

## 22. Completion boundary for this design stage

This document is the only deliverable. It authorizes neither implementation nor
runtime/database/scheduler changes. The next implementation cycle requires
separate user review and approval of this spec, followed by a dedicated
implementation plan.
