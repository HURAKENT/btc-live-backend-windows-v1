# Task 2 Report: Performance domain, exact arithmetic, and additive schema

## Status

DONE, pending final report-metadata commit.

## Scope

- Worktree: `/mnt/c/Users/gegos/Documents/Codex/btc_live_backend_windows_v1/.worktrees/final-live-system-cycle`
- Branch: `codex/final-live-system-cycle`
- Base commit: `88a4d93aced91b1e458d6e6fd3005225022fde76`
- Implementation commit: `cf0dd01`
- Post-review correction/report commit: recorded by the follow-up commit after
  this report update.
- Requirement source: `.superpowers/sdd/2026-08-15-final-live-system-cycle/task-2-brief.md`
- Approved design: `docs/superpowers/specs/2026-08-15-strategy-performance-engine-design.md`

## RED evidence

The production changes that make the first tests pass are the new migration,
performance domain/repository modules, and metrics module. Before those files
existed, the focused command was:

```text
python3 -m unittest tests.test_performance_repository tests.test_performance_metrics tests.test_c9_database_operations -v
```

Observed result before implementation:

```text
test_performance_repository ... ERROR
ModuleNotFoundError: No module named 'src.performance_models'

test_performance_metrics ... ERROR
ModuleNotFoundError: No module named 'src.performance_metrics'

7 C9 database-operation tests ... ERROR
sqlite3.OperationalError: no such table: strategy_performance_catchup

Ran 9 tests
FAILED (errors=9)
```

Self-review then added three narrower replay/read/backup tests. Command:

```text
python3 -m unittest \
  tests.test_performance_repository.PerformanceRepositoryTests.test_materialization_publication_is_atomic_and_keeps_prior_current \
  tests.test_performance_repository.PerformanceRepositoryTests.test_read_only_store_can_open_repository_but_cannot_cross_writer_boundary \
  tests.test_c9_database_operations.C9DatabaseOperationsTests.test_online_backup_captures_committed_live_wal_state -v
```

Observed RED:

```text
FAIL: changed aggregate child payload under an existing revision did not raise
ERROR: SqliteReadStore had no performance_repository method
ERROR: representative_rows omitted strategy_performance_cursors

Ran 3 tests
FAILED (failures=1, errors=2)
```

The root causes were respectively header-only materialization replay
comparison, absence of a query-only repository adapter, and an incomplete
backup representative-table allowlist. Exact child-set comparison, the
read-only adapter with explicit write rejection, and the cursor allowlist made
the three focused tests GREEN.

Nested immutability was also proven with a test that first failed because a
cursor's nested mapping remained mutable:

```text
python3 -m unittest tests.test_performance_repository.PerformanceRepositoryTests.test_observation_domain_is_frozen_and_rejected_rows_have_no_economics -v

FAIL: TypeError not raised
```

Canonical JSON inputs are now recursively normalized and domain mappings are
deep-frozen.

Post-commit review found that the first catch-up schema could not represent the
normal `PENDING -> RESOLVED` lifecycle because it allowed only one immutable
row per date. The added test first failed with:

```text
TypeError: CatchupClassification.__init__() got an unexpected keyword argument 'revision'
```

Catch-up classifications now use append-only revision/supersession and expose
one deterministic effective terminal classification while retaining the prior
row. A separate fail-closed metrics test also proved that an effective
resolution whose observation is absent must raise
`PERFORMANCE_RESOLUTION_OBSERVATION_MISSING`; it failed before the explicit raw
observation-key validation and passed after it.

## Implemented interfaces

`src/performance_models.py` provides:

- immutable `PerformanceObservation` with canonical selected-bucket,
  semantic-decision, and complete-payload hashes;
- append-only `PerformanceResolution` with explicit revision/supersession;
- immutable `PerformanceCursor`, `PerformanceIngestRun`, and append-only
  revisioned `CatchupClassification` records;
- `AggregateRevision`, `AggregateRow`, and `TimeseriesRow` materialization
  records;
- `canonical_json`, `canonical_sha256`, and `canonical_identity`;
- V1 explicit/reconstructed price basis, V2 stressed-q-3c price basis, and
  exact five-share economics helpers.

`src/performance_repository.py` provides `PerformanceRepository` over the
existing `SqliteStore`/`SqliteReadStore` connection:

- immutable observation and resolution append/replay/conflict;
- explicit terminal resolution supersession and effective-resolution reads;
- observation-plus-cursor atomic commits;
- immutable ingest records plus revisioned four-state catch-up history and
  effective reads;
