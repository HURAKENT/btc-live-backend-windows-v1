# Audit Handoff

## Task

- Task ID: Task 1 — Freeze the Windows Environment and Package Contract
- Gate: `C0_RUNTIME_FREEZE_PASS`
- Source commit: `0c38069eb55703ae297967cffe25ff7d3f928380`
- Branch: `codex/c0-c1`
- Scope: Windows runtime/package freeze only
- Status: PASS at the pre-commit verification gate

Task 2 has not started.

## Changed files

- `pyproject.toml`
- `requirements.in`
- `requirements.lock`
- `config/c0_c1_frozen_config.json`
- `src/config.py`
- `tests/test_config_registry.py`
- `reports/AUDIT_HANDOFF.md`

No file under `docs/frozen`, `contract`, or `registry` was changed.

## Acceptance criteria mapping

| Criterion | Evidence | Result |
|---|---|---|
| Dedicated Python 3.12.4 environment | `.venv\Scripts\python.exe --version` returned `Python 3.12.4` | PASS |
| One direct runtime dependency | `pyproject.toml` and `requirements.in` contain only `aiohttp==3.14.3`; package-contract check exit 0 | PASS |
| Resolved environment is locked | Sorted `pip freeze` equals `requirements.lock` exactly; 10 resolved distributions | PASS |
| Dependency graph is consistent | `.venv\Scripts\python.exe -m pip check` returned `No broken requirements found.` | PASS |
| Typed immutable runtime config | `RuntimeConfig` uses `@dataclass(frozen=True, slots=True)`; immutability test passes | PASS |
| Loopback-only bind | Loaded config is `127.0.0.1:8767`; non-loopback and invalid-port tests pass | PASS |
| Forbidden features remain disabled | Real orders, wallet, paper execution, and dashboard are false; enabled-value rejection tests pass | PASS |
| Exactly one database writer | Config value is 1; non-1 rejection test passes | PASS |
| Fail closed on malformed shape | Unknown, missing, and wrong-typed top-level values are rejected by passing tests | PASS |
| Task 1 suite and full repository suite | 13 tests, 0 failures, 0 errors in both commands | PASS |

## TDD red-green evidence

1. Initial RED:
   - Command: `& ".\.venv\Scripts\python.exe" -m unittest tests.test_config_registry.ConfigTests -v`
   - Exit code: 1
   - Expected cause: `ModuleNotFoundError: No module named 'src'`
2. First GREEN:
   - Same command
   - Exit code: 0
   - Result: 12 tests passed
3. Invalid-type RED:
   - Command: `& ".\.venv\Scripts\python.exe" -m unittest tests.test_config_registry.ConfigTests.test_incorrect_config_value_type_is_rejected -v`
   - Exit code: 1
   - Expected cause: unhandled `TypeError` for string port
4. Invalid-type GREEN:
   - Same command
   - Exit code: 0
   - Result: 1 test passed
5. Final Task 1 GREEN:
   - Command: `& ".\.venv\Scripts\python.exe" -m unittest tests.test_config_registry.ConfigTests -v`
   - Exit code: 0
   - Result: 13 tests passed

No test is labeled a regression test.

## Verification commands and results

| Command | Key result | Exit |
|---|---|---:|
| `& ".\.venv\Scripts\python.exe" -m unittest tests.test_config_registry.ConfigTests -v` | 13 passed | 0 |
| `& ".\.venv\Scripts\python.exe" -m unittest discover -s tests -v` | 13 passed | 0 |
| Python AST parse and import of `src.config` interfaces | `SYNTAX_IMPORT_PASS files=2 interfaces=3` | 0 |
| Python `tomllib` package-contract check | Python `==3.12.4`; one direct dependency | 0 |
| `& ".\.venv\Scripts\python.exe" -m pip check` | No broken requirements | 0 |
| Sorted `pip freeze` versus `requirements.lock` | Exact match, 10 lines | 0 |
| Load `config/c0_c1_frozen_config.json` | Loopback, one writer, four false flags | 0 |
| Registry/contract PowerShell verification | 47 unique; 34 V1; 13 V2; hashes and flags match | 0 |
| `docs/frozen` SHA-256 baseline plus `git diff --exit-code -- docs/frozen` | 5 files bytewise unchanged | 0 |
| Secret/runtime/forbidden-scope scan | 0 hits in every category | 0 |
| `git diff --check` | No whitespace errors | 0 |

PowerShell 5.1 script validation is not applicable because Task 1 created no PowerShell files.

## Registry and contract verification

- Registry rows: 47
- Unique `strategy_id`: 47
- V1: 34
- V2: 13
- CSV/JSON/XLSX content comparison: 0 mismatches
- CSV SHA-256: `77fc26814e3c05182b0b13dbb3d162c41536328517a40b4a6713d7d6e9bad8fd`
- JSON SHA-256: `88c54943cf84e4d123f979639cf30668f35a5dd6bc6792f8f50ab4c986ee3c2f`
- Both hashes match the Registry 47 lock, backend contract, frozen config, and plan verification.
- `real_order_submission=false`
- `executable_rule_pack_complete=false`

