from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.models import SourceEvent, payload_sha256
from src.storage import SqliteStore


class DataCompletionLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SqliteStore.open(Path(self.temp.name) / "data.sqlite3")
        self.store.migrate()

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    @staticmethod
    def _event(*, value: str = "100", natural_key: str = "import:binance:60000", timestamp_ms: int = 60_000) -> SourceEvent:
        body = json.dumps({"close": value}, sort_keys=True, separators=(",", ":"))
        return SourceEvent(
            source="binance",
            natural_key=natural_key,
            source_timestamp_ms=timestamp_ms,
            received_timestamp_ms=70_000,
            event_type="BINANCE_KLINE_CLOSED",
            payload_json=body,
            payload_sha256=payload_sha256(body),
            recovery_origin="HISTORICAL_IMPORT",
        )

    def _records(self):
        from src.data_completion import ImportRunRecord, SourceRangeRecord

        run = ImportRunRecord(
            import_run_id="import-run-001",
            artifact_sha256="a" * 64,
            source_path_fingerprint="b" * 64,
            dataset_start_ms=60_000,
            dataset_end_ms=120_000,
            declared_row_count=1,
            schema_mapping_sha256="e" * 64,
            inserted_row_count=1,
            replayed_row_count=0,
            conflict_row_count=0,
            dropped_row_count=0,
            status="COMPLETE",
            created_at_ms=80_000,
        )
        scope_json = json.dumps(
            {"interval": "1m", "symbol": "BTCUSDT"},
            sort_keys=True,
            separators=(",", ":"),
        )
        evidence_json = json.dumps(
            {"inventory": "verified-source-bundle"},
            sort_keys=True,
            separators=(",", ":"),
        )
        source_range = SourceRangeRecord(
            import_run_id=run.import_run_id,
            source="binance",
            canonical_scope_json=scope_json,
            canonical_scope_sha256=hashlib.sha256(scope_json.encode()).hexdigest(),
            requested_start_ms=60_000,
            requested_end_ms=120_000,
            granularity_ms=60_000,
            contract_version="BTC_DATA_COMPLETENESS_V1",
            status="COMPLETE",
            expected_row_count=1,
            observed_row_count=1,
            missing_row_count=0,
            reason_code=None,
            evidence_metadata_json=evidence_json,
            evidence_sha256=hashlib.sha256(evidence_json.encode()).hexdigest(),
            updated_at_ms=80_000,
        )
        return run, source_range

    def test_migration_v5_preserves_import_and_range_ledgers(self) -> None:
        self.assertEqual(self.store.integrity_report()["migration_version"], 5)
        tables = {
            row[0]
            for row in self.store.rows(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("data_import_runs", tables)
        self.assertIn("data_source_ranges", tables)

    def test_migration_rejects_empty_half_open_import_dataset(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.store._connection.execute(
                "INSERT INTO data_import_runs(import_run_id,artifact_sha256,source_path_fingerprint,"
                "schema_mapping_sha256,dataset_start_ms,dataset_end_ms,declared_row_count,"
                "inserted_row_count,replayed_row_count,conflict_row_count,dropped_row_count,status,"
                "created_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("empty", "a" * 64, "b" * 64, "c" * 64, 1, 1, 0, 0, 0, 0, 0, "COMPLETE", 1),
            )

    def test_migration_rejects_negative_or_unaligned_range_boundaries(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.store._connection.execute(
                "INSERT INTO data_import_runs(import_run_id,artifact_sha256,source_path_fingerprint,"
                "schema_mapping_sha256,dataset_start_ms,dataset_end_ms,declared_row_count,"
                "inserted_row_count,replayed_row_count,conflict_row_count,dropped_row_count,status,"
                "created_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("negative", "a" * 64, "b" * 64, "c" * 64, -1, 1, 0, 0, 0, 0, 0, "COMPLETE", 1),
            )
        for start_ms, end_ms in ((-1, 60_000), (1, 60_001)):
            with self.subTest(start_ms=start_ms, end_ms=end_ms):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.store._connection.execute(
                        "INSERT INTO data_source_ranges(range_key,source,canonical_scope_json,"
                        "canonical_scope_sha256,requested_start_ms,requested_end_ms,granularity_ms,"
                        "contract_version) VALUES (?,?,?,?,?,?,?,?)",
                        (
                            hashlib.sha256(f"{start_ms}:{end_ms}".encode()).hexdigest(),
                            "binance", "{}", hashlib.sha256(b"{}").hexdigest(),
                            start_ms, end_ms, 60_000, "BTC_DATA_COMPLETENESS_V1",
                        ),
                    )

    def test_exact_five_states_are_enforced_by_database(self) -> None:
        import dataclasses
        from src.data_completion import COMPLETENESS_STATES

        self.assertEqual(
            COMPLETENESS_STATES,
            (
                "COMPLETE",
                "EXPECTED_ABSENT",
                "SOURCE_UNAVAILABLE_RETRYABLE",
                "SOURCE_CONFLICT_FATAL",
                "NOT_REQUIRED_BY_CONTRACT",
            ),
        )
        run, source_range = self._records()
        with self.assertRaisesRegex(ValueError, "INVALID_COMPLETENESS_STATUS"):
            dataclasses.replace(source_range, status="UNKNOWN")

    def test_atomic_import_exact_replay_is_idempotent(self) -> None:
        from src.data_completion import DataCompletionLedger

        run, source_range = self._records()
        ledger = DataCompletionLedger(self.store)
        first = ledger.commit_import(run, (source_range,), (self._event(),))
        second = ledger.commit_import(run, (source_range,), (self._event(),))
        self.assertTrue(first.inserted)
        self.assertFalse(second.inserted)
        self.assertEqual(first.import_run_row_id, second.import_run_row_id)
        self.assertEqual(self.store.count("data_import_runs"), 1)
        self.assertEqual(self.store.count("data_source_ranges"), 1)
        self.assertEqual(self.store.count("source_events"), 1)

    def test_same_import_id_with_changed_artifact_fails_closed(self) -> None:
        import dataclasses
        from src.data_completion import DataCompletionLedger

        run, source_range = self._records()
        ledger = DataCompletionLedger(self.store)
        ledger.commit_import(run, (source_range,), (self._event(),))
        with self.assertRaisesRegex(ValueError, "DATA_IMPORT_RUN_CONFLICT"):
            ledger.commit_import(
                dataclasses.replace(run, artifact_sha256="d" * 64),
                (source_range,),
                (self._event(),),
            )

    def test_import_cannot_attach_noncomplete_assessment(self) -> None:
        import dataclasses
        from src.data_completion import DataCompletionLedger

        run, source_range = self._records()
        ledger = DataCompletionLedger(self.store)
        ledger.commit_import(run, (source_range,), (self._event(),))
        absent_evidence = json.dumps(
            {"inventory": {"absence_confirmed": True, "artifact_sha256": "f" * 64, "artifact_type": "SANITIZED_PROVIDER_INVENTORY_V1", "reason_code": "NO_MARKET"}},
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.assertRaisesRegex(ValueError, "IMPORT_COMPLETE_RANGES_REQUIRED"):
            ledger.commit_import(
                run,
                (dataclasses.replace(
                    source_range,
                    status="EXPECTED_ABSENT",
                    expected_row_count=0,
                    observed_row_count=0,
                    reason_code="NO_MARKET",
                    evidence_metadata_json=absent_evidence,
                    evidence_sha256=hashlib.sha256(absent_evidence.encode()).hexdigest(),
                ),),
                (self._event(),),
            )

    def test_event_conflict_rolls_back_new_run_and_range(self) -> None:
        from src.data_completion import DataCompletionLedger

        self.store.append_source_event(self._event())
        run, source_range = self._records()
        with self.assertRaisesRegex(ValueError, "SOURCE_EVENT_CONFLICT"):
            DataCompletionLedger(self.store).commit_import(
                run,
                (source_range,),
                (self._event(value="101"),),
            )
        self.assertEqual(self.store.count("data_import_runs"), 0)
        self.assertEqual(self.store.count("data_source_ranges"), 0)

    def test_range_identity_is_deterministic_and_half_open(self) -> None:
        import dataclasses

        _, source_range = self._records()
        self.assertEqual(source_range.range_key, dataclasses.replace(source_range).range_key)
        with self.assertRaisesRegex(ValueError, "INVALID_SOURCE_RANGE_BOUNDARY"):
            dataclasses.replace(
                source_range,
                requested_end_ms=source_range.requested_start_ms,
            )

    def test_complete_range_cannot_hide_missing_rows(self) -> None:
        import dataclasses

        _, source_range = self._records()
        with self.assertRaisesRegex(ValueError, "INCOMPLETE_RANGE_CANNOT_BE_COMPLETE"):
            dataclasses.replace(
                source_range,
                observed_row_count=0,
                missing_row_count=1,
            )

    def test_expected_absent_requires_confirmed_inventory_evidence(self) -> None:
        import dataclasses

        _, source_range = self._records()
        with self.assertRaisesRegex(ValueError, "EXPECTED_ABSENT_EVIDENCE_REQUIRED"):
            dataclasses.replace(
                source_range,
                status="EXPECTED_ABSENT",
                expected_row_count=0,
                observed_row_count=0,
                evidence_metadata_json="{}",
                evidence_sha256=hashlib.sha256(b"{}").hexdigest(),
            )

    def test_expected_absent_assessment_binds_verified_inventory_artifact_bytes(self) -> None:
        import dataclasses
        from src.data_completion import DataCompletionLedger

        _, source_range = self._records()
        inventory_bytes = json.dumps(
            {
                "canonical_scope_sha256": source_range.canonical_scope_sha256,
                "observed_row_count": 0,
                "reason_code": "NO_EVENT_IN_EXACT_RANGE",
                "requested_end_ms": source_range.requested_end_ms,
                "requested_start_ms": source_range.requested_start_ms,
                "schema_version": "SANITIZED_PROVIDER_INVENTORY_V1",
                "source": source_range.source,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        inventory_sha256 = hashlib.sha256(inventory_bytes).hexdigest()
        evidence = json.dumps(
            {
                "inventory": {
                    "absence_confirmed": True,
                    "artifact_sha256": inventory_sha256,
                    "artifact_type": "SANITIZED_PROVIDER_INVENTORY_V1",
                    "reason_code": "NO_EVENT_IN_EXACT_RANGE",
                }
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        absent = dataclasses.replace(
            source_range,
            import_run_id=None,
            status="EXPECTED_ABSENT",
            expected_row_count=1,
            observed_row_count=0,
            missing_row_count=1,
            reason_code="NO_EVENT_IN_EXACT_RANGE",
            evidence_metadata_json=evidence,
            evidence_sha256=hashlib.sha256(evidence.encode()).hexdigest(),
        )
        ledger = DataCompletionLedger(self.store)
        with self.assertRaisesRegex(ValueError, "EXPECTED_ABSENT_INVENTORY_BYTES_REQUIRED"):
            ledger.record_range_assessment(absent)
        with self.assertRaisesRegex(ValueError, "EXPECTED_ABSENT_INVENTORY_HASH_MISMATCH"):
            ledger.record_range_assessment(absent, inventory_artifact_bytes=b"wrong")
        wrong_inventory_bytes = json.dumps(
            {
                **json.loads(inventory_bytes),
                "observed_row_count": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        wrong_evidence = json.dumps(
            {
                "inventory": {
                    "absence_confirmed": True,
                    "artifact_sha256": hashlib.sha256(wrong_inventory_bytes).hexdigest(),
                    "artifact_type": "SANITIZED_PROVIDER_INVENTORY_V1",
                    "reason_code": "NO_EVENT_IN_EXACT_RANGE",
                }
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        self_consistent_but_false = dataclasses.replace(
            absent,
            evidence_metadata_json=wrong_evidence,
            evidence_sha256=hashlib.sha256(wrong_evidence.encode()).hexdigest(),
        )
        with self.assertRaisesRegex(ValueError, "EXPECTED_ABSENT_INVENTORY_CONTRACT_MISMATCH"):
            ledger.record_range_assessment(
                self_consistent_but_false,
                inventory_artifact_bytes=wrong_inventory_bytes,
            )
        for changed_inventory, error in (
            ({**json.loads(evidence)["inventory"], "artifact_type": "ARBITRARY_SCHEMA_V9"}, "EXPECTED_ABSENT_EVIDENCE_REQUIRED"),
            ({**json.loads(evidence)["inventory"], "reason_code": "CONTRADICTORY_REASON"}, "EXPECTED_ABSENT_EVIDENCE_REQUIRED"),
        ):
            with self.subTest(error=error, inventory=changed_inventory):
                changed_evidence = json.dumps(
                    {"inventory": changed_inventory},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                with self.assertRaisesRegex(ValueError, error):
                    dataclasses.replace(
                        absent,
                        evidence_metadata_json=changed_evidence,
                        evidence_sha256=hashlib.sha256(changed_evidence.encode()).hexdigest(),
                    )
        ledger.record_range_assessment(absent, inventory_artifact_bytes=inventory_bytes)
        self.assertEqual(self.store.count("data_source_range_assessments"), 1)

    def test_import_run_records_schema_and_zero_dropped_rows(self) -> None:
        import dataclasses

        run, _ = self._records()
        self.assertEqual(run.schema_mapping_sha256, "e" * 64)
        with self.assertRaisesRegex(ValueError, "IMPORT_DROPPED_ROWS_FORBIDDEN"):
            dataclasses.replace(run, dropped_row_count=1)

    def test_imported_event_is_linked_to_import_run(self) -> None:
        from src.data_completion import DataCompletionLedger

        run, source_range = self._records()
        DataCompletionLedger(self.store).commit_import(
            run, (source_range,), (self._event(),)
        )
        self.assertEqual(
            self.store.scalar(
                "SELECT import_run_id FROM data_import_run_events WHERE event_id=(SELECT event_id FROM source_events WHERE natural_key=?)",
                (self._event().natural_key,),
            ),
            run.import_run_id,
        )

    def test_counters_are_transaction_outcomes_not_caller_assertions(self) -> None:
        import dataclasses
        from src.data_completion import DataCompletionLedger

        run, source_range = self._records()
        false_receipt = dataclasses.replace(
            run, inserted_row_count=0, replayed_row_count=1
        )
        with self.assertRaisesRegex(ValueError, "IMPORT_ROW_COUNTS_MISMATCH"):
            DataCompletionLedger(self.store).commit_import(
                false_receipt, (source_range,), (self._event(),)
            )
        self.assertEqual(self.store.count("source_events"), 0)

    def test_second_immutable_run_records_canonical_replay(self) -> None:
        import dataclasses
        from src.data_completion import DataCompletionLedger

        run, source_range = self._records()
        ledger = DataCompletionLedger(self.store)
        ledger.commit_import(run, (source_range,), (self._event(),))
        replay_run = dataclasses.replace(
            run,
            import_run_id="import-run-002",
            inserted_row_count=0,
            replayed_row_count=1,
            created_at_ms=90_000,
        )
        replay_range = dataclasses.replace(
            source_range,
            import_run_id=replay_run.import_run_id,
            updated_at_ms=90_000,
        )
        result = ledger.commit_import(replay_run, (replay_range,), (self._event(),))
        self.assertTrue(result.inserted)
        self.assertEqual(self.store.count("source_events"), 1)
        self.assertEqual(self.store.count("data_import_runs"), 2)
        self.assertEqual(self.store.count("data_import_run_events"), 2)

    def test_event_must_match_source_and_half_open_range(self) -> None:
        import dataclasses
        from src.data_completion import DataCompletionLedger

        run, source_range = self._records()
        ledger = DataCompletionLedger(self.store)
        with self.assertRaisesRegex(ValueError, "IMPORT_EVENT_OUTSIDE_DECLARED_RANGE"):
            ledger.commit_import(
                run,
                (source_range,),
                (self._event(timestamp_ms=120_000),),
            )
        wrong_source = dataclasses.replace(source_range, source="polymarket")
        with self.assertRaisesRegex(ValueError, "IMPORT_EVENT_OUTSIDE_DECLARED_RANGE"):
            ledger.commit_import(run, (wrong_source,), (self._event(),))
        self.assertEqual(self.store.count("source_events"), 0)

    def test_invalid_second_event_rolls_back_first_event(self) -> None:
        import dataclasses
        from src.data_completion import DataCompletionLedger

        existing = self._event(value="old", natural_key="existing", timestamp_ms=120_000)
        self.store.append_source_event(existing)
        run, source_range = self._records()
        run = dataclasses.replace(run, declared_row_count=2, inserted_row_count=2, dataset_end_ms=180_000)
        source_range = dataclasses.replace(
            source_range, requested_end_ms=180_000, expected_row_count=2, observed_row_count=2
        )
        first = self._event(natural_key="new", timestamp_ms=60_000)
        conflict = self._event(value="changed", natural_key="existing", timestamp_ms=120_000)
        with self.assertRaisesRegex(ValueError, "SOURCE_EVENT_CONFLICT"):
            DataCompletionLedger(self.store).commit_import(
                run, (source_range,), (first, conflict)
            )
        self.assertIsNone(
            self.store.scalar("SELECT event_id FROM source_events WHERE natural_key='new'")
        )
        self.assertEqual(self.store.count("data_import_runs"), 0)

    def test_expected_absent_requires_zero_observations_and_structured_provenance(self) -> None:
        import dataclasses

        _, source_range = self._records()
        fake = json.dumps({"inventory": False}, sort_keys=True, separators=(",", ":"))
        with self.assertRaisesRegex(ValueError, "EXPECTED_ABSENT_EVIDENCE_REQUIRED"):
            dataclasses.replace(
                source_range,
                status="EXPECTED_ABSENT",
                expected_row_count=5,
                observed_row_count=5,
                evidence_metadata_json=fake,
                evidence_sha256=hashlib.sha256(fake.encode()).hexdigest(),
            )

    def test_retryable_assessment_can_be_followed_by_complete_assessment(self) -> None:
        import dataclasses
        from src.data_completion import DataCompletionLedger

        run, source_range = self._records()
        ledger = DataCompletionLedger(self.store)
        unavailable = dataclasses.replace(
            source_range,
            import_run_id=None,
            status="SOURCE_UNAVAILABLE_RETRYABLE",
            expected_row_count=1,
            observed_row_count=0,
            missing_row_count=1,
            reason_code="SOURCE_TIMEOUT",
            updated_at_ms=70_000,
        )
        ledger.record_range_assessment(unavailable)
        ledger.commit_import(run, (source_range,), (self._event(),))
        rows = self.store.rows(
            "SELECT status FROM data_source_range_assessments ORDER BY assessment_row_id"
        )
        self.assertEqual(rows, [("SOURCE_UNAVAILABLE_RETRYABLE",), ("COMPLETE",)])

    def test_terminal_state_semantics_reject_contradictory_counts(self) -> None:
        import dataclasses

        run, source_range = self._records()
        with self.assertRaisesRegex(ValueError, "IMPORT_STATUS_SEMANTICS_INVALID"):
            dataclasses.replace(run, conflict_row_count=1, inserted_row_count=0)
        with self.assertRaisesRegex(ValueError, "SOURCE_RANGE_STATUS_SEMANTICS_INVALID"):
            dataclasses.replace(
                source_range,
                status="NOT_REQUIRED_BY_CONTRACT",
                expected_row_count=1,
                observed_row_count=1,
            )

    def test_concurrent_identical_import_is_serialized_without_duplicates(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from src.data_completion import DataCompletionLedger

        run, source_range = self._records()
        database_path = Path(self.temp.name) / "data.sqlite3"
        barrier = Barrier(2)

        def worker() -> bool:
            store = SqliteStore.open(database_path)
            try:
                barrier.wait()
                return DataCompletionLedger(store).commit_import(
                    run, (source_range,), (self._event(),)
                ).inserted
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker) for _ in range(2)]
            outcomes = sorted(future.result() for future in futures)
        self.assertEqual(outcomes, [False, True])
        self.assertEqual(self.store.count("data_import_runs"), 1)
        self.assertEqual(self.store.count("source_events"), 1)

    def test_existing_run_replay_verifies_complete_event_content(self) -> None:
        from src.data_completion import DataCompletionLedger

        run, source_range = self._records()
        ledger = DataCompletionLedger(self.store)
        ledger.commit_import(run, (source_range,), (self._event(),))
        with self.assertRaisesRegex(ValueError, "SOURCE_EVENT_CONFLICT"):
            ledger.commit_import(run, (source_range,), (self._event(value="changed"),))

    def test_complete_observed_count_is_derived_from_unique_matching_events(self) -> None:
        import dataclasses
        from src.data_completion import DataCompletionLedger

        run, source_range = self._records()
        false_complete = dataclasses.replace(
            source_range, expected_row_count=2, observed_row_count=2
        )
        with self.assertRaisesRegex(ValueError, "SOURCE_RANGE_OBSERVED_COUNT_MISMATCH"):
            DataCompletionLedger(self.store).commit_import(
                run, (false_complete,), (self._event(),)
            )

    def test_dense_binance_range_derives_expected_closed_minutes(self) -> None:
        import dataclasses
        from src.data_completion import DataCompletionLedger

        run, source_range = self._records()
        run = dataclasses.replace(run, dataset_end_ms=120_000)
        false_complete = dataclasses.replace(
            source_range,
            requested_end_ms=180_000,
            expected_row_count=1,
            observed_row_count=1,
        )
        with self.assertRaisesRegex(ValueError, "SOURCE_RANGE_EXPECTED_COUNT_MISMATCH"):
            DataCompletionLedger(self.store).commit_import(
                run, (false_complete,), (self._event(),)
            )

    def test_dense_binance_requires_each_expected_timestamp_exactly_once(self) -> None:
        import dataclasses
        from src.data_completion import DataCompletionLedger

        run, source_range = self._records()
        run = dataclasses.replace(run, declared_row_count=2, inserted_row_count=2)
        source_range = dataclasses.replace(
            source_range,
            requested_end_ms=180_000,
            expected_row_count=2,
            observed_row_count=2,
        )
        duplicate_minute = self._event(natural_key="duplicate-minute", timestamp_ms=60_000)
        with self.assertRaisesRegex(ValueError, "DENSE_RANGE_TIMESTAMP_COVERAGE_MISMATCH"):
            DataCompletionLedger(self.store).commit_import(
                run, (source_range,), (self._event(), duplicate_minute)
            )

    def test_sparse_complete_range_requires_exact_expected_key_inventory(self) -> None:
        import dataclasses
        from src.data_completion import DataCompletionLedger

        run, source_range = self._records()
        sparse = dataclasses.replace(source_range, source="polymarket")
        event = dataclasses.replace(self._event(), source="polymarket")
        with self.assertRaisesRegex(ValueError, "SPARSE_RANGE_EXPECTED_KEYS_REQUIRED"):
            DataCompletionLedger(self.store).commit_import(run, (sparse,), (event,))

    def test_sparse_complete_range_accepts_exact_hashed_key_inventory(self) -> None:
        import dataclasses
        from src.data_completion import DataCompletionLedger

        run, source_range = self._records()
        event = dataclasses.replace(self._event(), source="polymarket")
        key_hash = hashlib.sha256(event.natural_key.encode()).hexdigest()
        evidence = json.dumps(
            {"expected_event_natural_key_sha256": [key_hash]},
            sort_keys=True,
            separators=(",", ":"),
        )
        sparse = dataclasses.replace(
            source_range,
            source="polymarket",
            evidence_metadata_json=evidence,
            evidence_sha256=hashlib.sha256(evidence.encode()).hexdigest(),
        )
        result = DataCompletionLedger(self.store).commit_import(
            run, (sparse,), (event,)
        )
        self.assertTrue(result.inserted)

    def test_sparse_complete_range_rejects_wrong_hashed_key_inventory(self) -> None:
        import dataclasses
        from src.data_completion import DataCompletionLedger

        run, source_range = self._records()
        event = dataclasses.replace(self._event(), source="polymarket")
        evidence = json.dumps(
            {"expected_event_natural_key_sha256": ["f" * 64]},
            sort_keys=True,
            separators=(",", ":"),
        )
        sparse = dataclasses.replace(
            source_range,
            source="polymarket",
            evidence_metadata_json=evidence,
            evidence_sha256=hashlib.sha256(evidence.encode()).hexdigest(),
        )
        with self.assertRaisesRegex(ValueError, "SPARSE_RANGE_EXPECTED_KEYS_MISMATCH"):
            DataCompletionLedger(self.store).commit_import(
                run, (sparse,), (event,)
            )

    def test_overlapping_ranges_cannot_double_count_one_event(self) -> None:
        import dataclasses
        from src.data_completion import DataCompletionLedger

        run, source_range = self._records()
        second = dataclasses.replace(
            source_range,
            canonical_scope_json='{"interval":"1m","partition":"duplicate","symbol":"BTCUSDT"}',
            canonical_scope_sha256=hashlib.sha256(b'{"interval":"1m","partition":"duplicate","symbol":"BTCUSDT"}').hexdigest(),
        )
        with self.assertRaisesRegex(ValueError, "IMPORT_EVENT_RANGE_AMBIGUOUS"):
            DataCompletionLedger(self.store).commit_import(
                run, (source_range, second), (self._event(),)
            )

    def test_existing_run_replay_cannot_append_changed_assessment(self) -> None:
        import dataclasses
        from src.data_completion import DataCompletionLedger

        run, source_range = self._records()
        ledger = DataCompletionLedger(self.store)
        ledger.commit_import(run, (source_range,), (self._event(),))
        changed = dataclasses.replace(source_range, updated_at_ms=90_000)
        with self.assertRaisesRegex(ValueError, "DATA_IMPORT_RUN_RANGE_SET_CONFLICT"):
            ledger.commit_import(run, (changed,), (self._event(),))
        self.assertEqual(self.store.count("data_source_range_assessments"), 1)

    def test_assessments_are_monotonic_and_terminal_states_do_not_regress(self) -> None:
        import dataclasses
        from src.data_completion import DataCompletionLedger

        _, source_range = self._records()
        ledger = DataCompletionLedger(self.store)
        retryable = dataclasses.replace(
            source_range,
            import_run_id=None,
            status="SOURCE_UNAVAILABLE_RETRYABLE",
            expected_row_count=1,
            observed_row_count=0,
            missing_row_count=1,
            reason_code="SOURCE_TIMEOUT",
            updated_at_ms=70_000,
        )
        ledger.record_range_assessment(retryable)
        with self.assertRaisesRegex(ValueError, "SOURCE_RANGE_ASSESSMENT_NOT_MONOTONIC"):
            ledger.record_range_assessment(dataclasses.replace(retryable, updated_at_ms=60_000))

        inventory_bytes = json.dumps(
            {
                "canonical_scope_sha256": retryable.canonical_scope_sha256,
                "observed_row_count": 0,
                "reason_code": "NO_MARKET",
                "requested_end_ms": retryable.requested_end_ms,
                "requested_start_ms": retryable.requested_start_ms,
                "schema_version": "SANITIZED_PROVIDER_INVENTORY_V1",
                "source": retryable.source,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        absence = json.dumps(
            {"inventory": {"absence_confirmed": True, "artifact_sha256": hashlib.sha256(inventory_bytes).hexdigest(), "artifact_type": "SANITIZED_PROVIDER_INVENTORY_V1", "reason_code": "NO_MARKET"}},
            sort_keys=True,
            separators=(",", ":"),
        )
        terminal = dataclasses.replace(
            retryable,
            status="EXPECTED_ABSENT",
            expected_row_count=1,
            missing_row_count=1,
            reason_code="NO_MARKET",
            evidence_metadata_json=absence,
            evidence_sha256=hashlib.sha256(absence.encode()).hexdigest(),
            updated_at_ms=80_000,
        )
        ledger.record_range_assessment(terminal, inventory_artifact_bytes=inventory_bytes)
        with self.assertRaisesRegex(ValueError, "SOURCE_RANGE_TERMINAL_REGRESSION"):
            ledger.record_range_assessment(dataclasses.replace(retryable, updated_at_ms=90_000))

    def test_offline_import_writer_holds_backend_mutex_for_entire_transaction(self) -> None:
        from src.data_completion import BACKEND_MUTEX_NAME, execute_offline_import

        run, source_range = self._records()
        trace: list[str] = []

        class Mutex:
            def close(self) -> None:
                trace.append("mutex-close")

        def acquire(name: str):
            self.assertEqual(name, "BTC_LIVE_BACKEND_WINDOWS_V1")
            self.assertEqual(name, BACKEND_MUTEX_NAME)
            trace.append("mutex-acquire")
            return Mutex()

        path = Path(self.temp.name) / "offline-import.sqlite3"
        artifact_bytes = b"verified-artifact"
        schema_bytes = b"verified-schema"
        import dataclasses
        run = dataclasses.replace(
            run,
            artifact_sha256=hashlib.sha256(artifact_bytes).hexdigest(),
            schema_mapping_sha256=hashlib.sha256(schema_bytes).hexdigest(),
        )
        result = execute_offline_import(
            path,
            run,
            (source_range,),
            (self._event(),),
            artifact_bytes=artifact_bytes,
            schema_mapping_bytes=schema_bytes,
            mutex_factory=acquire,
            trace=trace,
        )
        self.assertTrue(result.inserted)
        self.assertEqual(trace[0], "mutex-acquire")
        self.assertEqual(trace[-1], "mutex-close")
        self.assertIn("transaction-complete", trace)

    def test_offline_import_mutex_collision_writes_nothing(self) -> None:
        import dataclasses
        from src.data_completion import execute_offline_import
        from src.single_instance import AlreadyRunningError

        run, source_range = self._records()
        run = dataclasses.replace(
            run,
            artifact_sha256=hashlib.sha256(b"artifact").hexdigest(),
            schema_mapping_sha256=hashlib.sha256(b"schema").hexdigest(),
        )
        path = Path(self.temp.name) / "blocked-import.sqlite3"

        def blocked(_name: str):
            raise AlreadyRunningError("BACKEND_ALREADY_RUNNING")

        with self.assertRaisesRegex(AlreadyRunningError, "BACKEND_ALREADY_RUNNING"):
            execute_offline_import(
                path,
                run,
                (source_range,),
                (self._event(),),
                artifact_bytes=b"artifact",
                schema_mapping_bytes=b"schema",
                mutex_factory=blocked,
            )
        self.assertFalse(path.exists())

    def test_offline_import_verifies_artifact_and_schema_bytes_before_db_open(self) -> None:
        import dataclasses
        from src.data_completion import execute_offline_import

        run, source_range = self._records()
        path = Path(self.temp.name) / "hash-mismatch.sqlite3"
        run = dataclasses.replace(
            run,
            artifact_sha256=hashlib.sha256(b"expected-artifact").hexdigest(),
            schema_mapping_sha256=hashlib.sha256(b"expected-schema").hexdigest(),
        )
        with self.assertRaisesRegex(ValueError, "IMPORT_ARTIFACT_HASH_MISMATCH"):
            execute_offline_import(
                path,
                run,
                (source_range,),
                (self._event(),),
                artifact_bytes=b"changed-artifact",
                schema_mapping_bytes=b"expected-schema",
                mutex_factory=lambda _name: self.fail("mutex must not be acquired"),
            )
        self.assertFalse(path.exists())
        with self.assertRaisesRegex(ValueError, "IMPORT_SCHEMA_MAPPING_HASH_MISMATCH"):
            execute_offline_import(
                path,
                run,
                (source_range,),
                (self._event(),),
                artifact_bytes=b"expected-artifact",
                schema_mapping_bytes=b"changed-schema",
                mutex_factory=lambda _name: self.fail("mutex must not be acquired"),
            )
        self.assertFalse(path.exists())

    def test_retryable_range_can_record_partial_observations_then_complete(self) -> None:
        import dataclasses
        from src.data_completion import DataCompletionLedger

        _, source_range = self._records()
        partial = dataclasses.replace(
            source_range,
            import_run_id=None,
            requested_end_ms=240_000,
            status="SOURCE_UNAVAILABLE_RETRYABLE",
            expected_row_count=3,
            observed_row_count=1,
            missing_row_count=2,
            reason_code="PARTIAL_SOURCE_RESPONSE",
            updated_at_ms=70_000,
        )
        ledger = DataCompletionLedger(self.store)
        ledger.record_range_assessment(partial)
        self.assertEqual(
            self.store.scalar("SELECT status FROM data_source_range_assessments"),
            "SOURCE_UNAVAILABLE_RETRYABLE",
        )
        run, _ = self._records()
        events = (
            self._event(natural_key="partial:1", timestamp_ms=60_000),
            self._event(natural_key="partial:2", timestamp_ms=120_000),
            self._event(natural_key="partial:3", timestamp_ms=180_000),
        )
        run = dataclasses.replace(
            run,
            declared_row_count=3,
            inserted_row_count=3,
            dataset_end_ms=240_000,
        )
        complete = dataclasses.replace(
            partial,
            import_run_id=run.import_run_id,
            status="COMPLETE",
            observed_row_count=3,
            missing_row_count=0,
            reason_code=None,
            updated_at_ms=80_000,
        )
        ledger.commit_import(run, (complete,), events)
        self.assertEqual(
            self.store.scalar(
                "SELECT status FROM data_source_range_assessments ORDER BY assessment_row_id DESC LIMIT 1"
            ),
            "COMPLETE",
        )


if __name__ == "__main__":
    unittest.main()
