# BTC Live Backend Windows V1 — архитектурная спецификация

**Статус:** FROZEN FOR USER REVIEW  
**Дата:** 2026-07-27T13:16:09.778682+00:00  
**Активный этап:** Stage C  
**Платформа V1:** Windows  
**Архитектура:** модульный монолит  
**Dashboard:** отдельная надстройка Stage D  
**Реальные ордера:** запрещены

---

## 1. Цель

Построить постоянно работающий Windows-backend, который:

1. получает BTC и Polymarket данные в реальном времени;
2. сохраняет события до расчёта стратегии;
3. восстанавливает пропущенные данные после простоя;
4. воспроизводит пропущенные checkpoints;
5. создаёт и сохраняет сигналы без ручного запроса;
6. отправляет новые события подключённому dashboard через WebSocket;
7. позднее запускает все 47 frozen-стратегий и paper execution;
8. переносится на Linux только после Windows acceptance.

Stage D не содержит бизнес-логики и не обращается к Binance или Polymarket
напрямую.

---

## 2. Почему выбран модульный монолит

V1 использует:

```text
1 backend-процесс
1 SQLite-база
1 writer
1 internal event queue
1 strategy runtime
1 REST/WebSocket API
```

Не используются:

```text
Redis
PostgreSQL
Docker
микросервисы
встроенный desktop-dashboard
```

Это сокращает количество процессов, сетевых границ, миграций и независимых
точек отказа. Модули остаются изолированными внутри проекта, поэтому их можно
вынести в отдельные сервисы позднее без изменения доменных контрактов.

---

## 3. Важная граница Registry 47

Канонический список зафиксирован:

```text
34 V1
13 V2
47 total
```

Registry 47 хранит идентичность, lineage и исследовательские метрики. Он пока
не является полным исполняемым rule pack.

Поэтому backend не имеет права включить стратегию только потому, что её ID есть
в registry. Для каждой стратегии обязательны:

```text
exact source rule lock
historical trade-count parity
W/L parity
PnL/ROI parity
deterministic replay
input schema contract
```

Провал parity отключает только эту стратегию и создаёт
`STRATEGY_DISABLED_PARITY_FAILURE`.

Registry lock:

```text
JSON SHA-256: 88c54943cf84e4d123f979639cf30668f35a5dd6bc6792f8f50ab4c986ee3c2f
CSV SHA-256:  77fc26814e3c05182b0b13dbb3d162c41536328517a40b4a6713d7d6e9bad8fd
```

---

## 4. Вертикальный контур C1

Первая реализация не пытается сразу запустить 47 правил.

Она содержит:

```text
Binance BTCUSDT 1m live + REST backfill
1 active BTC Daily Range market
Polymarket public market WebSocket
current orderbook snapshot
price-history backfill
SQLite WAL event store
restart recovery
REST bootstrap
WebSocket push
CANARY_SYNC_READY_V1
```

`CANARY_SYNC_READY_V1` — инфраструктурный сигнал, не торговая стратегия. Он
создаётся один раз после `LIVE_READY`, когда committed новый закрытый BTC
1-minute candle и оба источника синхронизированы.

Он не входит в 47, не создаёт paper-позицию и не может отправить реальный ордер.

Цель canary — доказать полный путь:

```text
provider event
→ database commit
→ canonical state
→ evaluation
→ signal commit
→ transactional outbox
→ WebSocket client
```

---

## 5. Источник истины

SQLite в WAL-режиме является единственным live source of truth.

```text
journal_mode = WAL
synchronous = FULL
foreign_keys = ON
single writer = true
```

Parquet создаётся как ежедневный derived archive после commit. Он не участвует
в live-решении и не нужен для восстановления текущего состояния.

Цены, shares и деньги хранятся fixed-point integers:

```text
probability: 0..1,000,000
shares: micro-shares
money: USD micros
```

Float не используется в persistent financial state.

---

## 6. Порядок обработки события

Обязательный порядок:

```text
1. Получить raw event.
2. Нормализовать и вычислить idempotency key.
3. Сохранить event.
4. Commit.
5. Обновить canonical projection.
6. Рассчитать затронутые стратегии.
7. Сохранить evaluation и signal.
8. Сохранить outbox event в той же транзакции.
9. После commit отправить dashboard.
```

Dashboard никогда не получает событие, которого нет в базе.

Повторная доставка provider event не создаёт второй signal.

---

## 7. Перезапуск и восстановление

Backend проходит состояния:

```text
BOOTING
SINGLE_INSTANCE_LOCK
DATABASE_INTEGRITY
SOURCE_DISCOVERY
LIVE_BUFFERING
BINANCE_BACKFILL
POLYMARKET_BACKFILL
RECONCILIATION
STRATEGY_REPLAY
BUFFER_DRAIN
LIVE_READY
```

### Cutover-протокол

Чтобы не получить разрыв между REST и WebSocket:

