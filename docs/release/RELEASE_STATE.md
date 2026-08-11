# C9-C11 Release State

## Authority and bases

- Integration branch: `codex/final-project-completion`.
- Audited handoff input base: `b448fcb10d264840e0f918048d4ec975555def7d`.
- Accepted functional MVP: `e097d9bb2ea2e03d0ca43fe7e9130cb10dbe1a90`.
- Worker release-hub base: `cb6699e433a86c151e3953da30bfa4618931ba85`.
- Evidence: `docs/release/HANDOFF_MVP_TO_C9_C11.md` and the bootstrap Git
  verification recorded in the release-manager handoff.

## Sprint status

| Stage | Weight | Progress | State |
|---|---:|---:|---|
| C9 EXPRESS | 30 | 30 | ACCEPTED |
| C10 EXPRESS | 40 | 40 | ACCEPTED |
| C11 PREP | 20 | 20 | ACCEPTED |
| FINAL COMBINED ACCEPTANCE | 10 | 0 | NOT STARTED |
| **Total** | **100** | **90/100** | **ACTIVE** |

The real 48-hour C11 observation is outside this sprint. Sprint completion
means readiness to launch it, not completion of elapsed observation time.

## Control state

- ACTIVE: final combined acceptance on the integrated C9+C10+C11 PREP build.
- BLOCKED: none.
- Accepted C9 integration commit: `769c9eeb1aab4ac116a08acc6c1f79ae9b882f1d`.
- Accepted C9 boundary remediation:
  `83fb525435173958f6dc807822ac8688e6f77ff3` removes the forbidden scheduled
  action policy bypass and makes its native acceptance path canonical-checkout
  independent.
- Accepted C10 integration commit: `35ffc4be6a381540ece6533568d394ef1c0bce63`.
- Accepted C11 PREP implementation:
  `150b92c05e964c1bb1d93b2d22aa8b6fbe4411fb`.
- C11 PREP dry-run: 4 native Windows tests, 4 PASS, 0 FAIL; the real
  48-hour observation was not started. Receipt:
  `reports/C11_PREP_ACCEPTANCE.json`.
- Next integration step: perform final combined acceptance and the single full
  native Windows suite.

## Security state

- `trading_approval=false`
- `real_orders=false`
- `wallet=false`
- `signing=false`
- `authenticated_CLOB_writes=false`

These values are invariant for C9, C10, C11 PREP, and final acceptance.

## Worker state

| Worker | Proposed branch | Proposed worktree | Status | Accepted handoff |
|---|---|---|---|---|
| C9 Windows Operations | `codex/release-c9` | `.worktrees/release-c9` | INTEGRATED | `f7a1a3b0bc125fadfd5672b065b84618d1852da5` |
| C10 Failure Coverage | `codex/release-c10` | `.worktrees/release-c10` | INTEGRATED | `14fdcad66bd98c8a88ba8b5a19240664e880fbe6` |

C9 focused native Windows integration smoke: 16 tests, 16 PASS, 0 FAIL.
C10 focused native Windows integration smoke: 3 tests, 3 PASS, 0 FAIL.
