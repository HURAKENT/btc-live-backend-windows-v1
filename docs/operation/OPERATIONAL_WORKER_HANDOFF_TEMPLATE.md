# Operational Worker Handoff

## Required return format

```text
WORKER
BASE_COMMIT
HEAD_COMMIT
FILES_CHANGED
REPRODUCED_FINDINGS
NOT_REPRODUCED_FINDINGS
ROOT_CAUSES
NEW_BEHAVIOR
TEST_COMMANDS
TEST_RESULTS
P0_P1_OPEN
SHARED_CHANGE_REQUESTS
P2_P3_BACKLOG
DO_NOT_MERGE_IF
```

Each worker performs narrow inspection, deterministic RED, root-cause record,
minimal implementation, focused GREEN, one self-review, commit, push, handoff,
then stops. No full suite, broad docs, unrelated refactor, PF1 reconstruction,
telemetry platform, Windows Service, real-money surface, or reviewer agent.

## Stage packet A

```text
WORKER: A — SCHEDULER / SIGNAL SEMANTICS
BRANCH: codex/operation-scheduler
BASE: exact bootstrap commit containing this packet
FINDINGS: P0-A, P0-B, stale-market scheduler progression
FILES_OWNED:
  src/checkpoint_scheduler.py
  tests/test_checkpoint_scheduler.py
  tests/test_operational_scheduler.py (new, only if separation is clearer)
READ_ONLY_CONTEXT:
  reports/C5_STRATEGY_47_ACTIVATION_ACCEPTANCE.json
  reports/STRATEGY_47_STATUS_MATRIX.json
  src/mvp_current_runtime.py
  src/runtime_orchestrator.py
REQUIRED_RED:
  unsupported PF1 cannot enter operational schedule registration
  unavailable on-time claim cannot be reclaimed late as LIVE
  unsupported/expired old-market work cannot trigger stale-market poison
REQUIRED_GREEN:
  frozen C5 bytes unchanged
  operational set equals current production evaluator support
  late/missed work is explicit non-live history or explicit missed state
  no recovered checkpoint is exposed as a fresh Strict A signal
FOCUSED_TESTS:
  .venv/Scripts/python.exe -m unittest tests.test_checkpoint_scheduler tests.test_operational_scheduler -v
SHARED_CHANGE_RULE:
  do not edit src/runtime_orchestrator.py, src/storage.py, provider/runtime files,
  Dashboard/API files, Task Scheduler files, or authority docs; return
  SHARED_CHANGE_REQUEST with exact interface and test evidence
DO_NOT_MERGE_IF:
  any PF1 production evaluator is added; frozen C5 changes; a late checkpoint
  remains LIVE; real-money flags or paper-only boundaries weaken
```

## Stage packet B

```text
WORKER: B — RECOVERY / LIVENESS
BRANCH: codex/operation-recovery
BASE: exact bootstrap commit containing this packet
FINDINGS: P0-C, P1-D, P1-E, P1-F
FILES_OWNED:
  src/recovery.py
  src/runtime_adapters.py
  src/binance_provider.py
  src/polymarket_provider.py
  src/runtime_orchestrator.py
  src/app.py
  run_windows_backend.py
  tests/test_c2_recovery_hardening.py
  tests/test_binance_provider.py
  tests/test_polymarket_provider.py
  tests/test_runtime_orchestrator.py
  tests/test_runtime_core_adversarial.py
  tests/test_windows_lifecycle.py
  tests/test_c9_windows_launcher.py
  tests/test_operational_recovery.py (new, only if separation is clearer)
REQUIRED_RED:
  surviving reconnect resumes without bounded cursor backfill
  multi-hour Polymarket cursor is truncated by the one-hour floor
  fatal runtime leaves top-level process waiting
  disconnect/recovery/stale source remains LIVE or lacks visible blocker
REQUIRED_GREEN:
  disconnect -> non-LIVE -> persisted-cursor gap -> bounded backfill -> dedup ->
  reconcile -> LIVE for both sources
  unrecoverable source interval creates durable explicit gap incident
  fatal unrecoverable runtime persists incident and exits non-zero
  freshness is deterministic, bounded, and visible through existing health/API
FOCUSED_TESTS:
  .venv/Scripts/python.exe -m unittest tests.test_c2_recovery_hardening tests.test_binance_provider tests.test_polymarket_provider tests.test_runtime_orchestrator tests.test_runtime_core_adversarial tests.test_windows_lifecycle tests.test_c9_windows_launcher tests.test_operational_recovery -v
SHARED_CHANGE_RULE:
  do not edit scheduler/strategy files, src/storage.py, Dashboard/API selector,
  scripts/C9_TASK_SCHEDULER.ps1, tests/test_c9_task_scheduler.py, or authority
  docs; return SHARED_CHANGE_REQUEST with exact interface and test evidence
DO_NOT_MERGE_IF:
  reconnect declares LIVE before reconciliation; any interval is silently
  dropped; fatal state can remain process-healthy; retry can loop unbounded;
  real-money flags weaken
```

## Manager-only ownership

- Dashboard selector: `src/storage.py`, `src/api.py`,
  `tests/test_mvp_api_dashboard.py`, `tests/test_dashboard.py`.
- Current authority: `AGENTS.md`, `START_HERE.md`, `README.md`, current
  `docs/release/*` authority selected by the Manager, and `docs/operation/*`.
- Task Scheduler: `scripts/C9_TASK_SCHEDULER.ps1`,
  `tests/test_c9_task_scheduler.py`.
- Integration of any approved shared change and final deployment/launch.
