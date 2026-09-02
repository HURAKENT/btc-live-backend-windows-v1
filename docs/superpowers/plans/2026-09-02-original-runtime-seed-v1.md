# ORIGINAL_RUNTIME_SEED_V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Package the accepted ORIGINAL performance bootstrap as an immutable Git-controlled seed and make normal empty-database startup reproduce the old durable state without the nine external research datasets.

**Architecture:** Keep `PinnedAhrArtifactLoader` and the research rebuild path explicit. Add a separate fail-closed seed loader whose two transparent JSONL payloads reconstruct validated `PerformanceObservation` and `PerformanceResolution` domain objects; share the existing repository persistence/materialization path so old and new durable SQLite rows remain byte-for-byte equivalent at the column level.

**Tech Stack:** Python 3.12, stdlib JSON/hashlib/pathlib, existing SQLite store/repository/domain models, `unittest`.

**Spec:** `/mnt/c/Users/gegos/Downloads/BTC_DAILY_RANGE_CODEX_STAGE_A2_ORIGINAL_RUNTIME_SEED_v1.md`

## Global Constraints

- Work only on `codex/stage-a1-external-data-root` in `.worktrees/stage-a1-external-data-root`, based on `e03168f863c7c5f2396496a6d57da70e0f537264`.
- Preserve ORIGINAL/RECOVERED/FORWARD semantics, five-share economics, schemas, migrations, strategy identities, resolution semantics and performance formulas.
- Keep `real_orders=false`, `wallet=false`, `signing=false`, `authenticated_CLOB_writes=false`, and `trading_approval=false`.
- Do not modify the canonical checkout, production database, Task Scheduler or running backend; do not merge, push or cut over.
- Exact ordered full-column content hashes of every performance table written by bootstrap are the acceptance oracle.

---

### Task 1: Characterize and test the portable seed contract

**Files:**
- Create: `tests/test_original_runtime_seed.py`
- Create: `src/original_runtime_seed.py`

**Interfaces:**
- Consumes: existing `PerformanceObservation`, `PerformanceResolution`, `canonical_json`, and frozen contract files.
- Produces: `OriginalRuntimeSeedLoader(project_root, seed_dir=None).load() -> OriginalRuntimeSeedBundle`.

- [x] **Step 1: Write failing loader tests**

  Add real-file tests that require a valid seed to load 4,994 ORIGINAL observations and 1,850 resolutions, and controlled-copy tests for missing payload, changed payload bytes, non-ORIGINAL manifest provenance, duplicate identities, and current frozen contract hash drift. Each expected error is a literal fail-closed code.

- [x] **Step 2: Run RED**

  Run `../../.venv/Scripts/python.exe -m unittest -v tests.test_original_runtime_seed.OriginalRuntimeSeedLoaderTests`; expect import/feature failures because `src.original_runtime_seed` does not exist.

- [x] **Step 3: Implement the minimal loader**

  Define immutable dataclasses for manifest metadata and the loaded tuple payloads. Validate the pinned manifest byte hash, schema/seed identity, `ORIGINAL` provenance, source AHR hashes, current rule-map/status-matrix hashes, payload path containment/hash/count, exact domain payload round trips, identity uniqueness, accepted/rejected counts, strategy count, source layer, and exclusions. Reconstruct domain objects only through current model validation.

- [x] **Step 4: Run GREEN**

  Re-run the loader tests and require all cases to pass.

### Task 2: Deterministically package accepted ORIGINAL rows

**Files:**
- Create: `tools/build_original_runtime_seed_v1.py`
- Create: `strategy_sources/frozen/original_runtime_seed_v1/observations.jsonl`
- Create: `strategy_sources/frozen/original_runtime_seed_v1/resolutions.jsonl`
- Create: `strategy_sources/frozen/original_runtime_seed_v1/manifest.json`

**Interfaces:**
- Consumes: explicit old `PinnedAhrArtifactLoader`, `HistoricalPerformanceObservationBuilder`, `HistoricalSettlementReconciler` path.
- Produces: deterministic canonical JSONL payloads and manifest with payload/source/contract hashes and exact logical counts.

- [x] **Step 1: Add a failing deterministic packaging test**

  Extend `tests/test_original_runtime_seed.py` so building into a temporary directory produces byte-identical payload and manifest files to the committed seed.

- [x] **Step 2: Run RED**

  Run the named packaging test and require failure because the packager is absent.

- [x] **Step 3: Implement and run the packager**

  Build old accepted observations/resolutions once, preserve old observation order, emit each existing canonical domain `payload_json` as one UTF-8 line with LF, and emit a sorted/indented manifest without a machine-local timestamp. Include AHR acceptance/input-manifest/parity/results/settlement hashes, all nine source hashes, rule-map/status-matrix identities, migration version 7, payload hashes/counts, exclusions, and packaging version.

