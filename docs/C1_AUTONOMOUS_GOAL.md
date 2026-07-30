# C1 Autonomous Completion Goal

## Goal

Довести `btc_live_backend_windows_v1` до подтверждённого:

`BTC_LIVE_BACKEND_WINDOWS_V1_C1_PASS`

Самостоятельно устранять промежуточные blockers и продолжать работу без
отдельного пользовательского prompt для каждого исправления.

## Definition of Done

Goal выполнена только когда одновременно доказано:

- initial backend достигает `LIVE_READY`;
- top-level API health `PASS`;
- Binance source health `LIVE`;
- Polymarket source health `LIVE`;
- current market identity присутствует;
- 11 markets и 22 assets reconciled;
- Polymarket history recovery завершена;
- current Polymarket books reconciled;
- Binance closed-minute continuity complete;
- initial graceful stop exit `0`;
- forced termination `false`;
- port free;
- mutex free;
- child processes `0`;
- accepted downtime interval пройден по monotonic clock;
- restart использует ту же SQLite DB;
- post-restart recovery complete;
- post-restart `LIVE_READY`;
- recovered records имеют `RECOVERED_AFTER_DOWNTIME`;
- recovered records `execution_eligible=false`;
- current live reevaluation хранится отдельно;
- outbox replay полный, ordered и без duplicates;
- `quick_check=ok`;
- `integrity_check=ok`;
- `C1_DOWNTIME_ACCEPTANCE.json` status `PASS`;
- `C1_FINAL_ACCEPTANCE.json` status `PASS`;
- `C1_ACCEPTANCE_PACK.zip` создан;
- pack CRC и internal SHA-256 verified;
- Task 14 launcher exit `0`;
- `HEAD==origin`;
- worktree clean;
- wallet/order/signing/paper/live-money остаются disabled.

## Current first blocker

`POLYMARKET_HISTORY_SEQUENCE_ERROR`

## First autonomous checkpoint

Codex полностью читает production history path и выбирает один вариант.

### Вариант A — безопасный production fix доказуем

Применять только если semantic contract можно обосновать без догадки:

- timestamp type/range validation остаётся строгой;
- conflicting duplicate timestamps остаются fatal;
- exact duplicate authoritative rows могут быть idempotent только после
  полного canonical equality check;
- arbitrary malformed rows не сортируются и не скрываются;
- обработка одного asset атомарна;
- pagination имеет no-progress guard;
- pagination получает deterministic bounded request contract;
- current endpoint/query semantics сохраняются;
- RED tests воспроизводят старый failure;
- full offline gate проходит.

### Вариант B — фактический ordering недостаточно доказан

Не создавать отдельный probe. Добавить minimal sanitized instrumentation в
существующий runtime/Task 14 blocked evidence:

- page index;
- item count;
- timestamp type histogram;
- adjacent `LT/EQ/GT` counts;
- direction class;
- duplicate positions;
- cross-page overlap count;
- first rejected position;
- request-range fingerprints;
- rejection category.

Не сохранять raw timestamps, prices, sizes, raw asset IDs или raw responses.
После offline gate запросить ровно один manual Task 14 run.

## Autonomous loop

Для каждого нового blocker:

1. прочитать existing evidence;
2. определить earliest failed boundary;
3. отвергнуть alternatives;
4. выбрать minimal fix;
5. RED;
6. GREEN;
7. focused gate;
8. один full offline gate;
9. scope/security audit;
10. commit/push;
11. продолжить либо запросить manual run.

## Manual action protocol

```text
MANUAL_ACTION_REQUIRED

Reason:
One exact load-bearing reason established by evidence.

Why existing evidence is insufficient:
A concise statement of the missing evidence.

Command:
One complete Windows PowerShell command.

Allowed run count:
1

Expected duration:
A bounded duration estimate.

Expected artifacts:
Exact artifact paths outside or inside the repository as applicable.

Do not:
An explicit prohibition on retries or out-of-scope actions.

Resume condition:
The exact files and result needed to resume.
```

Поля в этом format block заполняются фактическими значениями перед manual
checkpoint; сам block определяет формат и не является незавершённым
engineering requirement.

После manual result Codex самостоятельно продолжает Goal.

## Iteration control

- no standalone diagnostic tool by default;
- no new manual runner when `RUN_TASK14_MANUAL.ps1` sufficient;
- no repeated full Task 14 without changed code or new instrumentation;
- no more than one diagnostic acceptance run per unresolved boundary;
- no more than two implementation cycles at the same boundary;
- after two no-progress cycles: `ARCHITECTURE_REVIEW_REQUIRED`.

## Terminal states

- Success: `C1_AUTONOMOUS_GOAL_COMPLETE`
- Manual action: `MANUAL_ACTION_REQUIRED`
- Dangerous scope expansion: `BLOCKED_SCOPE_AUTHORIZATION_REQUIRED`
- Architectural escalation: `ARCHITECTURE_REVIEW_REQUIRED`
