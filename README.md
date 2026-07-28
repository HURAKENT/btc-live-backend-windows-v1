# BTC Live Backend Windows V1

Frozen repository baseline for a Windows-first modular monolith.

- The registry contains 47 frozen strategy identities.
- Registry 47 is not yet an executable rule pack.
- Stage C0–C1 is active.
- Real orders are absent.
- Paper execution is outside C0–C1.
- The dashboard is outside C0–C1.
- Persistence uses SQLite WAL.
- Client updates use REST/WebSocket push.
- Restart recovery is mandatory.

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

Real orders remain disabled. Wallet/private-key and paper execution are
absent. Registry 47 remains non-executable. Launching the backend does not
establish strategy parity or authorize trading.

Tasks 13–14 remain mandatory before C1 acceptance can be declared.
