# Final Live System Cycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver and leave running a self-contained Windows BTC Daily Range live signal and analytics application with autonomous acquisition, catch-up, reconciliation, five-share performance accounting, read-only API, and active dashboard.

**Architecture:** Preserve the existing provider, single-writer SQLite, checkpoint scheduler, evaluator, and loopback API boundaries. Add an immutable performance/catch-up ledger and rebuildable materializations behind the existing writer, derive historical facts from the pinned AHR evidence, derive forward facts only from committed live evidence, and expose backend-computed values to the static dashboard.

**Tech Stack:** Python 3.12, asyncio, aiohttp, SQLite WAL, numbered SQL migrations, standard-library `decimal`/`hashlib`/`json`, pinned PyArrow runtime for Parquet ingestion, static HTML/CSS/JavaScript, Windows PowerShell 5.1 and Task Scheduler.

## Global Constraints

- Branch and worktree are exactly `codex/final-live-system-cycle` and `.worktrees/final-live-system-cycle`.
- Preserve 47 canonical identities: 34 V1 and 13 V2; all V2 stay `DISABLED_RESEARCH_ONLY` with `V2_FROZEN_POLICY_NOT_ROBUST_RESEARCH_ONLY`.
- Canonical reporting is exactly five shares (`5_000_000` share-micros); stored economics use integer USD micros and signed PnL.
- `HISTORICAL`, `FORWARD`, and fail-closed deduplicated `COMBINED` remain distinct; a semantic mismatch at one logical decision key is `COMBINED_CROSS_LAYER_CONFLICT`.
- Recovered/missed checkpoints never become fresh live signals; forward performance consumes only committed `LIVE` evaluations and linked non-infrastructure signals.
- Preserve one SQLite writer, immutable natural keys, idempotent replay, explicit conflicts, and atomic materialization publication.
- Missing/ambiguous bucket, price, settlement, calendar, or provider evidence remains `PENDING`, `EXPECTED_ABSENT`, `DATA_GAP`, or `UNSCORABLE`; never guess continuity or economics.
- `real_orders=false`, `wallet=false`, `signing=false`, `authenticated_CLOB_writes=false`, and `trading_approval=false` are absolute.
- No implicit 47-strategy portfolio, bankroll CAGR, strategy optimization, real/test orders, wallet, signing, authenticated writes, new Windows Service, or extra Scheduler task.
- Do not rerun the frozen AHR replay; bootstrap from the pinned PASS evidence and verify hashes before writes.

---

### Task 1: Repair startup live-buffer cutover race

**Files:**
- Modify: `src/runtime_orchestrator.py`
- Modify: `src/recovery.py` only if the summary contract needs an explicit cutover field
- Test: `tests/test_runtime_real_provider_seams.py`
- Test: `tests/test_c2_recovery_hardening.py`

**Interfaces:**
- Consumes: `_ingest_live(source: str, event: SourceEvent)` and the one-writer `_submit_source` queue.
- Produces: a cutover with an explicitly bounded buffered set, zero residual buffered events, and evidence whose `buffered_event_count == drained_event_count` compares the same population.

- [ ] **Step 1: Write a deterministic failing async test** that pauses the first buffered write, injects another live event during drain, and proves current code reports `LIVE_BUFFER_DRAIN_INCOMPLETE` even though the buffer empties.
- [ ] **Step 2: Run the focused test and verify RED** with the expected false blocker.
- [ ] **Step 3: Implement an atomic event-loop cutover**: freeze/clear the initial buffered population and switch subsequent ingress to normal queued live persistence before awaits; drain the frozen population in canonical order and retain fail-closed residual/stream/writer checks.
- [ ] **Step 4: Run focused recovery/orchestrator suites and verify GREEN**, including overflow, timeout, replay, current reevaluation, and shutdown tests.
- [ ] **Step 5: Commit** the narrow root-cause fix and tests.

### Task 2: Add performance domain, exact arithmetic, and additive schema

**Files:**
- Create: `migrations/0006_strategy_performance.sql`
- Create: `src/performance_models.py`
- Create: `src/performance_metrics.py`
- Create: `src/performance_repository.py`
- Modify: `src/storage.py`
- Modify: `src/windows_operations.py`
- Test: `tests/test_performance_repository.py`
- Test: `tests/test_performance_metrics.py`
- Modify: `tests/test_c9_database_operations.py`

**Interfaces:**
- Produces: immutable `PerformanceObservation`, append-only `PerformanceResolution`, ingest/catch-up records, aggregate/timeseries revisions, `PerformanceRepository`, and pure `build_metrics(observations, effective_resolutions, *, source_view, as_of_date)`.
- Natural keys and payload hashes use canonical JSON; accepted observations use exactly `shares_micros=5_000_000`; rejected observations carry no trade economics.