- atomic complete materialization publication with a single current revision;
- exact replay comparison for revision headers and all aggregate/timeseries
  children;
- query-only repository reads and explicit
  `PERFORMANCE_REPOSITORY_READ_ONLY` rejection for writes.

`src/performance_metrics.py` provides the pure function:

```text
build_metrics(observations, effective_resolutions, *, source_view, as_of_date)
```

It computes deterministic HISTORICAL, FORWARD, and fail-closed deduplicated
COMBINED metrics with integer USD micros and 12-place Decimal ratio strings.
It covers opportunities, accepted/emitted/priced/resolved/unresolved/
unscorable counts, W/L/WR, turnover, payout, signed PnL, ROI, average price,
winner/loser returns, drawdown, streaks, dates, coverage, cumulative/monthly/
30D/90D/365D series, and explicit null annualization reasons. Financial order
is exactly `market_date ASC, checkpoint_minutes DESC, observation_key ASC`;
`resolved_at_ms` is not used.

## Schema decisions

Migration `0006_strategy_performance.sql` is additive and creates eight tables:

1. `strategy_performance_observations`
2. `strategy_performance_resolutions`
3. `strategy_performance_ingest_runs`
4. `strategy_performance_cursors`
5. `strategy_performance_catchup`
6. `strategy_performance_materialization_revisions`
7. `strategy_performance_aggregates`
8. `strategy_performance_timeseries`

Load-bearing constraints include:

- HISTORICAL/FORWARD source layers and HISTORICAL/FORWARD/COMBINED views;
- unique `(source_layer, logical_decision_key)` while allowing one raw row in
  each layer;
- accepted observations at exactly `5_000_000` share-micros;
- rejected observations with zero shares, null performance price, no signal,
  and `REJECTED` scoring;
- immutable source identity and payload hashes;
- one resolution revision per `(observation_key, revision)`, one successor per
  superseded key, and explicit revision-1 versus successor checks;
- one catch-up classification revision per `(market_date, revision)`, explicit
  predecessor linkage, and one deterministic terminal classification;
- `pnl = gross_payout - cost`, `turnover = cost`, and payout tied to `won`;
- one partial-index-enforced current complete materialization per strategy,
  view, and calculation version.

`src/storage.py` count allowlists and both writer/query-only repository access
were extended without creating a second writer. `src/windows_operations.py`
now requires and counts all eight tables during startup validation and backup/
restore validation.

## GREEN and regression evidence

Fresh native Windows focused verification:

```text
../../.venv/Scripts/python.exe -m unittest \
  tests.test_performance_repository \
  tests.test_performance_metrics \
  tests.test_storage_outbox \
  tests.test_c9_database_operations -v
```

Result:

```text
Ran 58 tests in 2.140s

OK
```

Compilation and whitespace verification:

```text
python3 -m compileall -q src tests/test_performance_repository.py tests/test_performance_metrics.py
git diff --check
```

Result: exit code `0`; no compile or whitespace errors.

## Self-review

- COMBINED retains both raw layers, prefers FORWARD only for identical semantic
  hashes, and raises `COMBINED_CROSS_LAYER_CONFLICT` on drift.
- Identical source replay is a no-op; any same-identity payload drift fails
  closed without overwrite.
- Materialization publication is one `BEGIN IMMEDIATE` transaction and only
  flips the current revision after every child row is present.
- Historical resolutions with null `resolved_at_ms` remain scoreable;
  `resolution_date` supplies `last_resolved_date`.
- No API, dashboard, historical bootstrap, forward acquisition, provider
  network, runtime worker, Scheduler, wallet, order, signing, authenticated
  provider write, trading approval, PF1, Windows Service, Linux migration,
  `main`, force-push, or Git-configuration change was introduced.
- The full native Windows suite was not run because AGENTS.md reserves it for
  an explicitly authorized release boundary.

## Independent review

The independent review of `cf0dd01` found the catch-up lifecycle issue above as
Important: `UNIQUE(market_date)` and non-revisioned append logic prevented a
persisted `PENDING` date from later becoming `RESOLVED`. The follow-up change
addresses that root cause with append-only revision/supersession, retains both
rows, and has focused plus full Task 2 regression coverage. No Critical finding
was reported.

## Concerns

- None within Task 2 scope. Historical bootstrap and forward ingestion remain
  deliberately deferred to Tasks 3 and 4.
