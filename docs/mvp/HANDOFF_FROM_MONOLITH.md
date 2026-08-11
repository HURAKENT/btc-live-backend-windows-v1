# Legacy Monolith to MVP Hub Handoff

## AUTHORITY TRANSFER

- Old thread: `ARCHIVE / HISTORICAL ENGINEERING LOG`.
- New MVP Manager: `SOLE ACTIVE ORCHESTRATOR` after handoff acceptance.
- Source-of-truth order: (1) Git, (2) frozen/master contracts, (3) acceptance
  artifacts, (4) this file for legacy-only facts, (5) future `docs/mvp` control
  plane.

## CANONICAL STATE

- Integration branch: `codex/final-project-completion`.
- HEAD and `origin/codex/final-project-completion` before this handoff commit:
  `8c0f79406b5a0e17dcfff714311276455c666cac`.
- Integration worktree was clean: no staged, unstaged, or untracked files.
- Last published durable checkpoint: `8c0f794` (`test: add historical source
  import receipt runner`).
- Clean linked worktrees: `codex/c4-dispatcher-v2@49fe56f8cf1e70c18f9b031b69d65a11bb523f23`,
  `codex/c4-strategy-parity@8e464f03442d187a21b614443c5fe19bd6d73862`,
  `codex/c4-v1-confirmation-basket@803641bc25d78e78112fff7be4938568b61c3e27`,
  `codex/c4-v1-evaluators@4913cc6b805bf086b7d0f018ba0ddf26372540d7`,
  `codex/c4-v1-parity@c001e304159484623d87e9c2d9986bf6b9659e3d`,
  `codex/c4-v2-evaluators@7bbc5e1b98bc52727564c235c7b179b745ede0a9`,
  `codex/c5-activation@e6da8619ae1a9d73dc55d5475de06ed6db1d46f6`,
  `codex/c6-scheduler@fd20f7af162d2f7ac545aee4856e9f4eda62563a`,
  `codex/data-completion@b1adbbf1603a35fa46720b6809984363d2a0563a`,
  detached `phase0-eol-fresh@9ba200609e6d8509df52190d8b0f531f0912716e`,
  and `codex/phase0-eol-portability@9ba200609e6d8509df52190d8b0f531f0912716e`.
- Dirty linked worktree: `codex/data-inventory@f41e25cc78e12adc0316e6ac41fbaf99ecb3a72d`;
  details are under `LEGACY UNCOMMITTED STATE`.
- Git also reports prunable metadata for absent `/tmp/btc-final-phase0` at
  `codex/phase0-corrections@415a3441bf10106634f321bd00f8d19f2b123864`.

## ACCEPTED / DO NOT REOPEN

- C0: `PASS_HISTORICAL`; immutable 47-ID registry is in
  `registry/STRATEGY_REGISTRY_47.{json,csv,xlsx}` (foundation commit `0c38069`).
- C1: accepted; `reports/C1_FINAL_ACCEPTANCE.json` and
  `reports/C1_DOWNTIME_ACCEPTANCE.json`; evidence commit `b7ced26`.
- C2: accepted and Phase-0 revalidated; `reports/C2_RECOVERY_ACCEPTANCE.json`
  and `reports/C2_C3_FINAL_ACCEPTANCE.json`; gate commit `19e0788`.
- C3: accepted and Phase-0 revalidated; `reports/C3_MARKET_ROLLOVER_ACCEPTANCE.json`;
  combined evidence commit `d696de8`.
- C4: `C4_STRATEGY_47_ACCEPTANCE_PASS`; 47/47 source/spec/evaluator/parity;
  `reports/C4_STRATEGY_47_ACCEPTANCE.json`; acceptance commit `8e464f0`.
- C5: `C5_STRATEGY_47_ACTIVATION_PASS`; 47/47 explicitly classified;
  `reports/C5_STRATEGY_47_ACTIVATION_ACCEPTANCE.json`; commit `e6da861`.
- C6: `C6_CHECKPOINT_SCHEDULER_PASS`; persistent exactly-once scheduling and
  recovered replay; `reports/C6_CHECKPOINT_SCHEDULER_ACCEPTANCE.json`; commit
  `6a1c88e`.

## SECURITY INVARIANTS

`trading_approval=false`; `real_orders=false`; `wallet=false`; `signing=false`;
`authenticated_CLOB_writes=false`. Paper execution is local simulation only.

