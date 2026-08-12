# Start here — Windows signal-only operation

Run operational commands from the canonical project root in Windows
PowerShell 5.1.

## Current state

- Product mode: `NORMAL OPERATION / SIGNAL ONLY`.
- Integration branch: `codex/final-project-completion`.
- Operational launch state: `docs/operation/OPERATIONAL_LAUNCH_STATE.md`.
- Historical C9/C10/C11 PREP acceptance is preserved.
- C11 48-hour endurance: `OPTIONAL / DEFERRED`, not a launch blocker.
- Next research task after launch: `AUTONOMOUS HISTORICAL REVALIDATION`, also
  not a launch blocker.
- Trading approval: `false`.

## Operational entry points

The canonical Task Scheduler action uses:

```powershell
.\scripts\C9_RUN_BACKEND.ps1
```

Manual foreground startup remains available for incident diagnosis:

```powershell
.\scripts\RUN_BACKEND_SAFE.ps1
```

API and Dashboard are loopback-only at `http://127.0.0.1:8767`. Runtime SQLite
and rotating logs live under `data\runtime`.

The backend is signal-only. Local paper accounting is simulation. Real orders,
wallets, signing, private keys and authenticated provider writes are absent and
prohibited.
