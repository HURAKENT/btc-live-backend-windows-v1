# Final Project Decision Log

## D-001 — Preserve accepted C1–C3 history

Date: 2026-08-02

Decision: create `codex/final-project-completion` from exact verified commit
`579289b550cff3793a74bbafba0d8e78670f0c2a`. Previous accepted branches and
canonical C1/C2/C3 reports/packs remain immutable.

## D-002 — Package V2 is the remaining-scope authority

Date: 2026-08-02

Decision: use the hash-verified V2 package as the approved product/engineering
specification for C4–C11 and Stage D, subordinate to the explicit forbidden
financial/security boundaries. The older C2-C3-only operating contract does not
limit this newer direct authorization.

## D-003 — No inferred Strict A/PF1 selector

Date: 2026-08-02

Decision: aggregate metrics, identity names and compact recipes are not enough
to implement Strict A/PF1. Exact local sources must be located by basename and
SHA-256 under the bounded source-discovery protocol.

## D-004 — Implementation and activation are independent

Date: 2026-08-02

Decision: every one of 47 identities must reach an exact rule spec, evaluator
and parity result. Research-only or failed strategies may remain activation-
disabled but cannot remain unimplemented or unsearched.

## D-005 — Real time is required for C11

Date: 2026-08-02

Decision: C11 uses a durable observer and 48 actual valid hours. Synthetic time
may test the controller but cannot satisfy the observation gate.

## D-006 — Source discovery is not C4 completion

Date: 2026-08-02

Decision: the bounded local search located hash-matching exact source lineage for
34 V1 and 13 V2 identities. This closes the missing-source search risk only.
No identity advances to `RULE_SPEC_FROZEN`, `EVALUATOR_IMPLEMENTED` or a parity
status until the minimum verified artifacts are copied, specified and replayed
inside the backend repository. Ancillary manifest mismatches are preserved as
risks and are never silently substituted.

## D-007 — Phase 0 replaces provisional C2/C3 hardening evidence

Date: 2026-08-08

Decision: accept the reproduced recurring-rollover, durable-cutover and stream-
liveness corrections only through the receipt-linked Phase 0 report and pack at
evidence commit `b435e0e`. Historical C1/C2/C3 reports remain byte-immutable;
their product gates are revalidated by new evidence rather than rewritten.

## D-008 — Evidence output is sanitized but raw-output committed

Date: 2026-08-08

Decision: stored command output replaces exact project-root and user-home
prefixes with fixed placeholders. The receipt binds raw per-stream SHA-256,
stored per-stream SHA-256, replacement counters and aggregate commitments.
Any residual Windows or WSL user path fails closed. This preserves command
integrity without persisting an absolute user path.

## D-009 — C4 parity and C7 execution evidence are separate gates

Date: 2026-08-08

Decision: C4 accepts an identity only after exact source/spec binding, a trusted
dispatcher route and full accepted-plus-rejected decision parity. Historical V1
parity is explicitly labelled non-execution evidence; depth, fee, fill and
restart-safe accounting evidence remains mandatory in C7 and is not inferred
from historical profitability.

## D-010 — C5 activation never follows automatically from C4

Date: 2026-08-08

Decision: `EVALUATOR_IMPLEMENTED` and `PARITY_PASS` establish deterministic
behavior, not permission to activate. Every identity remains
`PENDING_C5_CLASSIFICATION` until C5 records a separate machine-readable
decision. `trading_approval=false` remains invariant.

## D-011 — Paper evaluation is not paper execution

Date: 2026-08-08

Decision: C5 enables paper evaluation only for the eight V1 identities whose
trusted rule binding requires an executable checkpoint input. The remaining 26
V1 identities have historical-only inputs and are disabled for missing
execution data; all 13 V2 overlays remain research-only. An enabled evaluation
cannot create a paper intent or fill until C7 separately proves depth, fee,
five-share and restart-safe accounting contracts. C5 keeps
`paper_execution_authorized=false` and `trading_approval=false`.

## D-012 — Data completion is a separate blocking gate before C7

Date: 2026-08-09

Decision: Task 2 data completion is not closed by the C4 strategy-parity gate.
`reports/DATA_COMPLETENESS_STATUS.json` remains authoritative at
`PHASE_0_AUDIT_PENDING`, with empty `source_ranges` and `import_runs` and
`unknown_ranges=NOT_YET_INVENTORIED`. After C6 scheduler/replay acceptance and
before any C7 paper-execution implementation, run a separate
`DATA_COMPLETION_ACCEPTANCE` train that inventories every required source/range,
adds a versioned import ledger where required, and proves append, bounded
backfill, reconcile, idempotent import and recurring future-market support.
Every requested range must end in one of the five states frozen by the database
contract; unknown or silently truncated ranges block the gate. The status report
may advance only from generated receipt-linked evidence. C7 and C11 are blocked
until this gate passes. Historical C1-C5 evidence is not rewritten.
