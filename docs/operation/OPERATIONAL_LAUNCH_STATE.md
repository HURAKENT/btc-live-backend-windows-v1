# Operational Launch State

## Authority

- Mode: `NORMAL OPERATION / SIGNAL ONLY`.
- Integration branch: `codex/final-project-completion`.
- Current HEAD before bootstrap: `263c344dda6b90720b4fd390c7308d856bd8f398`.
- Accepted functional commit: `4d6fe1897ced23035e7a8a50d9b6e2e561541998`.
- Accepted MVP: `e097d9bb2ea2e03d0ca43fe7e9130cb10dbe1a90`.
- Operational bootstrap: `d6593609bc9878469e5dbc1c5a960a074d4a0992`.
- User-reported Codex budget remaining at bootstrap: `44%`.

The historical 48-hour C11 contract is `OPTIONAL / DEFERRED ENDURANCE TEST`.
It is not a launch blocker and no timed observation is pending.

## Progress

| Stage | Weight | Progress | State |
|---|---:|---:|---|
| Scheduler + signal-time correctness | 35 | 35 | ACCEPTED_FOCUSED |
| Offline recovery + runtime liveness | 40 | 40 | ACCEPTED_FOCUSED |
| Dashboard/docs/deployment corrections | 10 | 10 | ACCEPTED_FOCUSED |
| Final Windows gate + permanent launch | 15 | 15 | RUNNING |
| **OPERATIONAL LAUNCH** | **100** | **100/100** | **PASS** |

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

## Integrated corrections

- Worker A source commit `64392622273a6e6dcfd07409c562bd1bd9db6620`
  was integrated as `f17ced72459db3e8698247c35eeedcd07868bfd9`;
  focused result: `38/38 PASS`.
- Worker B source commit `d9047ccb7dc2ce484d22e44bd1d11e38b30ab49a`
  was integrated as `6c853c97938e7fadf0b9221a79c2ce11d47ee547`.
  One bounded integration remediation updated two obsolete operational-count
  assertions from the historical eight/ten projection to three/four. Expanded
  focused result: `172/172 PASS`.
- P1-G canonical Strict A selector completed by RED/GREEN; focused
  storage/API/Dashboard result: `48/48 PASS`.
- P1-H current authority now makes C11 optional/deferred and records
  `AUTONOMOUS HISTORICAL REVALIDATION` as the next non-blocking research task.
- Task Scheduler restart policy is bounded at `3` attempts with interval
  `PT5M`; native registration/verifier acceptance passed.
- Integrated offline semantics: `13/13 PASS`.
- Historical registry/evaluator separation: `11/11 PASS`; all 47 historical
  identities/evaluators remain available while the operational projection is
  Strict A only.
- Frozen C5 hashes remain
  `ca05d61430047e6dad6774ae6243abed6dab1d2532641bc66977f0b5d349de9e` and
  `1594ae25f3ce17d77eb200f088f7b6287a7745a95e6cc6d0e1babf03a1e40cd0`.

## Security invariants

`trading_approval=false`, `real_orders=false`, `wallet=false`,
`signing=false`, `authenticated_CLOB_writes=false`.

No wallet, signing, authenticated write, real order, or live-money authority
may be added. Local paper accounting remains simulation only.

## Final gate and permanent operation

- Final native Windows gate: `1022` tests, `1020 PASS`, `2` documented skips,
  `0 FAIL`, `0 ERROR`, exit code `0`, `191.490s`.
- Earlier final-gate runs exposed operational projection, authority-doc and C10
  fixture regressions. They were remediated with focused GREEN before the final
  successful run; the failed results were not treated as acceptance.
- Production backup:
  `data/backups/btc_live_backend_prelaunch_20260812T095204Z.sqlite3`, SHA-256
  `882feb1602c32c613baef39d897d37aa7b705eb9f7fbfff3521f4dbdc1abe362`,
  `quick_check=ok`, schema PASS, migration `5`.
- Task Scheduler: one canonical user-level task
  `BTC Daily Range Backend V1`, Limited/Interactive, fingerprint
  `acb279a28dd39b18a3ac4d747e7d9b1a9dfa9decb5693ce966d0181e71fbcce8`,
  restart `3` at `PT5M`, state `Running`, no C11/test conflict.
- Live startup produced two durable incidents. Narrow fixes accepted canonical
  Gamma bucket/date metadata (`123/123 PASS`) and market-scoped WebSocket events
  (`108/108 PASS`); both fixes were committed and pushed before restart.
- Permanent runtime verification at `2026-08-12T10:12:09Z`: API and Dashboard
  HTTP `200`, database health PASS, runtime `LIVE_READY`, Binance `LIVE`,
  Polymarket `LIVE`, current market `11` markets / `22` assets.
- Operational scheduler contains exactly four current Strict A checkpoints at
  future T-60/T-30 boundaries. Duplicate source natural keys `0`, duplicate
  signal keys `0`, paper fills `0`, recovered execution-eligible signals `0`,
  and post-relaunch critical incidents `0`.

The backend remains running in normal signal-only operation. C11 remains
optional/deferred. The next separate research task is
`AUTONOMOUS HISTORICAL REVALIDATION`; it has not started.
