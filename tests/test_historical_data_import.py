from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from src.storage import SqliteStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _authoritative_fixture_sha256() -> dict[str, str]:
    return json.loads(
        (PROJECT_ROOT / "reports/C4_STRATEGY_47_ACCEPTANCE.json").read_text(encoding="utf-8")
    )["parity_fixture_sha256"]


class HistoricalDataImportTests(unittest.TestCase):
    def test_exact_four_frozen_parity_artifacts_build_verified_import_plans(self) -> None:
        from src.historical_data_import import build_historical_import_plans

        plans = build_historical_import_plans(
            PROJECT_ROOT, authoritative_fixture_sha256=_authoritative_fixture_sha256()
        )
        self.assertEqual(len(plans), 4)
        self.assertEqual(sum(len(plan.events) for plan in plans), 2341)
        self.assertEqual(tuple(len(plan.events) for plan in plans), (171, 621, 855, 694))
        self.assertTrue(all(plan.receipt["network_requests"] == 0 for plan in plans))
        self.assertTrue(all(plan.receipt["trading_approval"] is False for plan in plans))
        self.assertTrue(all(plan.run.artifact_sha256 == plan.receipt["fixture_sha256"] for plan in plans))

    def test_receipt_fixture_hash_mutation_fails_before_event_build(self) -> None:
        from src.historical_data_import import build_historical_import_plan

        fixture = PROJECT_ROOT / "strategy_sources/frozen/parity/V1_EARLY_HORIZON_FULL_DECISION_PARITY.jsonl"
        receipt = PROJECT_ROOT / "strategy_sources/frozen/parity/V1_EARLY_HORIZON_FULL_DECISION_PARITY_RECEIPT.json"
        with tempfile.TemporaryDirectory() as directory:
            changed = Path(directory) / fixture.name
            changed.write_bytes(fixture.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "HISTORICAL_FIXTURE_HASH_MISMATCH"):
                build_historical_import_plan(changed, receipt, ordinal=1)

    def test_fixture_and_receipt_mutation_cannot_override_authoritative_c4_hash(self) -> None:
        from src.historical_data_import import build_historical_import_plans

        authoritative = _authoritative_fixture_sha256()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parity = root / "strategy_sources/frozen/parity"
            parity.mkdir(parents=True)
            for source in (PROJECT_ROOT / "strategy_sources/frozen/parity").iterdir():
                if source.is_file():
                    shutil.copyfile(source, parity / source.name)
            fixture = parity / "V1_EARLY_HORIZON_FULL_DECISION_PARITY.jsonl"
            receipt = parity / "V1_EARLY_HORIZON_FULL_DECISION_PARITY_RECEIPT.json"
            lines = fixture.read_text(encoding="utf-8").splitlines()
            row = json.loads(lines[0])
            row["shares"] = 6
            lines[0] = json.dumps(row, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
            fixture.write_text("\n".join(lines) + "\n", encoding="utf-8")
            receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
            receipt_payload["fixture_sha256"] = hashlib.sha256(fixture.read_bytes()).hexdigest()
            receipt.write_text(json.dumps(receipt_payload, sort_keys=True), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "HISTORICAL_FIXTURE_AUTHORITATIVE_HASH_MISMATCH"):
                build_historical_import_plans(root, authoritative_fixture_sha256=authoritative)

    def test_import_is_atomic_idempotent_and_links_every_fixture_record(self) -> None:
        from src.data_completion import DataCompletionLedger
        from src.historical_data_import import build_historical_import_plans

        with tempfile.TemporaryDirectory() as directory:
            store = SqliteStore.open(Path(directory) / "historical.sqlite3")
            try:
                store.migrate()
                ledger = DataCompletionLedger(store)
                plans = build_historical_import_plans(
                    PROJECT_ROOT, authoritative_fixture_sha256=_authoritative_fixture_sha256()
                )
                for plan in plans:
                    first = ledger.commit_import(plan.run, (plan.source_range,), plan.events)
                    replay = ledger.commit_import(plan.run, (plan.source_range,), plan.events)
                    self.assertTrue(first.inserted)
                    self.assertFalse(replay.inserted)
                self.assertEqual(store.count("data_import_runs"), 4)
                self.assertEqual(store.count("source_events"), 2341)
                self.assertEqual(store.count("data_import_run_events"), 2341)
                self.assertEqual(store.integrity_report()["status"], "PASS")
            finally:
                store.close()

    def test_four_fixture_pack_rolls_back_all_artifacts_on_late_failure(self) -> None:
        from src.data_completion import DataCompletionLedger
        from src.historical_data_import import build_historical_import_plans

        authoritative = _authoritative_fixture_sha256()
        plans = list(
            build_historical_import_plans(
                PROJECT_ROOT, authoritative_fixture_sha256=authoritative
            )
        )
        bad_event = dataclasses.replace(plans[2].events[0], source="wrong-source")
        plans[2] = dataclasses.replace(
            plans[2], events=(bad_event, *plans[2].events[1:])
        )
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteStore.open(Path(directory) / "historical.sqlite3")
            try:
                store.migrate()
                ledger = DataCompletionLedger(store)
                with self.assertRaisesRegex(ValueError, "IMPORT_EVENT_OUTSIDE_DECLARED_RANGE"):
                    ledger.commit_import_pack(
                        tuple((plan.run, (plan.source_range,), plan.events) for plan in plans)
                    )
                self.assertEqual(store.count("data_import_runs"), 0)
                self.assertEqual(store.count("source_events"), 0)
                self.assertEqual(store.count("data_source_ranges"), 0)
            finally:
                store.close()

    def test_fixture_events_are_historical_nonexecution_and_sanitized(self) -> None:
        from src.historical_data_import import build_historical_import_plans

        for plan in build_historical_import_plans(
            PROJECT_ROOT, authoritative_fixture_sha256=_authoritative_fixture_sha256()
        ):
            for event in plan.events:
                self.assertEqual(event.recovery_origin, "HISTORICAL_IMPORT")
                self.assertEqual(event.event_type, "HISTORICAL_STRATEGY_PARITY_RECORD")
                self.assertNotIn("C:\\Users", event.payload_json)
                self.assertNotIn("/mnt/c/Users", event.payload_json)
                self.assertNotIn('"trading_approval":true', event.payload_json)


if __name__ == "__main__":
    unittest.main()
