# Start here — Windows C0–C1 backend

Run commands from the project root in Windows PowerShell 5.1.

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

Real orders are disabled. Wallet/private-key support and paper execution are
absent. Registry 47 remains non-executable.

Tasks 13–14 are still mandatory for C1 acceptance. Starting this backend does
not establish strategy parity and does not approve trading.
