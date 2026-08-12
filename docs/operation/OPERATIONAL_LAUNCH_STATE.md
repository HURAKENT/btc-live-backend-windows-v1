# Operational Launch State

## Authority

- Mode: `NORMAL OPERATION / SIGNAL ONLY`.
- Integration branch: `codex/final-project-completion`.
- Current HEAD before bootstrap: `263c344dda6b90720b4fd390c7308d856bd8f398`.
- Accepted functional commit: `4d6fe1897ced23035e7a8a50d9b6e2e561541998`.
- Accepted MVP: `e097d9bb2ea2e03d0ca43fe7e9130cb10dbe1a90`.
- Worker base: the bootstrap commit containing this control plane. Both worker
  branches must be created from that exact commit.
- User-reported Codex budget remaining at bootstrap: `44%`.

The historical 48-hour C11 contract is `OPTIONAL / DEFERRED ENDURANCE TEST`.
It is not a launch blocker and no timed observation is pending.

## Progress

| Stage | Weight | Progress | State |
|---|---:|---:|---|
| Scheduler + signal-time correctness | 35 | 0 | NOT_STARTED |
| Offline recovery + runtime liveness | 40 | 0 | NOT_STARTED |
| Dashboard/docs/deployment corrections | 10 | 0 | NOT_STARTED |
| Final Windows gate + permanent launch | 15 | 0 | NOT_STARTED |
| **OPERATIONAL LAUNCH** | **100** | **0/100** | **BOOTSTRAP_READY** |

Calculation: `35 + 40 + 10 + 15 = 100`.

## Bootstrap verification

- Local and remote branch HEAD were independently verified at
  `263c344dda6b90720b4fd390c7308d856bd8f398` before this bootstrap.
- Worktree was clean before bootstrap.
- Both accepted commits above are ancestors of the release HEAD.
- Release receipts exist and parse as PASS:
  `reports/C9_C11_PREP_FINAL_ACCEPTANCE.json` and
  `reports/C11_PREP_ACCEPTANCE.json`.
- The prior full native Windows result remains `1009` tests, `1007 PASS`,
  `2` documented skips, `0 FAIL`, exit code `0`. It was not rerun.
- Findings: `A=CONFIRMED`, `B=CONFIRMED`, `C=CONFIRMED`, `D=CONFIRMED`,
  `E=CONFIRMED`, `F=CONFIRMED`, `G=CONFIRMED`, `H=CONFIRMED`.
- P0/P1 not reproduced: none.
- Production implementation changes in bootstrap: none.

## Worker staging

- Worker A branch: `codex/operation-scheduler`.
- Worker B branch: `codex/operation-recovery`.
- Packets and exact ownership are frozen in
  `docs/operation/OPERATIONAL_WORKER_HANDOFF_TEMPLATE.md`.
- Neither worker starts until both branches are based on the same bootstrap
  commit.

## Security invariants

`trading_approval=false`, `real_orders=false`, `wallet=false`,
`signing=false`, `authenticated_CLOB_writes=false`.

No wallet, signing, authenticated write, real order, or live-money authority
may be added. Local paper accounting remains simulation only.

## Locked continuation

Dispatch exactly Worker A and Worker B from the bootstrap commit. Each performs
RED, root-cause confirmation, minimal GREEN, focused regression, self-review,
commit, push, handoff, then stops. The Manager integrates once, resolves at
most one normal P0/P1 remediation iteration, performs Manager-only corrections,
runs focused integrated tests, then exactly one fresh full native Windows suite
at the final release boundary.
