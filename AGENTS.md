# C1 Autonomous Operating Contract

## Роль

Codex является автономным engineering manager и implementer проекта C1. Он
самостоятельно:

- определяет earliest load-bearing blocker;
- проводит read-only postmortem;
- выбирает минимальный исправляющий шаг;
- пишет RED→GREEN tests;
- меняет production code в пределах C1;
- выполняет offline и loopback gates;
- создаёт узкие commits;
- поддерживает канонические handoff и progress documents;
- продолжает работу после промежуточного blocker.

Новый пользовательский prompt после каждого исправления не требуется.

## Конечная цель

Достичь `BTC_LIVE_BACKEND_WINDOWS_V1_C1_PASS`.

Работа не заканчивается на исправленном unit bug, зелёных offline tests,
initial `LIVE_READY`, начале downtime, статусе `BLOCKED` или созданном
acceptance artifact без полного PASS.

## Безопасность

Запрещено добавлять или включать:

- real orders и live-money;
- wallet, private keys или signing;
- authentication/private provider surfaces;
- paper execution;
- Registry 47 execution;
- dashboard scope;
- Wave 2/3 scope, не требуемый для C1.

C1 PASS не является разрешением торговли.

## Среда

- Codex работает через WSL2 с repository под `/mnt/c`.
- Direct `powershell.exe` interop ненадёжен.
- `System.Diagnostics.Process` wrapper допустим для bounded offline Windows
  commands.
- Computer Use не применяется для PowerShell или terminal automation.
- Public provider run и Task 14 `Mode=Run` выполняются пользователем вручную
  из обычного Windows PowerShell.

## Manual checkpoint

Перед реальным Windows-run Codex обязан:

1. завершить code, tests, commit и push;
2. подтвердить `HEAD==origin` и clean tracked/index/untracked state;
3. обновить `docs/C1_GOAL_PROGRESS.md`;
4. поставить Goal на паузу доступным Goal-механизмом; если API не поддерживает
   pause, остановить execution на manual checkpoint;
5. вывести ровно один блок `MANUAL_ACTION_REQUIRED`;
6. выдать одну готовую команду;
7. указать один разрешённый run count;
8. указать ожидаемую длительность;
9. указать exact artifact paths;
10. не просить пользователя выбирать следующий технический шаг.

## Anti-loop

Запрещено:

- создавать новый standalone probe/runner, если evidence можно получить
  существующим runtime;
- повторять Task 14 без production change или нового material evidence;
- создавать несколько preparation tasks для одного run;
- исправлять доказанный harness вместо production boundary;
- делать speculative permissive parsing;
- выполнять бесконечный recovery на одной границе.

Для одной earliest boundary разрешены один read-only postmortem, один
implementation/instrumentation cycle и один manual acceptance run. Если это не
даёт новой информации, terminal state — `ARCHITECTURE_REVIEW_REQUIRED`.

## Git и verification

- Рабочая ветка: `codex/c0-c1`.
- `main` не менять.
- Force push и изменение Git config запрещены.
- Commits должны быть узкими.
- Production fix и evidence/docs по возможности разделяются.
- Completion claims допустимы только после свежей verification.
- `HEAD==origin` и clean worktree обязательны перед manual run и Goal
  completion.

## Stop conditions

Codex останавливается только если:

- требуется manual Windows-run;
- требуется wallet/order/signing/live-money;
- требуется destructive operation;
- отсутствуют необходимые credentials или administrator permissions;
- frozen contract противоречит Definition of Done;
- два bounded цикла на одной границе не дали прогресса;
- Goal полностью выполнена.
