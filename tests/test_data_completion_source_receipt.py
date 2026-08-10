from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DataCompletionSourceReceiptTests(unittest.TestCase):
    def test_receipt_module_exists(self) -> None:
        self.assertIsNotNone(importlib.util.find_spec("src.data_completion_source_receipt"))

    def test_receipt_is_canonical_sanitized_and_self_verifying(self) -> None:
        from src.data_completion_source_receipt import (
            build_source_pack_receipt,
            validate_source_pack_receipt,
        )

        receipt = build_source_pack_receipt(
            project_root=PROJECT_ROOT,
            source_commit="a" * 40,
            generated_at_utc="2026-08-10T20:00:00Z",
            elapsed_seconds=12.5,
            packs=self._packs(),
            database=self._database(),
            historical_artifact_observations=self._historical_artifacts(),
        )
        validate_source_pack_receipt(receipt, expected_source_commit="a" * 40)
        raw = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        self.assertNotIn("C:\\Users", raw)
        self.assertNotIn("/mnt/c/Users", raw)
        self.assertNotIn("token_id", raw)
        self.assertEqual(receipt["source_pack_row_count"], 7_277_225)
        self.assertEqual(receipt["universe_date_count"], 170)
        self.assertEqual(receipt["network"]["provider_requests"], 0)
        self.assertFalse(receipt["trading_approval"])

    def test_receipt_mutation_fails_closed(self) -> None:
        from src.data_completion_source_receipt import (
            build_source_pack_receipt,
            validate_source_pack_receipt,
        )

        receipt = build_source_pack_receipt(
            project_root=PROJECT_ROOT,
            source_commit="a" * 40,
            generated_at_utc="2026-08-10T20:00:00Z",
            elapsed_seconds=12.5,
            packs=self._packs(),
            database=self._database(),
            historical_artifact_observations=self._historical_artifacts(),
        )
        mutated = json.loads(json.dumps(receipt))
        mutated["packs"][0]["price_row_count"] -= 1
        with self.assertRaisesRegex(ValueError, "SOURCE_PACK_RECEIPT_HASH_MISMATCH"):
            validate_source_pack_receipt(mutated, expected_source_commit="a" * 40)

    def test_self_consistent_database_or_historical_mutation_fails_closed(self) -> None:
        from src.data_completion_source_receipt import (
            build_source_pack_receipt,
            validate_source_pack_receipt,
        )

        receipt = build_source_pack_receipt(
            project_root=PROJECT_ROOT,
            source_commit="a" * 40,
            generated_at_utc="2026-08-10T20:00:00Z",
            elapsed_seconds=12.5,
            packs=self._packs(),
            database=self._database(),
            historical_artifact_observations=self._historical_artifacts(),
        )
        for mutation in ("database", "historical"):
            with self.subTest(mutation=mutation):
                changed = json.loads(json.dumps(receipt))
                if mutation == "database":
                    changed["database"]["source_event_count"] -= 1
                else:
                    changed["historical_acceptance"]["artifacts"][0]["sha256"] = "0" * 64
                unsigned = {key: value for key, value in changed.items() if key != "receipt_sha256"}
                changed["receipt_sha256"] = hashlib.sha256(
                    json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                with self.assertRaisesRegex(ValueError, "SOURCE_PACK_RECEIPT_CONTRACT_MISMATCH"):
                    validate_source_pack_receipt(changed, expected_source_commit="a" * 40)

        changed = json.loads(json.dumps(receipt))
        changed["absolute_user_path"] = "C:\\Users\\example\\secret"
        unsigned = {key: value for key, value in changed.items() if key != "receipt_sha256"}
        changed["receipt_sha256"] = hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.assertRaisesRegex(ValueError, "SOURCE_PACK_RECEIPT_CONTRACT_MISMATCH"):
            validate_source_pack_receipt(changed, expected_source_commit="a" * 40)

    def test_write_receipt_is_utf8_lf_and_deterministic(self) -> None:
        from src.data_completion_source_receipt import build_source_pack_receipt, write_source_pack_receipt

        receipt = build_source_pack_receipt(
            project_root=PROJECT_ROOT,
            source_commit="a" * 40,
            generated_at_utc="2026-08-10T20:00:00Z",
            elapsed_seconds=12.5,
            packs=self._packs(),
            database=self._database(),
            historical_artifact_observations=self._historical_artifacts(),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            write_source_pack_receipt(receipt, path)
            first = path.read_bytes()
            write_source_pack_receipt(receipt, path)
            second = path.read_bytes()
        self.assertEqual(first, second)
        self.assertTrue(first.endswith(b"\n"))
        self.assertEqual(json.loads(first), receipt)

    def test_receipt_write_is_immutable_no_overwrite(self) -> None:
        from src.data_completion_source_receipt import build_source_pack_receipt, write_source_pack_receipt

        receipt = build_source_pack_receipt(
            project_root=PROJECT_ROOT,
            source_commit="a" * 40,
            generated_at_utc="2026-08-10T20:00:00Z",
            elapsed_seconds=12.5,
            packs=self._packs(),
            database=self._database(),
            historical_artifact_observations=self._historical_artifacts(),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text("different", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SOURCE_PACK_RECEIPT_CONFLICT"):
                write_source_pack_receipt(receipt, path)
            self.assertEqual(path.read_text(encoding="utf-8"), "different")

    @staticmethod
    def _packs() -> tuple[dict[str, object], ...]:
        return (
            {
                "pack_id": "HISTORICAL_70_V1_10",
                "file_name": "btc_daily_range_merged_70_v1_10.zip",
                "outer_sha256": "911f4108cd8f6fe8d14c3bcbc435062caa0fa850c9f51a9712687507ac8691ce",
                "member_set_sha256": "065b7294bd0b1ca9bf1726891598ebce9160d281043c803ae5430d8d02985c32",
                "universe_dates_sha256": "c5d57431c006a6be71c6c7ec2f2e3c780339a29cfcc179febf59ebbcd6ec08ca",
                "market_identity_sequence_sha256": "432e35c6d4b5f73d8b8f8dd66779052854551746d0d13409f11144c3335dfb71",
                "universe_date_count": 70,
                "market_row_count": 770,
                "price_row_count": 2_853_424,
                "binance_row_count": 113_221,
                "settlement_row_count": 70,
                "inserted_row_count": 2_967_485,
                "replayed_row_count": 0,
                "conflict_row_count": 0,
                "dropped_row_count": 0,
                "status": "COMPLETE",
            },
            {
                "pack_id": "VALIDATION_100_V2_2",
                "file_name": "btc_daily_range_validation_100_merged_v2_2(1).zip",
                "outer_sha256": "cfcf8b47acb87c3efec9a0d0f86f86dd83c7eeed919975c80dac9094ed7d1dc2",
                "member_set_sha256": "c41cbe90df0afe1c749af23f1f3ed9e4e45b9a08b1f51db3a265deb8bee4367a",
                "universe_dates_sha256": "a0978f29c94555927c03ad4116bbf53c6d78a772501937a377df672a4fd5a782",
                "market_identity_sequence_sha256": "12ebad4dd4122b5b0b636f657f912503cca3a4a3fd150c4e329bc52c94f5d01a",
                "universe_date_count": 100,
                "market_row_count": 1100,
                "price_row_count": 4_144_919,
                "binance_row_count": 163_621,
                "settlement_row_count": 100,
                "inserted_row_count": 4_309_740,
                "replayed_row_count": 0,
                "conflict_row_count": 0,
                "dropped_row_count": 0,
                "status": "COMPLETE",
            },
        )

    @staticmethod
    def _database() -> dict[str, object]:
        return {
            "database_sha256": "d" * 64,
            "file_size_bytes": 123456,
            "integrity_check": "ok",
            "migration_version": 4,
            "import_run_count": 2,
            "source_event_count": 7_277_225,
            "source_range_count": 8,
        }

    @staticmethod
    def _historical_artifacts() -> tuple[dict[str, object], ...]:
        return (
            {"source_path": "artifacts/C1_ACCEPTANCE_PACK.zip", "external_path_class": "BTC_DAILY_RANGE_WINDOWS_V1_DATA_COMPLETION/historical_acceptance/C1_ACCEPTANCE_PACK.zip", "sha256": "9f6eaa9015f0698313c950571ae5f76be3f9c9d4327a461c4d504e8594b81a4f", "size_bytes": 26026},
            {"source_path": "artifacts/C2_C3_ACCEPTANCE_PACK.zip", "external_path_class": "BTC_DAILY_RANGE_WINDOWS_V1_DATA_COMPLETION/historical_acceptance/C2_C3_ACCEPTANCE_PACK.zip", "sha256": "07c0119585ead440036734926a3ad7bd86ff63002cc2d4a0e96df1a6c47062c9", "size_bytes": 13471},
        )


if __name__ == "__main__":
    unittest.main()