## Security and forbidden-scope verification

- Secret scan: 0 hits.
- Runtime-data scan: 0 hits outside ignored `.venv`.
- `.venv` is ignored by `.gitignore`.
- Forbidden backend component scan: 0 hits.
- Source modules created: only `src/config.py`.
- PowerShell scripts created: 0.
- No Binance, Polymarket, WebSocket, SQLite, recovery, outbox, replay, dashboard implementation, Strategy Registry execution, trading logic, wallet/signing, or order-submission code was created.

## Independent audit remediation

- Source finding: direct `RuntimeConfig` instances reached `validate_runtime_config()` without exact runtime-type validation.
- Confirmed bypass cases before the fix: `bind_port=8767.0`, `database_writer_count=True`, and `real_orders_enabled=0` were accepted.
- TDD RED: `test_runtime_config_direct_type_mismatches_are_rejected` failed in all 9 adversarial subtests because invalid types were accepted or returned a value-validation error instead of `INVALID_CONFIG_TYPE: <field>`.
- Implementation: `validate_runtime_config()` now applies an exact `type(value) is expected_type` gate for all seven frozen fields before preserving the existing value-validation order.
- GREEN: the focused test passed; all 14 `ConfigTests` passed; full `unittest discover` passed all 14 tests.
- Adversarial proof: all 9 direct-construction mismatches raised the exact `ValueError: INVALID_CONFIG_TYPE: <field>` contract; the canonical frozen config still loaded with loopback bind, port 8767, four false feature flags, and one database writer.
- Task 2 has not started.
- Remediation scope is limited to `src/config.py`, `tests/test_config_registry.py`, and this handoff. Frozen, contract, registry, and package files were not changed.

## Known gaps and next step

All live ingestion, persistence, recovery, outbox, API, canary, registry execution, paper, and trading capabilities remain absent by frozen Task 1 scope. Registry 47 remains non-executable.

The exact next permissible step is an independent audit of Task 1 in ChatGPT Project. Task 2 is explicitly not started.

## Task 2 — Registry 47 executable-rule gap

- Gate: `C0_REGISTRY_47_LOCK_PASS`.
- Scope: created `src/registry_lock.py` and `reports/EXECUTABLE_RULE_GAP_REPORT.json`; updated `tests/test_config_registry.py` and this handoff.
- Canonical verification: 47 rows, 47 unique `strategy_id` values, 34 V1, and 13 V2.
- Raw CSV SHA-256: `77fc26814e3c05182b0b13dbb3d162c41536328517a40b4a6713d7d6e9bad8fd`.
- Raw JSON SHA-256: `88c54943cf84e4d123f979639cf30668f35a5dd6bc6792f8f50ab4c986ee3c2f`.
- Negative coverage rejects JSON/CSV hash mismatch, wrong count, duplicate/missing/unexpected IDs, missing or invalid lineage, invalid top-level shape, and invalid primitive types.
- Gap report: 47 rows, `executable_now=0`, `requires_parity=47`; every row is registry-locked and has runtime status `DISABLED_EXECUTABLE_RULE_GAP`.
- Registry 47 remains non-executable: no exact rule source was inferred from names or research metrics, and no strategy execution was added.
- Frozen plan, contract, canonical registry, package, config, and README files were not changed.
- Earlier Task 1 statements that Task 2 had not started remain point-in-time audit history; Task 3 has not started.

## Core block — Tasks 3–5

- Gates: `C1_DOMAIN_CONTRACT_PASS`, `C1_EVENT_STORE_PASS`, and `C1_TRANSACTIONAL_OUTBOX_PASS`.
- Scope: created `src/fixed_point.py`, `src/models.py`, `src/storage.py`, `src/outbox.py`, `migrations/0001_core.sql`, `migrations/0002_indexes.sql`, and `tests/test_storage_outbox.py`; updated this handoff.
- Interfaces: integer-micros value objects; immutable source/snapshot/evaluation/signal/outbox models; `SqliteStore`, `SqliteWriter`, and `WriteCommand`; atomic `commit_signal_and_outbox`, resumable `read_outbox_after`, and committed-ID `OutboxBroker`.
- Targeted GREEN: 11 Domain tests, 10 Storage tests, and 9 Outbox tests.
- SQLite verification: `journal_mode=wal`, `synchronous=FULL` (`2`), and `foreign_keys=ON` (`1`); all nine contract-required tables are migration-managed.
- Duplicate source-event verification: two appends of one natural key produce one row and the second result has `inserted=False`; conflicting payload identity is rejected.
- Transactional outbox verification: signal and outbox rows commit together; forced failure of either insert leaves both counts at zero; duplicate signal identity creates no duplicate outbox event.
- Tasks 6 and later were not started. No trading, wallet/private-key, paper execution, provider/network, dashboard/API, or Registry 47 execution code was added.

