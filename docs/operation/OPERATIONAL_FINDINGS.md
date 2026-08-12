# Operational Findings

Bootstrap base inspected: `263c344dda6b90720b4fd390c7308d856bd8f398`.
All reproductions were deterministic and local/native Windows; no public live
data was required. No production file was changed.

## Classification

| ID | Status | Deterministic evidence | Root cause | Owner |
|---|---|---|---|---|
| P0-A | CONFIRMED | `load_c5_scheduler_contract()` returns 8 enabled identities: 5 PF1 and 3 Strict A. `StrictACurrentInputSource.capture()` rejects PF1 with `BLOCKED_MISSING_PRODUCTION_MODEL_BUNDLE`; `release_unavailable()` returns it to `PENDING`; old-market pending work triggers `STALE_MARKET_STRATEGY_GUARD`. | Operational eligibility is copied directly from frozen C5 instead of intersecting with the current production evaluator set. | Worker A |
| P0-B | CONFIRMED | A T-60 Strict A schedule was released for `PROVIDER_OFFLINE`, reclaimed 20 minutes late with the original recovery cutoff, and returned `origin=LIVE`. | `claim_due()` derives origin only from the fixed startup cutoff and has no on-time/expiry boundary for a failed live claim. | Worker A |
| P0-C | CONFIRMED | Restart path uses persisted Binance cursor and startup backfill. Binance and Polymarket stream loops reconnect internally after disconnect, but invoke no adapter/orchestrator cursor-gap recovery before continuing. | Startup reconciliation exists; surviving-process reconnect is only WebSocket reconnect and bypasses bounded persisted-cursor recovery. | Worker B |
| P1-D | CONFIRMED | With cursor-next `180`, floor `7200`, and end `10800`, `resolve_polymarket_history_starts()` returns `7200`, silently omitting `7020` seconds. Default runtime passes `history_start_ts=max(0, now-3600)`. | The one-hour floor overrides an older persisted per-asset cursor and the recovery summary treats completed requests as completion without representing the omitted interval. | Worker B |
| P1-E | CONFIRMED | A deterministic lifecycle probe set backend fatal after successful start; `run_backend()` remained blocked waiting only for an OS shutdown signal. Task Scheduler settings contain no restart count/interval. | Fatal state is internal to the orchestrator; top-level lifecycle does not await or propagate runtime termination, so the process can remain alive. | Worker B; Task Scheduler setting change is Manager-only |
| P1-F | CONFIRMED | Provider disconnect tests show reconnect loops and transient callback-only incidents. Orchestrator health changes to `LIVE` on an accepted event and to `FAILED` only when its outer provider task exits; the internal reconnect loop does not exit. No event-age freshness threshold exists. | Disconnect/recovery state and last-event age are not fed into the runtime health state machine; production stream constructors do not wire durable incident sinks. | Worker B |
| P1-G | CONFIRMED | After inserting older `STRICT_A_SIGNAL_V1` and newer `STRATEGY_EVALUATION_SIGNAL` rows for `YES_STRICT_A_OPERATIONAL`, `latest_strict_a_signal()` selected the generic newer row. | The query filters strategy IDs but not canonical `signal_type`. | Manager |
| P1-H | CONFIRMED | `AGENTS.md`, `docs/FINAL_PROJECT_AUTONOMOUS_GOAL.md`, `docs/FINAL_PROJECT_PROGRESS.md`, `docs/release/RELEASE_STATE.md`, and the final release receipt still route work toward C11/Data Completion/48h as the next blocking workflow; `START_HERE.md` and `README.md` also report obsolete C1/C2 state. | Current launch authority was not updated after the operational-mode decision. | Manager |

## Reproduction receipts

Focused native Windows command: eight selected tests covering C5 enabled set,
PF1 rejection, pending release, stale-market guard, restart cursor recovery,
both provider reconnect paths, and backfill/live health separation. Result:
`8 tests`, `8 PASS`, `0 FAIL`, exit code `0`, elapsed `1.899s`.

One-off native Windows bootstrap probe results:

- A: enabled `8`; PF1 `5`; Strict A `3`.
- B: T-60 retry `20` minutes late remained `LIVE`.
- D: `7200 - 180 = 7020` seconds silently omitted by the floor.
- E: fatal state true while `run_backend()` was still waiting.
- G: selected type `STRATEGY_EVALUATION_SIGNAL` instead of
  `STRICT_A_SIGNAL_V1`.

Source anchors: `src/checkpoint_scheduler.py:106-193,589-711,819-850`;
`src/mvp_current_runtime.py:27-29,88-97`;
`src/recovery.py:217-286`; `src/runtime_adapters.py:90-136,410-516`;
`src/runtime_orchestrator.py:481-700,845-970,1301-1339,1733-1756,2242-2389`;
`src/binance_provider.py:320-357`; `src/polymarket_provider.py:804-884`;
`src/app.py:335-357`; `src/storage.py:1527-1537`;
`scripts/C9_TASK_SCHEDULER.ps1:25-49,80-110,139-149`.

P0/P1 not reproduced: none. External-live-data-only findings: none.
