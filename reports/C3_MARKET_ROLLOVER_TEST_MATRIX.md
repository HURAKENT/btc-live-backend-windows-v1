# C3 Market Rollover Test Matrix

Gate: `C3_MARKET_ROLLOVER_PASS`

Implementation commit: `cec9e29255a207859006dfdffbc27aa140f273ad`

Evidence mode: deterministic offline tests and loopback Windows process
integration.

| Boundary | Evidence | Result |
|---|---|---|
| Current/next discovery | Future candidates are strictly validated, deduplicated by exact identity and ordered by resolution | PASS |
| Ambiguity handling | Equal current or next resolution and conflicting duplicate identity fail closed | PASS |
| Atomic next reconciliation | All 11 markets, 22 assets, 22 books and bounded per-asset history validate before the first next-market write | PASS |
| Invalid next | Incomplete next reconciliation persists `ROLLOVER_BLOCKED`, leaves current `LIVE_READY`, and commits zero next rows | PASS |
| Subscription migration | Next stream becomes ready while current remains active; current is cancelled only after cutover; exactly one active subscription remains | PASS |
| Projection cutover | Projector identity changes only after next identity/history/books/live evidence are complete | PASS |
| Post-cutover live ingress | Promoted next-stream events route to the writer and projection, not the retired pre-cutover buffer | PASS |
| Lifecycle | `NEXT_DISCOVERED → NEXT_BUFFERING → NEXT_RECONCILED → CUTOVER_COMMITTED → CURRENT_LIVE` is persisted | PASS |
| Same-database restart | Initial current plus next history issues 44 requests; restart adds zero and preserves two immutable identities | PASS |
| Outbox and source identity | Replay IDs are non-empty, unique and strictly increasing; duplicate natural keys remain zero | PASS |
| One writer | Rollover uses the existing runtime command queue and one SQLite writer consumer | PASS |
| Task ownership | Current, next and rollover tasks are cancelled or promoted without child-task leakage | PASS |
| Historical C1 artifacts | Both C1 JSON reports and the C1 ZIP retain their captured SHA-256 values | PASS |
| Forbidden surfaces | Public provider network, Task 14, Registry execution, auth, orders, paper fills and wallet/signing remain zero/false | PASS |

## TDD evidence

- Current/next model RED: 16 expected missing-module/interface errors.
- Runtime cutover RED: five expected constructor-interface errors for the
  missing opt-in rollover contract.
- Load-bearing process RED: rollover timeout proved that the default wait was
  incorrectly anchored to next-market expiry; GREEN anchors to current expiry.
- Independent-review RED: a post-cutover event remained in the retired buffer;
  GREEN routes promoted stream ingress through the normal writer/projection path.
- Final focused gate: 63 tests passed.
- Canonical Windows Offline launcher: process integration 11/11 passed; final
  suite 625 tests passed with one documented conditional skip; launcher exit 0.
- `compileall` and `pip check` passed.

No public provider request or Task 14 run was performed. C4 was not started.
Trading approval remains `false`.
