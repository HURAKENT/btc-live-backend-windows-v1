# Future Real C11 48-Hour Observation Contract

This contract governs the next stage. C11 PREP and its short dry-run do not
satisfy the real observation.

## Commands

Use one controller surface from the repository root:

```powershell
& .\scripts\C11_OBSERVATION.ps1 -Operation Start
& .\scripts\C11_OBSERVATION.ps1 -Operation Status -RunDirectory <run-directory>
& .\scripts\C11_OBSERVATION.ps1 -Operation Restart -RunDirectory <run-directory>
& .\scripts\C11_OBSERVATION.ps1 -Operation Stop -RunDirectory <run-directory>
```

`Start` launches only `run_windows_backend.py`, the accepted C9 launcher. The
same run ID, SQLite database, backend log, durable JSONL observation log, state,
and report skeleton remain bound for the whole observation.

## Required real-time protocol

1. Start from a clean, pushed integration commit and preserve the generated run
   directory unchanged.
2. Accumulate at least 172,800 real seconds. Synthetic clocks, prospective
   events, time acceleration, and a PREP dry-run do not count.
3. At elapsed 12 hours (after a status snapshot), execute one bounded
   `Restart`. Wait for the same run to return to health `PASS` before continuing.
4. At elapsed 24 hours (after a status snapshot), perform the one mandatory
   controlled outage: disconnect the Windows host's active network connection
   for 120 real seconds, reconnect it once, and leave the backend/observer
   running. Do not disable the observer, alter the database, or issue provider
   writes. Wait for both sources to return to `LIVE` and capture `Status`.
5. At elapsed 36 hours (after a status snapshot), execute the second bounded
   `Restart` and again wait for health `PASS`.
6. At or after 48 hours, capture `Status`, then execute `Stop` once. Preserve
   `observation_report.json`, `observation.jsonl`, `state.json`, backend logs,
   and the SQLite database/validated backup as acceptance evidence.

The network disconnect is a genuine manual boundary because the locally frozen
authority requires one simulated outage but does not authorize the controller
to mutate Windows adapters or firewall state automatically.

## PASS/BLOCKED gate

PASS requires all of the following from real observations:

- at least 48 real elapsed hours and heartbeat gaps no greater than five
  minutes;
- unattended backend operation, at least one market rollover, exactly the two
  planned restarts above, and at least one controlled source outage;
- recovery evidence for every planned restart and observed outage;
- coherent checkpoint lifecycle and observable incidents/block reasons;
- zero duplicate signal/fill violations and zero accounting violations;
- SQLite `quick_check(1)` equals `ok`, no fatal errors, and a final report
  generated from the real run;
- `trading_approval=false`, `real_orders=false`, `wallet=false`,
  `signing=false`, and `authenticated_clob_writes=false` throughout.

Any unmet item makes the machine-readable report `BLOCKED`; missing time or
events are never inferred or fabricated.
