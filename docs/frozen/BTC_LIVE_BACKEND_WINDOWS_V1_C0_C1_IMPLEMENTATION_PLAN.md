# BTC Live Backend Windows V1 — C0–C1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** построить на Windows минимальный постоянно работающий backend-контур, который фиксирует Registry 47, получает live BTC/Polymarket данные, восстанавливает пропуск после перезапуска, сохраняет события в SQLite и без ручного запроса отправляет deduplicated canary-сигнал через WebSocket.

**Architecture:** один Python-процесс и одна SQLite WAL-база. `aiohttp` используется как единственная runtime-зависимость одновременно для REST/WebSocket клиентов и локального REST/WebSocket сервера; вся доменная и persistence-логика остаётся на стандартной библиотеке Python. Сначала событие фиксируется в append-only store, затем обновляется projection, создаётся evaluation/signal и transactional outbox event, после commit событие отправляется клиенту.

**Tech Stack:** Windows 10/11, Python 3.12.4, `aiohttp==3.14.3`, `asyncio`, `sqlite3`, `unittest`, PowerShell 5.1.

## Global Constraints

- Project root: `C:\Users\gegos\Documents\Codex\btc_live_backend_windows_v1`
- Data root: `C:\Users\gegos\Documents\Codex\btc_live_backend_windows_v1_data`
- Runtime root: `C:\Users\gegos\Documents\Codex\btc_live_backend_windows_v1_runtime`
- Platform: Windows only for V1
- Backend processes: exactly `1`
- Database writers: exactly `1`
- Database: SQLite with `journal_mode=WAL`, `synchronous=FULL`, `foreign_keys=ON`
- Runtime dependencies: only `aiohttp==3.14.3` plus Python standard library
- Tests: standard-library `unittest`; no pytest dependency
- HTTP/WS bind: `127.0.0.1:8767`
- Remote access: disabled
- Binance: public market data only, `BTCUSDT`, `1m`
- Polymarket: public Gamma/CLOB/market WebSocket only
- Real order submission: absent
- Wallet/private-key support: absent
- Paper execution: outside C0–C1
- Dashboard: outside C0–C1
- Registry 47 execution: outside C0–C1
- Canary: infrastructure-only; never trading or paper eligible
- Write raw event before projection, compute or push
- Recovered signal is never represented as a current live signal
- Full historical Polymarket depth is not invented
- Maximum two technical hotfixes per release
- A third required patch stops the release for architectural review
- No Docker, Redis, PostgreSQL or child PowerShell
- No `ExecutionPolicy Bypass`, `Unblock-File`, `.bat`, `.cmd` or bundled executables

---

## Frozen Provider Contracts

### Binance

REST public market-data primary base:

```text
https://data-api.binance.vision
```

Fallbacks:

```text
https://api.binance.com
https://api-gcp.binance.com
```

Klines:

```text
GET /api/v3/klines
symbol=BTCUSDT
interval=1m
startTime=<milliseconds>
endTime=<milliseconds>
limit=1000
```

Live stream:

```text
wss://stream.binance.com:9443/ws/btcusdt@kline_1m
```

Canonical history accepts only payloads where `k.x == true`. The natural key is:

```text
binance:BTCUSDT:1m:<k.t>
```

### Polymarket

Market discovery uses public Gamma data. CLOB market data:

```text
https://clob.polymarket.com
```

Current orderbook:

```text
GET /book?token_id=<asset_id>
```

Price history:

```text
GET /prices-history?market=<asset_id>&startTs=<seconds>&endTs=<seconds>&fidelity=1
```

Live market channel:

```text
wss://ws-subscriptions-clob.polymarket.com/ws/market
```

Subscription:

```json
{
  "assets_ids": ["<yes_token_id>", "<no_token_id>"],
  "type": "market",
  "custom_feature_enabled": true
}
```

Client sends `PING` every 10 seconds. Events handled in C1:

```text
book
price_change
last_trade_price
best_bid_ask
tick_size_change
market_resolved
```

Unknown event types are persisted and marked `UNHANDLED_PROVIDER_EVENT`, not discarded.

---

## File Structure

