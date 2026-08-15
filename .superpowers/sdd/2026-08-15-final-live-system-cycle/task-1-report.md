# Task 1 Report: Startup live-buffer cutover race

## Status

DONE_WITH_CONCERNS until the final commit hash is filled by amend.

## Scope

- Worktree: `/mnt/c/Users/gegos/Documents/Codex/btc_live_backend_windows_v1/.worktrees/final-live-system-cycle`
- Branch: `codex/final-live-system-cycle`
- Base commit before fix: `d85b54b59d009378eea0e45f3b1307540249e96a`
- Final commit: `PENDING_AMEND`

## Requirement Source

Task brief: `.superpowers/sdd/2026-08-15-final-live-system-cycle/task-1-brief.md`.

Runtime audit: `/tmp/final_cycle_runtime_audit.md`.

Exact load-bearing requirement implemented: "freeze/clear the initial buffered population and switch subsequent ingress to normal queued live persistence before awaits; drain the frozen population in canonical order and retain fail-closed residual/stream/writer checks."

## Root Cause

The startup cutover counted two different populations:

1. `C1RuntimeOrchestrator.start()` captured `buffered_event_count = len(self._live_buffer)` before draining.
2. `_drain_live_buffer()` kept `_buffering_live = True` while awaiting `_submit_source()`.
3. Live ingress during those awaits appended new events into the same `_live_buffer`.
4. `_drain_live_buffer()` looped and drained those late arrivals too.
5. `assess_c2_recovery()` compared the original snapshot count with the dynamic drain total, so an empty buffer could still produce `drained_event_count > buffered_event_count` and blocker `LIVE_BUFFER_DRAIN_INCOMPLETE`.

This matches the audit evidence in `/tmp/final_cycle_runtime_audit.md`, which reports production late-arrival lower bounds after drain had begun and the same fatal blocker.

## RED

Production change that makes the test fail: leaving `_buffering_live` true while the first buffered write awaits lets late ingress join the drain population and makes recovery compare `buffered_event_count=1` with `drained_event_count=2`.

Command:

```text
../../.venv/Scripts/python.exe -m unittest tests.test_runtime_real_provider_seams.DynamicCutoverBoundaryTests.test_live_buffer_cutover_excludes_late_arrivals_from_drain_count
```

Result before implementation:

```text
F
======================================================================
FAIL: test_live_buffer_cutover_excludes_late_arrivals_from_drain_count (tests.test_runtime_real_provider_seams.DynamicCutoverBoundaryTests.test_live_buffer_cutover_excludes_late_arrivals_from_drain_count)
----------------------------------------------------------------------
AssertionError: Tuples differ: ('LIVE_BUFFER_DRAIN_INCOMPLETE',) != ()

First tuple contains 1 additional elements.
First extra element 0:
'LIVE_BUFFER_DRAIN_INCOMPLETE'

- ('LIVE_BUFFER_DRAIN_INCOMPLETE',)
+ () : {'asset_count': 22, 'binance_duplicate_count_after_dedup': 0, 'binance_expected_closed_minutes': 1, 'binance_missing_closed_minutes': 0, 'binance_recovered_closed_minutes': 1, 'blockers': ['LIVE_BUFFER_DRAIN_INCOMPLETE'], 'buffered_event_count': 1, 'current_book_count': 22, 'current_evaluation_committed': True, 'current_execution_eligible': False, 'drained_event_count': 2, 'history_completed_asset_count': 22, 'history_event_count': 22, 'live_ready_allowed': False, 'market_count': 11, 'recovered_evaluation_committed': True, 'recovered_execution_eligible': False, 'source_duplicate_count_after_dedup': 0, 'status': 'C2_RECOVERY_BLOCKED', 'trading_approval': False, 'writer_consumer_count': 1}

----------------------------------------------------------------------
Ran 1 test in 0.116s

FAILED (failures=1)
```

## GREEN Implementation

Files changed:

- `src/runtime_orchestrator.py`
- `tests/test_runtime_real_provider_seams.py`

Implementation:

- `_drain_live_buffer()` now sorts the current `_live_buffer`, clears it, and sets `_buffering_live = False` before the first awaited source write.
- The frozen population is drained in the existing canonical order: `(source_timestamp_ms, source, natural_key)`.
- Subsequent `_ingest_live()` calls during the drain observe `_buffering_live = False` and use normal queued live persistence through `_submit_source()`.
- The recovery summary continues to compare `buffered_event_count` and `drained_event_count`, but now both values refer to the same frozen buffered population.
- A fail-closed residual guard raises `LIVE_BUFFER_DRAIN_RESIDUAL` if any residual buffered events exist after the frozen drain.
- Existing stream and writer fail-closed checks remain in the surrounding startup path through `_ensure_running()`, `_ensure_streams_alive()`, `_submit_source()`, and the one-writer `_submit_write()` queue.

## GREEN / Regression Evidence

Command:

```text
../../.venv/Scripts/python.exe -m unittest tests.test_runtime_real_provider_seams.DynamicCutoverBoundaryTests.test_live_buffer_cutover_excludes_late_arrivals_from_drain_count
```

Result:

```text
.
----------------------------------------------------------------------
Ran 1 test in 0.115s

OK
```

Command:

```text
../../.venv/Scripts/python.exe -m unittest tests.test_runtime_real_provider_seams
```

Result:

```text
..............
----------------------------------------------------------------------
Ran 14 tests in 3.411s

OK
```

Command:

```text
../../.venv/Scripts/python.exe -m unittest tests.test_c2_recovery_hardening
```

Result:

```text
.......................
----------------------------------------------------------------------
Ran 23 tests in 2.526s

OK
```

Command:

```text
../../.venv/Scripts/python.exe -m unittest tests.test_runtime_orchestrator.RuntimeOrchestratorTests.test_recovered_scheduler_persists_current_reevaluation tests.test_runtime_orchestrator.RuntimeOrchestratorTests.test_stop_cancels_provider_tasks tests.test_runtime_orchestrator.RuntimeOrchestratorTests.test_stop_drains_accepted_queue tests.test_runtime_orchestrator.RuntimeOrchestratorTests.test_stop_leaves_no_pending_owned_tasks tests.test_runtime_orchestrator.RuntimeOrchestratorTests.test_stop_is_idempotent
```

Result:

```text
.....
----------------------------------------------------------------------
Ran 5 tests in 2.915s

OK
```

Command:

```text
git diff --check
```

Result:

```text
```

Exit code: 0.

## Self-Review

- The change is scoped to startup cutover accounting and routing.
- No `src/recovery.py` change was required because the existing equality contract remains valid once the population is frozen.
- No Task Scheduler, runtime process, database, trading, wallet, signing, authenticated provider write, PF1, Windows Service, Linux migration, force-push, `main`, or Git configuration action was performed.
- One SQLite writer is preserved: source persistence still goes through `_submit_source()` and `_submit_write()`.
- Fail-closed semantics are preserved: overflow remains in `_ingest_live()`, residual buffer now raises, writer failure still raises through `_submit_write()`, and stream checks remain in startup.

## Concerns

- Full native Windows suite was not run because AGENTS.md says to run it only at an explicitly authorized release boundary.