## Live data and recovery block — Tasks 6–8

- Gates: `C1_BINANCE_PROVIDER_PASS`, `C1_POLYMARKET_PROVIDER_PASS`, and `C1_RESTART_RECOVERY_PASS`.
- Interfaces: Binance closed-kline parser/backfill/stream and reconnect policy; immutable Polymarket market identity, discovery, current-book/history/stream adapters and deterministic book projection; lifecycle state machine, recovery coordinator, continuity/market reconciliation, and recovered-evaluation classification.
- Targeted GREEN: 12 Binance tests, 16 Polymarket tests, and 14 Recovery tests.
- Binance: only `k.x=true` becomes canonical history; exact natural keys, decimal strings, two-page 60-second pagination, final-closed inclusion, current-open exclusion, gap/duplicate rejection, reconnect cap/reset, and queue behavior are covered.
- Polymarket: one future active 11-bucket identity yields 22 unique YES/NO asset IDs; book snapshots replace state, price changes mutate listed levels with zero-size deletion, unknown events persist with WARNING, and PING/PONG is proven against a loopback aiohttp WebSocket server.
- Recovery: backfill commit precedes buffer drain; synthetic `[1,2] + [2,3]` commits `[1,2,3]`; a ten-minute outage recovers all ten closed minutes; missing minutes block `LIVE_READY`; missing required depth is `BLOCKED_MISSING_HISTORICAL_DEPTH`; recovered evaluations remain non-current and `execution_eligible=false`.
- No real external provider smoke request was executed; tests use fixtures, fakes, and the local loopback server only.
- Task 9 was not started. No trading, wallet/private-key, paper execution, API/dashboard, canary, or Registry 47 execution code was added.

## Observable vertical slice — Tasks 9–10

- Gates: `C1_CANARY_PASS` and `C1_PUSH_API_PASS`.
- Canary: `CANARY_SYNC_READY_V1` is infrastructure-only, is not part of Registry 47, and is never paper- or live-execution eligible.
- Deterministic session/market/closed-kline identity suppresses duplicate signal and outbox rows; recovered evaluations have `execution_eligible=false` and remain distinct from current reevaluation.
- REST routes: `/api/v1/bootstrap`, `/api/v1/health`, `/api/v1/sources`, `/api/v1/signals`, and `/api/v1/incidents`.
- WebSocket `/ws/v1/events` provides committed-only, strictly increasing, resumable delivery with database replay, race-window reread, and event-ID deduplication.
- Tests use temporary SQLite databases and loopback aiohttp servers; no external provider request is made.
- No trading, wallet/private-key, paper execution, dashboard, or Registry 47 execution code was added.
- Task 11 was not started.

## Windows delivery — Tasks 11–12

- Gates: `C1_WINDOWS_LIFECYCLE_PASS` and `C1_SAFE_DELIVERY_PASS`.
- Win32 named-mutex verification rejects a second instance with `BACKEND_ALREADY_RUNNING` and permits reacquisition after close.
- Shutdown order is fixed: stop API acceptance, stop provider reconnects, drain the source queue, commit pending writes, flush outbox state, close reads, close the writer, and release the mutex last.
- Process exit codes are `0` clean stop, `20` already running, `30` contract/configuration failure, and `40` database integrity failure.
- Safe PowerShell 5.1 entry points: `RUN_BACKEND_SAFE.ps1`, `RUN_TESTS_SAFE.ps1`, and `RUN_C1_ACCEPTANCE_SAFE.ps1`.
- Targeted verification passes 12 lifecycle tests and 20 offline smoke tests; smoke coverage includes launcher safety and frozen runtime invariants without dependency installation or forbidden PowerShell behavior.
- Delivery remains loopback-only; real orders, wallet/private-key support, paper execution, and Registry 47 execution remain disabled.
- Tasks 13–14 are not complete; Task 13 was not started.

## Task 13 — Real provider capability smoke

- Gate: `BLOCKED_EXTERNAL_INVENTORY`; `C1_PROVIDER_CAPABILITY_PASS` is not claimed.
- The pre-network offline gate passed 157 `unittest` tests, `compileall`, `pip check`, frozen config and Registry 47 checks, fresh SQLite integrity/PRAGMA checks, source deduplication, transactional outbox, lifecycle exit mapping, and launcher safety.
- Binance REST capability passed against the primary public endpoint: HTTP 200, canonical closed-kline adapter acceptance, and exact deterministic natural key.
- Binance public WebSocket capability passed: handshake and a schema-valid BTCUSDT 1m kline were observed; the open candle was correctly ignored by the canonical parser.
- Gamma returned five bounded pages containing 500 objects, but no active, future-resolution, structurally valid BTC Daily Range event with 11 binary markets and 22 unique CLOB asset IDs was present.
- Consequently, no canonical market identity or asset IDs existed; all 22-book, one-hour price-history, and Polymarket market-WebSocket checks were skipped as upstream-blocked rather than guessed.
- Observed schema warning: the Gamma listing endpoint capped each requested page at 100 objects.
- No authentication, secrets, wallet data, user channel, or order method was used. Requests were limited to public read-only allowlisted endpoints.
- Task 14 was not started, and final C1 PASS is not claimed.

