# MVP Hub State

Updated: 2026-08-11. Interface: `BTC_DAILY_RANGE_MVP_V1`.

## Canonical hub

- Branch: `codex/final-project-completion`.
- Worker base: `e6aa70d3442dc7f7766bc43e2b04b242572b35da`.
- Worker A accepted: `3284645d5129b0e793162ce428e1bc5532d171f8`.
- Worker B accepted with the five-share remediation in `MVP_DECISIONS.md`:
  `cf8a51b6f46b82c80d887a84899a19d4dd39688b`.
- Worker C accepted: `0b48dd9ef9a5d5acde7894fd4b7b30d0340801b6`.

## DONE

- C0-C6 remain accepted; migration v5 adds paper tables and evolves the existing
  outbox without adding a writer.
- Current Strict A input uses exactly 181 closed Binance one-minute candles,
  frozen model probabilities, structured market bounds, current books,
  five-share VWAP, public fee-schedule provenance, and CLOB price history q.
- Local paper C7 persists readiness, intent, fill, position, USD 1000 account,
  settlement and PnL with restart/idempotency constraints.
- Existing `/api/v1/bootstrap` and resumable `/ws/v1/events` expose paper state;
  the attached `/dashboard` consumes the same real backend state.
- A deterministic localhost vertical acceptance covers current input through
  persisted paper state, REST, WebSocket and dashboard rendering.

## BLOCKED, not MVP-blocking

- PF1: `BLOCKED_MISSING_PRODUCTION_MODEL_BUNDLE`.
- Full historical Data Completion: no receipt; the external DB/WAL/SHM remain
  research artifacts and are not acceptance evidence.

## Security

`trading_approval=false`; `real_orders=false`; `wallet=false`; `signing=false`;
`authenticated_CLOB_writes=false`. Only public provider reads and local paper
simulation are enabled. Recovered execution remains forbidden.

## Critical path result

`current Strict A -> local paper -> SQLite/outbox -> REST/WS -> dashboard` is
integrated. Production market conditions may correctly yield `NO_SIGNAL` or an
explicit fail-closed readiness reason; missing implementation is not used as a
successful demo condition.
