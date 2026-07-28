from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.fixed_point import (
    ProbabilityMicros,
    SharesMicros,
    UsdMicros,
    probability_to_micros,
)
from src.models import (
    CanonicalSnapshot,
    OutboxEvent,
    SignalRecord,
    SourceEvent,
    StrategyEvaluation,
)
from src.storage import SqliteStore, SqliteWriter, WriteCommand


class DomainTests(unittest.TestCase):
    def test_probability_string_converts_exactly(self):
        self.assertEqual(probability_to_micros("0.456"), 456000)
        self.assertEqual(ProbabilityMicros.from_decimal("0.456").value, 456000)

    def test_probability_boundaries_are_allowed(self):
        self.assertEqual(probability_to_micros("0"), 0)
        self.assertEqual(probability_to_micros("1"), 1_000_000)

    def test_probability_outside_unit_interval_is_rejected(self):
        for value in ("1.001", "-0.000001"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "PROBABILITY_OUT_OF_RANGE"):
                    probability_to_micros(value)

    def test_float_is_rejected_at_fixed_point_boundaries(self):
        for fixed_type in (ProbabilityMicros, SharesMicros, UsdMicros):
            with self.subTest(fixed_type=fixed_type.__name__):
                with self.assertRaisesRegex(ValueError, "FIXED_POINT_FLOAT_FORBIDDEN"):
                    fixed_type.from_decimal(0.5)

    def test_shares_and_usd_are_stored_as_integer_micros(self):
        shares = SharesMicros.from_decimal("1.25")
        usd = UsdMicros.from_decimal("42.000001")
        self.assertEqual(shares.value, 1_250_000)
        self.assertEqual(usd.value, 42_000_001)
        self.assertIs(type(shares.value), int)
        self.assertIs(type(usd.value), int)

    def test_binance_closed_kline_natural_key_is_deterministic(self):
        event = SourceEvent.binance_closed_kline(
            symbol="BTCUSDT",
            interval="1m",
            open_time_ms=1000,
            payload=b"{}",
        )
        self.assertEqual(event.natural_key, "binance:BTCUSDT:1m:1000")

    def test_equivalent_payloads_have_the_same_canonical_hash(self):
        first = self._event(payload=b'{"b":2,"a":1}')
        second = self._event(payload=b'{ "a": 1, "b": 2 }')
        self.assertEqual(first.payload_json, '{"a":1,"b":2}')
        self.assertEqual(first.payload_sha256, second.payload_sha256)

    def test_changed_payload_has_a_different_hash(self):
        first = self._event(payload=b'{"close":"100"}')
        second = self._event(payload=b'{"close":"101"}')
        self.assertNotEqual(first.payload_sha256, second.payload_sha256)

    def test_committed_domain_records_are_immutable(self):
        event = self._event(payload=b"{}")
        snapshot = CanonicalSnapshot(
            snapshot_key="snapshot:1",
            source_event_ids=(1,),
            payload_json="{}",
            payload_sha256=event.payload_sha256,
            created_at_ms=1001,
            recovery_origin="LIVE",
        )
        signal = SignalRecord(
            identity_key="signal:1",
            evaluation_key="evaluation:1",
            strategy_id="CANARY_SYNC_READY_V1",
            signal_type="SYNC_READY",
            payload_json="{}",
            created_at_ms=1002,
        )
        outbox = OutboxEvent(
            event_id=1,
            topic="signal.created",
            payload_json="{}",
            created_at_ms=1002,
        )
        records_and_fields = (
            (event, "natural_key"),
            (snapshot, "snapshot_key"),
            (signal, "identity_key"),
            (outbox, "event_id"),
        )
        for record, field in records_and_fields:
            with self.subTest(record=type(record).__name__):
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    setattr(record, field, "changed")

    def test_recovered_origin_is_explicit(self):
        event = self._event(
            payload=b"{}",
            recovery_origin="RECOVERED_AFTER_DOWNTIME",
        )
        self.assertEqual(event.recovery_origin, "RECOVERED_AFTER_DOWNTIME")

    def test_execution_eligibility_is_an_explicit_boolean(self):
        evaluation = StrategyEvaluation(
            evaluation_key="evaluation:1",
            strategy_id="CANARY_SYNC_READY_V1",
            strategy_version="1",
            status="SIGNAL",
            input_snapshot_hash="a" * 64,
            evaluation_revision=1,
            execution_eligible=False,
            evaluated_at_ms=1002,
            payload_json="{}",
        )
        self.assertIs(evaluation.execution_eligible, False)

        with self.assertRaisesRegex(
            ValueError, "INVALID_EXECUTION_ELIGIBILITY_TYPE"
        ):
            dataclasses.replace(evaluation, execution_eligible=0)

    @staticmethod
    def _event(
        *,
        payload: bytes,
        recovery_origin: str = "LIVE",
    ) -> SourceEvent:
        return SourceEvent.binance_closed_kline(
            symbol="BTCUSDT",
            interval="1m",
            open_time_ms=1000,
            payload=payload,
            received_timestamp_ms=1001,
            recovery_origin=recovery_origin,
        )


