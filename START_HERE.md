# Start here — Windows C0–C1 backend

Run commands from the project root in Windows PowerShell 5.1.

## Current state

- C0–C1: `COMPLETE`.
- Historical C1 run:
  `C1-ACCEPTANCE-20260730T145425Z-F5722878`.
- C1.1 hardening: `READY_TO_COMMIT`.
- C1.1 is offline-only hardening; not a new live-accepted baseline.
- C2: `NOT_STARTED`.
- Trading approval: `false`.

## Verify offline

```powershell
.\scripts\RUN_TESTS_SAFE.ps1
```

## Start the backend

```powershell
.\scripts\RUN_BACKEND_SAFE.ps1
```

The API is loopback-only at `http://127.0.0.1:8767`. Runtime data and the
single local run log are stored under `data\runtime`.

Stop the backend with `Ctrl+C`. Graceful shutdown stops API acceptance,
provider reconnect loops, drains and commits queued writes, closes SQLite,
and releases the Windows mutex last.

Process exit codes:

- `0`: clean stop;
- `20`: another backend instance is already running;
- `30`: frozen contract or configuration failure;
- `40`: database integrity failure.

Tasks 13–14 are complete for historical C1. Registry 47 remains
non-executable. Real orders, paper execution, wallet/private-key support and
signing remain absent. Running the backend does not approve trading.