- [ ] **Step 1: Write RED schema/replay/conflict tests** for observations, resolution revisions, cursors, catch-up classifications, atomic current materialization, required-table validation, and backup representative counts.
- [ ] **Step 2: Write RED arithmetic tests** for explicit/reconstructed V1 price, V2 `stressed_q_3c`, exact cost/payout/signed PnL/turnover, W/L/WR/ROI, average price and returns, drawdown, streaks, cumulative/monthly/30/90/365 rolling series, coverage, and null annualization reasons.
- [ ] **Step 3: Add migration 0006 and immutable domain validation**, including CHECK constraints for view/status enums, five shares, revision supersession, and signed PnL.
- [ ] **Step 4: Implement repository replay/conflict and atomic revision APIs** on the existing `SqliteStore` connection; extend count/read/schema/backup allowlists without adding a writer.
- [ ] **Step 5: Implement pure deterministic metrics** using integer/Decimal arithmetic and canonical order `market_date ASC, checkpoint_minutes DESC, observation_key ASC`.
- [ ] **Step 6: Run focused schema, repository, metrics, storage, and Windows database tests; commit.**

### Task 3: Bootstrap pinned AHR historical observations and resolutions

**Files:**
- Create: `src/performance_historical.py`
- Create: `scripts/BOOTSTRAP_PERFORMANCE.ps1`
- Modify: `src/performance_repository.py`
- Test: `tests/test_performance_historical.py`
- Test: `tests/test_performance_acceptance.py`

**Interfaces:**
- Consumes: AHR run `20260814T205244105513Z`, acceptance hash `20fa45912f1a892eb13c40deb5c5e8c3508fc642bf72db7f90c6676cebec1d0c`, result hash `26fad48c7d705bc4954fe65dfaefa7b3b87e1af77a7e94dfffe79c383aa38d84`, manifest hash, all manifest-pinned input source hashes, and settlement hash `ea9ed11c1aa7a975c499bcd27332fdbfa0dd927cf2ef4323e89b372516c4e8e1`.
- Produces: `bootstrap_historical_performance(...) -> PerformanceIngestReceipt` and a deterministic full materialization rebuild.

- [ ] **Step 1: Write RED fixture tests** for hash rejection, 4,994 deterministic opportunity classifications, 1,850 accepted versus 3,144 rejected, 47 identities, V1 index-to-canonical-bucket mapping from pinned AHR inputs, V2 parent/side lineage, settlement mapping, and offline/no-network behavior.
- [ ] **Step 2: Implement path resolution** that works from canonical checkout and linked worktree but accepts only explicitly verified artifacts; never silently selects a similarly named run.
- [ ] **Step 3: Build observations without reading outcomes**, preserving exact AHR result/input provenance and the section-11 price basis rules.
- [ ] **Step 4: Reconcile canonical settlement rows only after observation persistence**, keep trustworthy historical `resolved_at_ms` null, and mark missing/ambiguous facts explicitly.
- [ ] **Step 5: Rebuild HISTORICAL and available COMBINED materializations; run bootstrap twice and prove byte/hash-identical idempotency; commit.**

### Task 4: Implement autonomous forward ingestion, resolution, and post-baseline catch-up

**Files:**
- Create: `src/performance_forward.py`
- Create: `src/market_calendar.py`
- Modify: `src/polymarket_provider.py`
- Modify: `src/runtime_orchestrator.py`
- Modify: `src/app.py`
- Modify: `config/mvp_runtime_v1.json` only for bounded public-data settings
- Test: `tests/test_performance_forward.py`
- Test: `tests/test_market_calendar.py`
- Test: `tests/test_runtime_orchestrator.py`

**Interfaces:**
- Produces: `PerformanceCycle.run_once()`, a persisted evaluation cursor, canonical settlement projection, and one classification per expected/discovered post-baseline date: `RESOLVED`, `PENDING`, `EXPECTED_ABSENT`, or `DATA_GAP`.
- All writes submit through the existing runtime writer boundary; public provider reads are bounded and cursor-driven.

- [ ] **Step 1: Write RED tests** for committed `LIVE` evaluation/signal/checkpoint ingestion, recovered exclusion, cursor crash boundaries, resolution schema validation, ambiguous winner fail-closed behavior, four-state calendar classification, and replay idempotency.
- [ ] **Step 2: Implement forward observation reconstruction** from committed rows, linked emitted signals, captured canonical input and status metadata; unpriced accepted decisions become explicitly unscorable.
- [ ] **Step 3: Implement canonical public settlement discovery/projection** with unique market/date/bucket linkage and durable provenance; raw `market_resolved` passthrough is insufficient by itself.
- [ ] **Step 4: Implement resumable catch-up from 2026-07-08 through the latest defensible current/resolvable date**, persisting honest absence/gap evidence and never manufacturing missing days.
- [ ] **Step 5: Own a bounded performance worker in runtime lifecycle**, refresh materializations after committed deltas, stop/drain before writer close, and surface cycle health/failure.
- [ ] **Step 6: Prove live-evaluation → observation → later resolution → refreshed aggregate flow and duplicate-free restart; commit.**

### Task 5: Add read-only performance/system API

