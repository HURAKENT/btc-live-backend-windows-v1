# BTC Live Backend Windows V1

Windows-first, public read-only BTC Daily Range signal backend with local paper
simulation.

## Current operating mode

`NORMAL OPERATION / SIGNAL ONLY`

The backend is deployed through the existing user-level Windows Task Scheduler
path, exposes its API and Dashboard only on `127.0.0.1:8767`, and persists
runtime state in SQLite WAL under one external local data root. The Windows
default is `%USERPROFILE%\Documents\BTC Daily Range`; Linux uses absolute
`$XDG_DATA_HOME/btc_daily_range` or falls back to
`~/.local/share/btc_daily_range`. `--data-root` overrides
`BTC_DAILY_RANGE_DATA_ROOT`, which overrides the OS default. Active data roots
must be absolute local paths; Windows UNC, cloud-sync, and shared active SQLite
paths are unsupported.

The shared path contract derives `runtime/btc_daily_range.sqlite3`, `backups`,
`logs`, and `diagnostics` from that root. The legacy absolute
`--database-path` remains supported. When it is combined with a CLI or
environment data root, it must equal the derived database path or startup exits
with `DATA_ROOT_DATABASE_PATH_CONFLICT`.

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

Safe local entry points remain:

```powershell
.\scripts\RUN_TESTS_SAFE.ps1
.\scripts\RUN_BACKEND_SAFE.ps1
```

Historical C0–C1 is `COMPLETE`; the accepted Tasks 13–14 evidence remains
preserved. Registry 47 stays available to historical/research replay and is
separate from the production operational scheduler projection.

Historical C0-C11 evidence is retained. The historical 48-hour C11 protocol is
`OPTIONAL / DEFERRED ENDURANCE TEST`, not a blocking operational gate. The next
separate research task is `AUTONOMOUS HISTORICAL REVALIDATION`; it is not a
prerequisite for signal-only operation.

Security is invariant: `trading_approval=false`, `real_orders=false`,
`wallet=false`, `signing=false`, and `authenticated_CLOB_writes=false`.
Trading approval remains false in every operating and research mode.
