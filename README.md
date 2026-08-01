# BTC Live Backend Windows V1

Windows-first public read-only modular monolith.

## Project status

- C0–C1: `COMPLETE`.
- Historical C1 accepted runtime baseline:
  `1811b587fba7c069a8aa29de17364128fc10f716`.
- Historical C1 evidence commit:
  `b7ced2649781d4af033d92e3e0de36932eb409b6`.
- Historical C1 run:
  `C1-ACCEPTANCE-20260730T145425Z-F5722878`.
- C1.1 hardening: `READY_TO_COMMIT`.
- C1.1 is offline-only closure hardening and is not a new live-accepted
  baseline.
- C2: `NOT_STARTED`.
- Trading approval: `false`.

The registry contains 47 frozen strategy identities, but Registry 47 remains
non-executable. Real orders, paper execution, wallet/private-key support,
signing and authentication are absent. Persistence uses SQLite WAL; client
updates use loopback REST/WebSocket push; restart recovery is mandatory.

## Safe Windows commands

Run the complete offline suite from Windows PowerShell 5.1:

```powershell
.\scripts\RUN_TESTS_SAFE.ps1
```

Start the backend:

```powershell
.\scripts\RUN_BACKEND_SAFE.ps1
```

The API binds only to `127.0.0.1:8767`. SQLite data and the local run log are
stored under `data\runtime`. Stop the process with `Ctrl+C`.

Exit codes are `0` for a clean stop, `20` when another instance owns the
Windows mutex, `30` for frozen contract/configuration failure, and `40` for
database integrity failure.

Tasks 13–14 are complete for the historical C1 baseline. Starting this
backend does not establish strategy parity and does not authorize trading.
