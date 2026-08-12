from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from src.outbox import OutboxBroker
from src.runtime_orchestrator import C1RuntimeOrchestrator
from src.storage import SqliteStore
from tests.test_runtime_core_adversarial import (
    FakeBinance,
    FakePolymarket,
    MINUTE_MS,
    _binance,
    _history,
)


class ReconnectingBinance(FakeBinance):
    def __init__(self) -> None:
        super().__init__(
            recovered=(_binance(1), _binance(2)),
            live=(_binance(3, origin="LIVE"),),
        )
        self.recover_calls = []
        self.trigger = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.continue_reconnect = asyncio.Event()
        self.recovery_entered = asyncio.Event()
        self.allow_recovery = asyncio.Event()
        self.reconnected = asyncio.Event()
        self.fail_recovery = False
        self.state_sink = None
        self.reconnect_recovery = None

    def configure_runtime_callbacks(self, *, state_sink, reconnect_recovery):
        self.state_sink = state_sink
        self.reconnect_recovery = reconnect_recovery

    async def recover(self, *, start_ms, end_ms):
        self.recover_calls.append((start_ms, end_ms))
        if len(self.recover_calls) == 1:
            return list(self.recovered)
        self.recovery_entered.set()
        await self.allow_recovery.wait()
        if self.fail_recovery:
            raise RuntimeError("bounded upstream history unavailable")
        return [_binance(4), _binance(4)]

    async def stream(self, queue):
        await queue.put(self.live[0])
        await self.trigger.wait()
        await self.state_sink("DISCONNECTED", "BINANCE_STREAM_DISCONNECTED")
        self.disconnected.set()
        await self.continue_reconnect.wait()
        await self.state_sink("CONNECTING", "BINANCE_STREAM_DISCONNECTED")
        await self.state_sink("RECOVERING", "BINANCE_STREAM_DISCONNECTED")
        await self.reconnect_recovery()
        await self.state_sink("LIVE", None)
        self.reconnected.set()
        await asyncio.Event().wait()


class ReconnectingPolymarket(FakePolymarket):
    def __init__(self) -> None:
        super().__init__(history=())
        self.live = (self.reconciliation.current_events[0],)
        self.recover_calls = []
        self.trigger = asyncio.Event()
        self.reconnected = asyncio.Event()
        self.state_sink = None
        self.reconnect_recovery = None

    def configure_runtime_callbacks(self, *, state_sink, reconnect_recovery):
        self.state_sink = state_sink
        self.reconnect_recovery = reconnect_recovery

    async def recover(
        self,
        reconciliation,
        *,
        start_ts,
        end_ts,
        start_ts_by_asset=None,
    ):
        self.recover_calls.append((start_ts, end_ts, start_ts_by_asset))
        if len(self.recover_calls) == 1:
            event = _history(self.asset_ids[0], 120_000)
            return [
                replace(
                    event,
                    natural_key=(
                        f"polymarket:{self.asset_ids[0]}:price_history:120:"
                        + "0" * 64
                    ),
                )
            ]
        return []

    async def stream(self, *, asset_ids, queue):
        await queue.put(self.live[0])
        await self.trigger.wait()
        await self.state_sink(
            "DISCONNECTED", "POLYMARKET_STREAM_DISCONNECTED"
        )
        await self.state_sink(
            "CONNECTING", "POLYMARKET_STREAM_DISCONNECTED"
        )
        await self.state_sink(
            "RECOVERING", "POLYMARKET_STREAM_DISCONNECTED"
        )
        await self.reconnect_recovery()
        await self.state_sink("LIVE", None)
        self.reconnected.set()
        await asyncio.Event().wait()


class OperationalReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SqliteStore.open(Path(self.temp.name, "runtime.sqlite3"))
        self.store.migrate()
        self.now_ms = 120_000
        self.binance = ReconnectingBinance()
        self.polymarket = ReconnectingPolymarket()
        self.runtime = C1RuntimeOrchestrator(
            run_id="operational-reconnect",
            store=self.store,
            broker=OutboxBroker(),
            binance_adapter=self.binance,
            polymarket_adapter=self.polymarket,
            binance_start_ms=MINUTE_MS,
            binance_end_ms=2 * MINUTE_MS,
            binance_last_committed_open_ms=0,
            binance_end_resolver=lambda: self.now_ms,
            history_start_ts=60,
            history_end_ts=120,
            clock_ms=lambda: self.now_ms,
            source_freshness_threshold_seconds=3600,
        )
        await asyncio.wait_for(self.runtime.start(), timeout=2)

    async def asyncTearDown(self):
        await self.runtime.stop()
        self.store.close()
        self.temp.cleanup()

    async def test_binance_reconnect_backfills_cursor_gap_before_live(self):
        self.now_ms = 4 * MINUTE_MS
        self.binance.trigger.set()
        await asyncio.wait_for(self.binance.disconnected.wait(), timeout=1)

        self.assertEqual(
            dict(self.runtime.status().source_health)["binance"],
            "DISCONNECTED:BINANCE_STREAM_DISCONNECTED",
        )
        self.binance.continue_reconnect.set()
        await asyncio.wait_for(self.binance.recovery_entered.wait(), timeout=1)
        self.assertEqual(
            dict(self.runtime.status().source_health)["binance"],
            "RECOVERING:BINANCE_STREAM_DISCONNECTED",
        )
        self.binance.allow_recovery.set()
        await asyncio.wait_for(self.binance.reconnected.wait(), timeout=1)
        await self.runtime.wait_for_source_idle()

        self.assertEqual(self.binance.recover_calls[-1], (4 * MINUTE_MS,) * 2)
        self.assertEqual(
            self.store.read_source_cursor("binance")["updated_at_ms"],
            4 * MINUTE_MS,
        )
        self.assertEqual(
            self.store.scalar(
                "SELECT COUNT(*) FROM source_events WHERE natural_key = ?",
                (_binance(4).natural_key,),
            ),
            1,
        )
        self.assertEqual(
            dict(self.runtime.status().source_health)["binance"], "LIVE"
        )

    async def test_polymarket_reconnect_uses_old_cursor_without_floor_gap(self):
        self.now_ms = 10_800_000
        self.polymarket.trigger.set()
        await asyncio.wait_for(self.polymarket.reconnected.wait(), timeout=1)
        await self.runtime.wait_for_source_idle()

        start_ts, end_ts, starts = self.polymarket.recover_calls[-1]
        self.assertEqual((start_ts, end_ts), (7_200, 10_800))
        self.assertEqual(starts[self.polymarket.asset_ids[0]], 180)
        self.assertEqual(
            dict(self.runtime.status().source_health)["polymarket"], "LIVE"
        )

    async def test_unrecoverable_gap_is_durable_and_runtime_fatal(self):
        self.now_ms = 4 * MINUTE_MS
        self.binance.fail_recovery = True
        self.binance.trigger.set()
        await asyncio.wait_for(self.binance.disconnected.wait(), timeout=1)
        self.binance.continue_reconnect.set()
        await asyncio.wait_for(self.binance.recovery_entered.wait(), timeout=1)
        self.binance.allow_recovery.set()

        with self.assertRaisesRegex(
            RuntimeError, "UNRECOVERABLE_OPERATIONAL_GAP"
        ):
            await asyncio.wait_for(
                self.runtime.wait_runtime_termination(), timeout=1
            )

        self.assertEqual(self.runtime.status().state, "RECOVERY_BLOCKED")
        self.assertEqual(
            self.store.scalar(
                "SELECT COUNT(*) FROM incidents "
                "WHERE status = 'UNRECOVERABLE_OPERATIONAL_GAP'"
            ),
            1,
        )


class StartupUnrecoverableGapTests(unittest.IsolatedAsyncioTestCase):
    async def test_polymarket_startup_gap_failure_is_explicit_incident(self):
        class UnrecoverablePolymarket(FakePolymarket):
            async def recover(self, reconciliation, *, start_ts, end_ts):
                raise ValueError("POLYMARKET_HISTORY_REQUEST_LIMIT")

        temp = tempfile.TemporaryDirectory()
        store = SqliteStore.open(Path(temp.name, "runtime.sqlite3"))
        store.migrate()
        runtime = C1RuntimeOrchestrator(
            run_id="startup-gap",
            store=store,
            broker=OutboxBroker(),
            binance_adapter=FakeBinance(
                recovered=(_binance(1), _binance(2)),
                live=(_binance(3, origin="LIVE"),),
            ),
            polymarket_adapter=UnrecoverablePolymarket(
                live=(FakePolymarket().reconciliation.current_events[0],)
            ),
            binance_start_ms=MINUTE_MS,
            binance_end_ms=2 * MINUTE_MS,
            history_start_ts=60,
            history_end_ts=10_800,
            clock_ms=lambda: 10_800_000,
        )
        try:
            with self.assertRaisesRegex(
                RuntimeError, "POLYMARKET_HISTORY_REQUEST_LIMIT"
            ):
                await asyncio.wait_for(runtime.start(), timeout=2)
            self.assertEqual(
                store.scalar(
                    "SELECT COUNT(*) FROM incidents "
                    "WHERE status = 'UNRECOVERABLE_OPERATIONAL_GAP'"
                ),
                1,
            )
        finally:
            await runtime.stop()
            store.close()
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
