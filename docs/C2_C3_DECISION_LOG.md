# C2-C3 Decision Log

## D-001 — Preserve the C1 schema and one-writer boundary

Date: 2026-08-01

Decision: C2 and C3 will use the existing source cursor, market catalog,
incident, canonical, evaluation, signal and outbox tables. Every new runtime
write remains a command consumed by the existing single writer task.

Evidence: migration version 2 already represents immutable source events,
per-source cursors, multiple immutable market identities, lifecycle/incident
records and canonical/evaluation evidence. No required C2/C3 fact is inherently
relationally impossible in those tables.

Rejected: a new recovery/rollover table and migration. It adds backup/restart
risk without a proven data-model gap. A migration may be reconsidered only
after a specific failing persistence/restart test and a new decision entry.

## D-002 — Extend orchestration with bounded pure contracts

Date: 2026-08-01

Decision: add pure recovery-completeness and market-lifecycle contracts, then
compose them through the existing orchestrator. Provider adapters continue to
own provider I/O; the projector owns current canonical state; the orchestrator
owns task lifecycle and write serialization.

Alternatives considered:

1. Expand the current `start()` method inline. Rejected because recovery and
   rollover failure boundaries would become harder to test and audit.
2. Add a generic workflow/ORM framework. Rejected as unnecessary architecture.
3. Add focused immutable plans/results plus a rollover controller integrated
   by the orchestrator. Selected because boundaries remain explicit and pure.

## D-003 — Persist rollover evidence through existing seams

Date: 2026-08-01

Decision: immutable identities stay in `market_catalog`; current/next/cutover
state is emitted as append-only incidents with deterministic keys; source
progress stays in `source_cursors`. The current identity remains derivable from
the latest successfully committed cutover evidence and canonical snapshot.

This is not a duplicate lifecycle state machine: startup remains governed by
`LifecycleStateMachine`; rollover has a separate bounded state contract.

## D-004 — Deterministic replay is the C3 acceptance authority

Date: 2026-08-01

Decision: the load-bearing C3 gate uses synthetic identities, loopback provider
servers, controlled time and same-database restarts. It must exercise the real
provider parsers/adapters/orchestrator/storage path. No real daily wait is
authorized. Official provider documentation will be checked immediately before
release verification for endpoint/schema drift.

## D-005 — Trading boundary remains closed

Date: 2026-08-01

Decision: all evaluations remain infrastructure-only and execution-ineligible.
No order, wallet, signing, auth, paper, Registry execution, or C4 surface is
introduced. `trading_approval=false` is mandatory in every report.
