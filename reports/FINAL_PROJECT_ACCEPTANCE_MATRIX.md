# BTC Daily Range Windows V1 — Final Acceptance Matrix

Source baseline: `579289b550cff3793a74bbafba0d8e78670f0c2a`

| Gate | Status | Required evidence |
|---|---|---|
| C0 Registry lock | PASS_HISTORICAL | Immutable 47-ID registry and hashes |
| C1 Walking skeleton | PASS_HISTORICAL | Canonical C1 reports and pack |
| C2 Recovery hardening | PASS_PROVISIONAL_REVIEW | Deterministic recovery report plus Phase 0 receipt audit |
| C3 Market rollover | PASS_PROVISIONAL_REVIEW | Existing deterministic rollover plus A→B→C/crash hypothesis tests |
| C4 Executable rules/parity | NOT_STARTED | 47/47 exact sources/specs/evaluators/parity |
| C5 Activation classification | NOT_STARTED | 47/47 machine-readable activation decisions |
| C6 Scheduler/replay | NOT_STARTED | Exactly-once checkpoints and recovered replay |
| C7 Paper execution | NOT_STARTED | Five-share depth-backed restart-safe accounting |
| C8 API | NOT_STARTED | Versioned bootstrap and resumable WebSocket |
| Stage D Dashboard | NOT_STARTED | Backend-only research/paper UI acceptance |
| C9 Windows operations | NOT_STARTED | Autostart/log/backup/restore receipts |
| C10 Failure injection | NOT_STARTED | Fail-closed fault matrix |
| C11 48-hour observation | NOT_STARTED | Actual valid 48-hour observation receipt |
| Final Windows V1 | NOT_REACHED | All gates PASS; no real-money path; trading_approval=false |