```text
btc_live_backend_windows_v1/
├─ config/
│  ├─ c0_c1_frozen_config.json
│  └─ logging.json
├─ contract/
│  ├─ BTC_LIVE_BACKEND_WINDOWS_V1_CONTRACT.json
│  └─ STRATEGY_REGISTRY_47_LIVE_BACKEND_LOCK.json
├─ registry/
│  ├─ STRATEGY_REGISTRY_47.json
│  └─ STRATEGY_REGISTRY_47.csv
├─ migrations/
│  ├─ 0001_core.sql
│  └─ 0002_indexes.sql
├─ src/
│  ├─ __init__.py
│  ├─ app.py
│  ├─ config.py
│  ├─ lifecycle.py
│  ├─ fixed_point.py
│  ├─ models.py
│  ├─ storage.py
│  ├─ outbox.py
│  ├─ registry_lock.py
│  ├─ market_discovery.py
│  ├─ binance_provider.py
│  ├─ polymarket_provider.py
│  ├─ recovery.py
│  ├─ canary.py
│  ├─ api.py
│  ├─ health.py
│  └─ single_instance.py
├─ tests/
│  ├─ fixtures/
│  │  ├─ binance_kline_closed.json
│  │  ├─ binance_kline_open.json
│  │  ├─ polymarket_book.json
│  │  ├─ polymarket_price_change.json
│  │  └─ gamma_btc_daily_range.json
│  ├─ test_config_registry.py
│  ├─ test_storage_outbox.py
│  ├─ test_binance_provider.py
│  ├─ test_polymarket_provider.py
│  ├─ test_recovery.py
│  ├─ test_canary_api.py
│  ├─ test_single_instance.py
│  └─ test_live_contract_smoke.py
├─ tools/
│  ├─ create_test_fixture_db.py
│  ├─ simulate_downtime.py
│  └─ ws_watch_client.py
├─ scripts/
│  ├─ RUN_BACKEND_SAFE.ps1
│  ├─ RUN_TESTS_SAFE.ps1
│  └─ RUN_C1_ACCEPTANCE_SAFE.ps1
├─ run_backend.py
├─ pyproject.toml
├─ requirements.in
├─ requirements.lock
├─ README.md
└─ START_HERE.md
```

Each production module has one primary responsibility. C0–C1 create no `paper/`, `orders/`, `wallet/` or dashboard source tree.

---

### Task 1: Freeze the Windows Environment and Package Contract

**Files:**
- Create: `pyproject.toml`
- Create: `requirements.in`
- Create: `requirements.lock`
- Create: `config/c0_c1_frozen_config.json`
- Create: `src/config.py`
- Test: `tests/test_config_registry.py`

**Interfaces:**
- Produces:
  - `RuntimeConfig`
  - `load_runtime_config(path: Path) -> RuntimeConfig`
  - `validate_runtime_config(config: RuntimeConfig) -> None`

- [ ] **Step 1: Write failing configuration tests**

```python
class ConfigTests(unittest.TestCase):
    def test_bind_is_loopback_only(self):
        cfg = load_runtime_config(Path("config/c0_c1_frozen_config.json"))
        self.assertEqual(cfg.bind_host, "127.0.0.1")
        self.assertEqual(cfg.bind_port, 8767)

    def test_forbidden_features_are_disabled(self):
        cfg = load_runtime_config(Path("config/c0_c1_frozen_config.json"))
        self.assertFalse(cfg.real_orders_enabled)
        self.assertFalse(cfg.wallet_enabled)
        self.assertFalse(cfg.paper_enabled)
        self.assertFalse(cfg.dashboard_enabled)
```

- [ ] **Step 2: Run RED**

```powershell
Set-Location "C:\Users\gegos\Documents\Codex\btc_live_backend_windows_v1"
& ".\.venv\Scripts\python.exe" -m unittest tests.test_config_registry.ConfigTests -v
```

Expected: import/file failure because `src.config` does not exist.

- [ ] **Step 3: Create dedicated virtual environment**

```powershell
Set-Location "C:\Users\gegos\Documents\Codex\btc_live_backend_windows_v1"
py -3.12 -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install "aiohttp==3.14.3"
& ".\.venv\Scripts\python.exe" -m pip check
```

`requirements.in` contains exactly:

```text
aiohttp==3.14.3
```

Generate the transitive lock from the verified environment:

```powershell
& ".\.venv\Scripts\python.exe" -m pip freeze |
  Sort-Object |
  Set-Content -Encoding ASCII requirements.lock
```

- [ ] **Step 4: Implement typed immutable config**

Use `@dataclass(frozen=True, slots=True)`. Reject:

```text
non-loopback bind
port outside 1024..65535
real_orders_enabled=true
wallet_enabled=true
database_writer_count != 1
unknown top-level config keys
```

- [ ] **Step 5: Run GREEN**

```powershell
& ".\.venv\Scripts\python.exe" -m unittest tests.test_config_registry.ConfigTests -v
```

Expected: all Task 1 tests pass.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml requirements.in requirements.lock config src/config.py tests/test_config_registry.py
git commit -m "build: freeze Windows C0-C1 runtime"
```

**Gate:** `C0_RUNTIME_FREEZE_PASS`

---

### Task 2: Lock Registry 47 and Emit the Executable-Rule Gap Report

**Files:**
- Create: `src/registry_lock.py`
- Copy: `contract/STRATEGY_REGISTRY_47_LIVE_BACKEND_LOCK.json`
- Copy: `registry/STRATEGY_REGISTRY_47.json`
- Copy: `registry/STRATEGY_REGISTRY_47.csv`
- Create: `reports/EXECUTABLE_RULE_GAP_REPORT.json`
- Test: `tests/test_config_registry.py`

**Interfaces:**
- Produces:
  - `verify_registry_lock(...) -> RegistryLockReport`
  - `build_executable_gap_report(...) -> dict[str, Any]`

- [ ] **Step 1: Write failing lock tests**

```python
def test_registry_has_exactly_47_unique_ids(self):
    report = verify_registry_lock(
        Path("contract/STRATEGY_REGISTRY_47_LIVE_BACKEND_LOCK.json"),
        Path("registry/STRATEGY_REGISTRY_47.json"),
        Path("registry/STRATEGY_REGISTRY_47.csv"),
    )
    self.assertEqual(report.strategy_count, 47)
    self.assertEqual(report.unique_strategy_ids, 47)

