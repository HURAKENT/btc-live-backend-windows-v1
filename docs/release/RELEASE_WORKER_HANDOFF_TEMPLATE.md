# C9-C10 Worker Packets and Handoff Template

Both workers start from the same full SHA: the release-manager bootstrap commit
containing this file. Neither branch/worktree may be created before that commit
exists and is pushed.

## Stage packet: WORKER C9 — Windows Operations

- Branch/worktree: `codex/release-c9` / `.worktrees/release-c9`.
- Goal: reliable user-level Windows operation of the accepted backend.
- First inspect: launch/runtime scripts, `src/single_instance.py`, `src/app.py`,
  shutdown behavior, SQLite APIs, and existing Windows tests.
- Implement only missing Task Scheduler registration/verification, bounded
  rotating backend log, SQLite online backup, startup `quick_check` plus
  schema/version validation, and temporary-copy restore/corrupt-backup drill.
- Reuse the existing mutex. Do not build a Service, installer, updater,
  telemetry, backup catalog, cloud backup, or new locking architecture.
- Focused acceptance: launcher starts the accepted backend; second instance is
  blocked; shutdown is clean; backup opens; restored copy opens and preserves
  key MVP state; corrupt backup is refused; invalid DB fails startup; Task
  Scheduler register/verify is deterministic and idempotent.
- Ownership: new/local Windows operational scripts, modules, and tests.
  Shared accepted MVP production files require a manager
  `SHARED_CHANGE_REQUEST`.
- Verification: focused tests only, then one self-review. No full suite.

## Stage packet: WORKER C10 — Failure Coverage

- Branch/worktree: `codex/release-c10` / `.worktrees/release-c10`.
- Goal: close only the load-bearing gaps in
  `docs/release/C10_COVERAGE_INDEX.md`.
- Read that index first. Do not duplicate scenarios marked `no`.
- Maximum delta: one integrated provider outage/return case; one paper
  SQLite/transaction-or-process interruption case; one abrupt restart case at
  exactly one lifecycle/checkpoint boundary.
- Required assertions as applicable: no phantom signal/fill, no duplicate
  fill, no double bankroll mutation, no partial accounting, visible
  incident/block reason, recovery after the source/process returns, fail closed.
- Prefer tests/fixtures/helpers. Do not change production to ease injection.
  A reproduced production P0/P1 stops broad work and returns a manager
  `SHARED_CHANGE_REQUEST`.
- No Chaos Framework, Fault DSL, kill matrix, backup-corruption test,
  malformed-provider duplication, or C8 reconnect reimplementation.
- Verification: focused tests only, then one self-review. No full suite.

## Required handoff record

Workers must return every field below with concrete values. Use `none` where a
category is empty; placeholders are not accepted.

```text
WORKER
BASE_COMMIT
HEAD_COMMIT
FILES_CHANGED
EVIDENCE_REUSED
NEW_BEHAVIOR
TEST_COMMANDS
TEST_RESULTS
KNOWN_GAPS
P0_P1_FINDINGS
P2_P3_BACKLOG
SHARED_CHANGE_REQUESTS
INTEGRATION_ORDER
DO_NOT_MERGE_IF
```

## Integration order and manager-only boundary

Default order is C9, then C10, because their owned files should not overlap.
The Release Manager alone resolves shared production changes, integrates both
handoffs, performs C11 PREP, runs final combined acceptance including the one
full native Windows suite, updates durable release state, and decides whether
the system is ready to start the real 48-hour observation.
