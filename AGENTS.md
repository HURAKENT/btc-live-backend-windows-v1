# Windows V1 Operational Signal-Only Contract

## Current objective

Codex is the Operational Launch Manager for `btc_live_backend_windows_v1` on
branch `codex/final-project-completion`. The active mode is:

`NORMAL OPERATION / SIGNAL ONLY`

The accepted backend must run permanently on Windows through the existing
user-level Task Scheduler design. Normal lifecycle is:

`NORMAL OPERATION -> INCIDENT -> NARROW FIX -> CONTINUE OPERATION`

Durable current authority lives in `docs/operation/OPERATIONAL_LAUNCH_STATE.md`,
`docs/operation/OPERATIONAL_LAUNCH_DECISIONS.md`, and
`docs/release/RELEASE_STATE.md`.

## Operational rules

- Current live scheduling includes only strategies with the current production
  evaluator. Frozen C5 classification and historical 47-strategy evaluators
  remain immutable and separate from the operational projection.
- A missed or recovered checkpoint is non-live evidence and must never be
  presented as a fresh current signal.
- Disconnect or staleness is non-live. Reconnect requires bounded cursor-driven
  recovery, reconciliation and deduplication before returning to `LIVE`.
- An unrecoverable operational gap is a durable incident and fatal runtime exit;
  user-level Task Scheduler applies bounded restart policy.
- Zero trading signals is valid when current sources, checkpoints and Strict A
  evaluation are healthy and legitimate no-signal reasons are recorded.
- Preserve one SQLite writer and append-only/idempotent provenance.
- Use narrow reproduction, RED, minimal GREEN and focused regression. Run the
  full native Windows suite only at an explicitly authorized release boundary.

## Security invariants

`trading_approval=false`, `real_orders=false`, `wallet=false`, `signing=false`,
and `authenticated_CLOB_writes=false`.

Real-money orders, wallets, private keys, signing, authenticated provider
writes, PF1 production reconstruction, Windows Service work, C12/Linux
migration, force-push, changes to `main`, and Git configuration changes are
forbidden.

## Historical and research boundary

Historical C0-C11 evidence remains preserved. The 48-hour C11 protocol is
`OPTIONAL / DEFERRED ENDURANCE TEST`; it is not a launch or normal-operation
gate and must not be started routinely.

After launch, the next separate non-blocking research task is
`AUTONOMOUS HISTORICAL REVALIDATION`. It does not authorize Data Completion,
historical replay, or PF1 production work during operational launch.

## Completion and continuation

Operational launch reaches `100/100` only after corrections pass, one fresh
full native Windows suite is green, a validated SQLite backup exists, canonical
Task Scheduler registration and bounded restart policy are verified, the API
and Dashboard are observable, and the backend is left running.

Stop only for a genuine credential/manual/destructive boundary, an unresolved
P0/P1 after the authorized bounded remediation, or verified completion.