**Files:**
- Create: `src/performance_query.py`
- Modify: `src/api.py`
- Modify: `src/app.py`
- Test: `tests/test_performance_api.py`
- Modify: `tests/test_mvp_api_dashboard.py`

**Interfaces:**
- Produces the five approved `/api/v1/performance/*` resources plus truthful system/catch-up/freshness status using `SqliteReadStore` only.

- [ ] **Step 1: Write RED handler tests** for exactly 47 strategy identities/statuses, all three views, backend metric equality, explicit denominators/provenance, timeseries, bounded observation pagination, invalid query errors, and `NOT_READY` without fabricated zeroes.
- [ ] **Step 2: Implement `PerformanceQueryService`** that reads only complete current revisions and current canonical registry metadata; reject any implicit portfolio total.
- [ ] **Step 3: Register read-only handlers** and extend health/system payload with source timestamps/ages, last performance cycle, latest market/date, classification counts, DB revision/integrity summary, and blocking reason.
- [ ] **Step 4: Prove API reads do not mutate any ledger/cursor/materialization/outbox table; run API suites and commit.**

### Task 6: Replace the partial console with a minimal active performance dashboard

**Files:**
- Modify: `src/dashboard_static/index.html`
- Modify: `src/dashboard_static/app.js`
- Modify: `src/dashboard_static/styles.css`
- Modify: `tests/test_dashboard.py`
- Modify: `tests/test_mvp_api_dashboard.py`

**Interfaces:**
- Consumes only backend-computed API fields; JavaScript may format micros, ratios, and UTC dates but never computes economics or COMBINED results.

- [ ] **Step 1: Write RED static/behavior tests** for system, strategies, HISTORICAL/FORWARD/COMBINED selector, strategy detail, cumulative/monthly/rolling series, recent resolved/pending/unscorable evidence, canonical V2 labels, and truthful transport-versus-health state.
- [ ] **Step 2: Implement accessible system and strategy list/detail regions** with bounded status polling and same-origin API/WebSocket refresh; preserve CSP and no-store behavior.
- [ ] **Step 3: Render returned metrics verbatim** including null annualization reason and explicit denominators; add no JS formulas for WR/ROI/PnL/drawdown/streak/coverage.
- [ ] **Step 4: Run dashboard/API tests and a browser-capable local verification against a real backend response; commit.**

### Task 7: Integrated storage, restart, safety, and full release verification

**Files:**
- Create: `scripts/VERIFY_FINAL_LIVE_SYSTEM.ps1`
- Create: `reports/FINAL_LIVE_SYSTEM_ACCEPTANCE.json` from the verification run
- Modify: operational docs only with generated current facts
- Test: all existing and new test modules

**Interfaces:**
- Produces a machine-readable acceptance receipt with branch/commit, hashes, runtime/API/dashboard evidence, catch-up coverage, strategy counts/statistics, integrity/backup/restore/restart evidence, and five false security invariants.

- [ ] **Step 1: Run all focused new suites and the fresh authorized full native Windows suite**; require zero failures.
- [ ] **Step 2: Bootstrap/rebuild on a controlled copy twice**, compare canonical payload/revision hashes, run SQLite quick/integrity/FK checks, online backup and restore-to-copy validation.
- [ ] **Step 3: Run process-level restart/replay verification** proving no duplicate observations, resolutions, signals, turnover, payout, or PnL.
- [ ] **Step 4: Run static/behavioral safety scans** proving all five invariants false and absence of wallet/order/signing/authenticated-write paths.
- [ ] **Step 5: Commit verified code and receipt**, then push the final-cycle branch without changing Git configuration, `main`, or using force push.

### Task 8: Deploy canonical Scheduler action and leave the accepted system running

**Files:**
- Use existing: `scripts/C9_TASK_SCHEDULER.ps1`, `scripts/C9_RUN_BACKEND.ps1`, `scripts/C9_DATABASE.ps1`
- Update: `docs/operation/OPERATIONAL_LAUNCH_STATE.md`
- Update: `docs/release/RELEASE_STATE.md`

**Interfaces:**
- Produces current Task Scheduler action pointing at the accepted commit checkout, bounded restart `3` at `PT5M`, live loopback API/dashboard, and a validated backup.

- [ ] **Step 1: Back up and validate the production SQLite database** before applying migration/bootstrap; preserve a restorable artifact and receipt.
- [ ] **Step 2: Fast-forward/integrate the accepted commit into the canonical checkout used by Scheduler**, without touching `main` or unrelated dirty worktrees.
- [ ] **Step 3: Verify/register the one user-level Limited/Interactive Scheduler task** only if its action/fingerprint must change; start one instance and never a second.
- [ ] **Step 4: Wait for current Binance and Polymarket evidence, catch-up/performance refresh, loopback health and dashboard usability; verify restart once and return to running state.**
- [ ] **Step 5: Record exact current evidence and declare only `FINAL SYSTEM ACCEPTED` or `BLOCKED_EXTERNAL:<exact blocker>`.**
