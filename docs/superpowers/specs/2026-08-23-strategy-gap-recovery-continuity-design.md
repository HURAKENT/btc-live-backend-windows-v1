# Strategy Gap Recovery Continuity Design

**Date:** 2026-08-23
**Status:** APPROVED
**Scope:** extend the existing catch-up/replay path from recovered market truth to strategy-level retrospective performance.

## 1. Existing system and exact missing link

The canonical backend already discovers post-baseline markets, classifies each expected date, persists public-provider evidence, recovers settlements, reconciles performance, and rebuilds HISTORICAL/FORWARD/COMBINED materializations. The startup path is:

```text
RuntimeOrchestrator._refresh_performance_catchup
  -> PerformanceCatchupSource.load_inventory
  -> PERFORMANCE_CATCHUP_APPLY writer operation
  -> ForwardPerformanceCycle.run_once
```

`PERFORMANCE_CATCHUP_APPLY` currently persists market/source facts and then invokes the forward cycle. `ForwardPerformanceCycle` can only create observations from already persisted `strategy_evaluations`. It does not reconstruct missing historical checkpoint inputs or dispatch the 47 frozen historical strategy identities. Thus recovered market/settlement truth for 2026-07-08 onward never becomes strategy observations.

The implementation will insert one bounded retrospective reconstruction stage into this existing writer operation, before settlement reconciliation and materialization. It will reuse the existing historical dispatcher and performance ledgers. It will not create a second replay engine.

## 2. Required data flow

```text
existing date-gap detection and market inventory
  -> existing Gamma/Polymarket/Binance catch-up
  -> retrospective checkpoint-input adapter
  -> existing frozen historical dispatcher (47 identities)
  -> append-only recovered HISTORICAL observations
  -> existing settlement reconciliation
  -> existing materialization publisher
  -> resume genuine LIVE/FORWARD collection
```

The adapter receives only market identity, public price history at or before the checkpoint, Binance candles closed before the checkpoint, and frozen model artifacts. Settlement and future prices are not passed to decision construction. Resolution joins happen only after immutable decisions are stored.

## 3. Provenance and views

No original observation is updated or replaced.

- `ORIGINAL`: existing frozen AHR baseline rows only, selected by their pinned baseline provenance.
- `RECOVERED`: retrospective rows created by this recovery path, physically stored with `source_layer=HISTORICAL` and `provenance_kind=RECOVERED_RETROSPECTIVE`.
- `HISTORICAL`: `ORIGINAL + RECOVERED`.
- `FORWARD`: genuine observations captured by the running prospective evaluator only.
- `COMBINED`: fail-closed logical union of HISTORICAL and FORWARD, with existing cross-layer deduplication.

The existing 4,994 baseline observations remain byte-for-byte unchanged and their previous metrics remain independently reproducible through `ORIGINAL`. Recovered rows have deterministic observation keys derived from the market, strategy identity, checkpoint, evaluator contract, and recovered input hash. Re-running recovery is therefore a no-op for identical evidence.

## 4. Reconstruction coverage ledger

A narrow append-only/idempotent `strategy_reconstruction_status` table records one result for every expected `(market_date, strategy_id)`:

- `RECOVERED`: one or more objective observations were reconstructed;
- `EXPECTED_ABSENT`: the market date is canonically absent, so no strategy decision exists;
- `DATA_GAP`: a required causal input or exact frozen contract transition is unavailable.

Each DATA_GAP row contains a stable reason code, exact missing input, checkpoint set, evidence hashes, and attempted provider sources. Execution eligibility is not a reconstruction filter. All 47 canonical identities are dispatched or classified.

## 5. V1 reconstruction contract

The 34 V1 identities continue through the existing `dispatch_historical` interface. The new adapter reconstructs its `V1HistoricalCheckpointInput` from:

- canonical post-baseline market/token metadata;
- Polymarket public price history sampled at or before each frozen checkpoint;
- Binance candles closed strictly before the checkpoint;
- frozen terminal-distribution training representations and formulas used by the original historical baseline.

Before any post-baseline write, the adapter must reproduce the pinned 170-date checkpoint/model matrix and all existing dispatcher decisions exactly. Any parity difference fails closed and prevents recovered writes.

## 6. V2 exhaustive compatibility result

The 13 V2 identities are not excluded because they are research-only. Their evaluator is reusable and requires the parent V1 decision plus a compatible volatility forecast row.

The audited volatility sources define:

- exactly 170 ordered dates split into five 34-date outer blocks;
- block 5 trained/selected from blocks 1-4 and ending 2026-07-07;
- one refit per outer block;
- fixed parameters and causal conditional-state updates inside that evaluation block;
- no network or lookahead in a forecast row.

The formulas and Binance inputs are reproducible, but the frozen contract does not define a sixth block, a post-2026-07-07 refit/selection cadence, regime-threshold selection, or an authorized continuation of block-5 model state. The forecast ledger stores rows, not the final fitted state. Extending the last HAR_RV state would therefore introduce a new model-policy assumption and mix incompatible metrics.

Until an exact frozen continuation artifact exists, each post-baseline V2 identity is classified with:

```text
reason_code = FROZEN_VOLATILITY_CONTRACT_HAS_NO_POST_BLOCK5_TRANSITION
missing_input = exact model assignment, refit/selection rule, regime thresholds,
                and fitted state authorized for checkpoints after 2026-07-07
```

This is a strategy-level DATA_GAP, not a market DATA_GAP and not an execution-disabled shortcut.

## 7. Automatic startup behavior

On startup, after the existing catch-up inventory is persisted, the writer scans resolved historical dates for missing strategy reconstruction status/observations. It fetches only missing causal inputs, reconstructs in chronological order, appends status and observations atomically, reconciles settlements, and republishes metrics. A crash resumes from deterministic persisted keys. Provider retry exhaustion leaves a precise DATA_GAP and is retried on a later startup when evidence availability changes.

The mechanism scans any post-baseline missing date; it contains no July/August date list. A controlled deletion in an isolated database must therefore be recovered through the same ordinary startup operation.

## 8. Dashboard semantics

No redesign is required. The existing dashboard/API will be corrected so that:

- COMBINED is the default performance view;
- resolved historical detail rows display effective resolution rather than their immutable pre-settlement scoring placeholder;
- cumulative series are chronological;
- recent resolutions are newest first;
- rolling labels include both window and `as_of_date`;
- ORIGINAL/RECOVERED/FORWARD contribution counts are visible and auditable.

## 9. Safety and acceptance

The change performs public read-only acquisition and SQLite writes through the existing single writer. It does not create signals that claim to have been live, does not emit orders, and does not alter the trading layer. The invariants `real_orders=false`, `wallet=false`, `signing=false`, `authenticated_CLOB_writes=false`, and `trading_approval=false` remain mandatory.

Acceptance requires exact baseline parity, the real 2026-07-08 through 2026-08-22 recovery report, 47/47 classified identities, no-lookahead evidence, second-run idempotency, a controlled arbitrary-gap recovery, deduplicated COMBINED metrics, focused dashboard tests, and a fresh canonical runtime verification.