def test_registry_strategy_is_not_enabled_by_identity_alone(self):
    gap = build_executable_gap_report(...)
    self.assertEqual(gap["executable_now"], 0)
    self.assertEqual(gap["requires_parity"], 47)
```

- [ ] **Step 2: Run RED**

```powershell
& ".\.venv\Scripts\python.exe" -m unittest tests.test_config_registry.RegistryLockTests -v
```

- [ ] **Step 3: Implement exact SHA and identity checks**

Validation must fail on:

```text
JSON hash mismatch
CSV hash mismatch
count != 47
duplicate strategy_id
missing V1/V2 lineage
registry ID absent from lock
unexpected registry ID
```

- [ ] **Step 4: Emit a non-executable status for all 47**

Each row in the gap report:

```json
{
  "strategy_id": "YES_STRICT_A_OPERATIONAL",
  "registry_locked": true,
  "exact_rule_source_locked": false,
  "historical_parity": "NOT_RUN",
  "runtime_status": "DISABLED_EXECUTABLE_RULE_GAP"
}
```

No strategy logic is inferred from its name or metrics.

- [ ] **Step 5: Run GREEN and commit**

```powershell
& ".\.venv\Scripts\python.exe" -m unittest tests.test_config_registry -v
git add src/registry_lock.py contract registry reports tests/test_config_registry.py
git commit -m "feat: lock registry 47 for live backend"
```

**Gate:** `C0_REGISTRY_47_LOCK_PASS`

---

### Task 3: Define Fixed-Point Domain Models and Provider Event Envelopes

**Files:**
- Create: `src/fixed_point.py`
- Create: `src/models.py`
- Test: `tests/test_storage_outbox.py`

**Interfaces:**
- Produces:
  - `ProbabilityMicros`
  - `SharesMicros`
  - `UsdMicros`
  - `SourceEvent`
  - `CanonicalSnapshot`
  - `StrategyEvaluation`
  - `SignalRecord`
  - `OutboxEvent`

- [ ] **Step 1: Write failing fixed-point tests**

```python
def test_probability_string_converts_exactly(self):
    self.assertEqual(probability_to_micros("0.456"), 456000)

def test_probability_outside_unit_interval_is_rejected(self):
    with self.assertRaisesRegex(ValueError, "PROBABILITY_OUT_OF_RANGE"):
        probability_to_micros("1.001")
```

- [ ] **Step 2: Write failing deterministic event-key tests**

```python
def test_binance_closed_kline_natural_key(self):
    event = SourceEvent.binance_closed_kline(
        symbol="BTCUSDT",
        interval="1m",
        open_time_ms=1000,
        payload=b"{}",
    )
    self.assertEqual(event.natural_key, "binance:BTCUSDT:1m:1000")
```

- [ ] **Step 3: Run RED**

```powershell
& ".\.venv\Scripts\python.exe" -m unittest tests.test_storage_outbox.DomainTests -v
```

- [ ] **Step 4: Implement with `decimal.Decimal` at boundaries**

Provider decimal strings convert once into integers. Persisted financial values never use Python float.

- [ ] **Step 5: Run GREEN and commit**

```powershell
& ".\.venv\Scripts\python.exe" -m unittest tests.test_storage_outbox.DomainTests -v
git add src/fixed_point.py src/models.py tests/test_storage_outbox.py
git commit -m "feat: add fixed-point domain contracts"
```

**Gate:** `C1_DOMAIN_CONTRACT_PASS`

---

### Task 4: Create SQLite Migrations, Single Writer and Append-Only Event Store

**Files:**
- Create: `migrations/0001_core.sql`
- Create: `migrations/0002_indexes.sql`
- Create: `src/storage.py`
- Test: `tests/test_storage_outbox.py`

**Interfaces:**
- Produces:
  - `SqliteStore.open(path: Path) -> SqliteStore`
  - `SqliteStore.migrate() -> None`
  - `SqliteStore.append_source_event(event) -> AppendResult`
  - `SqliteWriter.run(queue: asyncio.Queue[WriteCommand]) -> None`
  - `SqliteStore.integrity_report() -> dict[str, Any]`

- [ ] **Step 1: Write failing migration tests**

```python
def test_database_uses_required_pragmas(self):
    store = SqliteStore.open(self.db_path)
    store.migrate()
    self.assertEqual(store.scalar("PRAGMA journal_mode").lower(), "wal")
    self.assertEqual(store.scalar("PRAGMA synchronous"), 2)
    self.assertEqual(store.scalar("PRAGMA foreign_keys"), 1)
```

- [ ] **Step 2: Write failing idempotency test**

```python
def test_duplicate_source_event_is_committed_once(self):
    first = self.store.append_source_event(self.event)
    second = self.store.append_source_event(self.event)
    self.assertTrue(first.inserted)
    self.assertFalse(second.inserted)
    self.assertEqual(self.store.count("source_events"), 1)
