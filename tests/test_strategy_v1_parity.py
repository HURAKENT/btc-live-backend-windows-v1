from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class StrategyV1ParityTests(unittest.TestCase):
    def _module(self):
        return importlib.import_module("src.strategy_v1_parity")

    @staticmethod
    def _buckets(
        *,
        selected_index: int = 2,
        selected_model_micros: int = 300_000,
        selected_q_yes_micros: int = 270_000,
        favorite_index: int = 5,
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for index in range(11):
            rows.append(
                {
                    "actual_winner": index == 2,
                    "bucket_index": index,
                    "model_p_micros": (
                        selected_model_micros if index == selected_index else 100_000
                    ),
                    "q_no_micros": (
                        1_000_000 - selected_q_yes_micros
                        if index == selected_index
                        else 900_000
                    ),
                    "q_yes_micros": (
                        200_000
                        if index == favorite_index
                        else selected_q_yes_micros
                        if index == selected_index
                        else 100_000
                    ),
                }
            )
        return rows

    def _expectation(
        self,
        *,
        family: str = "PF1",
        accepted: bool = True,
        selected_index: int | None = 2,
        favorite_index: int | None = 2,
        reason: str | None = None,
        side: str = "YES",
        raw_q_micros: int = 270_000,
        won: bool = True,
        universe: str = "ALL",
        partition: str = "NA",
    ) -> dict[str, object]:
        stressed = min(1_000_000, raw_q_micros + 30_000)
        return {
            "expected_accept": accepted,
            "expected_favorite_bucket_index": favorite_index,
            "expected_pnl_micros": (
                5 * ((1_000_000 if won else 0) - stressed) if accepted else 0
            ),
            "expected_raw_q_micros": raw_q_micros,
            "expected_reason": reason,
            "expected_selected_bucket_index": selected_index,
            "expected_stressed_q_micros": stressed,
            "expected_turnover_micros": 5 * stressed if accepted else 0,
            "expected_won": won,
            "family": family,
            "partition": partition,
            "side": side,
            "universe": universe,
        }

    def _record(
        self,
        label: str,
        checkpoint: int,
        expectation: dict[str, object],
        *,
        buckets: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        return {
            "buckets": self._buckets() if buckets is None else buckets,
            "case_identity_sha256": _sha(f"case:{label}:{checkpoint}"),
            "checkpoint_minutes": checkpoint,
            "expectations": [expectation],
            "market_identity_sha256": _sha(f"market:{label}"),
            "price_history_observation_timestamp_ms": 1_999,
            "price_history_snapshot_sha256": _sha(f"history:{label}:{checkpoint}"),
            "price_history_target_timestamp_ms": 2_000,
            "record_type": "decision",
        }

    @staticmethod
    def _manifest(strategy_ids: list[str]) -> dict[str, object]:
        from src.strategy_v1_parity import historical_population_identities

        populations = [
            name
            for name, identities in historical_population_identities().items()
            if set(strategy_ids).issubset(identities)
        ]
        if len(populations) != 1:
            raise AssertionError("test identities must use one source population")
        return {
            "covered_strategy_ids": strategy_ids,
            "execution_eligibility_covered": False,
            "historical_trade_economics_only": True,
            "record_type": "manifest",
            "schema_version": "C4_V1_HISTORICAL_PARITY_FIXTURE_V1",
            "shares": 5,
            "source_population": populations[0],
            "stress_micros": 30_000,
            "trading_approval": False,
        }

    def _verify(self, strategy_ids: list[str], records: list[dict[str, object]]):
        return self._module().verify_v1_historical_parity_rows(
            [self._manifest(strategy_ids), *records]
        )

    def test_module_exists(self) -> None:
        self.assertIsNotNone(importlib.util.find_spec("src.strategy_v1_parity"))

    def test_fixed_identity_replays_accepted_and_rejected_population(self) -> None:
        accepted = self._record("accepted", 60, self._expectation())
        rejected_buckets = self._buckets(
            selected_model_micros=280_000,
            selected_q_yes_micros=270_000,
        )
        rejected = self._record(
            "rejected",
            60,
            self._expectation(
                accepted=False,
                reason="PF1_GATE",
                raw_q_micros=270_000,
            ),
            buckets=rejected_buckets,
        )

        report = self._verify(["YES_PF1_T60"], [accepted, rejected])

        self.assertEqual(report.strategy_count, 1)
        identity = report.identities[0]
        self.assertEqual(identity.fixture_decision_count, 2)
        self.assertEqual(identity.expected_trade_count, 1)
        self.assertEqual(identity.actual_trade_count, 1)
        self.assertEqual(
            identity.expected_trade_identity_sha256,
            identity.actual_trade_identity_sha256,
        )
        self.assertTrue(identity.parity_pass)
        self.assertTrue(report.parity_pass)
        self.assertFalse(report.execution_eligibility_covered)
        self.assertTrue(report.historical_trade_economics_only)

    def test_false_positive_and_false_negative_mutations_fail_closed(self) -> None:
        false_positive = self._record(
            "false-positive",
            60,
            self._expectation(accepted=True),
            buckets=self._buckets(
                selected_model_micros=280_000,
                selected_q_yes_micros=270_000,
            ),
        )
        false_negative = self._record(
            "false-negative",
            60,
            self._expectation(
                accepted=False,
                reason="PF1_GATE",
            ),
        )
        for record in (false_positive, false_negative):
            with self.subTest(record=record["case_identity_sha256"]):
                with self.assertRaisesRegex(
                    ValueError, "C4_V1_PARITY_DECISION_MISMATCH"
                ):
                    self._verify(["YES_PF1_T60"], [record])

    def test_selected_bucket_and_historical_economics_are_recomputed(self) -> None:
        selected = self._record(
            "selection",
            60,
            self._expectation(selected_index=3),
        )
        economics = self._record(
            "economics",
            60,
            {
                **self._expectation(),
                "expected_turnover_micros": 1,
            },
        )
        with self.assertRaisesRegex(ValueError, "C4_V1_PARITY_SELECTION_MISMATCH"):
            self._verify(["YES_PF1_T60"], [selected])
        with self.assertRaisesRegex(ValueError, "C4_V1_PARITY_TURNOVER_MISMATCH"):
            self._verify(["YES_PF1_T60"], [economics])

    def test_operational_policy_uses_t60_primary_then_t30_fallback(self) -> None:
        records = [
            self._record("primary", 60, self._expectation()),
            self._record("primary", 30, self._expectation()),
            self._record(
                "fallback",
                60,
                self._expectation(accepted=False, reason="PF1_GATE"),
                buckets=self._buckets(
                    selected_model_micros=280_000,
                    selected_q_yes_micros=270_000,
                ),
            ),
            self._record("fallback", 30, self._expectation()),
        ]

        identity = self._verify(["YES_PF1_OPERATIONAL"], records).identities[0]

        self.assertEqual(identity.expected_trade_count, 2)
        self.assertEqual(identity.actual_trade_count, 2)
        self.assertEqual(identity.selected_checkpoint_counts, ((30, 1), (60, 1)))

    def test_no_fade_replays_historical_gate_without_execution_claim(self) -> None:
        buckets = self._buckets(
            selected_model_micros=50_000,
            selected_q_yes_micros=100_000,
        )
        expectation = self._expectation(
            family="NO_FADE",
            favorite_index=5,
            side="NO",
            raw_q_micros=900_000,
            universe="U1",
            partition="P1",
            won=False,
        )

        report = self._verify(
            ["NO_FADE_P1_U1_T12"],
            [self._record("no-fade", 720, expectation, buckets=buckets)],
        )

        self.assertTrue(report.parity_pass)
        self.assertFalse(report.execution_eligibility_covered)
        self.assertEqual(report.identities[0].expected_trade_count, 1)

    def test_noncanonical_json_fixture_fails_closed(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "fixture.jsonl"
        path.write_text(
            json.dumps(self._manifest(["YES_PF1_T60"])) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "C4_V1_PARITY_FIXTURE_NONCANONICAL"):
            self._module().verify_v1_historical_parity(path)

    def test_manifest_rejects_identity_from_wrong_source_population(self) -> None:
        manifest = self._manifest(["YES_PF1_T60"])
        manifest["source_population"] = "EARLY_HORIZON"
        with self.assertRaisesRegex(
            ValueError, "C4_V1_PARITY_MANIFEST_POPULATION_IDENTITY_MISMATCH"
        ):
            self._module().verify_v1_historical_parity_rows([manifest])


if __name__ == "__main__":
    unittest.main()
