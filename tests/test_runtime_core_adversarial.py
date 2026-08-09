from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.api import build_health_payload
from src.canary import evaluate_canary
from src.lifecycle import STARTUP_SEQUENCE
from src.models import CanonicalSnapshot, SourceEvent, payload_sha256
from src.outbox import OutboxBroker
from src.runtime_adapters import MarketReconciliation
from src.runtime_orchestrator import C1RuntimeOrchestrator
from src.runtime_projection import CanonicalProjector, CommittedSourceEvent
from src.storage import SqliteStore


MINUTE_MS = 60_000


def _canonical(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _event(
    *,
    source: str,
    natural_key: str,
    timestamp_ms: int,
    event_type: str,
    payload: dict,
    origin: str,
) -> SourceEvent:
    payload_json = _canonical(payload)
    return SourceEvent(
        source=source,
        natural_key=natural_key,
        source_timestamp_ms=timestamp_ms,
        received_timestamp_ms=timestamp_ms,
        event_type=event_type,
        payload_json=payload_json,
        payload_sha256=payload_sha256(payload_json),
        recovery_origin=origin,
    )


def _binance(
    minute: int,
    *,
    origin: str = "REST_BACKFILL",
    close: str = "101.000000",
) -> SourceEvent:
    timestamp_ms = minute * MINUTE_MS
    return _event(
        source="binance",
        natural_key=f"binance:BTCUSDT:1m:{timestamp_ms}",
        timestamp_ms=timestamp_ms,
        event_type="BINANCE_KLINE_CLOSED",
        payload={
            "close": close,
            "high": "102.000000",
            "interval": "1m",
            "low": "99.000000",
            "open": "100.000000",
            "open_time_ms": timestamp_ms,
            "symbol": "BTCUSDT",
            "volume": "2.500000",
        },
        origin=origin,
    )


def _book(
    asset_id: str,
    timestamp_ms: int,
    *,
    sequence: int = 0,
    origin: str = "REST_BACKFILL",
) -> SourceEvent:
    best_bid = 400_000 + sequence
    best_ask = 600_000 + sequence
    book_hash = f"hash-{asset_id}-{sequence}"
    return _event(
        source="polymarket",
        natural_key=f"polymarket:{asset_id}:book:{book_hash}",
        timestamp_ms=timestamp_ms,
        event_type="POLYMARKET_BOOK",
        payload={
            "asset_id": asset_id,
            "asks": [
                {
                    "price_micros": best_ask,
                    "size_micros": 2_000_000,
                }
            ],
            "best_ask_micros": best_ask,
            "best_bid_micros": best_bid,
            "bids": [
                {
                    "price_micros": best_bid,
                    "size_micros": 1_000_000,
                }
            ],
            "book_hash": book_hash,
            "event_type": "book",
            "spread_micros": best_ask - best_bid,
        },
        origin=origin,
    )


def _history(asset_id: str, timestamp_ms: int) -> SourceEvent:
    return _event(
        source="polymarket",
        natural_key=f"polymarket:{asset_id}:history:{timestamp_ms}",
        timestamp_ms=timestamp_ms,
        event_type="POLYMARKET_PRICE_HISTORY",
        payload={
            "asset_id": asset_id,
            "event_type": "price_history",
            "price_micros": 500_000,
            "timestamp_seconds": timestamp_ms // 1000,
        },
        origin="REST_BACKFILL",
    )


class FakeBinance:
    def __init__(
        self,
        *,
        recovered: tuple[SourceEvent, ...],
        live: tuple[SourceEvent, ...] = (),
        return_normally: bool = False,
    ) -> None:
        self.recovered = recovered
        self.live = live
        self.return_normally = return_normally
        self.closed = False

    async def recover(self, *, start_ms, end_ms):
        return list(self.recovered)

    async def stream(self, queue):
        for event in self.live:
            await queue.put(event)
        if self.return_normally:
            return
        await asyncio.Event().wait()

    async def close(self):
        self.closed = True


class FakePolymarket:
    def __init__(
        self,
        *,
        market_payload: dict | None = None,
        history: tuple[SourceEvent, ...] = (),
        live: tuple[SourceEvent, ...] = (),
        return_normally: bool = False,
    ) -> None:
        self.asset_ids = tuple(f"asset-{index:02d}" for index in range(22))
        self.market_ids = tuple(f"market-{index:02d}" for index in range(11))
        self.history = history
        self.live = live
        self.return_normally = return_normally
        self.closed = False
        identity_value = dict(market_payload) if market_payload is not None else {
            "asset_ids": list(self.asset_ids),
            "event_id": "event-1",
            "market_ids": list(self.market_ids),
        }
        identity_value.setdefault(
            "resolution_utc", "2033-05-18T03:33:20+00:00"
        )
        identity_json = _canonical(identity_value)
        self.reconciliation = MarketReconciliation(
            market_identity_json=identity_json,
            market_id="event-1",
            market_ids=self.market_ids,
            asset_ids=self.asset_ids,
            current_events=tuple(
                _book(asset_id, 240_000)
                for asset_id in self.asset_ids
            ),
            historical_depth="NOT_AVAILABLE_NOT_REQUIRED",
        )

    async def discover_and_reconcile(self):
        return self.reconciliation

    async def recover(self, reconciliation, *, start_ts, end_ts):
        return list(self.history)

    async def stream(self, *, asset_ids, queue):
        if tuple(asset_ids) != self.asset_ids:
            raise AssertionError("wrong asset set")
        for event in self.live:
            await queue.put(event)
        if self.return_normally:
            return
        await asyncio.Event().wait()

    async def close(self):
        self.closed = True


class RuntimeCoreAdversarialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name, "runtime.sqlite3")
        self.store = SqliteStore.open(self.path)
        self.store.migrate()
        self.runtime: C1RuntimeOrchestrator | None = None

    async def asyncTearDown(self):
        if self.runtime is not None:
            await self.runtime.stop()
        self.store.close()
        self.temp.cleanup()

    def _runtime(
        self,
        *,
        run_id: str = "run-1",
        clock_ms: int = 1_000,
        recovered: tuple[SourceEvent, ...] = (_binance(1), _binance(2)),
        binance_live: tuple[SourceEvent, ...] = (_binance(3, origin="LIVE"),),
        history: tuple[SourceEvent, ...] = (),
        polymarket_live: tuple[SourceEvent, ...] | None = None,
        market_payload: dict | None = None,
        binance_returns: bool = False,
        polymarket_returns: bool = False,
    ) -> C1RuntimeOrchestrator:
        polymarket = FakePolymarket(
            market_payload=market_payload,
            history=history,
            live=()
            if polymarket_live is None
            else polymarket_live,
            return_normally=polymarket_returns,
        )
        if polymarket_live is None:
            polymarket.live = (
                _book(
                    polymarket.asset_ids[0],
                    300_000,
                    sequence=1,
                    origin="LIVE",
                ),
            )
        return C1RuntimeOrchestrator(
            run_id=run_id,
            store=self.store,
            broker=OutboxBroker(),
            binance_adapter=FakeBinance(
                recovered=recovered,
                live=binance_live,
                return_normally=binance_returns,
            ),
            polymarket_adapter=polymarket,
            binance_start_ms=MINUTE_MS,
            binance_end_ms=2 * MINUTE_MS,
            history_start_ts=1,
            history_end_ts=2,
            clock_ms=lambda: clock_ms,
        )

    async def _start(self, runtime=None):
        self.runtime = runtime or self._runtime()
        await asyncio.wait_for(self.runtime.start(), timeout=2)
        return self.runtime

    async def test_same_database_restart_replays_identical_market_identity(self):
        first = await self._start(self._runtime(run_id="run-1", clock_ms=1_000))
        await first.stop()
        self.runtime = None

        second = await self._start(
            self._runtime(run_id="run-2", clock_ms=2_000)
        )

        self.assertTrue(second.status().live_ready)
        self.assertEqual(self.store.count("market_catalog"), 1)
        self.assertEqual(self.store.count("strategy_checkpoint_schedules"), 10)
        self.assertEqual(second.scheduler_writer_operation_count, 1)
        self.assertNotIn("MARKET_IDENTITY_CONFLICT", second.status().failure or "")

    async def test_same_market_id_with_changed_authoritative_payload_blocks(self):
        await self._start(self._runtime(run_id="run-1", clock_ms=1_000))
        await self.runtime.stop()
        self.runtime = None

        changed = {
            "asset_ids": [f"asset-{index:02d}" for index in range(22)],
            "event_id": "event-1",
            "market_ids": [f"changed-{index:02d}" for index in range(11)],
        }
        with self.assertRaisesRegex(Exception, "MARKET_IDENTITY_CONFLICT"):
            await self._start(
                self._runtime(
                    run_id="run-2",
                    clock_ms=2_000,
                    market_payload=changed,
                )
            )
        self.assertEqual(self.store.count("market_catalog"), 1)

    async def test_live_binance_is_buffered_until_backfill_and_continuity(self):
        runtime = await self._start()
        cursor = self.store.read_source_cursor("binance")

        self.assertTrue(runtime.status().live_ready)
        self.assertEqual(cursor["updated_at_ms"], 3 * MINUTE_MS)
        self.assertEqual(
            [
                row[0]
                for row in self.store.rows(
                    """
                    SELECT source_timestamp_ms
                    FROM source_events
                    WHERE source = 'binance'
                    ORDER BY event_id
                    """
                )
            ],
            [MINUTE_MS, 2 * MINUTE_MS, 3 * MINUTE_MS],
        )

    async def test_polymarket_history_commits_before_current_books(self):
        probe = FakePolymarket()
        history = (_history(probe.asset_ids[0], 60_000),)
        runtime = await self._start(self._runtime(history=history))
        first_asset = probe.asset_ids[0]
        timestamps = [
            row[0]
            for row in self.store.rows(
                """
                SELECT source_timestamp_ms
                FROM source_events
                WHERE source = 'polymarket'
                  AND payload_json LIKE ?
                ORDER BY event_id
                """,
                (f'%"asset_id":"{first_asset}"%',),
            )
        ]

        self.assertTrue(runtime.status().live_ready)
        self.assertEqual(timestamps[:2], [60_000, 240_000])

    async def test_missing_binance_minute_blocks_without_live_ready_incident(self):
        runtime = self._runtime(recovered=(_binance(1),))
        with self.assertRaisesRegex(Exception, "BINANCE_CONTINUITY"):
            await self._start(runtime)

        self.assertEqual(runtime.status().state, "RECOVERY_BLOCKED")
        self.assertFalse(runtime.status().live_ready)
        self.assertNotIn("LIVE_READY", runtime.lifecycle_states)

    async def test_normal_provider_stream_return_is_fatal(self):
        runtime = self._runtime(binance_returns=True, binance_live=())
        with self.assertRaisesRegex(
            Exception,
            "UNEXPECTED_PROVIDER_STREAM_EXIT",
        ):
            await self._start(runtime)

        self.assertEqual(runtime.status().source_health[0], ("binance", "FAILED"))
        self.assertFalse(runtime.status().live_ready)

    async def test_writer_failure_stops_startup_at_recovery_blocked(self):
        runtime = self._runtime()
        with mock.patch.object(
            self.store,
            "upsert_source_cursor",
            side_effect=RuntimeError("injected cursor failure"),
        ):
            with self.assertRaisesRegex(Exception, "WRITER_FAILED"):
                await self._start(runtime)

        self.assertEqual(runtime.status().state, "RECOVERY_BLOCKED")
        self.assertNotIn("LIVE_READY", runtime.lifecycle_states)
        self.assertEqual(runtime.pending_owned_tasks, ())

    async def test_old_exact_duplicate_does_not_regress_cursor_or_last_event_id(self):
        runtime = await self._start()
        before_cursor = self.store.read_source_cursor("binance")
        before_id = runtime.status().last_event_id

        await runtime.enqueue(_binance(1))
        await runtime.wait_for_source_idle()

        self.assertIsNone(runtime.status().failure)
        self.assertEqual(self.store.read_source_cursor("binance"), before_cursor)
        self.assertEqual(runtime.status().last_event_id, before_id)
        self.assertEqual(
            self.store.scalar(
                """
                SELECT COUNT(*) FROM source_events
                WHERE natural_key = ?
                """,
                (_binance(1).natural_key,),
            ),
            1,
        )

    async def test_conflicting_duplicate_payload_remains_fail_closed(self):
        runtime = await self._start()
        with self.assertRaisesRegex(ValueError, "SOURCE_EVENT_CONFLICT"):
            await runtime.enqueue(_binance(1, close="999.000000"))

        self.assertIn("SOURCE_EVENT_CONFLICT", runtime.status().failure or "")
        self.assertEqual(runtime.status().state, "RECOVERY_BLOCKED")

    async def test_backfill_alone_never_marks_stream_sources_live(self):
        runtime = self._runtime(binance_live=(), polymarket_live=())
        start = asyncio.create_task(runtime.start())
        await asyncio.sleep(0.05)
        try:
            self.assertFalse(start.done())
            self.assertEqual(
                dict(runtime.status().source_health),
                {"binance": "STARTING", "polymarket": "STARTING"},
            )
            self.assertFalse(runtime.status().live_ready)
        finally:
            start.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await start
            await runtime.stop()
            self.runtime = None

    async def test_all_runtime_sqlite_writes_have_one_task_owner(self):
        owners: list[str | None] = []
        methods = (
            "append_lifecycle_state",
            "append_incident",
            "persist_market_identity",
            "append_source_event",
            "upsert_source_cursor",
            "append_canonical_snapshot",
            "commit_evaluation_signal_and_outbox",
        )
        originals = {
            name: getattr(self.store, name)
            for name in methods
        }

        def wrap(name):
            def invoke(*args, **kwargs):
                task = asyncio.current_task()
                owners.append(None if task is None else task.get_name())
                return originals[name](*args, **kwargs)

            return invoke

        patches = [
            mock.patch.object(self.store, name, side_effect=wrap(name))
            for name in methods
        ]
        for patcher in patches:
            patcher.start()
        try:
            runtime = await self._start()
        finally:
            for patcher in reversed(patches):
                patcher.stop()

        self.assertTrue(runtime.status().live_ready)
        self.assertEqual(len(set(owners)), 1)
        self.assertEqual(set(owners), {"run-1:writer"})

    def test_snapshot_contains_real_bounded_current_state(self):
        assets = tuple(f"asset-{index:02d}" for index in range(22))
        reconciliation = MarketReconciliation(
            market_identity_json=_canonical(
                {"asset_ids": list(assets), "event_id": "event-1"}
            ),
            market_id="event-1",
            market_ids=tuple(f"market-{index:02d}" for index in range(11)),
            asset_ids=assets,
            current_events=(),
            historical_depth="NOT_AVAILABLE_NOT_REQUIRED",
        )
        projector = CanonicalProjector(backend_session_id="session")
        projector.set_market_identity(reconciliation)
        event_id = 1
        for asset_id in assets:
            projector.apply(
                CommittedSourceEvent(
                    event_id,
                    _book(asset_id, 100_000),
                    True,
                )
            )
            event_id += 1
        for sequence in range(1, 101):
            projector.apply(
                CommittedSourceEvent(
                    event_id,
                    _book(
                        assets[0],
                        100_000 + sequence,
                        sequence=sequence,
                        origin="LIVE",
                    ),
                    True,
                )
            )
            event_id += 1
        projector.set_live_ready()
        snapshot = projector.apply(
            CommittedSourceEvent(event_id, _binance(3, origin="LIVE"), True)
        )
        payload = json.loads(snapshot.payload_json)

        self.assertIn("binance", payload)
        self.assertEqual(payload["binance"]["close_usd_micros"], 101_000_000)
        self.assertEqual(len(payload["polymarket"]["books"]), 22)
        first = payload["polymarket"]["books"][0]
        self.assertEqual(first["best_bid_micros"], 400_100)
        self.assertEqual(first["best_ask_micros"], 600_100)
        self.assertEqual(first["spread_micros"], 200_000)
        self.assertLessEqual(len(snapshot.source_event_ids), 1 + len(assets))
        self.assertEqual(len(snapshot.source_event_ids), 23)

    def test_recovered_and_current_evaluation_origins_are_deterministic(self):
        base = {
            "backend_session_id": "run-1",
            "binance_ready": True,
            "canonical": True,
            "canonical_state_hash": "a" * 64,
            "evaluation_origin": "RECOVERED_AFTER_DOWNTIME",
            "market_identity": "event-1",
            "polymarket_ready": True,
            "trigger_committed_after_live_ready": False,
            "triggering_event_natural_key": _binance(2).natural_key,
        }

        def snapshot(value, *, key):
            payload_json = _canonical(value)
            return CanonicalSnapshot(
                snapshot_key=key,
                source_event_ids=tuple(range(1, 24)),
                payload_json=payload_json,
                payload_sha256=payload_sha256(payload_json),
                created_at_ms=2 * MINUTE_MS,
                recovery_origin=value["evaluation_origin"],
            )

        recovered = evaluate_canary(snapshot(base, key="recovered-1"))
        replay_value = {**base, "backend_session_id": "run-2"}
        replay = evaluate_canary(snapshot(replay_value, key="recovered-2"))
        current_value = {
            **replay_value,
            "canonical_state_hash": "b" * 64,
            "evaluation_origin": "CURRENT_LIVE_REEVALUATION",
            "trigger_committed_after_live_ready": True,
            "triggering_event_natural_key": _binance(3).natural_key,
        }
        current = evaluate_canary(snapshot(current_value, key="current"))

        self.assertEqual(recovered.evaluation_key, replay.evaluation_key)
        self.assertEqual(recovered.origin, "RECOVERED_AFTER_DOWNTIME")
        self.assertEqual(current.origin, "CURRENT_LIVE_REEVALUATION")
        self.assertNotEqual(recovered.evaluation_key, current.evaluation_key)
        for evaluation in (recovered, current):
            payload = json.loads(evaluation.payload_json)
            self.assertFalse(evaluation.execution_eligible)
            self.assertFalse(payload["trading_eligible"])

    async def test_failure_state_matches_persisted_lifecycle_and_api(self):
        runtime = self._runtime(recovered=(_binance(1),))
        with self.assertRaises(Exception):
            await self._start(runtime)
        reader = self.store.open_read_store()
        try:
            latest = reader.latest_lifecycle_state(run_id="run-1")
            health = build_health_payload(reader, runtime.status())
        finally:
            reader.close()

        self.assertEqual(runtime.status().state, "RECOVERY_BLOCKED")
        self.assertEqual(latest["state"], "RECOVERY_BLOCKED")
        self.assertFalse(runtime.status().live_ready)
        self.assertNotEqual(health["status"], "PASS")
        self.assertNotIn("LIVE_READY", runtime.lifecycle_states)

    async def test_combined_same_db_cutover_restart_gate(self):
        probe = FakePolymarket()
        history = (_history(probe.asset_ids[0], 60_000),)
        first_poly_live = _book(
            probe.asset_ids[0],
            300_000,
            sequence=1,
            origin="LIVE",
        )
        first = await self._start(
            self._runtime(
                run_id="combined-run-1",
                clock_ms=1_000,
                history=history,
                polymarket_live=(first_poly_live,),
            )
        )
        self.assertTrue(first.status().live_ready)
        self.assertEqual(first.writer_consumer_count, 1)
        self.assertEqual(
            set(first.pending_owned_tasks),
            {"writer", "binance_stream", "polymarket_stream"},
        )
        first_last_event_id = first.status().last_event_id
        await first.stop()
        self.runtime = None

        second_poly_live = _book(
            probe.asset_ids[0],
            360_000,
            sequence=2,
            origin="LIVE",
        )
        second = await self._start(
            self._runtime(
                run_id="combined-run-2",
                clock_ms=2_000,
                history=history,
                binance_live=(
                    _binance(3, origin="LIVE"),
                    _binance(4, origin="LIVE"),
                ),
                polymarket_live=(first_poly_live, second_poly_live),
            )
        )
        self.assertTrue(second.status().live_ready)
        self.assertEqual(second.writer_consumer_count, 1)
        self.assertEqual(
            set(second.pending_owned_tasks),
            {"writer", "binance_stream", "polymarket_stream"},
        )
        self.assertEqual(self.store.count("market_catalog"), 1)
        self.assertGreater(second.status().last_event_id, first_last_event_id)
        self.assertEqual(
            self.store.read_source_cursor("binance")["updated_at_ms"],
            4 * MINUTE_MS,
        )
        self.assertEqual(
            self.store.read_source_cursor(
                f"polymarket:{probe.asset_ids[0]}"
            )["updated_at_ms"],
            360_000,
        )

        reader = self.store.open_read_store()
        try:
            evaluations = reader.strategy_evaluations(limit=20)
            latest_snapshot = reader.latest_canonical_snapshot()
            first_lifecycle = reader.latest_lifecycle_state(
                run_id="combined-run-1"
            )
            second_lifecycle = reader.latest_lifecycle_state(
                run_id="combined-run-2"
            )
        finally:
            reader.close()
        origins = [row["payload"]["origin"] for row in evaluations]
        self.assertEqual(
            origins.count("RECOVERED_AFTER_DOWNTIME"),
            1,
        )
        self.assertEqual(
            origins.count("CURRENT_LIVE_REEVALUATION"),
            2,
        )
        self.assertLessEqual(
            len(latest_snapshot["source_event_ids"]),
            1 + second.status().asset_count,
        )
        self.assertEqual(first_lifecycle["state"], "LIVE_READY")
        self.assertEqual(second_lifecycle["state"], "LIVE_READY")
        self.assertEqual(
            self.store.scalar(
                """
                SELECT COUNT(*) FROM incidents
                WHERE incident_key LIKE 'lifecycle:combined-run-%'
                  AND status = 'RECOVERY_BLOCKED'
                """
            ),
            0,
        )
        self.assertEqual(self.store.integrity_report()["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
