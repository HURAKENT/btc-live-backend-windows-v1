# C2-C3 Recovery and Rollover Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with RED/GREEN checkpoints.

**Goal:** Deliver verified C2 recovery hardening and C3 deterministic daily-market rollover while preserving the accepted C1 safety and persistence contracts.

**Architecture:** Add small immutable recovery/lifecycle contracts and integrate them through the existing provider adapters, single-writer orchestrator and projector. Persist evidence in existing cursors, market catalog and incidents; use deterministic loopback process tests and version-2 SQLite without migration.

**Tech Stack:** Python 3.12.4, asyncio, aiohttp 3.14.3, sqlite3, unittest, Windows PowerShell 5.1 launchers.

---

### Task 1: Bootstrap durable autonomous control plane

**Files:**
- Modify: `AGENTS.md`
- Create: `docs/C2_C3_AUTONOMOUS_GOAL.md`
- Create: `docs/C2_C3_GOAL_PROGRESS.md`
- Create: `docs/C2_C3_DECISION_LOG.md`
- Create: `docs/superpowers/specs/2026-08-01-c2-c3-recovery-rollover-design.md`
- Create: `docs/superpowers/plans/2026-08-01-c2-c3-recovery-rollover-plan.md`

**Steps:** verify baseline/hash protection, write documents, run doc/static scope
checks, commit `docs: bootstrap autonomous C2-C3 goal`, push and prove clean
`HEAD==origin/codex/c2-c3`.

### Task 2: C2 recovery plan and cursor namespace contracts

**Files:**
- Create: `tests/test_c2_recovery_hardening.py`
- Modify: `src/recovery.py`
- Modify: `src/runtime_orchestrator.py`
- Modify: `tests/test_runtime_orchestrator.py`
- Modify: `tests/test_runtime_core_adversarial.py`

**RED tests:** first start, restart from Binance cursor, aligned closed-minute
range, distinct Polymarket history/book cursors, cursor regression/conflict,
empty range, restart replay, and no partial result on failed asset validation.

**GREEN implementation:** immutable plan/result helpers; exact-type validation;
history-specific cursor key; persisted-cursor-derived recovery bounds; no new
connection/table.

**Verification:** focused modules, storage/outbox regressions, compileall, diff
check; commit `feat: harden persistent recovery planning`.

### Task 3: C2 completeness ledger and recovery acceptance

**Files:**
- Modify: `src/recovery.py`
- Modify: `src/runtime_orchestrator.py`
- Modify: `tests/test_c2_recovery_hardening.py`
- Modify: `tests/test_runtime_real_provider_seams.py`
- Create: `reports/C2_RECOVERY_ACCEPTANCE.json`
- Create: `reports/C2_RECOVERY_TEST_MATRIX.md`
- Modify: `reports/AUDIT_HANDOFF.md`

**RED tests:** missing/duplicate/conflicting Binance minute; current open candle;
22-asset Polymarket completeness; history/current-book classification; buffer
overflow and deterministic drain; incident exact replay/conflict; recovered vs
current evaluation identity; same-DB restart and outbox replay.

**GREEN implementation:** sanitized completeness result and incident commands;
fail-closed C2 gate; acceptance report builder fed only by deterministic test
evidence.

**Verification:** C2 focused suite, process integration, full offline launcher,
SQLite integrity/pragmas, security/scope; commit implementation then evidence.

### Task 4: C3 current/next discovery contract

**Files:**
- Create: `src/market_rollover.py`
- Create: `tests/test_market_rollover.py`
- Modify: `src/market_discovery.py`
- Modify: `src/runtime_adapters.py`
- Modify: `tests/test_polymarket_provider.py`
- Modify: `tests/test_runtime_adapters.py`

**RED tests:** ordered current/next candidates; malformed current or next;
ambiguity; duplicate identity; expired current; deterministic identity hash;
11/22 validation; exact replay/conflict; no network/task side effect in pure
selection.

**GREEN implementation:** immutable `MarketPair` and bounded lifecycle state
objects; adapter discovery seam for explicit clock; no persistence or tasks in
the selector.

**Verification:** focused discovery/adapter/storage regressions and full suite;
commit `feat: model deterministic daily market lifecycle`.

### Task 5: C3 subscription migration and atomic cutover

**Files:**
- Modify: `src/market_rollover.py`
- Modify: `src/runtime_orchestrator.py`
- Modify: `src/runtime_projection.py`
- Modify: `tests/test_market_rollover.py`
- Modify: `tests/test_runtime_orchestrator.py`
- Modify: `tests/test_runtime_projection.py`
- Modify: `tests/support/fake_provider_servers.py`
- Modify: `tests/test_process_runtime_integration.py`

**RED tests:** next stream begins before reconciliation; 22 books and live
evidence required; invalid second item is atomic; current projection unchanged
on failure; duplicate migration idempotent; no duplicate final subscription;
old event ignored after cutover; one writer; task cleanup; pre/mid/post cutover
same-DB restart.

**GREEN implementation:** orchestrator-owned rollover task/controller, bounded
next buffer, writer commands for evidence/identity, projector switch only after
full validation, deterministic old-task cancellation.

**Verification:** pure, orchestrator, projection, loopback process and restart
gates; commit `feat: complete deterministic market rollover`.

### Task 6: C3 replay acceptance and reports

**Files:**
- Create: `reports/C3_ROLLOVER_ACCEPTANCE.json`
- Create: `reports/C3_ROLLOVER_REPLAY_MATRIX.md`
- Modify: `reports/AUDIT_HANDOFF.md`
- Add/modify focused offline report tests under `tests/`.

**RED tests:** PASS cannot be claimed without full lifecycle, exact old/new
identities, unique subscriptions, 11/22 next reconciliation, zero conflicts,
same-DB restart, cleanup, integrity and false trading approval.

**GREEN implementation:** deterministic report generation from replay evidence;
no raw IDs/provider dumps.

**Verification:** C3 focused and full offline gates; commit evidence separately.

### Task 7: Aggregate C2-C3 release gate

**Files:**
- Create: `reports/C2_C3_FINAL_ACCEPTANCE.json`
- Create (ignored artifact): `artifacts/C2_C3_ACCEPTANCE_PACK.zip`
- Modify: `README.md`
- Modify: `START_HERE.md`
- Modify: `docs/C2_C3_GOAL_PROGRESS.md`
- Modify: `reports/AUDIT_HANDOFF.md`
- Modify/add offline report/pack tests under `tests/`.

**Steps:** check official Binance and Polymarket documentation for current
public schema/endpoint contracts; run fresh focused C2/C3 tests, deterministic
process rollover, full Windows offline launcher, compileall and pip check;
validate migration version 2, nine tables, WAL/FULL/FK, one writer, outbox and
natural-key uniqueness; verify historical C1 hashes; build sanitized pack with
CRC/SHA manifest; scan forbidden surfaces; update docs only after PASS.

Commit `test: record C2-C3 final acceptance`, push once, prove clean
`HEAD==origin/codex/c2-c3`, mark Goal complete, and report
`BTC_LIVE_BACKEND_WINDOWS_V1_C2_C3_PASS` with `trading_approval=false`.
