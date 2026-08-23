# Strategy Gap Recovery Continuity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task with review checkpoints.

**Goal:** Extend the canonical startup catch-up path so every recoverable post-baseline strategy observation is reconstructed causally, settled, materialized, and provenance-auditable without manual replay.

**Architecture:** Add a point-in-time input adapter and reconstruction coordinator to the existing `PERFORMANCE_CATCHUP_APPLY` writer operation. Feed recovered checkpoint inputs to the existing frozen historical dispatcher and append its results to the existing performance ledger. Preserve baseline rows, genuine forward rows, and existing reconciliation/materialization behavior.

**Tech Stack:** Python 3.12, SQLite, aiohttp provider adapters, canonical fixed-point strategy evaluators, unittest/pytest, native Windows runtime.

---

### Task 1: Add provenance views and reconstruction coverage storage

**Files:**
- Create: `migrations/0007_strategy_reconstruction.sql`
- Modify: `src/performance_models.py`
- Modify: `src/performance_repository.py`
- Modify: `src/performance_metrics.py`
- Modify: `src/performance_query.py`
- Test: `tests/test_performance_repository.py`
- Test: `tests/test_performance_metrics.py`
- Test: `tests/test_performance_api.py`

1. Write failing tests proving ORIGINAL selects only pinned baseline provenance, RECOVERED selects only `RECOVERED_RETROSPECTIVE`, HISTORICAL selects both, FORWARD remains genuine, and COMBINED deduplicates once.
2. Write failing migration/repository tests for one idempotent `(market_date,strategy_id)` reconstruction status with stable reason/input/evidence fields.
3. Run the focused tests and confirm failures identify the missing view/status behavior.
4. Add migration 0007 and the smallest model/repository/query changes.
5. Re-run focused tests and `git diff --check`.

### Task 2: Freeze and prove the exact V1 point-in-time model adapter

**Files:**
- Create: `src/performance_reconstruction.py`
- Create: `strategy_sources/frozen/contracts/TERMINAL_DISTRIBUTION_RECOVERY_V1.json`
- Test: `tests/test_performance_reconstruction.py`

1. Write RED tests for checkpoint sampling (`observed_at <= target`, closed Binance candle only), deterministic model probability hashes, and settlement exclusion from evaluator inputs.
2. Generate the minimal frozen representation of the original 2026 terminal-distribution training contract from the pinned source artifacts: selected model per horizon, empirical/analog samples or fitted Student-t parameters, quantile definition, training cutoff, and source hashes.
3. Implement the pure terminal distribution adapter and Polymarket checkpoint sampler.
4. Add a parity test over all 170 baseline dates/checkpoints comparing reconstructed model probabilities to the pinned historical matrix, followed by existing-dispatcher decision parity.
5. Run parity twice and require zero differences and identical semantic hashes before enabling new dates.

### Task 3: Reconstruct all 47 identities inside existing catch-up

**Files:**
- Modify: `src/performance_reconstruction.py`
- Modify: `src/runtime_orchestrator.py`
- Modify: `src/performance_forward.py`
- Test: `tests/test_performance_reconstruction.py`
- Test: `tests/test_runtime_orchestrator.py`
- Test: `tests/test_performance_forward.py`

1. Write RED tests where a resolved recovered market has no strategy observations and ordinary `PERFORMANCE_CATCHUP_APPLY` must reconstruct them before reconciliation.
2. Test that all 34 V1 identities are dispatched regardless of execution status and all 13 V2 identities receive the exact `FROZEN_VOLATILITY_CONTRACT_HAS_NO_POST_BLOCK5_TRANSITION` classification unless a compatible row exists.
3. Implement chronological missing-date scanning, bounded public checkpoint acquisition, existing historical dispatch, recovered observation construction, and atomic coverage/status persistence.
4. Invoke the coordinator from the existing writer operation before `ForwardPerformanceCycle.run_once`; do not create a second cycle.
5. Prove a second identical catch-up inserts no observations/status revisions/materializations.

### Task 4: Fix the linked dashboard semantics

**Files:**
- Modify: `src/api.py`
- Modify: `src/performance_query.py`
- Modify: `src/dashboard_static/app.js`
- Modify: `src/dashboard_static/index.html`
- Test: `tests/test_dashboard.py`
- Test: `tests/test_performance_api.py`

1. Write RED tests for COMBINED default, effective resolved status in detail rows, chronological cumulative rows, recent-first resolutions, rolling window plus `as_of_date`, and provenance contribution counts.
2. Enrich observation responses by joining the latest effective resolution without mutating raw observations.
3. Split chronological and recent-first UI ordering and expose compact ORIGINAL/RECOVERED/FORWARD counts.
4. Run focused API/dashboard tests.

### Task 5: Prove real and arbitrary automatic recovery in isolated state

**Files:**
- Create: `tests/test_strategy_gap_recovery_acceptance.py`
- Create: `tools/verify_strategy_gap_recovery.py`

1. Build an isolated copy from the canonical database/provider cache and invoke the same startup recovery operation for 2026-07-08 through 2026-08-22.
2. Report exact expected dates, resolved/absent/data-gap classifications, 47-identity coverage, recovered identity/observation counts, missing checkpoint inputs, and provenance totals.
3. Invoke recovery a second time and compare row/revision/materialization counts and hashes.
4. Remove a bounded known set of recovered observations/status rows in another isolated copy while retaining raw evidence; invoke ordinary recovery and prove exact restoration.
5. Verify COMBINED arithmetic/deduplication and that FORWARD row identities are unchanged.

### Task 6: Canonical integration and operational verification

**Files:**
- Modify only directly affected operation/release evidence documents after fresh verification.

1. Run the focused strategy/performance/recovery/dashboard suite and static security scans.
2. Create a validated SQLite backup using the existing Windows operation before canonical migration/recovery.
3. Restart through the existing user-level Task Scheduler path and wait for bounded startup catch-up.
4. Run the real-gap verifier read-only against canonical state; capture exact counts/hashes and repeat-run no-op evidence.
5. Verify API/dashboard, `PASS/LIVE_READY`, Binance LIVE, Polymarket LIVE, and all five security invariants false.
6. Run the native Windows release suite once because this changes the canonical startup writer path; do not rerun it unless a material failure requires a narrow fix.
7. Record current HEAD and leave the canonical backend running in signal-only mode.
