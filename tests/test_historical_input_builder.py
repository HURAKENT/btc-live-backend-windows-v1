from __future__ import annotations

import csv
import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq

from src.historical_input_builder import (
    HistoricalArtifactPaths,
    HistoricalInputBuilder,
    NewHistoricalParentResult,
)
from src.strategy_dispatch import (
    V1HistoricalCheckpointInput,
    V1StrategyDispatchRequest,
    V2StrategyDispatchRequest,
    load_strategy_dispatcher,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class HistoricalInputBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        source70 = root / "source70.zip"
        source100 = root / "source100.zip"
        for path in (source70, source100):
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("manifest.txt", path.stem)

        matrix = root / "matrix.parquet"
        matrix_rows = []
        for checkpoint in (30, 60, 360, 1080):
            for index in range(11):
                matrix_rows.append(
                    {
                        "market_date": "2026-01-01",
                        "horizon_minutes": checkpoint,
                        "bucket_index": index,
                        "bucket_title": f"{80 + index}-{81 + index}",
                        "q_yes": 0.20 if index == 2 else 0.08,
                        "q_no": 0.80 if index == 2 else 0.92,
                        "model_p": 0.30 if index == 2 else 0.07,
                        "no_source_history_sha256": hashlib.sha256(
                            f"no:{checkpoint}:{index}".encode()
                        ).hexdigest(),
                    }
                )
        pq.write_table(pa.Table.from_pylist(matrix_rows), matrix)

        atlas = root / "atlas.parquet"
        atlas_rows = []
        market_rows = []
        coverage_rows = []
        for checkpoint in (30, 60):
            for index in range(11):
                market_id = f"market-{index}"
                token = f"no-token-{index}"
                atlas_rows.append(
                    {
                        "market_date": "2026-01-01",
                        "horizon_minutes": checkpoint,
                        "bucket_index": index,
                        "market_id": market_id,
                        "bucket_title": f"{80 + index}-{81 + index}",
                        "lower_bound": float(80 + index),
                        "upper_bound": float(81 + index),
                        "market_q_raw": 0.20 if index == 2 else 0.08,
                        "model_p": 0.30 if index == 2 else 0.07,
                    }
                )
                coverage_rows.append(
                    {
                        "market_date": "2026-01-01",
                        "batch_id": "TEST",
                        "no_token_id": token,
                        "checkpoint": f"T-{checkpoint}",
                        "target_timestamp": "100",
                        "selected_timestamp": "99",
                        "price": "0.70" if index == 2 else "0.90",
                        "age_seconds": "1",
                        "exists": "True",
                        "no_lookahead": "True",
                    }
                )
                if checkpoint == 60:
                    market_rows.append(
                        {
                            "market_date": "2026-01-01",
                            "market_id": market_id,
                            "no_token_id": token,
                        }
                    )
        pq.write_table(pa.Table.from_pylist(atlas_rows), atlas)

        markets = root / "markets.parquet"
        pq.write_table(pa.Table.from_pylist(market_rows), markets)
        settlements = root / "settlements.parquet"
        pq.write_table(
            pa.Table.from_pylist(
                [{"market_date": "2026-01-01", "independent_winner_bucket": "82-83"}]
            ),
            settlements,
        )
        coverage = root / "checkpoint_coverage.csv"
        with coverage.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=tuple(coverage_rows[0]))
            writer.writeheader()
            writer.writerows(coverage_rows)
        confirmation = root / "confirmation_dates.csv"
        confirmation.write_text("market_date\n2026-01-01\n", encoding="utf-8")

        forecast = root / "forecast.parquet"
        pq.write_table(
            pa.Table.from_pylist(
                [
                    {
                        "market_date": "2026-01-01",
                        "evaluation_block": 1,
                        "checkpoint_minutes": 60,
                        "model_config": "EWMA_094",
                        "model_family": "EWMA",
                        "forecast_variance": 0.01,
                        "student_t_df": 5.0,
                        "selected_model": True,
                        "spot": 85.0,
                        "zone": "OPERATIONAL",
                        "regime_low": 0.0001,
                        "regime_high": 0.0002,
                    }
                ]
            ),
            forecast,
        )
        self.artifacts = HistoricalArtifactPaths(
            source_pack_70=source70,
            source_pack_100=source100,
            checkpoint_matrix_170=matrix,
            atlas_170=atlas,
            markets_170=markets,
            settlements_170=settlements,
            no_checkpoint_coverage=coverage,
            confirmation_dates=confirmation,
            vol_forecast_ledger=forecast,
        )
        self.hashes = {
            field: _sha(getattr(self.artifacts, field))
            for field in self.artifacts.__dataclass_fields__
        }

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _builder(self) -> HistoricalInputBuilder:
        patcher = mock.patch.dict(
            "src.historical_input_builder.EXPECTED_SOURCE_SHA256",
            self.hashes,
            clear=True,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return HistoricalInputBuilder(
            project_root=PROJECT_ROOT,
            artifacts=self.artifacts,
        )

    def test_builder_has_no_expected_decision_path(self) -> None:
        fields = " ".join(HistoricalArtifactPaths.__dataclass_fields__).lower()
        self.assertNotIn("parity", fields)
        self.assertNotIn("decision", fields)

    def test_exact_47_identities_are_discovered_from_dispatcher(self) -> None:
        builder = self._builder()
        dispatcher = load_strategy_dispatcher(PROJECT_ROOT)

        self.assertEqual(
            builder.strategy_ids(),
            tuple(binding.strategy_id for binding in dispatcher.bindings),
        )

    def test_missing_required_artifact_fails_closed(self) -> None:
        missing = HistoricalArtifactPaths(
            **{
                **{
                    field: getattr(self.artifacts, field)
                    for field in self.artifacts.__dataclass_fields__
                },
                "source_pack_70": Path(self._temporary.name) / "missing.zip",
            }
        )
        builder = HistoricalInputBuilder(
            project_root=PROJECT_ROOT,
            artifacts=missing,
        )

        with self.assertRaisesRegex(ValueError, "AHR_SOURCE_MISSING"):
            builder.verify_sources()

    def test_changed_source_hash_fails_before_reading(self) -> None:
        builder = self._builder()
        self.artifacts.source_pack_70.write_bytes(b"changed")

        with self.assertRaisesRegex(ValueError, "AHR_SOURCE_HASH_MISMATCH"):
            builder.verify_sources()

    def test_historical_v1_strict_and_pf1_use_binding_checkpoints(self) -> None:
        builder = self._builder()
        for strategy_id, checkpoints in (
            ("NO_FADE_P1_U1_T18", (1080,)),
            ("YES_STRICT_A_T60", (60,)),
            ("YES_PF1_T60", (60,)),
        ):
            with self.subTest(strategy_id=strategy_id):
                unit = builder.build_v1_unit(
                    strategy_id=strategy_id,
                    market_date="2026-01-01",
                )
                self.assertIs(type(unit.request), V1StrategyDispatchRequest)
                self.assertEqual(
                    tuple(row.checkpoint_minutes for row in unit.request.checkpoints),
                    checkpoints,
                )
                self.assertTrue(
                    all(
                        type(row) is V1HistoricalCheckpointInput
                        for row in unit.request.checkpoints
                    )
                )

    def test_early_confidence_identity_requires_canonical_cohort_membership(self) -> None:
        builder = self._builder()

        self.assertTrue(
            builder.is_contract_opportunity(
                strategy_id="YES_PF1_T60", market_date="2026-01-01"
            )
        )
        self.assertFalse(
            builder.is_contract_opportunity(
                strategy_id="YES_PF1_T60", market_date="2026-01-02"
            )
        )
        self.assertFalse(
            builder.is_contract_opportunity(
                strategy_id="YES_STRICT_A_T60", market_date="2026-01-02"
            )
        )

    def test_confirmation_uses_canonical_actual_no_coverage(self) -> None:
        builder = self._builder()
        unit = builder.build_v1_unit(
            strategy_id="NO_A2",
            market_date="2026-01-01",
        )

        selected = unit.request.checkpoints[0].buckets[2]
        self.assertEqual(selected.market_q_yes, 0.20)
        self.assertEqual(selected.market_q_no, 0.70)

    def test_v2_requires_new_parent_result_and_preserves_its_provenance(self) -> None:
        builder = self._builder()
        with self.assertRaisesRegex(ValueError, "AHR_NEW_PARENT_RESULT_REQUIRED"):
            builder.build_v2_unit(
                strategy_id="NO_A2_V2_VOL",
                market_date="2026-01-01",
                parent_result=object(),
            )

        source_sha = hashlib.sha256(b"new-parent").hexdigest()
        parent = NewHistoricalParentResult(
            market_date="2026-01-01",
            strategy_id="NO_A2",
            decision_identity="new:NO_A2:2026-01-01:T-60m",
            side="NO",
            checkpoint_minutes=60,
            selected_bucket_indices=(2,),
            selected_buckets=(hashlib.sha256(b"82-83").hexdigest(),),
            horizon="T-60m",
            accepted=True,
            actual_price_micros=700_000,
            source_decision_sha256=source_sha,
        )
        self.assertTrue(
            builder.is_v2_contract_opportunity(
                strategy_id="NO_A2_V2_VOL",
                market_date="2026-01-01",
                parent_result=parent,
            )
        )
        unit = builder.build_v2_unit(
            strategy_id="NO_A2_V2_VOL",
            market_date="2026-01-01",
            parent_result=parent,
        )

        self.assertIs(type(unit.request), V2StrategyDispatchRequest)
        self.assertEqual(unit.request.parent.source_decision_sha256, source_sha)
        self.assertEqual(
            unit.request.volatility_input.source_decision_sha256,
            source_sha,
        )
        self.assertEqual(unit.request.parent.selected_buckets, parent.selected_buckets)


if __name__ == "__main__":
    unittest.main()
