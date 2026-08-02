# BTC Daily Range Windows V1 — Final Project Completion Plan

> Authority: hash-verified `BTC_PROJECT_MASTER_CONTEXT_FOR_CODEX_V2.zip`.
> Integration branch: `codex/final-project-completion`.

## Global constraints

- Preserve accepted C1 artifacts and historical C2/C3 reports byte-for-byte.
- Use root cause → RED → minimal GREEN → regression for every production fix.
- Keep one SQLite writer and append-only/idempotent evidence semantics.
- All 47 strategy identities require an exact verified source, immutable rule
  spec, evaluator and parity result; activation status is a separate decision.
- Never infer Strict A/PF1 rules from identity names or aggregate results.
- Paper execution requires contemporaneous depth evidence and at least five
  shares. Real orders, authenticated writes, wallet/signing and private keys
  are forbidden. `trading_approval=false` is invariant.
- C11 requires 48 actual valid Windows hours; simulated time may test the
  observer but cannot satisfy the gate.
- C12/Linux migration is out of scope.
- Each release train ends with focused tests, full offline tests, compileall,
  pip check, scope/security audit, independent review, commit and push.

## Task 1 — Phase 0: accepted-baseline audit and reproduced corrections

1. Record package, Git, C1/C2/C3 artifact and baseline offline verification.
2. Reproduce or reject recurring A→B→C rollover, crash/restart at every
   durable cutover boundary and C2/C3 receipt-linkage hypotheses.
3. For each reproduced production defect, write the smallest real-path RED,
   implement the minimal fix and run rollover/recovery/process regressions.
4. Add generated command receipts and immutable hash linkage for new Phase 0
   evidence without rewriting historical C2/C3 assertions.
5. Publish a machine-readable accepted-baseline report and Phase 0 pack.

## Task 2 — Train A: C4 source recovery and executable parity

1. Execute the bounded local source-discovery protocol and hash all candidates.
2. Resolve exact source lineage for 34 V1 and 13 V2 identities; park no identity
   as executable while its rule is unknown.
3. Define immutable input schemas and rule specs for all 47 identities.
4. Implement deterministic, lookahead-free evaluators and historical data
   import/completion with append/backfill/reconcile/future-market support.
5. Produce trade-level identity/count/W-L/PnL/ROI parity and rule hashes.
6. Gate only when 47/47 are rule-audited and evaluator-implemented.

## Task 3 — Train B: C5 activation and C6 scheduler/replay

1. Classify all 47 activation states with machine-readable reasons; keep V2
   research-only unless the approved policy and evidence explicitly permit more.
2. Persist horizon checkpoints and exactly-once evaluation identities.
3. Replay missed checkpoints after downtime with recovered/live origin retained.
4. Prevent duplicate signals across restart and schedule recurring future daily
   markets indefinitely.

## Task 4 — Train C: C7 paper execution

1. Add versioned persistence for intents, fills, positions, settlement,
   bankroll and PnL only after schema necessity is proven by restart RED.
2. Fill at least five shares only against contemporaneous bounded depth evidence.
3. Prove partial/no-fill, idempotent replay, restart-safe accounting and
   settlement reconciliation. Keep all real-money surfaces absent.

## Task 5 — Train C: C8 API and Stage D

1. Add stable versioned REST bootstrap and resumable event-id WebSocket.
2. Implement the approved research/paper dashboard using backend data only.
3. Prove reconnect replay, UI-closed backend operation and absence of provider or
   decision logic in the dashboard.

## Task 6 — Train D: C9 Windows operations

1. Add reversible Windows autostart/service control with single-instance safety.
2. Add bounded log rotation, backup/restore and crash-safe integrity startup.
3. Execute Windows offline Task Scheduler/service and restore drills.

## Task 7 — Train D: C10 failure injection

1. Inject provider disconnect/malformed payload, network outage, SQLite busy and
   failed transaction, corrupt-copy restore and process kills at recovery,
   rollover, scheduler, paper fill and settlement boundaries.
2. Prove fail-closed behavior, no phantom signal/fill and visible incidents.
3. Exercise dashboard disconnect/reconnect. Stop for architecture review after
   three material failed fixes at one boundary.

## Task 8 — Train E: C11 durable 48-hour observation

1. Build and offline-test a durable single-observer controller with immutable
   receipts, heartbeat, monotonic validity and resume-safe state.
2. Start only after C4–C10 and Stage D PASS.
3. Accumulate 48 actual valid Windows hours, at least one rollover, two planned
   backend restarts and one simulated network outage.
4. Require SQLite integrity, no missing closed minute, no duplicate signal/fill,
   classified gaps, visible strategies/accounting and absent real-money paths.

## Task 9 — Final closure

1. Generate the final acceptance matrix, 47-status matrix, provenance/data,
   paper, API/dashboard, Windows operations, failure-injection, observation and
   handoff reports from command receipts.
2. Build and verify the final SHA256SUMS acceptance pack, CRC, traversal,
   duplicate, secret and absolute-path gates.
3. Run final independent whole-branch review and fresh verification.
4. Push a clean branch with `HEAD==origin`; mark the durable Goal complete only
   at `BTC_DAILY_RANGE_WINDOWS_V1_PROJECT_COMPLETE`.
