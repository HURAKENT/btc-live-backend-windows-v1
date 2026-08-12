# Windows V1 Current Release State

## Authority

- Integration branch: `codex/final-project-completion`.
- Accepted functional MVP: `e097d9bb2ea2e03d0ca43fe7e9130cb10dbe1a90`.
- Accepted pre-operational functional commit:
  `4d6fe1897ced23035e7a8a50d9b6e2e561541998`.
- Operational bootstrap: `d6593609bc9878469e5dbc1c5a960a074d4a0992`.
- Current mode: `NORMAL OPERATION / SIGNAL ONLY`.
- Current progress and integration receipts:
  `docs/operation/OPERATIONAL_LAUNCH_STATE.md`.

## Preserved acceptance

C9, C10 and C11 PREP remain accepted. Their final native Windows receipt is
`reports/C9_C11_PREP_FINAL_ACCEPTANCE.json`: `1009` tests, `1007 PASS`, `2`
documented skips, `0 FAIL`, exit code `0`, tested commit
`4d6fe1897ced23035e7a8a50d9b6e2e561541998`.

The historical C11 48-hour contract and evidence are retained. The protocol is
now `OPTIONAL / DEFERRED ENDURANCE TEST`; it is not a blocking next stage and no
timed observation is pending.

## Current release state

Operational launch is `100/100 PASS`. The canonical user-level Task Scheduler
task is registered with bounded restart policy and the signal-only backend is
running. API, Dashboard, database, both sources and scheduler projection are
observable; the exact receipt is
`reports/OPERATIONAL_LAUNCH_ACCEPTANCE.json`.

After launch, normal lifecycle is `NORMAL OPERATION -> INCIDENT -> NARROW FIX ->
CONTINUE OPERATION`. The next separate non-blocking research task is
`AUTONOMOUS HISTORICAL REVALIDATION`.

## Security

`trading_approval=false`, `real_orders=false`, `wallet=false`, `signing=false`,
and `authenticated_CLOB_writes=false`.
