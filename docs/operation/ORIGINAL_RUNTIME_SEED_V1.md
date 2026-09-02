# ORIGINAL_RUNTIME_SEED_V1

## Purpose

`ORIGINAL_RUNTIME_SEED_V1` is the portable runtime representation of the
already accepted ORIGINAL historical performance baseline. It is not a new
historical calculation, a RECOVERED reconstruction, or FORWARD evidence.

The Git-controlled bundle is located at
`strategy_sources/frozen/original_runtime_seed_v1/` and contains:

- `manifest.json`, whose exact byte hash is pinned by the runtime loader;
- `observations.jsonl`, containing canonical `PERFORMANCE_OBSERVATION_V1`
  payloads in accepted AHR result order;
- `resolutions.jsonl`, containing canonical `PERFORMANCE_RESOLUTION_V1`
  payloads.

The loader validates the manifest, both payload hashes and counts, domain
payload round trips, ORIGINAL-only provenance, identity uniqueness, resolution
scope, the frozen strategy rule-map/status-matrix hashes, the five security
guards, and migration compatibility. Validation is fail-closed. Repository
insertion retains the existing idempotent replay and immutable-conflict rules.

## Runtime boundary

Normal `StrategyPerformanceEngine.bootstrap_historical()` reads only normal
project files and this seed. It does not fall back to the accepted AHR run or
to paths under Downloads/other local research projects. Consequently a fresh
migrated SQLite database can complete ORIGINAL bootstrap with an otherwise
empty external `DATA_ROOT` and without network access.

The seed excludes RECOVERED and FORWARD observations, emitted signals, paper
intents/fills, mutable cursors, and runtime incidents. Materializations are
still calculated by the existing performance engine, so no formula or schema
is duplicated in the seed.

## Research/revalidation boundary

`StrategyPerformanceEngine.bootstrap_historical_from_ahr()` and
`tools/build_original_runtime_seed_v1.py` are explicit research/parity paths.
They preserve the prior accepted AHR validation: acceptance/input/parity/result
artifact hashes, all nine external source hashes, historical input rebuilding,
frozen registry checks, and settlement reconciliation. Running the packager
requires the accepted ignored AHR directory and all nine hash-pinned datasets.
Those sources remain reproducibility evidence and are neither deleted nor
declared obsolete by this seed.

Fresh Codex tasks should not recursively inspect or copy the large external
research datasets unless historical revalidation or deterministic seed
repackaging is explicitly in scope.

## Data-root separation

The external `BTC_DAILY_RANGE_DATA_ROOT` is mutable, machine-local runtime
state (SQLite, backups, logs, diagnostics). The immutable seed is program data
inside Git and must not be moved into `DATA_ROOT`. Production database movement,
Task Scheduler changes, backend restart and production cutover are separate
operations and are not authorized by this document.
