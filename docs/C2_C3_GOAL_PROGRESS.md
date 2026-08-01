# C2-C3 Goal Progress

## ACTIVE GOAL

`C2_C3_AUTONOMOUS_GOAL_ACTIVE`

## CURRENT STAGE

`C2_RECOVERY_HARDENING_TDD`

## Repository

- Branch: `codex/c2-c3`.
- Branch base: `f167847c0da271592043db750b11249aac294bc9`.
- LAST VERIFIED COMMIT: `6281e7a6f2f715ae44e67522a868fd3d444bb7d4`.
- HEAD/ORIGIN STATUS: `HEAD==origin/codex/c2-c3` at
  `6281e7a6f2f715ae44e67522a868fd3d444bb7d4`; tree clean after bootstrap push.

## Verification

- LAST TEST COUNTS: baseline full Windows suite 561 tests PASS with one
  documented conditional skip; compileall and pip check PASS.
- Baseline process integration: PASS.
- Baseline production/network mutation: none.
- Historical C1 protected hashes were captured before branch creation.

## Boundary

- BLOCKER: `NONE`.
- EARLIEST FAILED BOUNDARY: `NONE — implementation has not started`.
- LOCKED NEXT STEP: C2 milestone 1 RED tests for persistent recovery planning
  and completeness evidence.

## Milestones

- [x] Verify C1 baseline, clean tree and historical artifact hashes.
- [x] Create `codex/c2-c3` from the exact accepted baseline.
- [x] Review frozen design/plan, contract, matrix, C1 handoff and relevant code.
- [x] Commit and push durable Goal/design/plan bootstrap.
- [ ] C2 persistent recovery plan and cursor contract.
- [ ] C2 cutover/reconciliation/incident/evaluation acceptance.
- [ ] C2 report and `C2_RECOVERY_HARDENING_PASS`.
- [ ] C3 current/next lifecycle and subscription migration.
- [ ] C3 deterministic rollover and restart matrix.
- [ ] C3 report and `C3_MARKET_ROLLOVER_PASS`.
- [ ] Combined report/pack and `BTC_LIVE_BACKEND_WINDOWS_V1_C2_C3_PASS`.
- [ ] Goal completion, clean `HEAD==origin/codex/c2-c3`.

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
- C2 STATUS: `IN_PROGRESS`.
- C3 STATUS: `NOT_STARTED`.
- C4 STATUS: `NOT_STARTED`.
- Trading approval: `false`.