### Task 13 Gamma keyset recovery

- Offset pagination evidence is exact: offset 2000 returned 100 objects, while offset 2100 returned HTTP 422 with `offset too large, use /events/keyset for deeper pagination`.
- The public keyset probe returned HTTP 200 with top-level fields `$schema`, `events`, and `next_cursor`; `events` was a list and `next_cursor` was a 212-character string.
- The filtered keyset scan read two pages and 100 unique events. It terminated `REPEATED_PAGE`: sending the observed `next_cursor` value as `cursor` returned the same 100 event IDs and the same next cursor.
- Candidate count is 2; structural matches are 0; adapter matches are 0. The root cause is `SCAN_INCOMPLETE`, and the Task 13 status is `BLOCKED_PROVIDER_SCHEMA`.
- CLOB books, price history, and market WebSocket remain `SKIPPED_BLOCKED_UPSTREAM` because no canonical asset identity was established.
- Provider implementation and all other production code remain unchanged. Task 14 was not started, and final C1 PASS is not claimed.

### Task 13 — Gamma keyset after_cursor recovery

- The official cursor contract is now applied exactly: response `next_cursor` is passed unchanged as request parameter `after_cursor`; the previous `cursor` probe was incorrect and is not used by the current scan.
- Correct pagination produced 50 distinct pages and 5,000 unique event IDs. Every page returned 100 new IDs, and each page input cursor hash matched the previous page next-cursor hash.
- Termination was `MAX_UNIQUE_EVENTS_REACHED`: page 50 still returned a non-empty next cursor, so the inventory remained incomplete at the approved bound.
- Candidate count is 39; structural matches are 0; adapter matches are 0. Task 13 is `BLOCKED_PROVIDER_NETWORK` with root cause `SCAN_INCOMPLETE`.
- CLOB books, price history, and market WebSocket remain `SKIPPED_BLOCKED_UPSTREAM` because no canonical asset identity was established.
- Provider implementation and all production code remain unchanged. Task 14 was not started, and final C1 PASS is not claimed.

### Task 13 — targeted Gamma discovery recovery

- The historical 5,000-event scan remains pagination evidence, not proof that the current inventory lacks a canonical event. The final targeted cycle used the dynamic UTC window `2026-07-28T14:10:49Z` through `2026-08-11T20:10:49Z`.
- Targeted keyset Query A (`title_search=Bitcoin price on`) returned HTTP 200 with six unique events on one page and terminated below the 500-result cursor threshold. Queries B/C were unnecessary.
- Public search was not invoked because Query A already produced five future manual structural matches. Candidate / manual structural / adapter counts are 6 / 5 / 0.
- Each of the five future matches has 11 unique binary markets, 22 unique CLOB token IDs, and 11 range labels. All five are therefore simultaneously eligible under the manual structural gate, so Task 13 is `BLOCKED_AMBIGUOUS_DISCOVERY` with root cause `AMBIGUOUS_DISCOVERY`.
- The existing adapter rejected all five with `BTC_DAILY_RANGE_DATA_GAP`; observed `ticker` values use `bitcoin-price-on-<date>` rather than `BTC-DAILY-RANGE-<date>`. This mismatch is recorded as evidence but does not override the higher-priority ambiguous-discovery classification.
- No single Gamma identity was selected. CLOB books, price history, and market WebSocket remain `SKIPPED_BLOCKED_UPSTREAM`.
- Requests used public read-only GET only, `DummyCookieJar`, `trust_env=False`, and no VPN/proxy bypass, authentication, cookies, wallet, or order method. Production code is unchanged. Task 14 was not started, and final C1 PASS is not claimed.

### Task 13 — discovery remediation and CLOB capability

