# C2-C3 Recovery and Rollover Design

## Context

C1 already supplies strict provider parsing, immutable source-event identity,
persistent source cursors, pre-backfill live buffering, one SQLite writer,
canonical projection, recovered/current canary separation, resumable outbox,
same-database restart, and fail-closed lifecycle incidents. C2 hardens these
seams into explicit completeness contracts. C3 adds automatic deterministic
daily-market rotation without changing the safety boundary.

## Architecture

### C2 recovery plan

An immutable recovery plan is derived from persisted cursors and explicit
closed-time boundaries. It records the requested range per source, the cutover
boundary, and whether recovery is a first start or restart. Pure validators
reject regressed, misaligned, future, incomplete or conflicting evidence.

Binance retains the established stream-first sequence and dynamic closed-minute
boundary refresh. Completeness is the exact set of expected minute opens;
REST/WS overlap is an idempotent replay only when canonical payloads agree.

Polymarket retains stream-first discovery/reconciliation. History cursors are
tracked separately from current-book cursors so a millisecond book observation
cannot suppress second-based history recovery. Recovery validates all assets
before reporting complete and never invents historical depth.

### Evidence and incidents

Recovery produces immutable sanitized summaries: expected/recovered/missing,
duplicates/conflicts, cursor before/after, buffered/drained counts, origins,
market/asset counts and integrity facts. State-changing evidence is appended
through the writer to `incidents`; deterministic keys make exact replay a no-op
and conflicting replay fatal.

Recovered evaluations are committed before live cutover with origin
`RECOVERED_AFTER_DOWNTIME`, `execution_eligible=false`, and
`trading_eligible=false`. A separate post-cutover event produces
`CURRENT_LIVE_REEVALUATION`.

### C3 market lifecycle

The market lifecycle has explicit immutable states:

`CURRENT_LIVE -> NEXT_DISCOVERED -> NEXT_BUFFERING -> NEXT_RECONCILED -> CUTOVER_COMMITTED -> CURRENT_LIVE`.

Failure from any transitional state becomes `ROLLOVER_BLOCKED`; it cannot emit
a successful cutover or replace the current projector identity.

Discovery returns an ordered current/next pair from valid future BTC Daily
Range events. Each identity must independently satisfy 11 unique markets and 22
unique assets. Equal-resolution ambiguity, repeated identity with changed
payload, an expired-only set, and current==next all fail closed.

### Subscription migration and atomicity

The orchestrator owns one managed rollover task. At a cutover trigger it:

1. discovers and validates the next identity;
2. starts a bounded next-market stream buffer;
3. obtains and validates all 22 current books and bounded history;
4. commits the immutable next identity and all authoritative replay through the
   single writer;
5. drains the next buffer in deterministic wire/event order;
6. requires live evidence for the next subscription;
7. switches projector market identity and commits a cutover incident;
8. cancels the previous subscription and proves no orphan task remains.

All next-market evidence is validated before the projector changes identity.
The previous market remains authoritative on any failure. At most one active
current subscription remains after cutover; duplicate migration commands are
idempotent.

### Restart behavior

On restart, immutable market identities and rollover incidents determine the
last committed cutover. A discovered-but-uncommitted next market is reconciled
again; a committed cutover is replayed idempotently; an expired former market
cannot become current. Cursor and natural-key conflict rules remain unchanged.

## Data model

No migration is planned:

- `source_events`: immutable provider evidence;
- `source_cursors`: source/history progress;
- `market_catalog`: immutable current and future identities;
- `incidents`: recovery and rollover state/evidence;
- `canonical_state`, `strategy_evaluations`, `signals`, `outbox_events`:
  current-state and infrastructure-canary evidence.

Every write uses the existing `SqliteStore` connection through the orchestrator
writer task. Read acceptance uses `SqliteReadStore` or query-only snapshots.

## Failure handling

- Invalid provider payload or identity: fatal, causal error preserved.
- Missing recovery item: `RECOVERY_BLOCKED`, no `LIVE_READY`.
- Buffer overflow, unstable boundary, duplicate subscription, incomplete next
  market, cursor regression or identity conflict: fail closed.
- Rollover failure: current market remains current; next evidence cannot leak
  into canonical current state.
- Provider disconnect remains reconnectable only under existing provider policy.

## Verification strategy

Each boundary follows TDD. Focused pure tests precede adapter/orchestrator tests;
same-database and loopback process tests follow. C2 and C3 each receive a
machine-readable PASS report and a human-readable matrix. Aggregate verification
checks SQLite, outbox ordering, task cleanup, protected C1 hashes, forbidden
surfaces, docs, ZIP CRC and internal SHA-256 coverage.

No public provider request is needed unless official schemas cannot be verified
from documentation and deterministic fixtures; any such request requires the
manual checkpoint contract and is limited to one bounded read-only run.
