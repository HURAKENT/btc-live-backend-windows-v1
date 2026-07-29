# Chat Real-Provider Seam Handoff

## Статус

**Статус пакета:** `INTEGRATION_READY_PATCH`  
**Базовый commit:** `dab52f0979182924a311047230465c6f73cc5112`  
**Назначение:** закрыть подтверждённые seams между REST backfill, live WebSocket и production startup до Windows process integration.

Этот пакет не является разрешением Task 14 и не подтверждает Windows process gate. Он содержит production-код и regression tests, которые Codex должен интегрировать в ветку `codex/c0-c1`, после чего выполнить Windows-специфическую проверку.

## Что исправлено

### 1. Единая authoritative Binance candle для REST и WebSocket

`src/binance_provider.py` нормализует оба транспорта в один payload:

- `symbol`, `interval`;
- `open_time_ms`, `close_time_ms`;
- OHLC;
- volume и quote volume;
- trade count;
- taker-buy volumes.

Decimal значения канонизируются без binary float, trailing zero и signed-zero различий. Одна закрытая свеча имеет одинаковые `natural_key`, `payload_json` и `payload_sha256` независимо от REST/WS acquisition path. Реально отличающиеся OHLCV остаются `SOURCE_EVENT_CONFLICT`.

### 2. Acquisition metadata не создаёт ложный event conflict

`src/storage.py` считает authoritative identity по:

- source;
- natural key;
- source timestamp;
- event type;
- canonical payload;
- payload hash.

`received_timestamp_ms` и `recovery_origin` описывают способ получения и не превращают один economic event в две конфликтующие записи.

### 3. Dynamic Binance cutover boundary

`src/runtime_orchestrator.py`:

1. запускает Binance stream в buffering mode до длительного Polymarket discovery;
2. ждёт подтверждённого WebSocket connect;
3. после discovery заново вычисляет последнюю закрытую минуту;
4. при движении границы делает incremental backfill;
5. проверяет continuity до фактической стабилизированной cutover boundary;
6. только затем дренирует live buffer.

Boundary refresh ограничен `MAX_BINANCE_BOUNDARY_REFRESHES=4` и fail-closed при нестабильности.

### 4. Polymarket stream-before-current-books

`src/runtime_adapters.py` разделяет:

- `discover_market()`;
- `reconcile_current_books()`.

Orchestrator после discovery запускает Polymarket WebSocket и ждёт subscription-ready до 22 последовательных REST book requests. Live events в это время буферизуются.

### 5. Polymarket canonical book identity

`src/polymarket_provider.py`:

- REST books получают origin `REST_BACKFILL`;
- WS books сохраняют origin `LIVE`;
- digit-only string timestamps поддерживаются для `book`, `price_change` и passthrough events;
- canonical book payload строится из нормализованных levels и hash, а не из сырого transport envelope;
- одинаковая книга не конфликтует из-за `"0.4500"`/`"0.45"` или int/string timestamp;
- `MarketBook.apply(price_change)` обновляет levels и текущий `book_hash` атомарно.

### 6. Bounded queues и fail-closed waits

- write queue: `DEFAULT_WRITE_QUEUE_MAXSIZE=4096`;
- pre-cutover live buffer: `DEFAULT_LIVE_BUFFER_MAX_EVENTS=8192`;
- stream connect timeout: `15s`;
- first live evidence timeout: `75s`.

Buffer overflow и live-evidence timeout переводят runtime в `RECOVERY_BLOCKED`, вместо бесконечного ожидания или неограниченного роста памяти.

### 7. API доступен во время startup

`src/app.py` поднимает loopback API после config/mutex/database integrity и до provider recovery. Callback состояния динамически переключается с `BOOTING` на фактический orchestrator status. Если runtime startup падает, API и частично запущенные providers закрываются в штатном rollback path.

## Изменённые production-файлы

- `src/app.py`
- `src/binance_provider.py`
- `src/polymarket_provider.py`
- `src/runtime_adapters.py`
- `src/runtime_orchestrator.py`
- `src/storage.py`

## Изменённые/новые tests

- `tests/test_binance_provider.py`
- `tests/test_polymarket_provider.py`
- `tests/test_single_instance.py`
- `tests/test_runtime_real_provider_seams.py` — новый

## Доказательные сценарии

`tests/test_runtime_real_provider_seams.py` покрывает 13 сценариев:

- REST/WS Binance canonical identity;
- same-candle idempotent storage replay;
- differing OHLCV conflict;
- signed-zero normalization;
- REST/WS Polymarket book identity при различном wire formatting;
- REST/WS provenance;
- price-change hash mutation;
- boundary refresh после discovery;
- multi-round boundary stabilization;
- REST-backfill/WS overlap одной минуты;
- bounded live-buffer overflow;
- bounded live-evidence timeout;
- Polymarket stream before book fetch.

Provider tests дополнительно подтверждают, что ready event устанавливается только после фактического WebSocket connect/subscription.

## Свежая верификация в chat sandbox

Среда аудитора:

- Python `3.13.5`;
- aiohttp `3.13.3`;
- Linux.

Результаты:

- real-provider seam tests: `13/13 PASS`;
- объединённый targeted regression: `346 PASS`, `2 SKIP`;
- полный suite без двух Windows-only named mutex tests: `377 PASS`, `2 SKIP`, `0 failures/errors`;
- `compileall`: PASS;
- `git diff --check`: PASS.

Полный unfiltered Linux suite содержит только две ожидаемые ошибки `WINDOWS_NAMED_MUTEX_UNAVAILABLE`; они требуют Windows и не относятся к изменённому коду.

`pip check` глобальной sandbox-среды не является валидным project gate: он падает на постороннем конфликте `moviepy`/`pillow`. Кроме того, sandbox не совпадает с frozen runtime (`Python 3.12.4`, `aiohttp 3.14.3`). Поэтому Codex обязан повторить весь gate в проектной Windows `.venv`.

## Что намеренно не выполнено в чате

- реальный Binance/Polymarket network run;
- настоящий `run_backend.py` subprocess gate;
- Windows named mutex;
- `SIGBREAK`/`CTRL_BREAK_EVENT` graceful shutdown;
- Polymarket reconnect loop;
- десятиминутный Task 14;
- acceptance runner completion;
- trading, wallet, signing, orders или paper execution.

## Обязательный следующий Windows gate для Codex

После применения patch:

1. запустить changed targeted tests в Windows `.venv`;
2. запустить полный suite;
3. проверить exact Python/aiohttp versions и `pip check`;
4. выполнить локальный fake HTTP/WS process integration через настоящий `run_backend.py`;
5. доказать API `STARTING` во время recovery;
6. доказать REST/WS overlap и no-gap cutover на настоящих provider parsers;
7. реализовать/проверить `SIGBREAK` и graceful exit `0`;
8. только после `C1_PROCESS_INTEGRATION_PASS` возвращаться к Task 14.

## Security boundary

- Registry execution: `0`;
- real orders: `0`;
- paper fills: `0`;
- wallet/signing/auth additions: `0`;
- migrations: unchanged;
- frozen contract: unchanged;
- исходный пользовательский ZIP: unchanged.
