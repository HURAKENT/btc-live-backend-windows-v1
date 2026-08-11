# MVP to C9-C11 Release Handoff

## AUTHORITY TRANSFER

Old Manager: **ARCHIVE / MVP INTEGRATION LOG**.

New Release Manager: **SOLE ACTIVE ORCHESTRATOR for C9/C10/C11 PREP**.

Source of truth, in order:

1. Git.
2. Frozen/master contracts.
3. Accepted MVP artifacts and tests.
4. This handoff for transition facts.
5. The future compact release control plane.

## ACCEPTED MVP BASE

Read-only verification on 2026-08-11 established:

- branch: `codex/final-project-completion`;
- HEAD: `e097d9bb2ea2e03d0ca43fe7e9130cb10dbe1a90`;
- `origin/codex/final-project-completion`:
  `e097d9bb2ea2e03d0ca43fe7e9130cb10dbe1a90`;
- integration worktree: clean; no uncommitted integration changes.

Final accepted native Windows verification, not rerun during this handoff:
986 tests, 984 PASS, 2 documented SKIP, 0 FAIL, exit code 0.

## FUNCTIONAL MVP ALREADY EXISTS

The accepted system already provides current BTC/Polymarket runtime, Strict A
current evaluation, exactly 181 closed Binance candles, q, VWAP5, public fee
provenance, fail-closed readiness, exactly five-share local paper execution,
intent/fill/position/account/PnL persistence, REST bootstrap, resumable
WebSocket, Dashboard, one-writer SQLite/outbox, and restart/idempotency
semantics. C9 and C10 must not reimplement these systems.

## SECURITY INVARIANTS

Always: `trading_approval=false`, `real_orders=false`, `wallet=false`,
`signing=false`, `authenticated_CLOB_writes=false`. C9/C10/C11 PREP must not
weaken this boundary.

## C9 EXPRESS SCOPE

C9 exists only to make the accepted MVP convenient and safe to leave running
on Windows. Required minimum:

1. Reliable user-level Windows autostart; prefer Task Scheduler.
2. Reuse existing single-instance protection; create no new lock system.
3. Bounded rotating backend log.
4. SQLite online backup.
5. Cheap bounded startup `quick_check` plus schema/version validation.
6. Restore drill using a copy in a temporary location.
7. Refusal of a corrupted backup.
8. Clean shutdown/restart verification.

Non-goals: Windows Service when Task Scheduler is sufficient; installer;
updater; service-management platform; cloud backup; backup catalog;
incremental backup framework; telemetry platform; new single-instance
architecture; heavy full `integrity_check` on every startup.

## C10 EXPRESS SCOPE

C10 is an acceptance/failure-testing stage. First perform a compact failure
coverage audit and reuse C2/C3/C6/C7/C8 evidence. Do not duplicate a test only
to rename it C10. Add only missing load-bearing scenarios, with at most these
intended new failure classes:

- provider/network outage;
- SQLite busy or failed transaction;
- process kill around paper/account commit;
- process kill/restart around one lifecycle/checkpoint boundary.

Required invariants: no phantom signal or fill; no duplicate fill; no double
bankroll mutation; no corrupted partial accounting; visible incident/block
reason; successful restart/recovery when the source returns; fail-closed state.

Non-goals: Chaos Framework; Fault DSL; scenario registry platform;
dashboard-disconnect reimplementation already proved by C8; process kill at
every line; repeated malformed-provider cases already proved elsewhere.
Backup corruption belongs to C9 restore acceptance, not a C10 subsystem.

## C11 PREP SCOPE

The real 48-hour observation is explicitly outside this sprint. C11 PREP only
makes the integrated C9+C10 build ready to start that observation. Minimum:

- one bounded launch command/script;
- observation/run ID and start timestamp;
- durable run-specific log;
- health/status snapshot and source/runtime incidents;
- signal/paper activity summary;
- restart/recovery evidence if it occurs;
- stop/finalize command;
- machine-readable final observation-report skeleton;
- explicit PASS/BLOCKED criteria for the future real 48-hour run.

Do not simulate or fabricate 48 hours or generate prospective fake events.

## SPRINT WEIGHTS

`C9 EXPRESS = 30%`; `C10 EXPRESS = 40%`; `C11 PREP = 20%`;
`FINAL COMBINED ACCEPTANCE = 10%`. Total: 100%.

Progress is 0% now. 100% means technically ready to start the real 48-hour
observation; the 48-hour wait is not part of 100%.

## PROCESS AND CONTEXT BUDGET

Target approximately 35-50% of the full usage limit and preserve approximately
18-28% absolute reserve where possible. Do not attempt unsupported token
measurement. Use no giant historical context, full-suite loops, redundant
reviewer loops, broad refactoring, or rebuilding of accepted MVP systems.
Reuse evidence aggressively, use focused worker verification, and run one full
native Windows suite only at the final integrated release boundary.

