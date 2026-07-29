from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.lifecycle import STARTUP_SEQUENCE
from src.models import CanonicalSnapshot, StrategyEvaluation, payload_sha256
from src.storage import SqliteStore


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


def canonical_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class RuntimePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_directory.name, "runtime.sqlite3")
        self.store = SqliteStore.open(self.db_path)
        self.store.migrate()

    def tearDown(self):
        self.store.close()
        self.temp_directory.cleanup()

    def test_missing_source_cursor_inserts(self):
        changed = self.store.upsert_source_cursor(
            source="binance",
            cursor_json='{"minute":1}',
            updated_at_ms=100,
        )
        self.assertTrue(changed)
        self.assertEqual(
            self.store.read_source_cursor("binance"),
            {
                "source": "binance",
                "cursor": {"minute": 1},
                "cursor_json": '{"minute":1}',
                "updated_at_ms": 100,
            },
        )

    def test_exact_source_cursor_replay_is_no_op(self):
        self.store.upsert_source_cursor(
            source="binance",
            cursor_json='{ "minute": 1 }',
            updated_at_ms=100,
        )
        changed = self.store.upsert_source_cursor(
            source="binance",
            cursor_json='{"minute":1}',
            updated_at_ms=100,
        )
        self.assertFalse(changed)
        self.assertEqual(self.store.count("source_cursors"), 1)

    def test_newer_source_cursor_timestamp_updates(self):
        self.store.upsert_source_cursor(
            source="binance",
            cursor_json='{"minute":1}',
            updated_at_ms=100,
        )
        changed = self.store.upsert_source_cursor(
            source="binance",
            cursor_json='{"minute":2}',
            updated_at_ms=101,
        )
        self.assertTrue(changed)
        self.assertEqual(
            self.store.read_source_cursor("binance")["cursor"],
            {"minute": 2},
        )

    def test_older_source_cursor_timestamp_is_rejected(self):
        self.store.upsert_source_cursor(
            source="binance",
            cursor_json='{"minute":2}',
            updated_at_ms=101,
        )
        with self.assertRaisesRegex(ValueError, "SOURCE_CURSOR_REGRESSION"):
            self.store.upsert_source_cursor(
                source="binance",
                cursor_json='{"minute":1}',
                updated_at_ms=100,
            )

    def test_same_source_cursor_timestamp_with_different_payload_is_rejected(self):
        self.store.upsert_source_cursor(
            source="binance",
            cursor_json='{"minute":1}',
            updated_at_ms=100,
        )
        with self.assertRaisesRegex(ValueError, "SOURCE_CURSOR_CONFLICT"):
            self.store.upsert_source_cursor(
                source="binance",
                cursor_json='{"minute":2}',
                updated_at_ms=100,
            )

    def test_source_cursor_invalid_json_nan_and_infinity_are_rejected(self):
        invalid_values = (
            "{",
            '{"value":NaN}',
            '{"value":Infinity}',
            '{"value":-Infinity}',
        )
        for cursor_json in invalid_values:
            with self.subTest(cursor_json=cursor_json):
                with self.assertRaisesRegex(ValueError, "INVALID_CURSOR_JSON"):
                    self.store.upsert_source_cursor(
                        source="binance",
                        cursor_json=cursor_json,
                        updated_at_ms=100,
                    )
        self.assertEqual(self.store.count("source_cursors"), 0)

    def test_source_cursor_survives_close_and_reopen(self):
        self.store.upsert_source_cursor(
            source="binance",
            cursor_json='{"minute":1}',
            updated_at_ms=100,
        )
        self._reopen()
        self.assertEqual(
            self.store.read_source_cursor("binance")["cursor_json"],
            '{"minute":1}',
        )

    def test_market_identity_inserts_and_is_readable(self):
        payload = canonical_json({"event_id": "733270", "markets": 11})
        result = self.store.persist_market_identity(
            market_id="733270",
            payload_json=payload,
            payload_sha256=self._sha(payload),
            updated_at_ms=200,
        )
        self.assertTrue(result.inserted)
        self.assertGreater(result.row_id, 0)
        reader = self.store.open_read_store()
        try:
            self.assertEqual(
                reader.current_market_identity(),
                {
                    "market_id": "733270",
                    "payload": {"event_id": "733270", "markets": 11},
                    "updated_at_ms": 200,
                },
            )
        finally:
            reader.close()

    def test_market_identity_exact_replay_is_idempotent(self):
        payload = canonical_json({"event_id": "733270"})
        first = self.store.persist_market_identity(
            market_id="733270",
            payload_json=payload,
            payload_sha256=self._sha(payload),
            updated_at_ms=200,
        )
        second = self.store.persist_market_identity(
            market_id="733270",
            payload_json='{ "event_id": "733270" }',
            payload_sha256=self._sha(payload),
            updated_at_ms=200,
        )
        self.assertTrue(first.inserted)
        self.assertFalse(second.inserted)
        self.assertEqual(first.row_id, second.row_id)

    def test_market_identity_payload_conflict_is_rejected(self):
        first = canonical_json({"event_id": "733270"})
        second = canonical_json({"event_id": "733270", "changed": True})
        self.store.persist_market_identity(
            market_id="733270",
            payload_json=first,
            payload_sha256=self._sha(first),
            updated_at_ms=200,
        )
        with self.assertRaisesRegex(ValueError, "MARKET_IDENTITY_CONFLICT"):
            self.store.persist_market_identity(
                market_id="733270",
                payload_json=second,
                payload_sha256=self._sha(second),
                updated_at_ms=200,
            )

    def test_market_identity_wrong_payload_sha_is_rejected(self):
        payload = canonical_json({"event_id": "733270"})
        with self.assertRaisesRegex(ValueError, "PAYLOAD_SHA256_MISMATCH"):
            self.store.persist_market_identity(
                market_id="733270",
                payload_json=payload,
                payload_sha256="0" * 64,
                updated_at_ms=200,
            )
        self.assertEqual(self.store.count("market_catalog"), 0)

    def test_canonical_snapshot_persists_source_ids_in_original_order(self):
        snapshot = self._snapshot(source_event_ids=(3, 1, 2))
        result = self.store.append_canonical_snapshot(snapshot)
        self.assertTrue(result.inserted)
        reader = self.store.open_read_store()
        try:
            stored = reader.latest_canonical_snapshot()
        finally:
            reader.close()
        self.assertEqual(stored["source_event_ids"], [3, 1, 2])
        self.assertEqual(stored["payload"], {"canonical": True})

    def test_canonical_snapshot_exact_replay_is_idempotent(self):
        snapshot = self._snapshot()
        first = self.store.append_canonical_snapshot(snapshot)
        second = self.store.append_canonical_snapshot(snapshot)
        self.assertTrue(first.inserted)
        self.assertFalse(second.inserted)
        self.assertEqual(first.row_id, second.row_id)

    def test_canonical_snapshot_key_collision_is_rejected(self):
        snapshot = self._snapshot()
        self.store.append_canonical_snapshot(snapshot)
        changed = dataclasses.replace(snapshot, source_event_ids=(9,))
        with self.assertRaisesRegex(ValueError, "CANONICAL_SNAPSHOT_CONFLICT"):
            self.store.append_canonical_snapshot(changed)

    def test_recovered_snapshot_origin_survives_reopen(self):
        snapshot = self._snapshot(
            recovery_origin="RECOVERED_AFTER_DOWNTIME"
        )
        self.store.append_canonical_snapshot(snapshot)
        self._reopen()
        reader = self.store.open_read_store()
        try:
            stored = reader.latest_canonical_snapshot()
        finally:
            reader.close()
        self.assertEqual(
            stored["recovery_origin"],
            "RECOVERED_AFTER_DOWNTIME",
        )

    def test_strategy_evaluation_persists_with_boolean_eligibility(self):
        evaluation = self._evaluation()
        result = self.store.append_strategy_evaluation(evaluation)
        self.assertTrue(result.inserted)
        reader = self.store.open_read_store()
        try:
            rows = reader.strategy_evaluations(limit=10)
        finally:
            reader.close()
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0]["execution_eligible"], False)
        self.assertEqual(
            rows[0]["payload"],
            {
                "current_reevaluation_required": False,
                "origin": "LIVE",
                "reason_code": "CANARY_READY",
            },
        )

    def test_strategy_evaluation_exact_replay_is_idempotent(self):
        evaluation = self._evaluation()
        first = self.store.append_strategy_evaluation(evaluation)
        second = self.store.append_strategy_evaluation(evaluation)
        self.assertTrue(first.inserted)
        self.assertFalse(second.inserted)
        self.assertEqual(first.row_id, second.row_id)

    def test_strategy_evaluation_key_collision_is_rejected(self):
        evaluation = self._evaluation()
        self.store.append_strategy_evaluation(evaluation)
        changed = dataclasses.replace(evaluation, status="NO_SIGNAL")
        with self.assertRaisesRegex(
            ValueError,
            "STRATEGY_EVALUATION_CONFLICT",
        ):
            self.store.append_strategy_evaluation(changed)

    def test_recovered_evaluation_cannot_be_execution_eligible(self):
        evaluation = self._evaluation(
            origin="RECOVERED_AFTER_DOWNTIME",
            execution_eligible=True,
        )
        with self.assertRaisesRegex(
            ValueError,
            "RECOVERED_EVALUATION_EXECUTION_FORBIDDEN",
        ):
            self.store.append_strategy_evaluation(evaluation)
        self.assertEqual(self.store.count("strategy_evaluations"), 0)

    def test_append_evaluation_does_not_create_signal_or_outbox(self):
        self.store.append_strategy_evaluation(self._evaluation())
        self.assertEqual(self.store.count("strategy_evaluations"), 1)
        self.assertEqual(self.store.count("signals"), 0)
        self.assertEqual(self.store.count("outbox_events"), 0)

    def test_incident_exact_replay_is_idempotent(self):
        first = self._append_incident()
        second = self._append_incident()
        self.assertTrue(first.inserted)
        self.assertFalse(second.inserted)
        self.assertEqual(first.row_id, second.row_id)

    def test_incident_key_collision_is_rejected(self):
        self._append_incident()
        with self.assertRaisesRegex(ValueError, "INCIDENT_CONFLICT"):
            self.store.append_incident(
                incident_key="incident:1",
                severity="CRITICAL",
                status="OPEN",
                payload_json='{"code":"CHANGED"}',
                created_at_ms=300,
            )

    def test_lifecycle_state_uses_frozen_sequence_index(self):
        result = self.store.append_lifecycle_state(
            run_id="run-1",
            state="SOURCE_DISCOVERY",
            sequence_index=3,
            created_at_ms=400,
        )
        self.assertTrue(result.inserted)
        incident = self.store.rows(
            """
            SELECT incident_key, severity, status, payload_json
            FROM incidents
            """
        )[0]
        self.assertEqual(
            incident[:3],
            ("lifecycle:run-1:3:SOURCE_DISCOVERY", "INFO", "SOURCE_DISCOVERY"),
        )
        self.assertEqual(
            json.loads(incident[3]),
            {
                "record_type": "LIFECYCLE_STATE",
                "run_id": "run-1",
                "sequence_index": 3,
                "state": "SOURCE_DISCOVERY",
            },
        )
        self.assertEqual(STARTUP_SEQUENCE[3], "SOURCE_DISCOVERY")

    def test_latest_lifecycle_state_ignores_ordinary_incidents(self):
        self.store.append_lifecycle_state(
            run_id="run-1",
            state="BOOTING",
            sequence_index=0,
            created_at_ms=400,
        )
        self.store.append_incident(
            incident_key="provider:late-warning",
            severity="WARNING",
            status="OPEN",
            payload_json='{"code":"PROVIDER_WARNING"}',
            created_at_ms=999,
        )
        reader = self.store.open_read_store()
        try:
            latest = reader.latest_lifecycle_state()
        finally:
            reader.close()
        self.assertEqual(latest["state"], "BOOTING")
        self.assertEqual(latest["run_id"], "run-1")

    def test_latest_lifecycle_state_filters_by_escaped_run_id(self):
        self.store.append_lifecycle_state(
            run_id="run%_one",
            state="BOOTING",
            sequence_index=0,
            created_at_ms=400,
        )
        self.store.append_lifecycle_state(
            run_id="runXXone",
            state="BOOTING",
            sequence_index=0,
            created_at_ms=500,
        )
        reader = self.store.open_read_store()
        try:
            latest = reader.latest_lifecycle_state(run_id="run%_one")
        finally:
            reader.close()
        self.assertEqual(latest["run_id"], "run%_one")
        self.assertEqual(latest["created_at_ms"], 400)

    def test_invalid_lifecycle_transition_index_is_rejected(self):
        invalid = (
            ("SOURCE_DISCOVERY", 2),
            ("NOT_A_STATE", 0),
            ("BOOTING", -1),
        )
        for state, sequence_index in invalid:
            with self.subTest(state=state, sequence_index=sequence_index):
                with self.assertRaisesRegex(ValueError, "INVALID_LIFECYCLE"):
                    self.store.append_lifecycle_state(
                        run_id="run-1",
                        state=state,
                        sequence_index=sequence_index,
                        created_at_ms=400,
                    )
        self.assertEqual(self.store.count("incidents"), 0)

    def test_recovery_blocked_is_stored_as_critical(self):
        self.store.append_lifecycle_state(
            run_id="run-1",
            state="BOOTING",
            sequence_index=0,
            created_at_ms=400,
        )
        self.store.append_lifecycle_state(
            run_id="run-1",
            state="RECOVERY_BLOCKED",
            sequence_index=1,
            created_at_ms=401,
            detail="SOURCE_GAP",
        )
        reader = self.store.open_read_store()
        try:
            latest = reader.latest_lifecycle_state(run_id="run-1")
        finally:
            reader.close()
        self.assertEqual(latest["severity"], "CRITICAL")
        self.assertEqual(latest["status"], "RECOVERY_BLOCKED")
        self.assertEqual(latest["detail"], "SOURCE_GAP")

    def test_recovery_blocked_requires_current_next_sequence_index(self):
        self.store.append_lifecycle_state(
            run_id="run-1",
            state="BOOTING",
            sequence_index=0,
            created_at_ms=400,
        )
        with self.assertRaisesRegex(
            ValueError,
            "INVALID_LIFECYCLE_SEQUENCE_INDEX",
        ):
            self.store.append_lifecycle_state(
                run_id="run-1",
                state="RECOVERY_BLOCKED",
                sequence_index=2,
                created_at_ms=401,
            )
        self.assertEqual(self.store.count("incidents"), 1)

    def test_lifecycle_state_survives_close_and_reopen(self):
        self.store.append_lifecycle_state(
            run_id="run-1",
            state="BOOTING",
            sequence_index=0,
            created_at_ms=400,
        )
        self._reopen()
        reader = self.store.open_read_store()
        try:
            latest = reader.latest_lifecycle_state(run_id="run-1")
        finally:
            reader.close()
        self.assertEqual(latest["state"], "BOOTING")

    def test_migration_version_remains_exactly_two(self):
        self.assertEqual(self.store.count("schema_migrations"), 2)
        self.assertEqual(self.store.integrity_report()["migration_version"], 2)

    def test_required_table_set_is_unchanged(self):
        rows = self.store.rows(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        self.assertEqual(
            {row[0] for row in rows},
            REQUIRED_TABLES | {"sqlite_sequence"},
        )

    def test_new_persistence_methods_open_no_write_connection(self):
        with mock.patch("src.storage.sqlite3.connect") as connect:
            self.store.upsert_source_cursor(
                source="binance",
                cursor_json="{}",
                updated_at_ms=1,
            )
            payload = canonical_json({"event_id": "733270"})
            self.store.persist_market_identity(
                market_id="733270",
                payload_json=payload,
                payload_sha256=self._sha(payload),
                updated_at_ms=2,
            )
            self.store.append_canonical_snapshot(self._snapshot())
            self.store.append_strategy_evaluation(self._evaluation())
            self._append_incident()
        connect.assert_not_called()

    def test_failed_conflict_leaves_original_row_unchanged(self):
        original = canonical_json({"event_id": "733270"})
        changed = canonical_json({"event_id": "changed"})
        self.store.persist_market_identity(
            market_id="733270",
            payload_json=original,
            payload_sha256=self._sha(original),
            updated_at_ms=200,
        )
        with self.assertRaisesRegex(ValueError, "MARKET_IDENTITY_CONFLICT"):
            self.store.persist_market_identity(
                market_id="733270",
                payload_json=changed,
                payload_sha256=self._sha(changed),
                updated_at_ms=201,
            )
        stored = self.store.rows(
            """
            SELECT payload_json, payload_sha256, updated_at_ms
            FROM market_catalog
            WHERE market_id = ?
            """,
            ("733270",),
        )[0]
        self.assertEqual(stored, (original, self._sha(original), 200))

    def test_read_methods_cannot_write(self):
        self.store.append_canonical_snapshot(self._snapshot())
        reader = self.store.open_read_store()
        try:
            self.assertIsNotNone(reader.latest_canonical_snapshot())
            with self.assertRaises(sqlite3.OperationalError):
                reader._connection.execute("DELETE FROM canonical_state")
        finally:
            reader.close()
        self.assertEqual(self.store.count("canonical_state"), 1)

    def test_storage_seams_add_no_auth_order_or_wallet_functionality(self):
        source = Path("src/storage.py").read_text(encoding="utf-8").lower()
        forbidden = (
            "authorization",
            "private_key",
            "sign_order",
            "place_order",
            "cancel_order",
            "wallet_address",
        )
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def _append_incident(self):
        return self.store.append_incident(
            incident_key="incident:1",
            severity="WARNING",
            status="OPEN",
            payload_json='{"code":"TEST"}',
            created_at_ms=300,
        )

    def _snapshot(
        self,
        *,
        source_event_ids=(2, 1),
        recovery_origin="LIVE",
    ):
        payload = canonical_json({"canonical": True})
        return CanonicalSnapshot(
            snapshot_key="snapshot:1",
            source_event_ids=source_event_ids,
            payload_json=payload,
            payload_sha256=payload_sha256(payload),
            created_at_ms=250,
            recovery_origin=recovery_origin,
        )

    def _evaluation(
        self,
        *,
        origin="LIVE",
        execution_eligible=False,
    ):
        payload = canonical_json(
            {
                "current_reevaluation_required": (
                    origin == "RECOVERED_AFTER_DOWNTIME"
                ),
                "origin": origin,
                "reason_code": "CANARY_READY",
            }
        )
        return StrategyEvaluation(
            evaluation_key="evaluation:1",
            strategy_id="CANARY_SYNC_READY_V1",
            strategy_version="1",
            status="SIGNAL",
            input_snapshot_hash="a" * 64,
            evaluation_revision=1,
            execution_eligible=execution_eligible,
            evaluated_at_ms=275,
            payload_json=payload,
            reason_code="CANARY_READY",
            origin=origin,
            historical_signal_is_current_live_signal=False,
            current_reevaluation_required=(
                origin == "RECOVERED_AFTER_DOWNTIME"
            ),
        )

    def _reopen(self):
        self.store.close()
        self.store = SqliteStore.open(self.db_path)
        self.store.migrate()

    @staticmethod
    def _sha(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    unittest.main()
