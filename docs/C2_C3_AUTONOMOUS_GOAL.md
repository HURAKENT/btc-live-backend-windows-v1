# C2-C3 Autonomous Goal

## Objective

Advance the accepted C1 walking skeleton through C2 recovery hardening and C3
daily-market rollover, ending only when all of these gates are verified:

- `C2_RECOVERY_HARDENING_PASS`;
- `C3_MARKET_ROLLOVER_PASS`;
- `BTC_LIVE_BACKEND_WINDOWS_V1_C2_C3_PASS`;
- `C2_C3_AUTONOMOUS_GOAL_COMPLETE`.

## Baseline

- Branch point: `f167847c0da271592043db750b11249aac294bc9`.
- Historical C1 accepted runtime baseline:
  `1811b587fba7c069a8aa29de17364128fc10f716`.
- Historical C1 evidence commit:
  `b7ced2649781d4af033d92e3e0de36932eb409b6`.
- C1 final reports and acceptance pack are protected historical evidence.

## C2 Definition of Done

- Persistent source cursors bound restart recovery without losing continuity.
- Stream buffering begins before recovery boundaries are captured.
- Binance closed-minute recovery proves missing/duplicate/conflict counts zero.
- Polymarket history and all current books reconcile without invented depth.
- Buffered live events drain only after authoritative replay is committed.
- Recovery incidents and lifecycle state are persisted fail-closed.
- Recovered and current reevaluations are distinct, deterministic,
  infrastructure-only, and execution-ineligible.
- Same-database restart, outbox replay, integrity, and one-writer boundaries pass.
- `reports/C2_RECOVERY_ACCEPTANCE.json` is PASS and
  `reports/C2_RECOVERY_TEST_MATRIX.md` records the deterministic evidence.

## C3 Definition of Done

- Current and next BTC Daily Range identities are deterministic and immutable.
- Discovery rejects ambiguity and malformed/incomplete 11-market/22-asset sets.
- The next subscription is prepared without duplicate active subscriptions.
- Cutover buffers events, reconciles the next market, commits its identity, and
  switches canonical projection atomically from the runtime's perspective.
- The expired market cannot regain current status after cutover.
- Restart during pre-cutover, cutover, and post-cutover is idempotent.
- Deterministic replay proves one complete rollover; no real day-long wait is
  required.
- `reports/C3_ROLLOVER_ACCEPTANCE.json` is PASS and
  `reports/C3_ROLLOVER_REPLAY_MATRIX.md` records the replay evidence.

## Aggregate Definition of Done

- Both stage reports and `reports/C2_C3_FINAL_ACCEPTANCE.json` are PASS.
- `artifacts/C2_C3_ACCEPTANCE_PACK.zip` has a verified manifest, CRC and
  recursive SHA-256 coverage and contains no raw database/runtime secrets.
- Historical C1 artifacts are byte-identical to baseline hashes.
- Full Windows offline gate, deterministic process/restart/rollover tests,
  compileall, pip check, SQLite integrity, scope and security scans pass.
- `README.md`, `START_HERE.md`, `reports/AUDIT_HANDOFF.md`, and this progress
  record describe the verified state without starting C4.
- `trading_approval=false`; order, wallet, signing, auth, paper and Registry
  execution counters remain zero.

## Stop Conditions

Work pauses only for a required physical/public-provider action that cannot be
replaced by deterministic evidence, unavailable credentials/permissions,
destructive action, frozen-contract conflict, two bounded non-progress cycles
at the same boundary, or full Goal completion.