## DATA COMPLETION CURRENT STATE

- `<EXTERNAL_DATA_ROOT>/historical_source.sqlite3` exists: `8,015,867,904`
  bytes. Its WAL is `4,791,374,632` bytes and SHM is `9,306,112` bytes.
- The old import/integrity process is not running: its managed session is gone
  and a fresh WSL process-name scan returned no match.
- Final receipt is absent. The DB/WAL/SHM are a research artifact, not accepted
  evidence. Do not rerun the 7.27M-row import for MVP.
- Partial external evidence exists: two hash-verified source-pack copies and
  hash-verified C1 and C2/C3 acceptance-pack copies. No report was promoted.
- `reports/DATA_COMPLETENESS_STATUS.json` remains
  `PHASE_0_AUDIT_PENDING`; do not claim Data Completion PASS.
- The importer contract and offline storage capability were implemented at
  `791e9c2`, `f9f8af3`, and `8c0f794`; completion of the interrupted external
  evidence run was not proved.

**FULL HISTORICAL DATA COMPLETION IS NOT A PRE-MVP C7 DEPENDENCY.** Missing
historical depth blocks affected recovered evaluations; recovered historical
execution is forbidden. MVP C7 may proceed with fail-closed current execution
evidence.

Path aliases used here: `<PROJECT_ROOT>` is this Git checkout; `<DOWNLOADS>` is
the current user's Downloads directory; `<EXTERNAL_DATA_ROOT>` is the external
`btc_daily_range_windows_v1_data_completion` directory. Absolute user paths are
intentionally not persisted.

## AUTHORITATIVE LOCAL ARTIFACTS

| Artifact | Purpose | SHA-256 | Current local path | State |
|---|---|---|---|---|
| 70-day source pack | Historical provider/source rows | `911f4108cd8f6fe8d14c3bcbc435062caa0fa850c9f51a9712687507ac8691ce` | `<DOWNLOADS>/btc_daily_range_merged_70_v1_10.zip`; copied under `<EXTERNAL_DATA_ROOT>/source_packs/` | external, untracked |
| 100-day source pack | Frozen validation/source rows | `cfcf8b47acb87c3efec9a0d0f86f86dd83c7eeed919975c80dac9094ed7d1dc2` | `<DOWNLOADS>/btc_daily_range_validation_100_merged_v2_2(1).zip`; copied under `<EXTERNAL_DATA_ROOT>/source_packs/` | external, untracked |
| Master context V2 | Canonical C4-C11/Stage-D authority | `0497121f93bbd549b3e9f6b8a9e76c8c1d3d411bdc9c7c71abfe98c6aa7c1855` | `<DOWNLOADS>/BTC_PROJECT_MASTER_CONTEXT_FOR_CODEX_V2.zip` | external, untracked |
| C1 acceptance pack | Historical accepted C1 evidence | `9f6eaa9015f0698313c950571ae5f76be3f9c9d4327a461c4d504e8594b81a4f` | `<PROJECT_ROOT>/artifacts/C1_ACCEPTANCE_PACK.zip`; copied under `<EXTERNAL_DATA_ROOT>/historical_acceptance/` | Git-ignored/external |
| C2/C3 acceptance pack | Historical accepted C2/C3 evidence | `07c0119585ead440036734926a3ad7bd86ff63002cc2d4a0e96df1a6c47062c9` | `<PROJECT_ROOT>/artifacts/C2_C3_ACCEPTANCE_PACK.zip`; copied under `<EXTERNAL_DATA_ROOT>/historical_acceptance/` | Git-ignored/external |

## CURRENT TRUE MVP BLOCKERS

1. Production `checkpoint_input_source` is missing; C6 records
   `production_input_ready=false`.
2. Strict A current input requires 181 closed Binance 1-minute candles, model
   probability, market `q`, five-share VWAP, and current fee provenance.
3. C7 paper intents/fills/positions/accounting/PnL do not exist.
4. Existing REST/WS must be extended, not rebuilt.
5. Dashboard does not exist.
6. Migration-v3 regression assertions are stale and reproducibly fail after
   migration v4: `test_migration_version_is_exactly_three` observes `4 != 3`;
   `test_required_table_set_is_unchanged` omits the four v4 data-import tables.

## PF1

The exact current production model bundle is unavailable/not proved. PF1 does
not block the first MVP. Do not reconstruct, infer, or guess the missing bundle.