- Confirmed root cause: discovery accepted only the legacy `BTC-DAILY-RANGE-<date>` ticker and treated every set of multiple future candidates as ambiguous. The pre-fix RED ran 10 focused tests and failed six exactly at current naming, nearest-future selection, and duplicate-ID handling.
- Discovery now supports both legacy naming and the current `bitcoin-price-on-<date>` Gamma identifiers. It selects the minimum authoritative future `endDate`/resolution, and raises `AMBIGUOUS_BTC_DAILY_RANGE` only when different event IDs share that nearest resolution.
- Targeted Query A returned six events. Five were valid future 11-market/22-asset candidates; event `733270`, slug `bitcoin-price-on-july-29-2026`, resolution `2026-07-29T16:00:00Z`, was selected and four later rollover candidates were ignored.
- CLOB books are blocked by confirmed provider/adapter schema mismatch: all 22 public requests returned HTTP 200 with string price/size boundaries and string millisecond `timestamp`; existing `_parse_book` requires exact `int`, so successful/schema-valid adapter results are 0/22.
- One-hour price history returned HTTP 200 with 31 points; `t` is `int` and `p` is `float`, while the existing adapter requires decimal-string `p`, so the check is `BLOCKED_PROVIDER_SCHEMA`.
- Market WebSocket handshake, subscription, and literal `PING` succeeded. The initial event exposed the same string timestamp mismatch and was rejected before a `PONG` could be observed.
- Only public read-only allowlisted endpoints were used, with `DummyCookieJar`, `trust_env=False`, redirect rejection, bounded timeouts/retries, and no VPN/proxy bypass, authentication, secrets, wallet, user channel, or order method.
- Production changes are limited to `src/market_discovery.py`; the CLOB/provider adapter was not changed. Task 14 was not started. `C1_PROVIDER_CAPABILITY_PASS` and final C1 PASS are not claimed.

### Task 13 — CLOB boundary remediation

- Confirmed root cause: actual CLOB book timestamps are positive decimal strings, while the provider boundary required exact integers; standard JSON history decoding also materialized fractional `p` values as binary floats.
- TDD RED: 16 focused boundary tests produced one failure and six errors on the unchanged provider, specifically reproducing string-timestamp rejection, ordinary `response.json()` use for fractional history, and rejection of `Decimal`/exact-integer history prices.
- Book timestamps now use one strict REST/WS normalization policy: exact positive integers or non-empty positive ASCII-digit strings become canonical integer milliseconds; bool, float, signed, whitespace, fractional, exponent, and non-numeric forms remain rejected.
- Price-history response bytes are decoded once with `parse_float=Decimal`; `Decimal`, decimal strings, and exact integers are accepted, while direct float, bool, malformed, NaN, and Infinity remain rejected.
- Fresh public read-only evidence: 22/22 books returned HTTP 200 and 22/22 passed the canonical parser; eight books had at least one empty side and were accepted without invented liquidity.
- One-hour price history returned 31 points, all fractional prices decoded as `Decimal`, zero floats, and 31 canonical adapter events.
- Market WebSocket handshake, subscription, literal `PING`, literal `PONG`, initial `book`, string-timestamp normalization, and canonical parser acceptance all passed for the selected YES/NO pair.
- Task 13 status is `PASS` and gate `C1_PROVIDER_CAPABILITY_PASS` is reached. This is provider capability evidence only and is not trading approval.
- Production scope changed only `src/polymarket_provider.py`; no authentication, cookies, secrets, wallet, user channel, order method, VPN/proxy bypass, Gamma rerun, or Binance rerun was used.
- Task 14 was not started. Final C1 PASS is not claimed until Task 14 is completed.

## Task 14 — C1 Intentional-Downtime Acceptance

- Run ID: `C1-ACCEPTANCE-20260728T212826Z-4C5B0826`.
- Final status: `BLOCKED_RUNTIME_PATH_CONTRACT`; gate: `NOT_REACHED`.
- The mandatory offline pre-acceptance gate passed 65 targeted tests and 228 full-suite tests with one documented historical skip; `compileall` and `pip check` passed.
- After blocked-report and pack contract coverage was added, the fresh final gate passed 72 targeted tests and 235 full-suite tests with the same one documented historical skip.
- Acceptance-tool TDD began with the expected 24-test RED because `tools/simulate_downtime.py` did not exist, followed by 24/24 GREEN tests covering duration, fail-closed status, marker hashing, report policy, and safe ZIP behavior.
- Exact blocker: `run_backend.py` fixes its database path at `data/runtime/btc_live_backend.sqlite3` and exposes no CLI or environment contract for the required isolated acceptance data/runtime roots. The frozen Task 14 instructions require `BLOCKED_RUNTIME_PATH_CONTRACT` in this condition and prohibit changing production code.
- The backend was not started. Initial `LIVE_READY`, second-instance exit code, graceful stops, process/restart timestamps, real downtime duration, recovery, Binance continuity, Polymarket reconciliation, historical-depth classification, canary semantics, outbox replay, and runtime database-integrity checks are therefore explicitly `NOT_RUN_BLOCKED_RUNTIME_PATH_CONTRACT`, not simulated.
- No network request, forced termination, unknown-process stop, wallet/signing, paper execution, Registry 47 execution, or real order action occurred.
- Blocked evidence pack: `artifacts/C1_ACCEPTANCE_PACK.zip`; SHA-256 `5d009ff3563d793855f550ed5434a1ea2e54b2a39da20f91170faa691050a3dc`; 22,218 bytes; ZIP CRC PASS; internal `SHA256SUMS` PASS; path traversal, duplicate entries, missing hashes, SHA mismatches, raw SQLite/WAL/SHM, and raw WebSocket logs all equal zero.
- `scripts/RUN_C1_ACCEPTANCE_SAFE.ps1` now invokes `tools\simulate_downtime.py` directly through the existing Windows `.venv` Python and propagates its exit code.
- C2, C3 rollover, C4, C5, paper execution, and trading work were not started. Registry 47 remains non-executable. `BTC_LIVE_BACKEND_WINDOWS_V1_C1_PASS` and trading approval are not claimed.