```

- [ ] **Step 3: Run RED**

```powershell
& ".\.venv\Scripts\python.exe" -m unittest tests.test_storage_outbox.StorageTests -v
```

- [ ] **Step 4: Implement schema**

Required core columns:

```sql
source_events(
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  natural_key TEXT NOT NULL UNIQUE,
  source_timestamp_ms INTEGER NOT NULL,
  received_timestamp_ms INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  recovery_origin TEXT NOT NULL,
  committed_at_ms INTEGER NOT NULL
)
```

Add the other tables frozen in the architecture contract:

```text
source_cursors
market_catalog
canonical_state
strategy_evaluations
signals
outbox_events
incidents
schema_migrations
```

- [ ] **Step 5: Enforce one writer**

Only `SqliteWriter` owns the write connection. REST/WS handlers use a separate read-only connection opened with URI mode:

```text
file:<db>?mode=ro
```

- [ ] **Step 6: Run GREEN and commit**

```powershell
& ".\.venv\Scripts\python.exe" -m unittest tests.test_storage_outbox.StorageTests -v
git add migrations src/storage.py tests/test_storage_outbox.py
git commit -m "feat: add append-only SQLite event store"
```

**Gate:** `C1_EVENT_STORE_PASS`

---

### Task 5: Add Transactional Outbox and Resumable Event Delivery

**Files:**
- Create: `src/outbox.py`
- Modify: `src/storage.py`
- Test: `tests/test_storage_outbox.py`

**Interfaces:**
- Produces:
  - `commit_signal_and_outbox(...) -> int`
  - `read_outbox_after(event_id: int, limit: int) -> list[OutboxEvent]`
  - `OutboxBroker.publish_committed(event_id: int) -> None`

- [ ] **Step 1: Write failing atomicity test**

```python
def test_signal_and_outbox_are_atomic(self):
    signal_id, outbox_id = self.store.commit_signal_and_outbox(...)
    self.assertIsNotNone(signal_id)
    self.assertIsNotNone(outbox_id)
    self.assertEqual(self.store.count("signals"), 1)
    self.assertEqual(self.store.count("outbox_events"), 1)
```

- [ ] **Step 2: Write failing resume test**

```python
def test_outbox_resume_is_strictly_after_event_id(self):
    ids = [self.insert_outbox() for _ in range(3)]
    rows = self.store.read_outbox_after(ids[0], limit=100)
    self.assertEqual([row.event_id for row in rows], ids[1:])
```

- [ ] **Step 3: Run RED**

```powershell
& ".\.venv\Scripts\python.exe" -m unittest tests.test_storage_outbox.OutboxTests -v
```

- [ ] **Step 4: Implement one-transaction signal/outbox commit**

Do not publish until the SQLite transaction has committed.

- [ ] **Step 5: Run GREEN and commit**

```powershell
& ".\.venv\Scripts\python.exe" -m unittest tests.test_storage_outbox -v
git add src/outbox.py src/storage.py tests/test_storage_outbox.py
git commit -m "feat: add transactional outbox"
```

**Gate:** `C1_TRANSACTIONAL_OUTBOX_PASS`

---

### Task 6: Implement Binance Closed-Kline Live Adapter and REST Backfill

**Files:**
- Create: `src/binance_provider.py`
- Create fixtures: `tests/fixtures/binance_kline_closed.json`, `tests/fixtures/binance_kline_open.json`
- Test: `tests/test_binance_provider.py`

**Interfaces:**
- Produces:
  - `parse_binance_kline_message(payload: dict) -> SourceEvent | None`
  - `iter_binance_backfill(session, start_ms, end_ms) -> AsyncIterator[SourceEvent]`
  - `BinanceStream.run(buffer: asyncio.Queue[SourceEvent]) -> None`

- [ ] **Step 1: Write failing closed/open tests**

```python
def test_closed_kline_becomes_event(self):
    payload = load_fixture("binance_kline_closed.json")
    event = parse_binance_kline_message(payload)
    self.assertEqual(event.event_type, "BINANCE_KLINE_CLOSED")

def test_open_kline_is_not_canonical_history(self):
    payload = load_fixture("binance_kline_open.json")
    self.assertIsNone(parse_binance_kline_message(payload))
