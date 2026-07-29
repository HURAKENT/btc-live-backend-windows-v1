# Chat Wave 2 Restart Identity Fix

## Status

`CHAT_FIX_READY_FOR_WINDOWS_PROCESS_GATE`

This handoff records a direct fix prepared from the uploaded archive
`btc_live_backend_windows_v1 (4)(1).zip`.

Repository state at intake:

- branch: `codex/c0-c1`
- HEAD: `8eac311fe819499a9c0825f53fbde1dd04222c54`
- tracking ref: `dab52f0979182924a311047230465c6f73cc5112`
- branch ahead by one committed Gate A chatfix
- Wave 2 implementation remained uncommitted in the worktree

No network request and no external provider endpoint was used by this chat fix.

## Root cause

The frozen contract defines Polymarket book event identity as:

```text
source:asset_id:book_hash
```

`_parse_book()` follows that contract:

- the provider book hash is part of `natural_key`;
- canonical `payload_json` contains the book state but not the observation
  timestamp;
- `source_timestamp_ms` records when the provider state was observed.

`SqliteStore.append_source_event()` nevertheless compared
`source_timestamp_ms` as an authoritative identity field for every event type.
Consequently, the same hashed book state observed later on restart produced:

```text
SOURCE_EVENT_CONFLICT
→ WRITER_FAILED
→ RECOVERY_BLOCKED
```

The failure was reproduced before the production change with:

- equal natural key;
- equal canonical payload and payload SHA-256;
- different Polymarket book observation timestamps;
- `ValueError: SOURCE_EVENT_CONFLICT` on replay.

## Production correction

`src/storage.py` now applies the frozen event-identity policy explicitly:

- `POLYMARKET_BOOK` replay compares source, event type, canonical payload and
  payload SHA-256;
- a later observation timestamp and different acquisition origin do not turn
  an identical hashed book state into a conflict;
- all non-book events retain strict source-timestamp equality;
- the same book hash with changed canonical levels remains fail-closed;
- a changed book hash remains a new event.

No migration or schema change was required.

## Tests added or corrected

### Storage identity tests

`tests/test_storage_outbox.py` now proves:

1. same book hash + same canonical state + later timestamp is idempotent;
2. same book hash + changed levels is `SOURCE_EVENT_CONFLICT`;
3. changed book hash is inserted as a new source event;
4. non-book timestamp changes remain conflicts.

### Cross-platform real-provider restart test

`tests/test_runtime_restart_identity.py` starts the real runtime composition
against the local aiohttp fake HTTP/WS provider twice on the same SQLite file.
It proves:

- both runs reach `LIVE_READY`;
- no runtime failure is recorded;
- the market catalog remains one row;
- the 22 REST book identities remain one row each;
- no `RECOVERY_BLOCKED` incident is produced;
- SQLite quick and integrity checks remain `ok`.

This test uses the real provider parsers, adapters, orchestrator and SQLite
store. It does not use a Windows subprocess or external network.

### Reconnect-compatible heartbeat test

The pre-Wave-2 heartbeat test expected `PolymarketStream.run()` to return after
a normal WebSocket close. Reconnect semantics intentionally make that coroutine
long-lived. The test now waits for subscription and `PING`, then explicitly
cancels the owned stream task and verifies `CancelledError` propagation.
Production reconnect behavior was not weakened.

## Verification evidence in chat environment

Environment available to the chat:

- Python `3.13.5`
- aiohttp `3.13.3`
- Linux container

Observed TDD RED before the storage correction:

```text
3 focused tests
1 error: SOURCE_EVENT_CONFLICT
2 pass
```

Focused GREEN after correction:

```text
Polymarket book identity tests: 4 PASS
Cross-platform same-DB restart: 1 PASS
```

Wave 2/runtime targeted set:

```text
56 tests
55 PASS
1 platform skip (SIGBREAK unavailable)
0 failures/errors
```

Full discovery suite in Linux:

```text
408 tests discovered
405 non-Windows-process tests: PASS
3 skips
3 intentionally excluded Windows-only checks:
- Windows process CTRL_BREAK integration
- two Windows named-mutex tests
```

The unfiltered Linux run failed only on those three unavailable Windows
facilities. There were no application-logic failures in the remaining suite.

## Scope

Direct chat additions beyond the pre-existing dirty Wave 2 worktree:

- `src/storage.py`
- `tests/test_storage_outbox.py`
- `tests/test_polymarket_provider.py` (test harness only)
- `tests/test_runtime_restart_identity.py`
- `reports/CHAT_WAVE2_RESTART_IDENTITY_FIX.md`

The uploaded source archive itself was not modified.

## Required Windows completion gate

Codex should not redesign the identity contract. It should integrate the exact
fix, then run in the project Windows environment:

```text
Python 3.12.4
aiohttp 3.14.3
```

Required final evidence:

1. full offline suite with zero failures/errors;
2. `ProcessRuntimeIntegrationTests` first start → PASS;
3. reconnect evidence;
4. second instance exit 20;
5. first CTRL_BREAK exit 0;
6. same-DB restart → PASS;
7. second CTRL_BREAK exit 0;
8. SQLite quick/integrity/WAL/FULL/FK checks;
9. clean process, mutex and port cleanup.

Only after that may Codex claim:

```text
C1_PROCESS_INTEGRATION_PASS
```

Task 14 remains locked until that Windows process gate passes.
