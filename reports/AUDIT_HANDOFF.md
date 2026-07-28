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
