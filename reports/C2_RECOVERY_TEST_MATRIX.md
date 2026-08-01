# C2 Recovery Test Matrix

Gate: `C2_RECOVERY_HARDENING_PASS`

Implementation commit: `d772428844dc65ca47f5a147bfc75a2e51c2b706`

Evidence mode: deterministic offline tests and loopback process integration.

| Boundary | Evidence | Result |
|---|---|---|
| Persistent cursor bounds | Binance plan validates aligned persisted cursor evidence; Polymarket history cursor validates source, timestamp, natural key and future boundary | PASS |
| Stream-first recovery | Provider streams enter bounded buffering before authoritative replay and cutover | PASS |
| Binance completeness | Expected/recovered set reconciliation reports zero missing minutes and zero duplicates after deduplication | PASS |
| Polymarket 11/22 | One identity, 11 markets, 22 assets, 22 current books and all 22 history requests complete | PASS |
| History/current-book separation | `polymarket-history:<asset>` cursors cannot be suppressed by newer current-book observations | PASS |
| Atomic per-asset history | A wrong-asset event rejects the adapter result before the orchestrator enqueues any history event | PASS |
| Buffered drain | Accepted buffered events drain in deterministic order only after authoritative replay | PASS |
| Recovered/current evaluations | Both persisted origins are distinct and execution-ineligible; evidence is queried from SQLite | PASS |
| Same-database restart | Restart reuses persisted Binance/history cursors; zero additional history requests; no natural-key conflicts | PASS |
| Outbox replay | IDs are non-empty, unique and strictly increasing across the loopback process/restart gate | PASS |
| One writer | All new incident/source/cursor/snapshot/evaluation writes remain owned by the existing writer consumer | PASS |
| SQLite contract | Migration version 2, 9 tables, quick/integrity `ok`, WAL, synchronous 2, foreign keys 1 | PASS |
| Historical C1 artifacts | Both canonical JSON reports and the C1 ZIP retain their captured SHA-256 values | PASS |
| Forbidden surfaces | Provider network, Task 14, Registry execution, auth, orders, paper fills and wallet/signing all remain zero/false | PASS |

## TDD evidence

- Initial recovery-contract RED: 12 expected missing-interface errors for the
  recovery plan, per-asset history bounds and cursor namespace.
- Completeness-ledger RED: five expected missing summary/runtime-evidence
  errors.
- Independent-review RED: six cursor/evidence failures proved that history
  cursor fields and runtime duplicate/evaluation facts were not yet read
  fail-closed; one additional RED restored the inverted-range rejection.
- Final focused gate: 129 tests passed.
- Canonical Windows Offline launcher: process integration 11/11 passed; full
  suite 591 tests passed with one documented conditional skip; launcher exit 0.
- `compileall` and `pip check` passed.

No public provider request or Task 14 run was performed. C3 was not started by
this C2 report. Trading approval remains `false`.