```

- [ ] **Step 2: Write failing pagination boundary test**

Synthetic REST pages must prove:

```text
no duplicate open_time
no gap
last closed minute included
current open minute excluded
```

- [ ] **Step 3: Run RED**

```powershell
& ".\.venv\Scripts\python.exe" -m unittest tests.test_binance_provider -v
```

- [ ] **Step 4: Implement bounded backfill**

Advance the next page with:

```python
next_start_ms = last_open_time_ms + 60_000
```

Stop when `next_start_ms > end_ms`.

- [ ] **Step 5: Implement reconnect policy**

```text
initial delay 1 second
doubling to 30 seconds
±20% jitter
reset after 5 continuous healthy minutes
```

Every disconnect creates an incident; it does not terminate the backend.

- [ ] **Step 6: Run GREEN and commit**

```powershell
& ".\.venv\Scripts\python.exe" -m unittest tests.test_binance_provider -v
git add src/binance_provider.py tests/fixtures tests/test_binance_provider.py
git commit -m "feat: add Binance live and backfill adapter"
```

**Gate:** `C1_BINANCE_PROVIDER_PASS`

---

### Task 7: Implement Polymarket Discovery, Current Book and Market WebSocket

**Files:**
- Create: `src/market_discovery.py`
- Create: `src/polymarket_provider.py`
- Create fixtures:
  - `tests/fixtures/gamma_btc_daily_range.json`
  - `tests/fixtures/polymarket_book.json`
  - `tests/fixtures/polymarket_price_change.json`
- Test: `tests/test_polymarket_provider.py`

**Interfaces:**
- Produces:
  - `discover_active_btc_daily_range(...) -> MarketIdentity`
  - `fetch_current_books(...) -> list[SourceEvent]`
  - `iter_price_history(...) -> AsyncIterator[SourceEvent]`
  - `parse_market_ws_message(payload) -> list[SourceEvent]`
  - `PolymarketStream.run(asset_ids, buffer) -> None`

- [ ] **Step 1: Write failing discovery test**

```python
def test_discovery_selects_one_active_future_resolution_event(self):
    identity = discover_from_payload(load_fixture("gamma_btc_daily_range.json"), now_utc=NOW)
    self.assertTrue(identity.active)
    self.assertEqual(len(identity.asset_ids), 22)
    self.assertEqual(len(identity.outcomes), 11)
```

The fixture represents one 11-bucket BTC Daily Range event with YES/NO token IDs per market.

- [ ] **Step 2: Write failing book and price-change tests**

```python
def test_book_event_uses_provider_hash_for_natural_key(self):
    event = parse_market_ws_message(load_fixture("polymarket_book.json"))[0]
    self.assertIn(event.payload["hash"], event.natural_key)

def test_zero_size_price_change_removes_level(self):
    events = parse_market_ws_message(load_fixture("polymarket_price_change.json"))
    self.assertEqual(events[0].payload["size_micros"], 0)
```

- [ ] **Step 3: Write heartbeat test**

Use an aiohttp test WebSocket server. Assert that the client sends literal `PING` no later than 10 seconds after subscription and accepts `PONG`.

- [ ] **Step 4: Run RED**

```powershell
& ".\.venv\Scripts\python.exe" -m unittest tests.test_polymarket_provider -v
```

- [ ] **Step 5: Implement schema-preserving parser**

Persist all provider fields needed for later audit. Unknown event type:

```text
event_type = UNHANDLED_PROVIDER_EVENT
incident severity = WARNING
```

- [ ] **Step 6: Implement book reconciliation**

A `book` snapshot replaces the local book for that asset. `price_change` mutates only listed levels. The resulting projection records:

```text
best_bid
best_ask
spread
book_hash
source_timestamp_ms
last_reconciled_event_id
```

- [ ] **Step 7: Run GREEN and commit**

```powershell
& ".\.venv\Scripts\python.exe" -m unittest tests.test_polymarket_provider -v
git add src/market_discovery.py src/polymarket_provider.py tests
git commit -m "feat: add Polymarket live market adapter"
```

**Gate:** `C1_POLYMARKET_PROVIDER_PASS`

---

### Task 8: Implement Cutover Buffering, Backfill and Restart Reconciliation

**Files:**
- Create: `src/recovery.py`
- Modify: `src/lifecycle.py`
- Test: `tests/test_recovery.py`

**Interfaces:**
- Produces:
  - `RecoveryCoordinator.run() -> RecoveryReport`
  - `reconcile_binance_minutes(...) -> ContinuityReport`
  - `reconcile_polymarket_state(...) -> MarketReconciliationReport`
  - `classify_recovered_evaluation(...) -> RecoveryClassification`

- [ ] **Step 1: Write failing cutover ordering test**

```python
def test_live_buffer_is_drained_only_after_backfill_commit(self):
    trace = run_synthetic_cutover(
        backfill_events=[minute(1), minute(2)],
        buffered_events=[minute(2), minute(3)],
    )
    self.assertEqual(trace.committed_minutes, [1, 2, 3])
    self.assertEqual(trace.duplicate_count, 1)
    self.assertLess(trace.backfill_complete_index, trace.buffer_drain_start_index)
```

- [ ] **Step 2: Write failing 10-minute outage test**

```python
def test_ten_minute_binance_gap_is_fully_recovered(self):
    report = recover_synthetic_gap(last_open_ms=T0, now_ms=T0 + 11 * MINUTE)
    self.assertEqual(report.missing_closed_minutes, 0)
    self.assertEqual(report.recovered_closed_minutes, 10)
```

- [ ] **Step 3: Write failing Polymarket gap-classification test**

```python
def test_missing_historical_depth_is_explicitly_blocked(self):
    result = classify_recovered_evaluation(
        price_history=True,
        current_book=True,
        historical_depth=False,
        requires_depth=True,
    )
    self.assertEqual(result.status, "BLOCKED_MISSING_HISTORICAL_DEPTH")
