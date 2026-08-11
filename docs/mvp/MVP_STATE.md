# MVP Hub State

Updated: 2026-08-11. Interface: `BTC_DAILY_RANGE_MVP_V1`.

## Canonical hub

- Integration branch: `codex/final-project-completion`.
- HUB base HEAD: `b047cb182267e989845926264a02607605531b0f`.
- Remote verification at bootstrap start: local HEAD and remote branch both
  resolved to the HUB base HEAD; integration worktree was clean.
- Legacy authority: `docs/mvp/HANDOFF_FROM_MONOLITH.md`, only where a higher
  priority Git, frozen-contract, or acceptance source is not more direct.

## Status

DONE:

- C0-C6 remain accepted; do not reopen without a new reproducible production
  regression.
- Existing SQLite WAL, one-writer path, transactional outbox, REST bootstrap,
  and resumable WebSocket path are the integration base.
- Migration v4 is canonical. The two stale migration-v3 assertions were freshly
  reproduced and corrected to the v4 schema contract during HUB bootstrap.

ACTIVE after `HUB_READY`:

1. Worker A: bounded current Strict A checkpoint input.
2. Worker B: local paper intent/fill/position/account persistence.
3. Worker C: minimal dashboard consumer against this frozen interface.
4. Manager: boundary review, shared wiring, integrated focused gates, then one
   fresh full Windows suite.

BLOCKED but not MVP-blocking:

- PF1: `BLOCKED_MISSING_PRODUCTION_MODEL_BUNDLE`.
- Full historical Data Completion: no final receipt; external DB/WAL/SHM remain
  research artifacts only.

## Security state

`trading_approval=false`; `real_orders=false`; `wallet=false`; `signing=false`;
`authenticated_CLOB_writes=false`. Paper execution is local simulation only.

## Critical path

`Strict A current input -> current signal -> local paper state -> Manager REST/WS
extension -> real-state dashboard -> integrated Windows verification`.

Historical Data Completion and PF1 are not on this path. Recovered historical
execution is forbidden.

## Worker branches and ownership

| Worker | Branch | Owned production files | Owned tests |
|---|---|---|---|
| A / CURRENT INPUT | `codex/mvp-input` | `src/current_input.py` | `tests/test_current_input.py` |
| B / PAPER | `codex/mvp-paper` | `src/paper.py`, `migrations/0005_paper.sql` | `tests/test_paper.py` |
| C / DASHBOARD | `codex/mvp-dashboard` | `src/dashboard.py`, `src/dashboard_static/**` | `tests/test_dashboard.py` |

Manager-only shared files: `src/models.py`, `src/storage.py`, `src/api.py`,
`src/runtime_orchestrator.py`, `src/checkpoint_scheduler.py`, `src/config.py`,
`src/app.py`, and existing provider modules. A worker needing one returns a
`SHARED_CHANGE_REQUEST`; it does not edit the shared file or change the frozen
interface.

## Accepted handoffs

No MVP worker handoff is accepted at HUB bootstrap. Acceptance requires the
handoff template, focused GREEN evidence, no P0/P1 finding, and Manager boundary
review. P2/P3 findings go to `MVP_BACKLOG.md`.

## Stage packets

All workers start from the later `HUB_READY` bootstrap commit, not from the
pre-bootstrap HUB base above. The Manager publishes that full SHA in the final
handoff block.

### Worker A — CURRENT INPUT

- ROLE: bounded current-input producer.
- BASE_COMMIT: the commit containing this document; it must equal the full
  `HUB_READY` SHA published by the Manager.
- GOAL: 181 closed Binance BTCUSDT 1m candles plus frozen Strict A model,
  current Polymarket market/books, five-share VWAP, and fee provenance into
  `V1ExecutableCheckpointInput` and `CurrentExecutionEvidenceV1`.
- INTERFACES CONSUMED: existing providers/`MarketBook`; `BTC_DAILY_RANGE_MVP_V1`.
- INTERFACES PROVIDED: deterministic current capture result; no shared wiring.
- ALLOWED FILES: Worker A ownership row only.
- FORBIDDEN SHARED FILES: all Manager-only files above.
- NON-GOALS: historical DB/import, PF1, orderbook redesign, paper execution.
- SECURITY: public reads only; no auth writes, wallet, signing, or orders.
- ACCEPTANCE TESTS: focused RED/GREEN for closed-candle count, deterministic
  probabilities, exact five-share VWAP, fee provenance, stale/missing fail-close.
- HANDOFF FORMAT: `WORKER_HANDOFF_TEMPLATE.md`; integration order 1.

### Worker B — PAPER

- ROLE: restart-safe local paper ledger.
- BASE_COMMIT: the commit containing this document; it must equal the full
  `HUB_READY` SHA published by the Manager.
- GOAL: Strict A signal to idempotent intent, fill/partial/blocked result,
  position, settlement, PnL, and USD 1000 account at exactly five shares.
- INTERFACES CONSUMED: `StrictASignalV1`, `CurrentExecutionEvidenceV1` fixtures.
- INTERFACES PROVIDED: paper contracts and migration; no shared wiring.
- ALLOWED FILES: Worker B ownership row only.
- FORBIDDEN SHARED FILES: all Manager-only files above.
- NON-GOALS: recovered execution, optimizer/Kelly, router, TP/SL, real orders.
- SECURITY: local simulation only; recovered origin must fail closed.
- ACCEPTANCE TESTS: focused RED/GREEN for one logical execution/date, T60/T30
  policy, exact/partial/blocked fills, restart idempotency, settlement and PnL.
- HANDOFF FORMAT: `WORKER_HANDOFF_TEMPLATE.md`; integration order 2.

### Worker C — DASHBOARD

- ROLE: thin aiohttp dashboard consumer.
- BASE_COMMIT: the commit containing this document; it must equal the full
  `HUB_READY` SHA published by the Manager.
- GOAL: `/dashboard` HTML/CSS/JS showing real bootstrap state and resumable WS
  updates for all fields frozen in `DashboardBootstrapV1`.
- INTERFACES CONSUMED: `DashboardBootstrapV1` and existing WS envelope.
- INTERFACES PROVIDED: static handler/assets attachable by Manager.
- ALLOWED FILES: Worker C ownership row only.
- FORBIDDEN SHARED FILES: all Manager-only files above.
- NON-GOALS: React/Vite, router, state framework, design system, multi-page UI.
- SECURITY: localhost backend only; no provider or trading calls from browser.
- ACCEPTANCE TESTS: isolated UI fixture tests plus aiohttp handler/assets test;
  mock-only evidence is not integrated MVP acceptance.
- HANDOFF FORMAT: `WORKER_HANDOFF_TEMPLATE.md`; integration order 3 after paper
  API seams are available.
