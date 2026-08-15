# Final Live System Cycle Handoff

**Prepared:** 2026-08-15
**Repo:** `C:\Users\gegos\Documents\Codex\btc_live_backend_windows_v1`
**Branch:** `codex/final-live-system-cycle`
**Approved base:** `be9401a45babbcc38d3540cb540a366b46dcf842`

This is the self-contained authority for the next outcome-based implementation
cycle. An old PASS receipt proves a completed gate, not current process health;
current runtime evidence is stated separately.

## A. Product goal

Deliver a continuously running, signal-only BTC Daily Range system that
autonomously receives current Binance/Polymarket evidence, evaluates the
existing strategy universe according to its status, persists decisions,
signals and provenance, reconciles resolved markets, maintains historical and
forward strategy performance, calculates its own metrics, exposes a read-only
API, serves a minimal active dashboard, and survives restart without duplicate
or phantom signals. Normal calculation requires no recurring ChatGPT/Codex.

## B. Current verified capabilities

| Subsystem | State | Verified truth |
| --- | --- | --- |
| Git | **VERIFIED** | Canonical branch, HEAD and origin were clean and equal to `be9401a...`; this worktree starts at that exact commit. |
| Runtime | **IMPLEMENTED / FOCUSED VERIFIED** | `run_windows_backend.py`, `src/app.py`, and `src/runtime_orchestrator.py` provide single-instance startup, one writer, provider/recovery lifecycle, checkpoint processing, API startup and ordered shutdown. Focused baseline: 228/228 PASS. |
| Providers | **IMPLEMENTED / FOCUSED VERIFIED** | `src/binance_provider.py` implements closed one-minute klines, bounded backfill/reconnect/cursor recovery. `src/polymarket_provider.py`, `src/market_discovery.py`, and `src/runtime_adapters.py` implement public discovery, books/price changes, bounded history recovery and reconnect. |
| Strategies | **VERIFIED** | `src/strategy_registry.py` and `src/strategy_dispatch.py` enforce 47 bindings: 34 V1 + 13 V2. Current and historical dispatch are separate; unknown identities fail closed. All 47 are source-verified, spec-frozen, evaluator-implemented and parity-passing in `reports/STRATEGY_47_STATUS_MATRIX.json`. |
| Operational scheduler | **IMPLEMENTED / DEFINITION VERIFIED** | `src/checkpoint_scheduler.py` and migration `0003` persist checkpoint state. Operational projection is Strict A only. The canonical task verifier returned PASS: one Limited/Interactive task, restart 3 at `PT5M`, fingerprint `acb279a28dd39b18a3ac4d747e7d9b1a9dfa9decb5693ce966d0181e71fbcce8`. |
| Persistence | **IMPLEMENTED / VERIFIED** | `src/storage.py` and migrations `0001`-`0005` provide SQLite WAL, one writer, read-only queries, immutable natural keys, source cursors/events, market/snapshot/evaluation/signal/outbox/incident/checkpoint/data-import/paper state. Current DB validation: `quick_check=ok`, schema PASS, migration 5. |
| Signal evidence | **IMPLEMENTED / FOCUSED VERIFIED** | Evaluations and signals have unique identities and atomic outbox persistence. Recovered decisions are explicitly non-live/non-executable; current reevaluation is separate. |
| Historical AHR | **VERIFIED CLOSED GATE** | Run `20260814T205244105513Z`: PASS, 170/170 dates, 47/47 strategies, 4 families, 4,888/4,888 units, 5,063 parity comparisons, zero construction/dispatch/skip/lookahead/network failures, production DB/scheduler unchanged. |
| API | **IMPLEMENTED / CURRENTLY UNAVAILABLE** | `src/api.py` implements loopback bootstrap/health/sources/signals/incidents and WebSocket events. Tests pass. On 2026-08-15 the live health endpoint refused connection because no backend process was listening. |
| Dashboard | **IMPLEMENTED / PARTIAL** | `src/dashboard.py` and `src/dashboard_static/` serve a read-only operational/paper console at `/dashboard`. It is not a strategy-performance dashboard. |
| Deployment | **DEFINITION VERIFIED / CURRENT RUN FAILED** | Scheduler definition verifies, but `Get-ScheduledTaskInfo` reported `LastTaskResult=1`. Latest durable production incident: `RECOVERY_BLOCKED: C2_RECOVERY_BLOCKED:LIVE_BUFFER_DRAIN_INCOMPLETE` at `2026-08-15T08:19:23.109Z`. No repair was attempted. |
| Backup/integrity | **IMPLEMENTED / VERIFIED** | `scripts/C9_DATABASE.ps1`, `windows_database.py`, and `src/windows_operations.py` implement online backup, read-only validation and restore-to-copy drill. Current production DB read-only validation passed. |
| Performance Engine | **DESIGNED_ONLY** | Approved authority: `docs/superpowers/specs/2026-08-15-strategy-performance-engine-design.md` at `be9401a...`. No performance migration, ledger, reconciler, aggregator, materialization or performance API exists. |