## EXISTING API

`src/api.py` already provides localhost `127.0.0.1:8767`:

- `GET /api/v1/bootstrap`
- `GET /api/v1/health`
- `GET /api/v1/sources`
- `GET /api/v1/signals?limit=...`
- `GET /api/v1/incidents?limit=...`
- `GET /ws/v1/events?after_event_id=...`

The WebSocket replays durable outbox events after an event ID, then subscribes
to the broker. Extend these backend seams for MVP paper state; do not replace
the architecture.

## DO NOT REPEAT

- Do not reopen C0-C6 absent a newly reproduced production regression.
- Do not rerun the 7.27M-row historical import for MVP.
- Do not use the receipt-less external DB as acceptance evidence.
- Do not make historical depth a global C7 blocker.
- Do not reconstruct PF1 or infer Strict/PF1 rules from names/aggregates.
- Do not make all C5 evaluation identities paper-executable.
- Do not mistake C4 sanitized parity fixtures for provider history/depth.
- Do not vendor the unnecessary Strict engine carrying auth/order/signing
  surface; the frozen evaluator contract is already in-repo.
- Do not rebuild REST/WS architecture.
- Do not create a frontend framework/design system for the first MVP.
- Do not run endless reviewer loops or a full regression after every small
  feature; use focused RED/GREEN and stage gates.
- Do not turn post-MVP evidence hardening into an MVP dependency.
- Do not rely on direct WSL-to-Windows invocation when it emits the known
  `UtilBindVsockAnyPort` error; use the established Windows process wrapper for
  required Windows gates.

## CURRENT MVP EXECUTION POLICY

- Strict A first: T60 primary; T30 fallback only if no logical execution or
  position already exists for that `market_date`.
- Maximum one Strict A logical paper position per `market_date`.
- Base/minimum size: exactly 5 shares. Default paper bankroll: USD 1000.
- Recovered historical execution is forbidden.
- Current fills require contemporaneous book/depth, five-share VWAP and fee
  provenance; missing evidence blocks with zero fills.

## POST-MVP BACKLOG

- Historical DB final receipt and evidence hardening.
- PF1 model-bundle recovery.
- C9 Windows operations; C10 failure injection; C11 real 48-hour observation.
- Release/evidence portability; P2/P3 findings; nonessential refactoring and UI
  polish.

## LEGACY UNCOMMITTED STATE

- Worktree/branch: `<PROJECT_ROOT>/.worktrees/data-inventory`,
  `codex/data-inventory@f41e25cc78e12adc0316e6ac41fbaf99ecb3a72d`.
- Modified files only:
  - `src/data_completion_acceptance.py`, SHA-256
    `39044cebf0ea125404a5ae3080ff83a8834846a6fa8e9938f231b8d2062e2532`;
  - `tests/test_data_completion_acceptance.py`, SHA-256
    `27dc0cf3e505fbd8aac1e8d6cdae7d5b28032a6b94a9a3326f1f1a00615ddee6`.
- The original RED ran 7 tests: 2 assertion failures and 1 missing-interface
  error. Later implementation was drafted and compile-checked, but no final
  receipt existed, so no receipt-backed GREEN was possible.
- Status: `UNPROVEN / DO NOT MERGE`. The new Manager should ignore or discard
  this legacy diff unless independently re-scoped after MVP.

## LAST TRUSTED VERIFICATION

- Phase 0 receipt records focused `101/101`, full offline `647/647`, compileall,
  pip check and scope audit PASS at source commit `33467cc`; report SHA-256
  `c7172837fc337a5d991c50d8c95833b081b5a5e006c2733693ef98100102574b`.
- C4/C5/C6 remain accepted by their committed reports listed above; they were
  not rerun during this handoff.
- Source-pack importer: committed synthetic gate `11/11`; authoritative
  structure scan `1/1` was recorded before the interrupted external import.
- Expected-absent binding tests: `42/42`; source-receipt/runner tests: `11/11`
  before commits `f9f8af3` and `8c0f794`.
- Fresh handoff-only regression probes: the two migration-v3 tests named above
  each FAIL against migration v4. Windows Python invocation itself failed with
  the known WSL vsock transport error; Linux Python reproduced the assertions.
- No long-running import, backend, Task 14, unittest or compileall process was
  present at handoff time.
