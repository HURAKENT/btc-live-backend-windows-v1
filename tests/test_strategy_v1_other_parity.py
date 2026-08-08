from __future__ import annotations

import copy
import importlib
import importlib.util
import json
import unittest
from pathlib import Path


_FIXTURE = Path(
    "strategy_sources/frozen/parity/V1_CONFIRMATION_BASKET_FULL_DECISION_PARITY.jsonl"
)


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
