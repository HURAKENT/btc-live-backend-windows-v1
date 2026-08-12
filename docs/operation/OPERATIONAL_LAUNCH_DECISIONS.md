# Operational Launch Decisions

1. The active product mode is `NORMAL OPERATION / SIGNAL ONLY`. A legitimate
   no-signal evaluation is healthy behavior.
2. The historical C5 matrix remains immutable. Operational scheduling is a
   separate projection and may include only identities supported by the current
   production evaluator.
3. PF1 reconstruction is prohibited. Unsupported PF1 remains historical or
   research metadata and cannot leave operational schedules pending.
4. A checkpoint is live only when evaluated within its operational time
   boundary. A missed checkpoint is recorded or authoritatively reconstructed
   as non-live history; it cannot become a fresh current signal.
5. Restart recovery and surviving-process reconnect recovery are distinct.
   Reconnect must mark the source non-live, recover from persisted cursor,
   reconcile and deduplicate, then restore live health.
6. A source/API limitation that prevents full recent-gap recovery must create
   an explicit unrecoverable-gap incident. It cannot be reported as reconciled.
7. A fatal unrecoverable runtime state must persist a critical incident and
   terminate the backend non-zero. Task Scheduler owns bounded restart policy;
   no Windows Service is introduced.
8. `CONNECTING`, disconnected, stale, and recovery-in-progress sources are not
   `LIVE`. Health exposes the current block or incident reason.
9. Dashboard Strict A selection requires the canonical specialized signal type,
   not only a Strict A strategy ID.
10. The 48-hour C11 protocol is optional and deferred. Historical evidence is
    preserved, but current authority documents must not route operation back to
    it as a blocking next gate.
11. Worker A exclusively owns scheduler and signal-time semantics. Worker B
    exclusively owns provider recovery, liveness, and runtime fatal propagation.
    `src/runtime_orchestrator.py` belongs only to Worker B.
12. `src/storage.py`, current authority documents, Dashboard/API selection, and
    `scripts/C9_TASK_SCHEDULER.ps1` are Manager-only. A worker needing any other
    file returns `SHARED_CHANGE_REQUEST` before editing it.
13. Worker branches are `codex/operation-scheduler` and
    `codex/operation-recovery`, both from the bootstrap commit containing this
    file. No additional workers or reviewer agents are authorized.
14. Bootstrap performs no production fix, no permanent launch, and no full
    suite. After integration, the full native Windows suite runs exactly once.
15. The final operational gate requires a validated SQLite backup, verified
    canonical user Task Scheduler registration and restart settings, clean and
    pushed integration HEAD, observable API/Dashboard health, advancing
    scheduler/evaluation loop, and the backend left running.
16. Security is invariant: `trading_approval=false`, `real_orders=false`,
    `wallet=false`, `signing=false`, `authenticated_CLOB_writes=false`.
