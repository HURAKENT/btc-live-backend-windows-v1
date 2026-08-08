from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FROZEN_V2_FIXTURE = (
    PROJECT_ROOT
    / "strategy_sources"
    / "frozen"
    / "parity"
    / "VOL_OVERLAY_DECISION_PARITY.jsonl"
)
FROZEN_V2_RECEIPT = FROZEN_V2_FIXTURE.with_name(
    "VOL_OVERLAY_DECISION_PARITY_RECEIPT.json"
)
V2_FIXTURE_BUILDER = PROJECT_ROOT / "tools" / "build_c4_v2_parity_fixture.py"


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class StrategyParityContractTests(unittest.TestCase):
    def _module(self):
        return importlib.import_module("src.strategy_parity")

    def _row(self, **overrides: object) -> dict[str, object]:
        row: dict[str, object] = {
            "actual_price_micros": 400_000,
            "baseline_accept": True,
            "decision_identity": "decision:NO_A2:2026-01-01:T60",
            "expected_accept": True,
            "expected_pnl_micros": 2_850_000,
            "expected_stressed_q_micros": 430_000,
            "expected_turnover_micros": 2_150_000,
            "forecast_row_sha256": _sha("forecast"),
            "horizon": "T-60m",
            "overlay_strategy_id": "NO_A2_V2_VOL",
            "p_vol_side_micros": 450_000,
            "parent_strategy_id": "NO_A2",
            "selected_buckets": ["bucket-2"],
            "side": "NO",
            "source_decision_sha256": _sha("decision"),
            "won": True,
        }
        row.update(overrides)
        return row

    def _write_fixture(self, rows: list[dict[str, object]]) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "fixture.jsonl"
        content = "".join(
            json.dumps(
                row,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
            for row in rows
        )
        path.write_text(content, encoding="utf-8", newline="\n")
        return path

    def test_strategy_parity_module_exists(self) -> None:
        self.assertIsNotNone(importlib.util.find_spec("src.strategy_parity"))

    def test_v2_fixture_replays_exact_decision_and_aggregate(self) -> None:
        result = self._module().verify_v2_overlay_parity(
            self._write_fixture([self._row()])
        )

        self.assertEqual(result.fixture_row_count, 1)
        self.assertEqual(result.strategy_count, 1)
        identity = result.identities[0]
        self.assertEqual(identity.strategy_id, "NO_A2_V2_VOL")
        self.assertEqual(identity.expected_trade_count, 1)
        self.assertEqual(identity.actual_trade_count, 1)
        self.assertEqual(identity.expected_wins, 1)
        self.assertEqual(identity.actual_wins, 1)
        self.assertEqual(identity.expected_pnl_micros, 2_850_000)
        self.assertEqual(identity.actual_pnl_micros, 2_850_000)
        self.assertEqual(identity.expected_turnover_micros, 2_150_000)
        self.assertEqual(identity.actual_turnover_micros, 2_150_000)
        self.assertEqual(identity.expected_trade_identity_sha256, identity.actual_trade_identity_sha256)
        self.assertTrue(identity.parity_pass)
        self.assertTrue(result.parity_pass)

    def test_rejected_row_is_verified_but_not_counted_as_trade(self) -> None:
        row = self._row(
            expected_accept=False,
            expected_pnl_micros=0,
            expected_turnover_micros=0,
            p_vol_side_micros=449_999,
        )
        identity = self._module().verify_v2_overlay_parity(
            self._write_fixture([row])
        ).identities[0]

        self.assertEqual(identity.expected_trade_count, 0)
        self.assertEqual(identity.actual_trade_count, 0)
        self.assertTrue(identity.parity_pass)

    def test_any_row_decision_mismatch_fails_closed(self) -> None:
        row = self._row(
            expected_accept=False,
            expected_pnl_micros=0,
            expected_turnover_micros=0,
        )
        with self.assertRaisesRegex(ValueError, "C4_V2_PARITY_DECISION_MISMATCH"):
            self._module().verify_v2_overlay_parity(self._write_fixture([row]))

    def test_stressed_cost_mismatch_fails_closed(self) -> None:
        row = self._row(expected_stressed_q_micros=430_001)
        with self.assertRaisesRegex(ValueError, "C4_V2_PARITY_STRESSED_COST_MISMATCH"):
            self._module().verify_v2_overlay_parity(self._write_fixture([row]))

    def test_fixture_turnover_is_checked_against_five_share_economics(self) -> None:
        row = self._row(expected_turnover_micros=2_150_001)
        with self.assertRaisesRegex(ValueError, "C4_V2_PARITY_TURNOVER_MISMATCH"):
            self._module().verify_v2_overlay_parity(self._write_fixture([row]))

    def test_fixture_pnl_and_outcome_are_independently_recomputed(self) -> None:
        for row in (
            self._row(expected_pnl_micros=2_849_999),
            self._row(won=False),
        ):
            with self.subTest(row=row):
                with self.assertRaisesRegex(ValueError, "C4_V2_PARITY_PNL_MISMATCH"):
                    self._module().verify_v2_overlay_parity(
                        self._write_fixture([row])
                    )

    def test_duplicate_decision_identity_fails_closed(self) -> None:
        row = self._row()
        with self.assertRaisesRegex(ValueError, "C4_V2_PARITY_DUPLICATE_IDENTITY"):
            self._module().verify_v2_overlay_parity(
                self._write_fixture([row, dict(row)])
            )

    def test_unknown_or_wrong_parent_binding_fails_closed(self) -> None:
        for row in (
            self._row(overlay_strategy_id="UNKNOWN_V2"),
            self._row(parent_strategy_id="NO_A0"),
        ):
            with self.subTest(row=row):
                with self.assertRaises(ValueError):
                    self._module().verify_v2_overlay_parity(
                        self._write_fixture([row])
                    )

    def test_fixture_schema_and_exact_types_are_fail_closed(self) -> None:
        mutations = (
            {"unexpected": 1},
            {"actual_price_micros": True},
            {"selected_buckets": "bucket-2"},
            {"won": 1},
        )
        for mutation in mutations:
            row = self._row(**mutation)
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(ValueError, "C4_V2_PARITY_FIXTURE"):
                    self._module().verify_v2_overlay_parity(
                        self._write_fixture([row])
                    )

    def test_empty_or_noncanonical_fixture_is_rejected(self) -> None:
        empty = self._write_fixture([])
        with self.assertRaisesRegex(ValueError, "C4_V2_PARITY_FIXTURE_EMPTY"):
            self._module().verify_v2_overlay_parity(empty)

        row = self._row()
        path = self._write_fixture([row])
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "C4_V2_PARITY_FIXTURE_NONCANONICAL"):
            self._module().verify_v2_overlay_parity(path)

    def test_models_are_frozen_and_results_are_deterministic(self) -> None:
        module = self._module()
        path = self._write_fixture([self._row()])
        first = module.verify_v2_overlay_parity(path)
        second = module.verify_v2_overlay_parity(path)
        self.assertEqual(first, second)
        with self.assertRaises(Exception):
            first.parity_pass = False

    def test_frozen_v2_evidence_replays_all_thirteen_identities(self) -> None:
        receipt = json.loads(FROZEN_V2_RECEIPT.read_text(encoding="utf-8"))
        self.assertEqual(receipt["fixture_row_count"], 694)
        self.assertEqual(receipt["strategy_count"], 13)
        self.assertEqual(receipt["quantization_ambiguities"], 0)
        self.assertEqual(receipt["network_requests"], 0)
        self.assertFalse(receipt["trading_approval"])
        self.assertEqual(
            hashlib.sha256(FROZEN_V2_FIXTURE.read_bytes()).hexdigest(),
            receipt["fixture_sha256"],
        )
        self.assertEqual(
            hashlib.sha256(V2_FIXTURE_BUILDER.read_bytes()).hexdigest(),
            receipt["converter_sha256"],
        )

        result = self._module().verify_v2_overlay_parity(FROZEN_V2_FIXTURE)

        self.assertEqual(result.fixture_row_count, 694)
        self.assertEqual(result.strategy_count, 13)
        self.assertTrue(result.parity_pass)
        self.assertTrue(all(identity.parity_pass for identity in result.identities))
        self.assertTrue(
            all(
                identity.expected_trade_identity_sha256
                == identity.actual_trade_identity_sha256
                for identity in result.identities
            )
        )


if __name__ == "__main__":
    unittest.main()