Current production DB facts (SQLite URI `mode=ro`): 91,150 source events
(4,216 Binance; 86,934 Polymarket); source evidence through
`2026-08-15T08:19:20.482Z`; catalogued dates 2026-08-12 and 2026-08-15; 18
evaluations and 18 signals, all infrastructure canaries; zero paper
fills/positions/intents; eight schedules still `PENDING` (four for each
catalogued date). This proves persisted acquisition, not current liveness or
completed forward strategy processing.

## C. Current missing capabilities

- Restore normal operation from the durable `LIVE_BUFFER_DRAIN_INCOMPLETE`
  block without weakening fail-closed recovery.
- Implement the approved Strategy Performance Engine: canonical observation
  and resolution ledgers, rebuildable aggregates, autonomous refresh and query
  layer.
- Reconcile canonical forward resolutions into strategy performance; the
  existing paper settlement path is separate simulation truth.
- Autonomously classify/catch up the period after the AHR end date 2026-07-07.
- Add performance API resources and the minimal active performance dashboard.
- Prove historical bootstrap, catch-up, forward reconciliation, API/dashboard
  refresh and restart idempotency together.

Legacy `.worktrees/data-inventory` is dirty in
`src/data_completion_acceptance.py` and
`tests/test_data_completion_acceptance.py` and contains
`PENDING_ACTUAL_RECEIPT_SHA256`. It was not touched and is not in this branch.
Other legacy worktrees also remain untouched on historical branches.

## D. Canonical data, hashes and runs

| Authority | Verified value |
| --- | --- |
| Base/spec commit | `be9401a45babbcc38d3540cb540a366b46dcf842` |
| Approved spec | `docs/superpowers/specs/2026-08-15-strategy-performance-engine-design.md` |
| AHR run | `20260814T205244105513Z` |
| AHR acceptance | `reports/historical_revalidation/20260814T205244105513Z/ACCEPTANCE.json`; SHA-256 `20fa45912f1a892eb13c40deb5c5e8c3508fc642bf72db7f90c6676cebec1d0c` |
| AHR results | `reports/historical_revalidation/20260814T205244105513Z/STRATEGY_RESULTS.parquet`; SHA-256 `26fad48c7d705bc4954fe65dfaefa7b3b87e1af77a7e94dfffe79c383aa38d84` |
| AHR manifest | `reports/historical_revalidation/20260814T205244105513Z/INPUT_MANIFEST.json`; final date 2026-07-07 |
| Settlement | `C:\Users\gegos\Documents\Codex\btc_edge_search_lab_v1\data\normalized\unified_research_visible_170_v1\settlements.parquet` |
| Settlement SHA-256 | `ea9ed11c1aa7a975c499bcd27332fdbfa0dd927cf2ef4323e89b372516c4e8e1` |
| Registry/status | `strategy_sources/frozen/STRATEGY_RULE_MAP_47.json`; `registry/STRATEGY_REGISTRY_47.json`; `reports/STRATEGY_47_STATUS_MATRIX.json` |
| Production DB | `C:\Users\gegos\Documents\Codex\btc_live_backend_windows_v1\data\runtime\btc_live_backend.sqlite3` |
| Runtime config | `config/mvp_runtime_v1.json` |
| Point-in-time launch receipt | `reports/OPERATIONAL_LAUNCH_ACCEPTANCE.json` (verified 2026-08-12T10:12:09Z) |

The AHR directory is ignored generated evidence. Verify its PASS receipt and
hashes before bootstrap.

## E. Strategy contract

