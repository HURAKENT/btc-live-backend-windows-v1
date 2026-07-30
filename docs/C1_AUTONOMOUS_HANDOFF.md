# C1 Autonomous Handoff

## 1. Purpose and current stage

**VERIFIED FACT:** Проект реализует Windows-first public read-only backend на
Stage C0–C1. Текущая конечная цель — подтверждённый
`BTC_LIVE_BACKEND_WINDOWS_V1_C1_PASS`.

**DECISION:** Этот документ является каноническим state handoff для
автономного продолжения C1. Изменяемый engineering progress ведётся в
`docs/C1_GOAL_PROGRESS.md`.

## 2. Repository identity

**VERIFIED FACT:**

- Repository: `C:\Users\gegos\Documents\Codex\btc_live_backend_windows_v1`
- WSL path: `/mnt/c/Users/gegos/Documents/Codex/btc_live_backend_windows_v1`
- Branch: `codex/c0-c1`
- Initial handoff base: `b722b04db1fa5b0f6e17eae03d504922bc2e5ae9`

Значение initial handoff base остаётся историческим и не заменяется SHA
последующих documentation или engineering commits.

## 3. Frozen sources of truth

**VERIFIED FACT:** Неизменяемый контракт задают:

- `docs/frozen/BTC_LIVE_BACKEND_WINDOWS_V1_DESIGN.md`
- `docs/frozen/BTC_LIVE_BACKEND_WINDOWS_V1_C0_C1_IMPLEMENTATION_PLAN.md`
- `docs/frozen/BTC_LIVE_BACKEND_WINDOWS_V1_ACCEPTANCE_MATRIX.csv`
- `docs/frozen/BTC_LIVE_BACKEND_WINDOWS_V1_C0_C1_TASK_MATRIX.csv`
- `contract/BTC_LIVE_BACKEND_WINDOWS_V1_CONTRACT.json`
- `config/c0_c1_frozen_config.json`
- Registry 47 lock и canonical registry files.

**PROHIBITION:** Этот handoff не изменяет frozen design/plan и не создаёт
новую архитектурную версию.

## 4. Architecture summary

**VERIFIED FACT:** Архитектура — modular monolith: один backend process, одна
SQLite WAL database, exactly one runtime writer, internal event queue,
fixed-point canonical state и loopback REST/WebSocket API.

Обязательный data path:

```text
provider event
→ append-only source event commit
→ canonical projection
→ evaluation/signal
→ transactional outbox
→ committed WebSocket delivery
```

Recovery и same-DB restart являются обязательными C1 acceptance boundaries.

## 5. Security boundaries

**VERIFIED FACT:** Registry 47 остаётся non-executable. Real orders, paper
execution, wallet/private keys, signing и live-money отсутствуют.

**PROHIBITION:** Не добавлять authentication/private provider operations,
orders, wallet/signing, paper execution, Registry 47 execution или dashboard
scope. C1 PASS не означает trading approval.

## 6. Completed gates

**VERIFIED FACT:** Existing offline/loopback evidence подтверждает:

- frozen config и Registry locks;
- SQLite integrity и one-writer ownership;
- canonical provider events и deduplication;
- transactional outbox;
- recovery coordinator и runtime lifecycle;
- API health and resumable outbox;
- provider reconnect;
- mutex и second-instance exit `20`;
- graceful stop и same-DB restart;
- load-bearing process integration;
- Task 14 harness.

Последний зафиксированный полный offline suite: 532 tests, один documented
conditional skip, без failures/errors. Источники: `reports/AUDIT_HANDOFF.md`
и evidence второго Task 14 run.

## 7. First Task 14 and WS batch root cause

**VERIFIED FACT:**

- Run: `C1-ACCEPTANCE-20260729T162927Z-C63F88BF`
- Source state: `cf1a02e0e409b00e6de36d1f3efaf729143bb2ad`
- Persisted failure:
  `POLYMARKET_STREAM_FAILED: POLYMARKET_INVALID_TYPE: payload`
- Lifecycle boundary: `LIVE_BUFFERING → RUNTIME_FATAL → RECOVERY_BLOCKED`

**VERIFIED FACT:** Root cause —
`POLYMARKET_WS_INITIAL_BOOK_BATCH_NOT_SUPPORTED`.

## 8. Manual WS probe evidence

**VERIFIED FACT:** Один manual Windows probe:

- Probe ID: `POLYMARKET-WS-PROBE-20260730T112412Z-28728040`
- Source commit: `7ee89bd064588cf8dd9e67dd141be5e6a3246ada`
- Canonical internal `report_sha256`:
  `a76883c1ad9c564763d9873975f5aef6e5259a5ddedefe4288906ba22101b487`
- External JSON file SHA-256:
  `95f032075cde72683617722ecd8638166587c6a142ec96811b6467fc15110bf5`

Sanitized report path:

```text
C:\Users\gegos\Documents\Codex\polymarket_ws_wire_shape_probes\
manual_probe_20260730T112411548Z\polymarket_ws_wire_shape_report.json
```

Evidence подтверждает 11 markets, 22 assets и первый WS frame как top-level
array длины 2 для двух subscribed assets, содержащий два homogeneous
`event_type=book` objects. Raw frame/identifier persistence counters равны
нулю.

## 9. Implemented WS batch fix

**VERIFIED FACT:** Commits:

- `7c0a72be35acd1367e4c57ba46e022f3c72370a1`
- `b722b04db1fa5b0f6e17eae03d504922bc2e5ae9`