1. live streams открываются в buffering mode;
2. фиксируется cutover boundary;
3. REST backfill догоняет boundary;
4. события сохраняются и deduplicate;
5. buffered live events применяются после backfill;
6. выполняется reconciliation;
7. только затем устанавливается `LIVE_READY`.

### Binance

Цель восстановления — все закрытые BTCUSDT 1m candles. После backfill:

```text
missing closed minutes = 0
duplicate natural keys = 0
```

### Polymarket

Обязательны:

```text
price history recovery
current orderbook snapshot
current token identity
current best bid/ask
book hash
```

Историческая глубина используется только после capability probe. Если для
пропущенного checkpoint точная глубина недоступна, evaluation получает:

```text
BLOCKED_MISSING_HISTORICAL_DEPTH
```

Backend не выдумывает стакан и fill.

### Пропущенные сигналы

Сигнал, найденный после простоя, сохраняется как:

```text
origin = RECOVERED_AFTER_DOWNTIME
execution_eligible = false
```

Текущая повторная оценка создаётся отдельной записью и не переписывает
исторический recovered signal.

---

## 8. Push, а не запрос

Stage C самостоятельно оценивает события и checkpoints. Dashboard не запускает
стратегии.

REST используется для стартового snapshot:

```text
GET /api/v1/bootstrap
GET /api/v1/health
GET /api/v1/sources
GET /api/v1/signals
GET /api/v1/incidents
```

Live-обновления идут через:

```text
GET /ws/v1/events?after_event_id=<id>
```

При reconnect dashboard передаёт последний event ID и получает пропущенные
outbox events.

Backend продолжает работу при закрытом браузере.

---

## 9. Модульные границы

```text
app/
  bootstrap
  lifecycle
  config

providers/
  binance
  polymarket

storage/
  sqlite
  migrations
  event_store
  projections
  outbox

recovery/
  cursors
  backfill
  reconciliation
  replay

domain/
  market
  snapshots
  time
  fixed_point

strategies/
  registry_lock
  primitives
  parity
  runtime

paper/
  execution
  positions
  settlement

api/
  rest
  websocket

operations/
  health
  incidents
  backup
```

Strategy module не знает о HTTP, WebSocket, SQLite или provider SDK.

Provider module не вызывает strategy runtime напрямую.

---

## 10. Этапы C0–C11

```text
C0  Registry 47 lock and executable-rule gap report
C1  Walking skeleton: Binance + one Polymarket market + canary + push
C2  Persistent event store, cursor recovery and reconciliation hardening
C3  Rolling BTC Daily Range market discovery
C4  Executable strategy primitives and historical parity harness
C5  Family-by-family activation of Registry 47
C6  Persistent checkpoint scheduler and recovered-signal replay
C7  Paper execution, fills, positions and settlement
C8  Stable REST/WebSocket contract for Stage D
C9  Windows service/autostart, single-instance lock and backups
C10 Failure injection and restart recovery audit
C11 48-hour Windows live observation acceptance
```

Stage D запрещено начинать до API acceptance C8. Его UI-макеты можно обсуждать,
но dashboard не должен компенсировать отсутствующую backend-функцию.

---

## 11. Acceptance вертикального контура

C1 принят только если одновременно выполнено:

```text
active BTC Daily Range market discovered
Binance live event committed
Polymarket live event committed
10-minute intentional downtime recovered
no missing closed Binance minute
current Polymarket book reconciled
canary signal committed exactly once
canary appeared in WebSocket client without refresh
client reconnect replayed missed outbox event
duplicate source input did not duplicate signal
backend worked while client was closed
```

---

## 12. Финальный Windows acceptance

```text
48 continuous hours
≥1 daily-market rollover
2 intentional backend restarts
1 simulated network outage
database integrity PASS
no duplicate signals
no missed closed Binance minutes
all recovery gaps explicitly classified
47/47 parity or explicit disabled list
real-money path absent
```

`47/47 parity or explicit disabled list` означает: релиз не блокируется навечно
одной неподтверждённой стратегией, но такая стратегия остаётся disabled и явно
видна в health/API.

---

## 13. Stop rules

- максимум два технических hotfix на release;
- третья ошибка, требующая patch, останавливает release и запускает
  архитектурный review;
- изменение архитектуры создаёт новую версию контракта;
- dashboard не становится источником истины;
- реальные ордера не добавляются в Windows V1;
- Linux migration начинается только после C11.

---

## 14. Проверенные capability assumptions

На дату 2026-07-27 официальная документация подтверждает:

- Polymarket public market WebSocket передаёт book, price и trade events;
- Polymarket public REST предоставляет current orderbook и price history;
- Binance Spot предоставляет push kline streams;
- Binance REST поддерживает ограниченную временными границами загрузку
  market data.

Конкретные endpoint и payload schemas остаются provider-adapter configuration и
повторно проверяются перед каждым release.