- [x] **Step 4: Pin the resulting manifest and run GREEN**

  Set `PINNED_ORIGINAL_RUNTIME_SEED_MANIFEST_SHA256` to the generated manifest byte hash, regenerate, and require deterministic packaging and loader tests to pass.

### Task 3: Wire normal bootstrap to the seed while preserving the old oracle

**Files:**
- Modify: `src/performance_engine.py`
- Modify: `tests/test_performance_engine.py`
- Modify: `tests/test_original_runtime_seed.py`

**Interfaces:**
- Consumes: `OriginalRuntimeSeedBundle` and existing repository methods.
- Produces: `bootstrap_historical()` using the seed; explicit `bootstrap_historical_from_ahr()` retained for research/parity; one shared persistence path.

- [x] **Step 1: Write failing integration/idempotency/conflict tests**

  Require fresh migrated DB seed bootstrap success, second-run replay, exact existing immutable-conflict failure, migration-version rejection, and successful normal bootstrap while `default_artifacts()` is patched to inaccessible paths.

- [x] **Step 2: Run RED**

  Run the new engine/seed integration cases and confirm they fail because runtime still invokes the old AHR loader/external inputs.

- [x] **Step 3: Implement shared persistence and explicit old path**

  Make normal `bootstrap_historical` load the seed, validate applied migration version 7, and persist its domain objects. Add `bootstrap_historical_from_ahr` to build the same objects through the old accepted mechanism. Keep historical ingest fields, keys, timestamps, and materialization calculation unchanged so full durable content can match exactly.

- [x] **Step 4: Run GREEN and focused regression**

  Run `tests.test_original_runtime_seed`, `tests.test_performance_engine`, `tests.test_performance_historical`, and `tests.test_performance_repository`.

### Task 4: Prove full durable parity and exclusions

**Files:**
- Create: `tools/verify_original_runtime_seed_v1.py`
- Test: `tests/test_original_runtime_seed.py`

**Interfaces:**
- Consumes: fresh migrated SQLite A/B databases and explicit old/new engine paths.
- Produces: JSON receipt containing ordered full-column row counts and SHA-256 per written table plus exclusion/security checks.

- [x] **Step 1: Write a failing parity receipt test**

  Require the verifier to return equality for observations, resolutions, ingest runs, materialization revisions, aggregates and timeseries, and zero FORWARD/emitted/signal/paper rows.

- [x] **Step 2: Run RED**

  Run the parity test and require failure because the verifier does not exist.

- [x] **Step 3: Implement the verifier**

  Migrate two temporary stores, run `bootstrap_historical_from_ahr(as_of_date='2026-08-15')` for A and normal seed bootstrap for B, query all columns ordered by declared primary key, canonicalize only the query envelope (not values), and hash it with SHA-256. Report exact A/B counts/hashes/equality and guards.

- [x] **Step 4: Run GREEN and standalone oracle**

  Run the parity test and then `../../.venv/Scripts/python.exe tools/verify_original_runtime_seed_v1.py`; require status `PASS` and exact equality for all six durable tables.

### Task 5: Document boundaries and verify the change

**Files:**
- Modify: `README.md`
- Create: `docs/operation/ORIGINAL_RUNTIME_SEED_V1.md`
- Modify: this plan checklist.

**Interfaces:**
- Consumes: verified seed/manifest/parity receipt.
- Produces: current operator guidance distinguishing runtime seed from research revalidation inputs.

- [x] **Step 1: Document the runtime/research split**

  State that normal runtime uses the immutable Git seed, external `DATA_ROOT` remains mutable, full AHR research/revalidation still requires the nine pinned inputs, and fresh chats must not recursively inspect them unless revalidation is in scope.

- [x] **Step 2: Verify unchanged protected identities**

  Compare migration file list/hash, `STRATEGY_RULE_MAP_47.json`, `STRATEGY_47_STATUS_MATRIX.json`, and the five security guards against the base commit and manifest.

- [x] **Step 3: Run required suites/checks**

  Run the seed/parity suites; historical/performance repository suites; A1/A1.1 path/launcher suites excluding Task Scheduler mutation; `python -m compileall`; and `git diff --check`. Record exact results.

- [x] **Step 4: Request independent code review and resolve findings**

  Give a reviewer the A2 specification, base SHA and complete diff. Fix all Critical/Important findings through RED→GREEN and re-run affected checks.

- [x] **Step 5: Commit locally and re-verify isolation**

  Commit the complete A2 change on `codex/stage-a1-external-data-root`, then verify commit ancestry, clean worktree, canonical HEAD/status, and no production/runtime state mutations.