```

- [ ] **Step 4: Run RED**

```powershell
& ".\.venv\Scripts\python.exe" -m unittest tests.test_recovery -v
```

- [ ] **Step 5: Implement persistent startup states**

Each state transition is committed to `incidents`/health state. Backend cannot set `LIVE_READY` until all mandatory reconciliation gates pass.

- [ ] **Step 6: Implement recovered-event origin**

```text
LIVE
REST_BACKFILL
BUFFERED_DURING_RECOVERY
RECOVERED_AFTER_DOWNTIME
```

- [ ] **Step 7: Run GREEN and commit**

```powershell
& ".\.venv\Scripts\python.exe" -m unittest tests.test_recovery -v
git add src/recovery.py src/lifecycle.py tests/test_recovery.py
git commit -m "feat: add restart recovery coordinator"
```

**Gate:** `C1_RESTART_RECOVERY_PASS`

---

### Task 9: Implement the Infrastructure Canary and Idempotent Evaluation

**Files:**
- Create: `src/canary.py`
- Test: `tests/test_canary_api.py`

**Interfaces:**
- Produces:
  - `evaluate_canary(snapshot: CanonicalSnapshot) -> StrategyEvaluation`
  - `commit_canary_if_new(...) -> SignalRecord | None`

- [ ] **Step 1: Write failing eligibility test**

```python
def test_canary_requires_both_sources_live_ready(self):
    snapshot = snapshot_fixture(binance_ready=True, polymarket_ready=False)
    result = evaluate_canary(snapshot)
    self.assertEqual(result.status, "BLOCKED")
    self.assertEqual(result.reason_code, "POLYMARKET_NOT_RECONCILED")
```

- [ ] **Step 2: Write failing once-per-session test**

```python
def test_duplicate_closed_kline_does_not_duplicate_canary(self):
    first = commit_canary_if_new(self.store, self.snapshot)
    second = commit_canary_if_new(self.store, self.snapshot)
    self.assertIsNotNone(first)
    self.assertIsNone(second)
    self.assertEqual(self.store.count("signals"), 1)
```

- [ ] **Step 3: Write failing recovered-signal test**

```python
def test_recovered_canary_is_never_execution_eligible(self):
    signal = commit_canary_if_new(self.store, self.recovered_snapshot)
    self.assertEqual(signal.origin, "RECOVERED_AFTER_DOWNTIME")
    self.assertFalse(signal.execution_eligible)
```

- [ ] **Step 4: Run RED**

```powershell
& ".\.venv\Scripts\python.exe" -m unittest tests.test_canary_api.CanaryTests -v
```

- [ ] **Step 5: Implement and commit**

```powershell
& ".\.venv\Scripts\python.exe" -m unittest tests.test_canary_api.CanaryTests -v
git add src/canary.py tests/test_canary_api.py
git commit -m "feat: add infrastructure canary"
```

**Gate:** `C1_CANARY_PASS`

---

### Task 10: Expose REST Bootstrap and Resumable WebSocket Push

**Files:**
- Create: `src/api.py`
- Create: `tools/ws_watch_client.py`
- Test: `tests/test_canary_api.py`

**Interfaces:**
- Produces:
  - `create_api_app(read_store, outbox_broker) -> aiohttp.web.Application`
  - REST routes frozen in the architecture contract
  - `/ws/v1/events?after_event_id=<int>`

- [ ] **Step 1: Write failing bootstrap test**

```python
async def test_bootstrap_contains_health_sources_and_last_event_id(self):
    response = await self.client.get("/api/v1/bootstrap")
    body = await response.json()
    self.assertIn("health", body)
    self.assertIn("sources", body)
    self.assertIn("last_event_id", body)
```

- [ ] **Step 2: Write failing push-without-refresh test**

```python
async def test_committed_canary_is_pushed_without_http_request(self):
    ws = await self.client.ws_connect("/ws/v1/events?after_event_id=0")
    await self.commit_canary()
    message = await ws.receive_json(timeout=2)
    self.assertEqual(message["event_type"], "SIGNAL_CREATED")
```

- [ ] **Step 3: Write failing reconnect replay test**

```python
async def test_reconnect_replays_missed_outbox_events(self):
    first = await self.commit_outbox()
    second = await self.commit_outbox()
    ws = await self.client.ws_connect(f"/ws/v1/events?after_event_id={first}")
    message = await ws.receive_json(timeout=2)
    self.assertEqual(message["event_id"], second)
