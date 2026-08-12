# BTC Live Backend Windows V1

Windows-first, public read-only BTC Daily Range signal backend with local paper
simulation.

## Current operating mode

`NORMAL OPERATION / SIGNAL ONLY`

The backend is deployed through the existing user-level Windows Task Scheduler
path, exposes its API and Dashboard only on `127.0.0.1:8767`, and persists
runtime state in SQLite WAL under `data/runtime`.

Operational recovery is cursor-driven: a disconnected or stale source is not
live, recent gaps are reconciled before returning to `LIVE`, and unrecoverable
gaps are durable fatal incidents so bounded Task Scheduler restart can occur.
Missed/recovered checkpoints cannot become fresh live signals. A legitimate
`NO_SIGNAL` evaluation is normal healthy behavior.

Current authority:

- `START_HERE.md`;
- `docs/operation/OPERATIONAL_LAUNCH_STATE.md`;
- `docs/operation/OPERATIONAL_LAUNCH_DECISIONS.md`;
- `docs/release/RELEASE_STATE.md`.

Historical C0-C11 evidence is retained. The historical 48-hour C11 protocol is
`OPTIONAL / DEFERRED ENDURANCE TEST`, not a blocking operational gate. The next
separate research task is `AUTONOMOUS HISTORICAL REVALIDATION`; it is not a
prerequisite for signal-only operation.

Security is invariant: `trading_approval=false`, `real_orders=false`,
`wallet=false`, `signing=false`, and `authenticated_CLOB_writes=false`.
