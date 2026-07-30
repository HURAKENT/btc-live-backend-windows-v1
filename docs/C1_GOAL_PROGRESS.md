# C1 Goal Progress

## Goal Status

`ACTIVE`

## Repository

- Branch: `codex/c0-c1`
- Initial handoff base:
  `b722b04db1fa5b0f6e17eae03d504922bc2e5ae9`

## Current Verified Boundary

`POLYMARKET_BACKFILL`

## Current Blocker

`POLYMARKET_HISTORY_SEQUENCE_ERROR`

## Last Real Run

`C1-ACCEPTANCE-20260730T120442Z-EBA92FA3`

## Current Decision

Separate history-order probe cancelled:
`SEPARATE_HISTORY_ORDER_PROBE_CANCELLED`.

History semantics review selected option B: current persisted evidence does
not prove provider ordering, so normalization would be speculative.

Current cycle adds minimal sanitized sequence diagnostics to the existing
provider failure and Task 14 blocked evidence. It does not change acceptance,
ordering, deduplication or pagination semantics.

## Manual Action Required

Yes. One manual Windows Task 14 run is required after the checkpoint
containing instrumentation commit `12e522a`.

## Definition of Done Checklist

- [ ] Initial backend reaches `LIVE_READY`.
- [ ] Top-level API health is `PASS`.
- [ ] Binance source health is `LIVE`.
- [ ] Polymarket source health is `LIVE`.
- [ ] Current market identity is present.
- [ ] 11 markets and 22 assets are reconciled.
- [ ] Polymarket history recovery is complete.
- [ ] Current Polymarket books are reconciled.
- [ ] Binance closed-minute continuity is complete.
- [ ] Initial graceful stop exit is `0`.
- [ ] Forced termination is `false`.
- [ ] API port is free after stop.
- [ ] Named mutex is free after stop.
- [ ] Child process count is `0`.
- [ ] Accepted downtime interval is measured by monotonic clock.
- [ ] Restart uses the same SQLite database.
- [ ] Post-restart recovery is complete.
- [ ] Post-restart state reaches `LIVE_READY`.
- [ ] Recovered records use `RECOVERED_AFTER_DOWNTIME`.
- [ ] Recovered records have `execution_eligible=false`.
- [ ] Current live reevaluation is stored separately.
- [ ] Outbox replay is complete, ordered and duplicate-free.
- [ ] SQLite `quick_check=ok`.
- [ ] SQLite `integrity_check=ok`.
- [ ] `C1_DOWNTIME_ACCEPTANCE.json` has status `PASS`.
- [ ] `C1_FINAL_ACCEPTANCE.json` has status `PASS`.
- [ ] `C1_ACCEPTANCE_PACK.zip` exists.
- [ ] Acceptance pack CRC and internal SHA-256 verify.
- [ ] Task 14 launcher exit is `0`.
- [ ] `HEAD==origin/codex/c0-c1`.
- [ ] Worktree/index/untracked state is clean.
- [ ] Wallet/order/signing/paper/live-money remain disabled.

## Checkpoint Log

### Checkpoint 0 — Autonomous Handoff Bootstrap

- Verified evidence: frozen design/plan, audit handoff, sanitized WS report,
  two Task 14 run records and external evidence hashes reviewed.
- Current HEAD:
  `b722b04db1fa5b0f6e17eae03d504922bc2e5ae9`
- Files created: `AGENTS.md`, `docs/C1_AUTONOMOUS_HANDOFF.md`,
  `docs/C1_AUTONOMOUS_GOAL.md`, `docs/C1_GOAL_PROGRESS.md`.
- Code changes: 0.
- Network: 0 before documentation push.
- Task 14: not run.
- Current blocker: `POLYMARKET_HISTORY_SEQUENCE_ERROR`.
- Next autonomous step: activate Goal, review history semantics and choose
  bounded normalization or sanitized instrumentation.

### Checkpoint 1 — History Boundary Instrumentation

- Goal API status: active.
- Decision: option B, minimal sanitized existing-runtime instrumentation.
- Rejected option A: provider ordering is not established by current
  persisted evidence.
- TDD RED: 5 focused tests ran; missing diagnostics/hook caused 1 failure and
  3 errors, while the no-raw-value assertion already passed.