```

- [ ] **Step 4: Run RED**

```powershell
& ".\.venv\Scripts\python.exe" -m unittest tests.test_canary_api.ApiTests -v
```

- [ ] **Step 5: Implement ordered replay then live subscription**

Connection sequence:

```text
validate after_event_id
read and send committed backlog
subscribe broker
recheck database for race-window events
stream future committed events
```

Deduplicate by `event_id`.

- [ ] **Step 6: Run GREEN and commit**

```powershell
& ".\.venv\Scripts\python.exe" -m unittest tests.test_canary_api -v
git add src/api.py tools/ws_watch_client.py tests/test_canary_api.py
git commit -m "feat: expose resumable REST and WebSocket API"
```

**Gate:** `C1_PUSH_API_PASS`

---

### Task 11: Add Single-Instance Windows Lifecycle and Graceful Shutdown

**Files:**
- Create: `src/single_instance.py`
- Create: `src/app.py`
- Create: `run_backend.py`
- Test: `tests/test_single_instance.py`

**Interfaces:**
- Produces:
  - `WindowsMutex.acquire(name: str) -> WindowsMutex`
  - `LiveBackend.start()`
  - `LiveBackend.stop()`
  - process exit codes:
    - `0` clean stop
    - `20` already running
    - `30` contract/config failure
    - `40` database integrity failure

- [ ] **Step 1: Write failing mutex test**

```python
def test_second_instance_is_rejected(self):
    first = WindowsMutex.acquire("BTC_LIVE_BACKEND_WINDOWS_V1")
    with self.assertRaisesRegex(AlreadyRunningError, "BACKEND_ALREADY_RUNNING"):
        WindowsMutex.acquire("BTC_LIVE_BACKEND_WINDOWS_V1")
    first.close()
```

- [ ] **Step 2: Write failing shutdown-order test**

Expected order:

```text
stop accepting API connections
stop provider reconnect loops
drain source queue
commit pending writer commands
flush outbox state
close read connections
close writer
release mutex
```

- [ ] **Step 3: Run RED**

```powershell
& ".\.venv\Scripts\python.exe" -m unittest tests.test_single_instance -v
```

- [ ] **Step 4: Implement Windows named mutex with `ctypes`**

No third-party process-lock dependency.

- [ ] **Step 5: Run GREEN and commit**

```powershell
& ".\.venv\Scripts\python.exe" -m unittest tests.test_single_instance -v
git add src/single_instance.py src/app.py run_backend.py tests/test_single_instance.py
git commit -m "feat: add Windows backend lifecycle"
```

**Gate:** `C1_WINDOWS_LIFECYCLE_PASS`

---

### Task 12: Build Safe PowerShell Launchers and Offline Contract Tests

**Files:**
- Create: `scripts/RUN_BACKEND_SAFE.ps1`
- Create: `scripts/RUN_TESTS_SAFE.ps1`
- Create: `scripts/RUN_C1_ACCEPTANCE_SAFE.ps1`
- Create: `README.md`
- Create: `START_HERE.md`
- Test: `tests/test_live_contract_smoke.py`

**Interfaces:**
- Produces one-command Windows test/run/acceptance entry points.

- [ ] **Step 1: Write failing launcher safety tests**

```python
def test_launchers_do_not_use_forbidden_powershell(self):
    for path in Path("scripts").glob("*.ps1"):
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("ExecutionPolicy", text)
        self.assertNotIn("Unblock-File", text)
        self.assertNotIn("powershell.exe", text.lower())