Initial batch поддерживается только как первая data frame connection:
non-empty, не длиннее subscription, только book objects, unique subscribed
asset IDs, полная atomic validation до enqueue. Existing single-object book
и price-change semantics сохранены. Mixed, nested, service, duplicate,
unknown и malformed arrays fail closed.

## 10. Second Task 14

**VERIFIED FACT:**

- Run: `C1-ACCEPTANCE-20260730T120442Z-EBA92FA3`
- Source commit: `b722b04db1fa5b0f6e17eae03d504922bc2e5ae9`
- Result: `BLOCKED_INITIAL_LIVE_READY`
- Launcher exit: `2`
- Elapsed: `48.9967718` seconds
- Downtime/restart: not reached.

Runtime root:

```text
C:\Users\gegos\Documents\Codex\btc_live_backend_windows_v1_runtime\
acceptance\C1-ACCEPTANCE-20260730T120442Z-EBA92FA3
```

Data root:

```text
C:\Users\gegos\Documents\Codex\btc_live_backend_windows_v1_data\
acceptance\C1-ACCEPTANCE-20260730T120442Z-EBA92FA3
```

## 11. Current Polymarket history blocker

**VERIFIED FACT:** Initial WS batch fix operationally passed: old invalid
top-level payload failure did not recur; discovery, 11/22 identity, 22 current
books, market identity commit and Binance backfill/continuity progressed.
Runtime reached `POLYMARKET_BACKFILL`.

**VERIFIED FACT:** Persisted fatal error:
`POLYMARKET_HISTORY_SEQUENCE_ERROR`.

**DATA GAP:** Persisted evidence не различает descending response, duplicate
timestamp внутри page, overlap между requests или иной non-increasing
ordering. Raw timestamps и raw response отсутствуют.

## 12. External evidence paths and hashes

**VERIFIED FACT:**

- Blocked evidence:
  `runtime root\evidence\task14_blocked.json`
  - SHA-256:
    `7da23e0c38c49fed9e6fb01d9b841f0694df3bfae16270a9c1a18aa1467b3817`
- Acceptance DB:
  `data root\btc_live_backend.sqlite3`
  - SHA-256:
    `8b32a15fea579d412a7e53e185b6545cd7882dc9b12ca67877a4dde2ff160ae5`
- Initial backend log:
  `runtime root\initial\backend.log`
  - Size: `0` bytes.

## 13. WSL/Windows execution audit

**VERIFIED FACT:** Codex Desktop commands execute through Ubuntu 24.04 WSL2;
repository is mounted under `/mnt/c`. Bare WSL→`powershell.exe` calls produced
both a successful Windows PowerShell 5.1 invocation and a
`UtilBindVsockAnyPort` failure in the same audit.

**DECISION:** Use `System.Diagnostics.Process` wrapper only for bounded
offline Windows commands. Public provider probes and Task 14 `Mode=Run`
remain user-manual Windows actions. Computer Use is not a PowerShell
automation path.

## 14. Manual-run policy

**DECISION:** Before a real Windows run, Codex must finish and push the
relevant code/evidence changes, verify `HEAD==origin` and clean state, update
progress, pause execution and emit one `MANUAL_ACTION_REQUIRED` block with one
command and allowed run count `1`.

## 15. Cancelled approaches

**DECISION:** `SEPARATE_HISTORY_ORDER_PROBE_CANCELLED`.

**PROHIBITION:** Не возобновлять standalone history-order probe/runner без
architecture review. Required evidence должно быть получено через safe
production semantics или minimal sanitized instrumentation существующего
runtime/Task 14 evidence.

## 16. Current engineering decision boundary

**DECISION:** Полностью review production history path и выбрать ровно один:

- A: safe bounded production normalization, только если semantics доказуема
  без догадки;
- B: minimal sanitized instrumentation существующего runtime/Task 14, если
  provider ordering недостаточно доказан.

**INFERENCE:** Текущего persisted evidence недостаточно для безопасного выбора
конкретного ordering normalization. Source review должен определить, можно ли
сохранить conflict detection и atomicity независимо от provider order.

## 17. Definition of Done

**VERIFIED CONTRACT:** Goal завершена только при полном Task 14 PASS:
initial и post-restart `LIVE_READY`, обе source health `LIVE`, 11/22
reconciliation, complete Binance continuity, Polymarket history/current book
recovery, graceful stops exit `0`, accepted monotonic downtime, same DB
restart, recovered/current evaluation separation, ordered deduplicated outbox
replay, SQLite integrity, PASS acceptance JSONs, verified acceptance pack,
launcher exit `0`, `HEAD==origin`, clean worktree и все forbidden execution
surfaces disabled.

## 18. Non-goals

**PROHIBITION:** C2/C3 rollout, strategy parity, Registry 47 execution, paper
trading, live orders, wallet/signing, dashboard implementation и Linux
migration не входят в эту Goal.

## 19. Recovery and escalation policy

**DECISION:** Для каждого нового blocker: evidence review → earliest boundary
→ alternatives rejection → minimal RED/GREEN cycle → focused gate → один full
offline gate → scope/security audit → commit/push → autonomous continuation
или один manual checkpoint.

Не более двух implementation cycles на одной boundary. Два no-progress cycles
или противоречие frozen contract приводят к
`ARCHITECTURE_REVIEW_REQUIRED`.