### Task 14 — isolated database remediation and runtime retry

- Runtime-path TDD reproduced the hardcoded-path blocker: the initial 16-test RED had 15 expected feature-missing errors. Commit under test `a6348f6e0c4e0eee4bd529d35d360a99e8d455fb` adds `--database-path` with exact absolute `.sqlite3` validation while preserving the no-argument path `data/runtime/btc_live_backend.sqlite3`; the final focused gate passed 18 tests.
- New run ID: `C1-ACCEPTANCE-20260728T215615Z-10F16FD7`. The initial and planned restart commands use the same sanitized database label `data-root/acceptance/<run_id>/btc_live_backend.sqlite3`; the resolved path SHA-256 is `f96a3c42c5bec2adcedc4ca28375f52097f420068de2fdd1af5e3249e6e52fba`.
- The default database did not exist before or after the run, so its existence/size/mtime/SHA-256 fingerprint is unchanged.
- The Windows backend process started, created/migrated the isolated database, and exposed the loopback API. Three final bootstrap observations reported database health `PASS` and `last_event_id=0`, but no startup state, no Binance or Polymarket sources, and no current market identity.
- Final Task 14 status is `BLOCKED_INITIAL_LIVE_READY`; gate is `NOT_REACHED`. Source evidence is exact: `BackendRuntime.start_runtime_tasks()` returns without starting provider/recovery tasks. Per the hard-stop rule, no production remediation was attempted.
- The process was stopped before any intentional downtime. `stop_requested_at_ms=1785275786535`, `process_stopped_at_ms=1785275786540`, `restart_requested_at_ms=null`, and no 600,000–630,000 ms interval was claimed.
- Cleanup used `CTRL_BREAK_EVENT` and no forced termination, but the observed Windows process exit code was `3221225786`, not clean exit `0`; this is separately recorded as `BLOCKED_GRACEFUL_SHUTDOWN` evidence and was not fixed.
- Isolated SQLite evidence remained valid: `quick_check=ok`, `integrity_check=ok`, `journal_mode=wal`, `synchronous=2`, and `foreign_keys=1`. Final state: backend absent, mutex free, and port 8767 free.
- The replacement blocked acceptance pack is sanitized and self-verifying; no raw database/WAL/SHM or raw WebSocket log is included. C2/C3, paper execution, Registry 47 execution, wallet/signing, and trading were not started. Final C1 PASS is not claimed.

## C1 Runtime Integration — Task 1 Persistence Seams

- Gate: `C1_RUNTIME_PERSISTENCE_PASS`.
- TDD RED: the new 34-test runtime-persistence module produced 36 expected `AttributeError` errors across 31 feature tests because the persistence/read seams did not exist; three schema/security baselines passed and no production file had changed before the RED.
- `SqliteStore` now provides source-cursor upsert/read, immutable market identity, canonical snapshot, strategy evaluation, append-only incident, and lifecycle-state persistence primitives with exact replay idempotency, monotonic cursor handling, fail-closed identity conflicts, canonical JSON, and SHA-256 validation.
- `SqliteReadStore` now provides latest lifecycle state, latest canonical snapshot, and bounded strategy-evaluation reads. Lifecycle transitions map to `incidents`; `RECOVERY_BLOCKED` is `CRITICAL` and requires the persisted run's current next sequence index.
- Targeted GREEN: 35 runtime-persistence, 30 domain/storage/outbox, 21 canary/API, and 14 recovery tests passed. The full offline suite passed 289 tests with one historical conditional skip; `compileall` and `pip check` passed.
- Schema and migrations are unchanged at version 2 with the same nine required tables. Fresh SQLite evidence is `quick_check=ok`, `integrity_check=ok`, `journal_mode=wal`, `synchronous=2`, and `foreign_keys=1`.
- Every new write uses the existing `SqliteStore._connection`; no persistence method opens another write connection. Read seams remain on the query-only `SqliteReadStore` connection, and `WriteCommand`/`SqliteWriter.run()` were not expanded.
- Tasks 2–9 of C1 Runtime Integration were not started. The backend was not run and no network was used. Trading, authentication, wallet/signing, order, paper execution, provider, orchestration, frozen, contract, registry, config, package, and migration scope is unchanged.

## C1 Runtime Integration — Wave 1 Core Runtime

