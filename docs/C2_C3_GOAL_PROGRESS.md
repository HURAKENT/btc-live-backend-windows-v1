# C2-C3 Goal Progress

## ACTIVE GOAL

`C2_C3_AUTONOMOUS_GOAL_COMPLETE`

## CURRENT STAGE

`BTC_LIVE_BACKEND_WINDOWS_V1_C2_C3_PASS`

## Repository

- Branch: `codex/c2-c3`.
- Branch base: `f167847c0da271592043db750b11249aac294bc9`.
- LAST VERIFIED IMPLEMENTATION COMMIT:
  `cec9e29255a207859006dfdffbc27aa140f273ad`.
- HEAD/ORIGIN STATUS: local implementation commit is pending the final
  evidence commit and single push.

## Verification

- LAST TEST COUNTS: C3 focused 63 PASS; process integration 11 PASS; final
  Windows suite 625 PASS with one documented conditional skip.
- Canonical `RUN_C1_ACCEPTANCE_SAFE.ps1 -Mode Offline`: exit 0.
- `compileall`, `pip check`, fresh SQLite integrity and C1 protected hashes:
  PASS.
- Public provider requests and Task 14 runs: zero.
- Historical C1 protected hashes were captured before branch creation.

## Boundary

- BLOCKER: `NONE`.
- EARLIEST FAILED BOUNDARY: `NONE — implementation has not started`.
- LOCKED NEXT STEP: commit and push final evidence, then stop. C4 requires a
  separate authorized project stage.

## Milestones

- [x] Verify C1 baseline, clean tree and historical artifact hashes.
- [x] Create `codex/c2-c3` from the exact accepted baseline.
- [x] Review frozen design/plan, contract, matrix, C1 handoff and relevant code.
- [x] Commit and push durable Goal/design/plan bootstrap.
- [x] C2 persistent recovery plan and cursor contract.
- [x] C2 cutover/reconciliation/incident/evaluation acceptance.
- [x] C2 report and `C2_RECOVERY_HARDENING_PASS`.
- [x] C3 current/next lifecycle and subscription migration.
- [x] C3 deterministic rollover and restart matrix.
- [x] C3 report and `C3_MARKET_ROLLOVER_PASS`.
- [x] Combined report/pack and `BTC_LIVE_BACKEND_WINDOWS_V1_C2_C3_PASS`.
- [ ] Goal completion, clean `HEAD==origin/codex/c2-c3` (pending push).

## Risks and controls

- No schema change is planned; existing `source_cursors`, `market_catalog`,
  `incidents`, canonical/evaluation/outbox tables are sufficient unless a RED
  test proves otherwise.
- Rollover must not create two SQLite writers or leak unmanaged provider tasks.
- Current and next markets must never be projected simultaneously as current.
- Provider payload handling remains strict and non-permissive.
- At most two production hotfix cycles are allowed per release boundary.

## Manual action

- MANUAL ACTION REQUIRED: no.
- LIVE RUN COUNT: 0.
- C2 STATUS: `C2_RECOVERY_HARDENING_PASS`.
- C3 STATUS: `C3_MARKET_ROLLOVER_PASS`.
- C4 STATUS: `NOT_STARTED`.
- Trading approval: `false`.