- Deduplication RED: 1 focused test failed because fatal and lifecycle
  incidents exposed the same diagnostic twice.
- Diagnostic-bound RED: 1 adversarial test errored with
  `POLYMARKET_HISTORY_DIAGNOSTIC_TOO_LARGE`; the narrow correction limits
  analysis to the validated prefix through the first rejected position.
- Focused GREEN: 95 provider/Task 14 runner tests passed; the subsequent
  adversarial diagnostic-bound test passed, and the fresh post-commit focused
  total was 96 tests.
- Full offline GREEN: process integration 11/11; full suite 539 tests with
  one documented conditional skip; compileall and pip check passed through
  `RUN_C1_ACCEPTANCE_SAFE.ps1 -Mode Offline`.
- Production semantics: unchanged; sequence violations remain fatal.
- Network: 0.
- Task 14: not run.
- Next gate: scope/security audit, commit/push, then one manual Task 14 run.

### Checkpoint 2 — Manual Task 14 Evidence

- Reason: the existing run proves a history sequence violation but not whether
  it is a within-page duplicate, cross-page overlap, descending/non-monotonic
  order or another sequence shape.
- Instrumentation commit:
  `12e522a`.
- Allowed run count: 1.
- Expected duration: 12–18 minutes, including the 605-second downtime only if
  initial `LIVE_READY` is reached.
- Command:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'C:\Users\gegos\Documents\Codex\btc_live_backend_windows_v1\scripts\RUN_TASK14_MANUAL.ps1'
```

- Canonical PASS outputs:
  `C:\Users\gegos\Documents\Codex\btc_live_backend_windows_v1\reports\C1_DOWNTIME_ACCEPTANCE.json`,
  `C:\Users\gegos\Documents\Codex\btc_live_backend_windows_v1\reports\C1_FINAL_ACCEPTANCE.json`
  and
  `C:\Users\gegos\Documents\Codex\btc_live_backend_windows_v1\artifacts\C1_ACCEPTANCE_PACK.zip`.
- Blocked evidence base:
  `C:\Users\gegos\Documents\Codex\btc_live_backend_windows_v1_runtime\acceptance`.
- Acceptance data base:
  `C:\Users\gegos\Documents\Codex\btc_live_backend_windows_v1_data\acceptance`.
- Manual transcript base:
  `C:\Users\gegos\Documents\Codex\task14_manual_logs`.
- Do not rerun automatically or run a standalone history probe.
- Resume with the launcher exit code, generated run ID, transcript path and
  either the three canonical PASS outputs or the new run-specific
  `evidence\task14_blocked.json`.

### Checkpoint 3 — C1 Final Acceptance

- Run evidence established the history failure as one exact within-page
  duplicate: 61 integer timestamps, 59 `LT`, one `EQ`, duplicate position 60,
  and no cross-page overlap.
- Production commit `9e0f8193ed5b6204a9c02dab2ff18947b84d50f7`
  coalesces only full-canonical-equal history rows, keeps conflicting
  duplicates fatal, validates an asset atomically, fails on pagination
  no-progress, and caps requests at 64 per asset.
- TDD RED: six focused tests produced four failures and two errors on the old
  semantics. GREEN: 84 provider tests, 38 adapter/real-provider/process tests,
  and the 545-test offline suite passed with one documented skip.
- Task 14 run `C1-ACCEPTANCE-20260730T142620Z-AD3D3272` completed initial
  `LIVE_READY`, a 605,016 ms downtime, same-DB restart and final runtime audit.
- Evidence finalization exposed `KeyError('backend_process_started')` after
  runtime PASS. Commit `3f8be0d` fixes only that pack evidence mapping; its
  RED reproduced the exact KeyError and its GREEN passed 19 focused and 546
  full-suite tests with one documented skip.
- Canonical reports and pack now verify with
  `BTC_LIVE_BACKEND_WINDOWS_V1_C1_PASS`; pack SHA-256 is
  `c50a12300a3cca5b5f86fb04e107c2ba596ef361ef6d9453b653e5fcc8f07b0f`.
- Trading approval remains false. Registry execution, orders, paper
  execution, wallet/signing and authentication remain disabled.