## RECOMMENDED PARALLEL MODEL

Start the Release Manager plus at most two workers:

- WORKER C9: Windows Operations.
- WORKER C10: Failure Coverage / Missing Fault Tests.

Do not start C11 PREP as a third parallel worker. The Release Manager performs
C11 PREP after C9+C10 integration so it consumes their final interfaces and
results.

## OWNERSHIP PRINCIPLE

Workers prefer new/local files. Shared MVP production files remain Release
Manager/Integrator-owned. C9 must not rewrite runtime architecture. C10 must
not mutate production merely to ease injection unless it reproduces a real
P0/P1 resilience defect. Shared change requests return to the Release Manager.

## REVIEW BUDGET

Worker: narrow inspection -> coverage/TDD as applicable -> focused GREEN ->
one self-review -> handoff.

Release Manager: one boundary review -> integrate -> at most one remediation
iteration for P0/P1. P2/P3 enter the release backlog. Do not run the full suite
in workers; run one full native Windows suite at final release integration.

## DO NOT REOPEN

C0-C8 and the MVP are accepted. Do not reopen historical Data Completion or
PF1; redesign Strict A; rebuild API, Dashboard, or paper engine; revisit the
exact five-share decision; build real-order functionality; or perform unrelated
refactoring.

## EXISTING REUSABLE RESILIENCE EVIDENCE

Provider malformed/failure behavior:

- `tests/test_binance_provider.py`;
- `tests/test_polymarket_provider.py`;
- `tests/test_runtime_real_provider_seams.py`;
- `tests/test_runtime_core_adversarial.py`.

Recovery/restart:

- `tests/test_recovery.py`;
- `tests/test_c2_recovery_hardening.py`;
- `tests/test_runtime_restart_identity.py`;
- `tests/test_runtime_core_adversarial.py`;
- `reports/C2_RECOVERY_ACCEPTANCE.json`;
- `reports/C2_RECOVERY_TEST_MATRIX.md`.

Rollover:

- `tests/test_market_rollover.py`;
- `tests/test_phase0_rollover_recovery.py`;
- `tests/test_c3_market_rollover_acceptance.py`;
- `reports/C3_MARKET_ROLLOVER_ACCEPTANCE.json`;
- `reports/C3_MARKET_ROLLOVER_TEST_MATRIX.md`;
- `reports/C2_C3_FINAL_ACCEPTANCE.json`.

Scheduler exactly-once/replay:

- `tests/test_checkpoint_scheduler.py`;
- `tests/test_checkpoint_c6_acceptance.py`;
- `tests/test_runtime_orchestrator.py`;
- `reports/C6_CHECKPOINT_SCHEDULER_ACCEPTANCE.json`.

Paper restart/idempotency:

- `tests/test_paper.py`;
- `tests/test_mvp_paper_storage.py`;
- `tests/test_mvp_vertical.py`.

WebSocket reconnect:

- `tests/test_canary_api.py`;
- `tests/test_mvp_api_dashboard.py`.

Storage transaction/outbox behavior:

- `tests/test_storage_outbox.py`;
- `tests/test_runtime_persistence.py`;
- `tests/test_checkpoint_scheduler.py`.

## DURABLE WORKTREE SNAPSHOT

MVP worker branches and clean linked worktrees remain available:

- `codex/mvp-input` at `3284645d5129b0e793162ce428e1bc5532d171f8`;
- `codex/mvp-paper` at `cf8a51b6f46b82c80d887a84899a19d4dd39688b`;
- `codex/mvp-dashboard` at `0b48dd9ef9a5d5acde7894fd4b7b30d0340801b6`.

Each local worker HEAD equals its origin-tracking HEAD. Preserve old worktrees.
`codex/data-inventory` remains intentionally dirty with
`src/data_completion_acceptance.py` and
`tests/test_data_completion_acceptance.py`; these unproven changes are not an
MVP or release source and must not be absorbed. Other preserved legacy
worktrees include C4, C5, C6, Data Completion, and Phase 0 worktrees. One
prunable `/tmp/btc-final-phase0` registration exists; do not clean it during
release work. A quick process-name scan found no active backend process.

## KNOWN WINDOWS ENVIRONMENT NOTES

- WSL-to-Windows invocation can fail with the known vsock/interoperability
  error; native Windows `.venv` is final authority for Windows acceptance.
- Windows Git/Credential Manager is the established push fallback when WSL Git
  cannot obtain credentials.
- The repository enforces `core.autocrlf=true`, `core.safecrlf=true`, with
  `.gitattributes` exceptions; preserve its CRLF/LF policy.
- Prefer user-level operation wherever practical.

## POST-SPRINT

After C9+C10+C11 PREP reaches 100%, next is the real C11 48-hour observation.
Not included now: PF1 recovery, historical receipt hardening, unrelated UI
polish, real-money trading, C12, or new features.