class StorageTests(unittest.TestCase):
    REQUIRED_TABLES = {
        "schema_migrations",
        "source_events",
        "source_cursors",
        "market_catalog",
        "canonical_state",
        "strategy_evaluations",
        "signals",
        "outbox_events",
        "incidents",
    }

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_directory.name, "store.sqlite3")
        self.store = SqliteStore.open(self.db_path)
        self.store.migrate()
        self.event = SourceEvent.binance_closed_kline(
            symbol="BTCUSDT",
            interval="1m",
            open_time_ms=1000,
            payload=b'{"close":"100.000000"}',
            received_timestamp_ms=1001,
        )

    def tearDown(self):
        self.store.close()
        self.temp_directory.cleanup()

    def test_database_uses_required_pragmas(self):
        self.assertEqual(self.store.scalar("PRAGMA journal_mode").lower(), "wal")
        self.assertEqual(self.store.scalar("PRAGMA synchronous"), 2)
        self.assertEqual(self.store.scalar("PRAGMA foreign_keys"), 1)

    def test_all_required_tables_exist(self):
        rows = self.store.rows(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        names = {row[0] for row in rows}
        self.assertTrue(self.REQUIRED_TABLES <= names)

    def test_migrations_are_idempotent(self):
        self.store.migrate()
        self.assertEqual(self.store.count("schema_migrations"), 2)
        self.assertEqual(self.store.integrity_report()["migration_version"], 2)

    def test_duplicate_source_event_is_committed_once(self):
        first = self.store.append_source_event(self.event)
        second = self.store.append_source_event(self.event)
        self.assertTrue(first.inserted)
        self.assertFalse(second.inserted)
        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(self.store.count("source_events"), 1)

    def test_payload_hash_is_persisted(self):
        result = self.store.append_source_event(self.event)
        stored_hash = self.store.scalar(
            "SELECT payload_sha256 FROM source_events WHERE event_id = ?",
            (result.event_id,),
        )
        self.assertEqual(stored_hash, self.event.payload_sha256)

    def test_natural_key_collision_cannot_overwrite_an_event(self):
        self.store.append_source_event(self.event)
        changed_json = '{"close":"101.000000"}'
        changed = dataclasses.replace(
            self.event,
            payload_json=changed_json,
            payload_sha256=hashlib.sha256(changed_json.encode("utf-8")).hexdigest(),
        )
        with self.assertRaisesRegex(ValueError, "SOURCE_EVENT_CONFLICT"):
            self.store.append_source_event(changed)
        self.assertEqual(self.store.count("source_events"), 1)
        self.assertEqual(
            self.store.scalar("SELECT payload_sha256 FROM source_events"),
            self.event.payload_sha256,
        )

    def test_read_only_connection_reads_but_cannot_write(self):
        self.store.append_source_event(self.event)
        reader = self.store.open_read_only()
        try:
            count = reader.execute("SELECT COUNT(*) FROM source_events").fetchone()[0]
            self.assertEqual(count, 1)
            with self.assertRaises(sqlite3.OperationalError):
                reader.execute("DELETE FROM source_events")
        finally:
            reader.close()

    def test_migration_failure_does_not_accept_a_partial_migration(self):
        import src.storage as storage_module
        from unittest import mock

        self.store.close()
        broken_db = Path(self.temp_directory.name, "broken.sqlite3")
        migrations = Path(self.temp_directory.name, "broken_migrations")
        migrations.mkdir()
        Path(migrations, "0001_broken.sql").write_text(
            "CREATE TABLE partially_applied(value INTEGER);\n"
            "CREATE TABL invalid_sql(value INTEGER);\n",
            encoding="utf-8",
        )

        with mock.patch.object(storage_module, "_MIGRATIONS_DIR", migrations):
            broken_store = SqliteStore.open(broken_db)
            try:
                with self.assertRaises(sqlite3.Error):
                    broken_store.migrate()
                names = {
                    row[0]
                    for row in broken_store.rows(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertNotIn("partially_applied", names)
                self.assertEqual(broken_store.count("schema_migrations"), 0)
            finally:
                broken_store.close()
        self.store = SqliteStore.open(self.db_path)

    def test_integrity_report_passes_for_valid_database(self):
        report = self.store.integrity_report()
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["quick_check"], "ok")
        self.assertEqual(report["integrity_check"], "ok")
        self.assertEqual(report["foreign_keys"], 1)
        self.assertEqual(report["journal_mode"], "wal")
        self.assertEqual(report["migration_version"], 2)

    def test_sqlite_writer_owns_runtime_write_connection(self):
        self.store.close()

        async def exercise_writer():
            writer = SqliteWriter(self.db_path)
            queue = asyncio.Queue()
            task = asyncio.create_task(writer.run(queue))
            future = asyncio.get_running_loop().create_future()
            await queue.put(WriteCommand.append_source_event(self.event, future))
            result = await future
            await queue.put(WriteCommand.stop())
            await task
            return result

        result = asyncio.run(exercise_writer())
        self.assertTrue(result.inserted)
        self.store = SqliteStore.open(self.db_path)
        self.assertEqual(self.store.count("source_events"), 1)


if __name__ == "__main__":
    unittest.main()