- Historical/research universe is exactly 47 identities (34 V1, 13 V2)
  through the current rule pack and dispatcher.
- Operational scheduling is a separate Strict A projection, not all 47.
- Status counts: 8 `PAPER_EVALUATION_ENABLED`, 26
  `DISABLED_MISSING_EXECUTION_DATA`, 13 `DISABLED_RESEARCH_ONLY`.
- All V2 identities remain `DISABLED_RESEARCH_ONLY` with reason
  `V2_FROZEN_POLICY_NOT_ROBUST_RESEARCH_ONLY`; metrics do not activate them.
- Canonical reporting uses exactly five shares.
- Identities overlap. Never sum them into an implicit portfolio; portfolio
  policy is out of scope.
- Do not alter identities, thresholds, opportunity maps or dispatcher semantics
  merely to complete analytics.

## F. Performance contract

The approved spec above is the full authority. Its load-bearing rules are:

- immutable/auditable observations and resolution revisions are primary truth;
  aggregates/time series are rebuildable materializations;
- HISTORICAL and FORWARD remain independently queryable; COMBINED is a
  deterministic composition;
- equal cross-layer `logical_decision_key` and semantic content counts once in
  COMBINED with FORWARD preferred; differing content fails closed as
  `COMBINED_CROSS_LAYER_CONFLICT`;
- opportunities, accepted, emitted, priced, resolved, unresolved and
  unscorable counts have explicit distinct denominators;
- settlement attachment is after decision, idempotent and append-revisioned;
  missing/ambiguous evidence never creates a guessed outcome, price or PnL;
- historical `resolved_at_ms` may be null; financial sequence is
  `market_date ASC, checkpoint_minutes DESC, observation_key ASC`;
- V1/V2 price bases and exact five-share integer formulas are pinned by spec
  section 11; binary float is not canonical state;
- API/dashboard consume engine-calculated economics rather than recalculate it;
- an annualized metric must name its window, numerator, denominator and
  convention. Without an explicit bankroll model it must be labelled an
  annualized historical rate or linearized fixed-unit statistic, never CAGR,
  compounded bankroll return, or independent portfolio-capital performance.

## G. Live catch-up requirement

The baseline ends on 2026-07-07. Autonomously discover/process the subsequent
period through the latest legitimately resolvable/current market, then continue
forward through the same pipeline. Do not fabricate calendar continuity.
Persist evidence and classify each expected/discovered date as exactly:

- **RESOLVED** — canonical outcome exists and is scoreable;
- **PENDING** — legitimate current/future market is unresolved;
- **EXPECTED_ABSENT** — authoritative evidence says no market should exist;
- **DATA_GAP** — required expected evidence is missing/ambiguous.

Catch-up must be resumable, idempotent and auditable. Current DB dates
2026-08-12 and 2026-08-15 do not cover/classify the whole interval.

## H. Final dashboard requirement

Build a minimal, functional, active dashboard; no framework is prescribed.

- **System:** running/degraded state, source freshness, last cycle, latest
  market/date, unresolved/data gaps, database/update state.
- **Strategies:** identities, family/status, HISTORICAL/FORWARD/COMBINED,
  opportunities, accepted/resolved/unresolved, W/L, WR, ROI, PnL at five shares,
  turnover, honestly labelled annualized metric, max/current drawdown, last
  signal/resolution.
- **Detail:** cumulative PnL, monthly/rolling performance and recent
  decisions/resolutions.

Browser code selects and formats API-computed values; it is not a calculation
engine. Simple and reliable is preferred to elaborate styling.

## I. Security invariants

```text
real_orders=false
wallet=false
signing=false
authenticated_CLOB_writes=false
trading_approval=false
```

Public data, signals, settlement analytics, API and dashboard are in scope.
Private keys, wallet access, signing, authenticated writes, orders and trading
approval are not.

## J. Things already closed

Do not reopen absent new contradictory evidence:

- pinned AHR-1 replay and semantic parity;
- 47-identity rule pack/bindings/historical seam and V2 classification;
- single-writer SQLite/natural-key/outbox architecture;
- missed/recovered checkpoint cannot become a fresh live signal;
- bounded recovery and fail-closed unrecoverable gaps;
- loopback/security boundary and all no-execution invariants;
- one user-level Limited/Interactive Scheduler task, restart 3 at `PT5M`;
- C11 48-hour endurance remains optional/deferred.

