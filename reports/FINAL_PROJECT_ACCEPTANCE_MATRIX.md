# BTC Daily Range Windows V1 — Final Acceptance Matrix

Source baseline: `579289b550cff3793a74bbafba0d8e78670f0c2a`

| Gate | Status | Required evidence |
|---|---|---|
| C0 Registry lock | PASS_HISTORICAL | Immutable 47-ID registry and hashes |
| C1 Walking skeleton | PASS_HISTORICAL | Canonical C1 reports and pack |
| Phase 0 baseline/hardening | PASS | Receipt `4e1ceb2a...4155d`; report `c7172837...2574b`; pack `b6bd4965...dee39`; focused 101/full 647 |
| C2 Recovery hardening | PASS_REVALIDATED_PHASE0 | Historical report plus receipt-bound recovery/restart regressions |
| C3 Market rollover | PASS_REVALIDATED_PHASE0 | Recurring A→B→C, durable cutover restart and stream-liveness regressions |
| C4 Executable rules/parity | PASS | 47/47 exact sources/specs/evaluators/dispatcher; V1 full-decision historical parity 34/34, V2 full-decision parity 13/13; `C4_STRATEGY_47_ACCEPTANCE.json` |
| C5 Activation classification | PASS | 47/47 classified: 8 paper-evaluation enabled, 26 missing-execution-data disabled, 13 V2 research-only; zero unknown; `C5_STRATEGY_47_ACTIVATION_ACCEPTANCE.json` |
| C6 Scheduler/replay | IN_PROGRESS | Exactly-once checkpoints and recovered replay |
| Data completion | NOT_STARTED_BLOCKS_C7 | `DATA_COMPLETENESS_STATUS.json` currently remains `PHASE_0_AUDIT_PENDING`; require source/range inventory, import runs, append/backfill/reconcile/import and future-market evidence |
| C7 Paper execution | NOT_STARTED | Five-share depth-backed restart-safe accounting |
| C8 API | NOT_STARTED | Versioned bootstrap and resumable WebSocket |
| Stage D Dashboard | NOT_STARTED | Backend-only research/paper UI acceptance |
| C9 Windows operations | NOT_STARTED | Autostart/log/backup/restore receipts |
| C10 Failure injection | NOT_STARTED | Fail-closed fault matrix |
| C11 48-hour observation | NOT_STARTED | Actual valid 48-hour observation receipt |
| Final Windows V1 | NOT_REACHED | All gates PASS; no real-money path; trading_approval=false |
