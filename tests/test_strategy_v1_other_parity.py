from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json
import unittest
from pathlib import Path


_FIXTURE = Path(
    "strategy_sources/frozen/parity/V1_CONFIRMATION_BASKET_FULL_DECISION_PARITY.jsonl"
)
_RECEIPT = Path(
    "strategy_sources/frozen/parity/V1_CONFIRMATION_BASKET_FULL_DECISION_PARITY_RECEIPT.json"
)
_BUILDER = Path("tools/build_c4_v1_other_parity_fixture.py")


class StrategyV1OtherParityTests(unittest.TestCase):
    def _module(self):
        return importlib.import_module("src.strategy_v1_other_parity")

    def _records(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in _FIXTURE.read_text("utf-8").splitlines()]

    def test_module_and_fixture_exist(self) -> None:
        self.assertIsNotNone(importlib.util.find_spec("src.strategy_v1_other_parity"))
        self.assertTrue(_FIXTURE.is_file())

    def test_real_fixture_proves_six_identities_full_decision_parity(self) -> None:
        report = self._module().verify_v1_other_parity(_FIXTURE)
        self.assertTrue(report.parity_pass)
        self.assertEqual(report.strategy_count, 6)
        self.assertEqual(report.decision_count, 884)
        self.assertEqual(
            {item.strategy_id for item in report.identities},
            {
                "NO_A0",
                "NO_A2",
                "NO_B2",
                "NO_C1",
                "YES_FAVORITE_NEIGHBOR_BASKET",
                "YES_FAVORITE_ONLY",
            },
        )

    def test_frozen_fixture_receipt_binds_converter_sources_and_counts(self) -> None:
        receipt = json.loads(_RECEIPT.read_text("utf-8"))
        self.assertEqual(
            receipt["fixture_sha256"],
            hashlib.sha256(_FIXTURE.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            receipt["converter_sha256"],
            hashlib.sha256(_BUILDER.read_bytes()).hexdigest(),
        )
        self.assertEqual(receipt["source_commit"], "fd3afac2c9254cf32f41062a92de49a99f2e823b")
        self.assertEqual(receipt["fixture_record_count"], 171)
        self.assertEqual(receipt["decision_count"], 884)
        self.assertEqual(receipt["strategy_count"], 6)
        self.assertEqual(receipt["network_requests"], 0)
        self.assertFalse(receipt["trading_approval"])
        self.assertEqual(
            receipt["source_hashes"],
            {
                "BASKET_TRADE_CLASSIFICATION.csv": "6d6d71d03002a5c3e62566b5ddd4cc20d18204affff6debaa6c461c3618d5361",
                "CONFIRMATION_DATES_136.csv": "2f87e5bcb0078a10e3a58453c3c4f1c6eb5e7d0057bfb359f5fb873eb15aeea8",
                "CONFIRMATION_TRADE_LEDGER.csv": "74e0c479e81558f38c5f152a0ec62c923fe6eda080dda75167f8015a8da51440",
                "actual_bucket_probabilities_170.parquet": "66e81c1864c353be6b52318891c23439d4090baaedec22fbca5adb547a6b25fa",
                "checkpoint_coverage.csv": "4d4a1f9c634990b2941ac3dc90e88660331030da8255dfdbf1b2bb26d48d4967",
                "markets.parquet": "8c197dc99a1acf48a0c85166ffeba8af0dff9536d19d3beab1d0991faa1fa6ec",
                "settlements.parquet": "ea9ed11c1aa7a975c499bcd27332fdbfa0dd927cf2ef4323e89b372516c4e8e1",
            },
        )

    def test_false_positive_and_false_negative_expectations_fail_closed(self) -> None:
        records = self._records()
        cases = []
        for original_accept in (True, False):
            target = next(
                row
                for row in records[1:]
                if any(item["expected_accept"] is original_accept for item in row["expectations"])
            )
            expectation_index = next(
                index
                for index, item in enumerate(target["expectations"])
                if item["expected_accept"] is original_accept
            )
            cases.append((target, expectation_index, not original_accept))
        for target, expectation_index, expected_accept in cases:
            mutated = copy.deepcopy(records)
            mutated_target = next(
                row for row in mutated if row.get("market_identity_sha256") == target["market_identity_sha256"]
            )
            expectation = mutated_target["expectations"][expectation_index]
            expectation["expected_accept"] = expected_accept
            if expected_accept is False:
                expectation["expected_selected_bucket_indices"] = []
                expectation["expected_checkpoint_minutes"] = None
                expectation["expected_turnover_micros"] = 0
                expectation["expected_pnl_micros"] = 0
            else:
                expectation["expected_selected_bucket_indices"] = [0]
                expectation["expected_checkpoint_minutes"] = 60
            with self.subTest(expected_accept=expected_accept):
                with self.assertRaisesRegex(ValueError, "C4_V1_OTHER_PARITY_"):
                    self._module().verify_v1_other_parity_rows(mutated)

    def test_trade_economics_are_independently_recomputed(self) -> None:
        records = self._records()
        target = next(
            row
            for row in records[1:]
            if any(item["expected_accept"] for item in row["expectations"])
        )
        expectation = next(item for item in target["expectations"] if item["expected_accept"])
        expectation["expected_pnl_micros"] += 1
        with self.assertRaisesRegex(ValueError, "C4_V1_OTHER_PARITY_PNL_MISMATCH"):
            self._module().verify_v1_other_parity_rows(records)

    def test_fixture_contains_no_raw_identifiers_or_dates(self) -> None:
        text = _FIXTURE.read_text("utf-8")
        self.assertNotRegex(text, r"20\d\d-\d\d-\d\d")
        self.assertNotIn('"yes_token_id":', text)
        self.assertNotIn('"no_token_id":', text)
        self.assertNotIn('"market_id":', text)


if __name__ == "__main__":
    unittest.main()