- Base `79cad7923f128450dca9bc5259e64a671b3b8d3e`; commits: adapters `da0fdbd483fd7025ae163f3a0024ac8b98346e15`, canonical pipeline `ff96d10acb1749c1c16f26ac96f98b7363244339`, runtime composition `5fb6c77ed2c1d9d09a9529bfe6e943d9368e0cb2`.
- `BinanceRuntimeAdapter` maps existing backfill/stream primitives to recovery, queue streaming, cancellation-safe close; `PolymarketRuntimeAdapter` maps discovery, 11/22 current-book reconciliation, available price history, exact-asset streaming, and explicit `NOT_AVAILABLE_NOT_REQUIRED` historical-depth classification. Adapters own no SQLite connection or unmanaged task.
- `CanonicalProjector` accepts committed inserted events only, rejects open/stale Binance state, requires all 22 reconciled assets and `LIVE_READY`, preserves causal source IDs and recovery origin, and emits deterministic immutable snapshots. Canary persistence now commits evaluation, infrastructure-only signal, and outbox in one transaction; injected failure leaves all three tables unchanged and replay remains idempotent.
- `C1RuntimeOrchestrator` exclusively owns the source queue and the three managed task handles (writer consumer, Binance stream, Polymarket stream). Providers only enqueue; one consumer commits raw events before cursor/projection/snapshot/canary, balances `task_done`, suppresses duplicate projection, drains accepted work on stop, and does not close the externally owned store.
- Startup uses `LifecycleStateMachine` and the exact 11-state `STARTUP_SEQUENCE`; each transition persists through lifecycle incidents. The legacy `RecoveryCoordinator` remains regression-covered but is not the runtime owner because its existing contract commits events directly instead of routing them through the single queue consumer. The requested `tests.test_lifecycle` module does not exist in the frozen repository layout; lifecycle coverage is in `tests.test_recovery`, runtime persistence, orchestrator, and existing single-instance tests.
- API health now separates database health, runtime readiness, and source health. Top-level `PASS` requires SQLite PASS, `LIVE_READY`, and both mandatory sources `LIVE`; STARTING or a missing source cannot report PASS. Market identity/counts and committed-event position are exposed from runtime status.
- Offline in-process integration used a temporary SQLite database, real storage/projector/canary/outbox/API construction, and fake adapters: discovery/recovery reached `LIVE_READY`, persisted market/source/cursor/snapshot/evaluation/signal/outbox rows, ignored duplicate redelivery, returned API PASS only after readiness, drained to zero, left no owned task pending, and retained database integrity.
- TDD evidence: adapters RED 14 errors then 14 GREEN; projection/atomic RED 20 errors then 20 GREEN; orchestrator/integration RED 21 errors then 25 GREEN. Unified targeted gate passed 213 tests; full offline suite passed 348 tests with one historical conditional skip. `compileall` and `pip check` passed.
- Schema remains nine tables at migration version 2; migrations and provider production files are unchanged. Fresh SQLite evidence: `quick_check=ok`, `integrity_check=ok`, `journal_mode=wal`, `synchronous=2`, `foreign_keys=1`.
- No provider network, backend process, port, mutex, Task 14, Wave 2, or Wave 3 operation ran. Registry execution, orders, paper fills, wallet/signing, authentication, and secrets remain absent. Gate: `C1_RUNTIME_CORE_PASS`.

## C1 Runtime Integration — Independent Audit Correction

