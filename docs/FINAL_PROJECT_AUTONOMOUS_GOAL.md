# BTC Daily Range Windows V1 — Final Autonomous Goal

## Terminal objective

`BTC_DAILY_RANGE_WINDOWS_V1_PROJECT_COMPLETE`

This Goal executes the remaining Windows V1 scope from C4 through C11 and
Stage D on branch `codex/final-project-completion`.

## Verified authority and baseline

- Authority package: `BTC_PROJECT_MASTER_CONTEXT_FOR_CODEX_V2.zip`.
- Package SHA-256: `0497121f93bbd549b3e9f6b8a9e76c8c1d3d411bdc9c7c71abfe98c6aa7c1855`.
- Package CRC: PASS; 58/58 `SHA256SUMS` entries and 57/57 manifest payload
  records verified.
- Verified baseline branch: `codex/c2-c3`.
- Verified baseline commit: `579289b550cff3793a74bbafba0d8e78670f0c2a`.
- Integration branch: `codex/final-project-completion`.
- Historical C1 and C2-C3 artifacts are immutable.

## Mandatory gates

- Phase 0: verified/reproduced baseline and C3 review hypotheses.
- C4: 47 exact rule sources/specs/evaluators and deterministic parity results.
- C5: 47 activation classifications, independent from implementation status.
- C6: persistent exactly-once checkpoints and recovered replay.
- C7: depth-backed paper execution, minimum five shares, restart-safe accounting.
- C8: versioned backend API and resumable outbox WebSocket.
- Stage D: backend-only research/paper dashboard.
- C9: Windows operations, autostart, logging and backup/restore.
- C10: fail-closed failure injection.
- C11: actual valid 48-hour Windows observation.

## Non-negotiable boundaries

Real-money orders, authenticated provider writes, wallet/signing, private keys,
C12/Linux migration and fabricated strategy rules are forbidden.
`trading_approval=false` remains mandatory. A disabled activation never waives
exact source, evaluator or parity work.

## Resume protocol

After context compression or session restart, read this file followed by
`docs/FINAL_PROJECT_PROGRESS.md`, the decision log, risk register, final
acceptance matrix, Strategy 47 status matrix and data-completeness status.
Continue the locked next step without asking for a routine choice.