```

- [ ] **Step 2: Write forbidden-module scan**

Fail if the source tree contains:

```text
wallet
private_key
sign_order
place_order
cancel_order
api_secret
```

Allow the exact phrases only in tests/docs asserting absence.

- [ ] **Step 3: Run RED**

```powershell
& ".\.venv\Scripts\python.exe" -m unittest tests.test_live_contract_smoke -v
```

- [ ] **Step 4: Implement launchers**

`RUN_BACKEND_SAFE.ps1`:

```text
sets project root
verifies Python 3.12.x
verifies requirements.lock with pip check
sets PYTHONNOUSERSITE=1
sets PYTHONDONTWRITEBYTECODE=1
creates data/runtime directories
calls .venv Python directly
writes one run log
```

It must not install packages automatically.

- [ ] **Step 5: Run GREEN and commit**

```powershell
& ".\.venv\Scripts\python.exe" -m unittest tests.test_live_contract_smoke -v
git add scripts README.md START_HERE.md tests/test_live_contract_smoke.py
git commit -m "build: add safe Windows C1 launchers"
```

**Gate:** `C1_SAFE_DELIVERY_PASS`

---

### Task 13: Run Full Offline Verification and Real Provider Capability Smoke

**Files:**
- Modify: `tests/test_live_contract_smoke.py`
- Create: `reports/C1_OFFLINE_VERIFICATION.json`
- Create at run time: `reports/C1_PROVIDER_CAPABILITY_SMOKE.json`

**Interfaces:**
- Produces final pre-live evidence before intentional downtime test.

- [ ] **Step 1: Run complete offline suite**

```powershell
Set-Location "C:\Users\gegos\Documents\Codex\btc_live_backend_windows_v1"
& ".\.venv\Scripts\python.exe" -m unittest discover -s tests -v
& ".\.venv\Scripts\python.exe" -m compileall -q src tests tools run_backend.py
& ".\.venv\Scripts\python.exe" -m pip check
```

Expected:

```text
0 failures
0 errors
compileall exit 0
pip check exit 0
```

- [ ] **Step 2: Run read-only provider smoke**

The smoke performs:

```text
Binance REST: latest closed 1m candles
Binance WS: receive one kline payload
Gamma: discover one active BTC Daily Range event
CLOB REST: current books for discovered asset IDs
CLOB price history: bounded one-hour query
Polymarket WS: subscribe, send PING, receive at least one valid response/event
```

No authenticated endpoint and no order method is imported or called.

- [ ] **Step 3: Freeze observed provider schemas**

The capability report records:

```text
endpoint
HTTP/status or WS handshake
observed top-level fields
timestamp unit
market/token identity
payload SHA-256
latency
no secret/auth used
```

Unexpected schema blocks C1 rather than adding permissive guessing.

- [ ] **Step 4: Commit**

```bash
git add tests reports
git commit -m "test: verify C1 provider capabilities"
```

**Gate:** `C1_PROVIDER_CAPABILITY_PASS`

---

### Task 14: Execute the C1 Intentional-Downtime Acceptance

**Files:**
- Create: `tools/simulate_downtime.py`
- Create at run time:
  - `reports/C1_DOWNTIME_ACCEPTANCE.json`
  - `reports/C1_FINAL_ACCEPTANCE.json`
  - `artifacts/C1_ACCEPTANCE_PACK.zip`

**Interfaces:**
- Produces the first real acceptance artifact for Stage C.

- [ ] **Step 1: Start backend and watcher**

Terminal 1:

```powershell
& ".\scripts\RUN_BACKEND_SAFE.ps1"
```

Terminal 2:

```powershell
& ".\.venv\Scripts\python.exe" tools\ws_watch_client.py --after-event-id 0
```

- [ ] **Step 2: Confirm initial live state**

Required:

```text
startup_state = LIVE_READY
Binance source health = LIVE
Polymarket source health = LIVE
current market identity present
current orderbooks reconciled
```

- [ ] **Step 3: Stop backend for exactly 10 full minutes**

The tool writes a signed local test marker with:

```text
stop_requested_at_ms
process_stopped_at_ms
restart_requested_at_ms
```

This is an acceptance action, not a mocked unit test.

- [ ] **Step 4: Restart backend**

Required recovery outcomes:

```text
all closed Binance 1m candles recovered
duplicate closed-minute keys = 0
current Polymarket books reconciled
historical-depth completeness explicitly classified
startup reaches LIVE_READY
```

- [ ] **Step 5: Verify canary semantics**

Required:

```text
exactly one current-session canary signal
no duplicate after source redelivery
recovered records carry RECOVERED_AFTER_DOWNTIME
recovered record execution_eligible = false
current live reevaluation is a separate row
```

- [ ] **Step 6: Verify client push and reconnect**

Close the watcher before one outbox event, reopen with its last event ID and prove:

```text
missed event replayed
event IDs strictly increase
no manual REST request triggered evaluation
backend continued collecting while client was closed
```

- [ ] **Step 7: Build acceptance pack**

Pack includes:

```text
contract/config hashes
registry lock report
provider capability report
database integrity report
source continuity report
downtime timestamps
canary/evaluation rows
outbox replay evidence
incidents
logs
SHA256SUMS
```

- [ ] **Step 8: Commit acceptance tooling**

```bash
git add tools/simulate_downtime.py
git commit -m "test: add C1 intentional downtime acceptance"
```

**Final C1 Gate:** `BTC_LIVE_BACKEND_WINDOWS_V1_C1_PASS`

---

## Final C0–C1 Verification Checklist

Before claiming C0–C1 complete:

- [ ] Dedicated Python 3.12.4 environment exists.
- [ ] `aiohttp==3.14.3` is the only direct runtime dependency.
- [ ] `pip check` passes.
- [ ] Registry JSON/CSV hashes match the frozen lock.
- [ ] Exactly 47 unique strategy IDs are visible.
- [ ] Exactly 0 registry strategies are executable in C1.
- [ ] No trading/wallet/private-key modules exist.
- [ ] SQLite reports WAL, synchronous FULL and foreign keys ON.
- [ ] Exactly one writer owns the write connection.
- [ ] Duplicate provider events are idempotent.
- [ ] Signal and outbox insert atomically.
- [ ] Outbox resumes strictly after `event_id`.
- [ ] Binance open candles are not committed as canonical closed history.
- [ ] Binance backfill has no missing closed minute after the 10-minute outage.
- [ ] Polymarket current books are reconciled after restart.
- [ ] Missing historical depth is explicitly classified.
- [ ] Recovery and live-buffer cutover produce no gap.
- [ ] Canary is infrastructure-only and emitted once.
- [ ] Canary appears through WebSocket without refresh or evaluation request.
- [ ] Reconnect replays missed committed events.
- [ ] Dashboard/browser closure does not stop backend.
- [ ] Second backend instance exits with code 20.
- [ ] Graceful shutdown drains writer before closing the database.
- [ ] API binds only to `127.0.0.1`.
- [ ] Full offline suite has 0 failures and 0 errors.
- [ ] Provider capability smoke uses no auth or secret.
- [ ] Acceptance pack CRC and internal SHA-256 pass.
- [ ] C1 PASS is not described as strategy parity, paper readiness or live-trading approval.

## Execution Handoff

Plan complete. Recommended execution:

```text
Subagent-driven development in an isolated worktree
```

The implementation stops after C1 acceptance. C2, Registry 47 parity, paper execution and Stage D require separate approved plans.