- The earlier Wave 1 gate above is retained as historical evidence but was revoked by independent audit. Current corrected gate: `C1_RUNTIME_CORE_PASS`.
- Root-cause review confirmed RC-1 through RC-11: same-DB market timestamp conflict; live-before-backfill writes; current-books-before-history ordering; missing Binance continuity gate; normal stream exit accepted; writer failure not stopping startup; duplicate cursor/last-ID regression; multiple runtime write owners; metadata-only snapshots; unbounded source IDs; and non-idempotent recovered/current evaluation identity.
- TDD RED ran 14 adversarial tests against unchanged production code: 12 failures and one error reproduced RC-1 through RC-11 and lifecycle/API inconsistency; one pre-existing fail-closed identity-conflict baseline passed.
- Correction: one typed runtime command queue now serializes lifecycle, incident, market identity, source/cursor, canonical snapshot, evaluation, signal, and outbox writes through the single writer task. Fatal provider/writer state cancels streams, rejects pending futures, persists `RECOVERY_BLOCKED` where the writer remains available, and prevents later startup transitions.
- Same-DB combined proof runs R1 and R2 with different local clocks and one immutable market identity row. Exact identity replay ignores the local observation clock; changed authoritative payload remains `MARKET_IDENTITY_CONFLICT`.
- Cutover proof starts streams in buffering mode before recovery writes, commits Binance backfill and continuity first, then Polymarket history, then current books, creates a recovered evaluation, drains ordered buffered live events, requires one live event from each still-running stream, and only then creates the current reevaluation and reaches `LIVE_READY`.
- Binance continuity uses `reconcile_binance_minutes`; a missing closed minute ends in `RECOVERY_BLOCKED` with no later `LIVE_READY`. Polymarket history/current ordering and per-asset cursor namespaces prevent newer current books from being regressed by older history.
- Exact duplicates are no-ops for cursor and projection; new out-of-order rows remain append-only without rolling back the watermark; conflicting natural-key payloads fail closed; `last_event_id` uses a monotonic maximum.
- Normal provider return before stop is `UNEXPECTED_PROVIDER_STREAM_EXIT`. Source health becomes `LIVE` only after confirmed stream delivery, and both stream tasks must remain alive before `LIVE_READY`.
- Canonical snapshots now contain fixed-point Binance open/high/low/close/volume/open-time/source/origin plus 22 ordered MarketBook states with asset, market/outcome, best bid/ask/spread, book hash, timestamp, source ID, and origin. The current input set is bounded to `1 + asset_count`; the exercised maximum is 23 after 100 updates of one asset.
- Recovered and current canary evaluations explicitly use `RECOVERED_AFTER_DOWNTIME` and `CURRENT_LIVE_REEVALUATION`; both are infrastructure-only with execution/trading eligibility false. Evaluation identity uses canonical state plus origin rather than run ID/local clock, so R2 creates no duplicate recovered evaluation and does create a separate current reevaluation.
- Final consistency proof shows successful R1/R2 lifecycle states at `LIVE_READY` with no `RECOVERY_BLOCKED`, while failure paths persist `RECOVERY_BLOCKED`, expose `live_ready=false`, and cannot produce top-level API `PASS`.
- Final verification: 15/15 adversarial tests, 159/159 targeted runtime/storage/recovery/API tests, and 363 full-suite tests passed with one historical conditional skip. `compileall`, `pip check`, `git diff --check`, secret scan, and forbidden execution-surface scan passed.
- SQLite remained at migration version 2 and the same nine tables: `quick_check=ok`, `integrity_check=ok`, `journal_mode=wal`, `synchronous=2`, and `foreign_keys=1`. Migrations, provider production files, frozen/contract/registry/config/package files, `run_backend.py`, tools, and scripts are unchanged.
- Wave 2 and Wave 3 were not started. The backend, provider network, and Task 14 were not run. Registry execution, orders, paper fills, wallet/signing, authentication, and secrets remain absent.

## C1 Runtime Integration — Wave 2 Process Integration

- Base commit: `8eac311fe819499a9c0825f53fbde1dd04222c54`. Restart-identity bundle SHA-256: `5fee4e57c8c247d7cf50ea552f2607d3c72b5a2bce3bb4af3dd3d73b49790aed`; applied fix-only patch SHA-256: `d4f6b63ab4a047a92c2ed9981d0a6027705041b76cbdeb3189916daffe8f0338`.
- Root cause: `POLYMARKET_BOOK` natural identity is source/asset/book-hash, but storage previously treated a later observation timestamp as a conflicting identity field. TDD RED reproduced `SOURCE_EVENT_CONFLICT`; GREEN accepts an identical canonical hashed book replay while preserving conflicts for changed levels and strict timestamp identity for non-book events.
- Focused Windows verification passed 47 real-provider/storage tests, 24 reconnect/lifecycle/process-targeted tests, and the one-test cross-platform same-DB restart module. The full Windows suite passed 408 tests with one historical conditional skip; `compileall` and `pip check` passed.
- Polymarket reconnect is owned by the stream, uses bounded exponential backoff with jitter, cancels the previous heartbeat, reports transient disconnects, propagates cancellation, and leaves malformed payloads fail-closed.
- The local integration contract accepts only an absolute exact-schema JSON file with loopback HTTP/WS endpoints. The process gate used only dynamic `127.0.0.1` aiohttp fake endpoints; external provider endpoint calls were zero.
- The real Windows `run_backend.py` subprocess exposed API `STARTING`, then `PASS` with Binance and Polymarket `LIVE`, 11 markets, and 22 assets. The intentional local Polymarket disconnect reconnected and committed subsequent evidence.
- A simultaneous second backend exited 20. Outbox replay returned strictly increasing unique event IDs. The first `CTRL_BREAK_EVENT` produced exit 0 and released the API port and named mutex.
- Same-DB restart reached `PASS` without `SOURCE_EVENT_CONFLICT` or `RECOVERY_BLOCKED`; identical market/book identity replay remained idempotent and new live events continued to commit. The second `CTRL_BREAK_EVENT` also produced exit 0.
- Fresh SQLite evidence remained migration version 2 with nine tables, `quick_check=ok`, `integrity_check=ok`, `journal_mode=wal`, `synchronous=2`, and `foreign_keys=1`.
- Registry execution, real orders, paper fills, wallet/signing, credentials, and secrets remain absent. Wave 3 and Task 14 were not started in this integration cycle. Gate: `C1_PROCESS_INTEGRATION_PASS`.