The current runtime failure is new operational evidence requiring a narrow fix,
not permission to redesign closed contracts.

## K. Start, test and operate

Run operational commands from canonical Windows PowerShell 5.1.

### Environment

```powershell
Set-Location -LiteralPath `
  'C:\Users\gegos\Documents\Codex\btc_live_backend_windows_v1'
& .\.venv\Scripts\python.exe -c `
  "import sys; print(sys.version); print(sys.executable)"
& .\.venv\Scripts\python.exe -m pip check
```

The worktree intentionally does not duplicate the ignored venv:

```powershell
Set-Location -LiteralPath `
  'C:\Users\gegos\Documents\Codex\btc_live_backend_windows_v1\.worktrees\final-live-system-cycle'
$ProjectPython = (Resolve-Path `
  -LiteralPath '..\..\.venv\Scripts\python.exe').Path
& $ProjectPython -c "import sys; print(sys.version); print(sys.executable)"
```

Focused baseline used here:

```powershell
& $ProjectPython -m unittest `
  tests.test_config_registry tests.test_storage_outbox `
  tests.test_strategy_dispatch tests.test_runtime_orchestrator `
  tests.test_binance_provider tests.test_polymarket_provider `
  tests.test_mvp_api_dashboard -v
```

Result: 228/228 PASS. `tests.test_c9_task_scheduler` was separately attempted
but expects a worktree-local `.venv`; its only failure was
`WINDOWS_VENV_NOT_FOUND`. The real canonical task definition was verified
read-only with the command below and returned PASS.

Current backend and health:

```powershell
# Canonical scheduled action / manual incident diagnosis
& .\scripts\C9_RUN_BACKEND.ps1
& .\scripts\RUN_BACKEND_SAFE.ps1

Invoke-RestMethod -Method Get `
  -Uri 'http://127.0.0.1:8767/api/v1/health'
```

Do not start a second instance. At handoff time health could not connect;
inspect the durable incident before restart and do not substitute the old
launch receipt for current health.

Database/scheduler inspection:

```powershell
$Db = 'C:\Users\gegos\Documents\Codex\btc_live_backend_windows_v1\data\runtime\btc_live_backend.sqlite3'
& .\scripts\C9_DATABASE.ps1 -Operation Validate -Source $Db
& .\scripts\C9_TASK_SCHEDULER.ps1 -Operation Verify
Get-ScheduledTask -TaskName 'BTC Daily Range Backend V1'
Get-ScheduledTaskInfo -TaskName 'BTC Daily Range Backend V1'
Get-Content -LiteralPath '.\data\runtime\backend.log' -Tail 100
```

`Validate` is read-only. Register/Unregister/Backup/RestoreDrill are not
inspection commands.

Existing endpoints:

```text
Dashboard  http://127.0.0.1:8767/dashboard
Bootstrap  http://127.0.0.1:8767/api/v1/bootstrap
Health     http://127.0.0.1:8767/api/v1/health
Sources    http://127.0.0.1:8767/api/v1/sources
Signals    http://127.0.0.1:8767/api/v1/signals
Incidents  http://127.0.0.1:8767/api/v1/incidents
Events     ws://127.0.0.1:8767/ws/v1/events
```

## L. Final acceptance target

- Scheduled backend is continuously observable and restart-safe; health and
  sources are live or expose a truthful degraded reason.
- Decisions/signals/checkpoints remain idempotent; recovered/missed checkpoints
  never become current signals.
- Historical performance bootstraps offline from pinned AHR/settlements for all
  47 identities; rejected results never become trades.
- Post-2026-07-07 markets are caught up and explicitly classified without
  fabricated dates.
- Forward observations/resolutions autonomously update the same ledger and
  resume without duplicate economics.
- HISTORICAL, FORWARD and fail-closed/deduplicated COMBINED rebuild
  deterministically at five shares.
- Read-only performance API exposes metrics, provenance, freshness, unresolved
  state and time series without handler-side economics.
- Minimal active dashboard presents system/strategy/detail views, explicit V2
  research status and honest annualization.
- Migration, backup/restore, restart-boundary, focused and authorized final
  Windows tests pass; production Scheduler/DB are left healthy and observable.
- All five security invariants remain false and no money-execution surface is
  introduced.
