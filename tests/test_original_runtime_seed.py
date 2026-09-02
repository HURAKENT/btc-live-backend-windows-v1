from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import src.original_runtime_seed as seed_module
from src.historical_input_builder import EXPECTED_SOURCE_SHA256
from src.original_runtime_seed import OriginalRuntimeSeedLoader
from src.performance_engine import StrategyPerformanceEngine
from src.performance_models import (
    PerformanceObservation,
    PerformanceResolution,
)
from src.performance_repository import PerformanceRepository
from src.storage import SqliteStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEED_DIR = PROJECT_ROOT / "strategy_sources" / "frozen" / "original_runtime_seed_v1"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _observation() -> PerformanceObservation:
    return PerformanceObservation.create(
        observation_key="performance-observation-historical:test",
        logical_decision_key="performance-logical-decision:test",
        source_layer="HISTORICAL",
        strategy_id="NO_A0",
        strategy_version="V1",
        family="HISTORICAL_V1",
        registry_index=4,
        activation_status="DISABLED_MISSING_EXECUTION_DATA",
        activation_reason_code="HISTORICAL_INPUT_HAS_NO_LIVE_DEPTH_FEE_CONTRACT",
        market_id="127251",
        market_date="2026-01-03",
        evaluation_key=None,
        signal_identity_key=None,
        parent_strategy_id=None,
        source_decision_identity=None,
        checkpoint_minutes=60,
        horizon="T-60m",
        side="NO",
        selected_buckets=("a" * 64,),
        accepted=True,
        emitted=False,
        reason_code="ACCEPTED",
        reference_price_micros=665_000,
        performance_price_micros=695_000,
        performance_price_basis="CONTRACT_STRESSED_REFERENCE_RECONSTRUCTED",
        scoring_status="RESOLUTION_PENDING",
        scoring_reason_code="SETTLEMENT_PENDING",
        observed_at_ms=1_767_398_400_000,
        source_created_at_ms=None,
        provenance_run_id="20260814T205244105513Z",
        source_result_sha256="b" * 64,
        input_sha256="c" * 64,
    )


def _resolution(observation: PerformanceObservation) -> PerformanceResolution:
    return PerformanceResolution.create_for_observation(
        observation,
        resolution_key="performance-resolution-historical:test",
        revision=1,
        supersedes_resolution_key=None,
        settlement_identity="historical-settlement:test",
        settlement_source_event_id=None,
        winning_bucket_identity="a" * 64,
        won=True,
        resolution_date="2026-01-03",
        resolved_at_ms=None,
        provenance_json={
            "artifact_sha256": seed_module.PINNED_SETTLEMENTS_SHA256,
            "dataset_version": "test",
            "event_end_utc": "2026-01-04T00:00:00Z",
            "source_hash": "d" * 64,
        },
    )


def _write_fixture(
    root: Path, *, duplicate: bool = False, conflicting_duplicate: bool = False
) -> str:
    root.mkdir(parents=True)
    observation = _observation()
    resolution = _resolution(observation)
    observation_rows = [observation.payload_json]
    if duplicate:
        observation_rows.append(observation.payload_json)
    if conflicting_duplicate:
        values = observation.constructor_values()
        values["logical_decision_key"] += ":conflict"
        observation_rows.append(PerformanceObservation.create(**values).payload_json)
    (root / "observations.jsonl").write_text(
        "\n".join(observation_rows) + "\n", encoding="utf-8", newline="\n"
    )
    (root / "resolutions.jsonl").write_text(
        resolution.payload_json + "\n", encoding="utf-8", newline="\n"
    )
    manifest = {
        "schema_version": "ORIGINAL_RUNTIME_SEED_MANIFEST_V1",
        "seed_id": "ORIGINAL_RUNTIME_SEED_V1",
        "provenance_classification": "ORIGINAL",
        "source_accepted_baseline": {
            "path_name": "historical_revalidation/20260814T205244105513Z",
            "run_id": "20260814T205244105513Z",
            "acceptance_sha256": seed_module.PINNED_AHR_ACCEPTANCE_SHA256,
            "input_manifest_file_sha256": seed_module.PINNED_AHR_MANIFEST_SHA256,
            "input_manifest_semantic_sha256": seed_module.PINNED_AHR_MANIFEST_SEMANTIC_SHA256,
            "parity_report_sha256": "9e5a5b0e64d84e98bf813b0dbfa6662c63e9901b00cf342b7748b3cbe9d764c2",
            "strategy_results_sha256": seed_module.PINNED_AHR_RESULTS_SHA256,
            "settlements_sha256": seed_module.PINNED_SETTLEMENTS_SHA256,
            "source_sha256": dict(sorted(EXPECTED_SOURCE_SHA256.items())),
        },
        "frozen_contracts": [
            {
                "relative_path": "strategy_sources/frozen/STRATEGY_RULE_MAP_47.json",
                "sha256": _sha256(PROJECT_ROOT / "strategy_sources/frozen/STRATEGY_RULE_MAP_47.json"),
                "schema_version": "BTC_STRATEGY_RULE_MAP_47_V2",
            },
            {
                "relative_path": "reports/STRATEGY_47_STATUS_MATRIX.json",
                "sha256": _sha256(PROJECT_ROOT / "reports/STRATEGY_47_STATUS_MATRIX.json"),
                "schema_version": "BTC_STRATEGY_47_STATUS_MATRIX_V3",
            },
        ],
        "payload_files": [
            {
                "logical_type": "performance_observations",
                "relative_path": "observations.jsonl",
                "record_count": len(observation_rows),
                "sha256": _sha256(root / "observations.jsonl"),
            },
            {
                "logical_type": "performance_resolutions",
                "relative_path": "resolutions.jsonl",
                "record_count": 1,
                "sha256": _sha256(root / "resolutions.jsonl"),
            },
        ],
        "record_counts": {
            "observations": len(observation_rows),
            "resolutions": 1,
            "accepted_observations": len(observation_rows),
            "rejected_observations": 0,
            "strategies": 1,
        },
        "creation_method": {
            "version": "ORIGINAL_RUNTIME_SEED_PACKAGER_V1",
            "script": "tools/build_original_runtime_seed_v1.py",
            "ordering": "ACCEPTED_AHR_RESULT_ORDER",
        },
        "schema_compatibility": {
            "migration_version": 7,
            "observation_schema": "PERFORMANCE_OBSERVATION_V1",
            "resolution_schema": "PERFORMANCE_RESOLUTION_V1",
        },
        "security_guards": {
            "authenticated_CLOB_writes": False,
            "real_orders": False,
            "signing": False,
            "trading_approval": False,
            "wallet": False,
        },
        "strategy_identities": [
            {
                "registry_index": 4,
                "rule_spec_sha256": next(
                    row["rule_spec_sha256"]
                    for row in json.loads(
                        (PROJECT_ROOT / "reports/STRATEGY_47_STATUS_MATRIX.json").read_text(encoding="utf-8")
                    )["strategies"]
                    if row["strategy_id"] == "NO_A0"
                ),
                "strategy_id": "NO_A0",
                "version": "V1",
            }
        ],
        "excluded_data": [
            "RECOVERED",
            "FORWARD",
            "EMITTED_SIGNALS",
            "PAPER_INTENTS",
            "PAPER_FILLS",
            "MUTABLE_CURSORS",
            "RUNTIME_INCIDENTS",
        ],
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return _sha256(root / "manifest.json")


class OriginalRuntimeSeedLoaderTests(unittest.TestCase):
    def test_valid_manifest_and_payload_reconstruct_domain_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "seed"
            manifest_hash = _write_fixture(root)
            with patch.object(
                seed_module,
                "PINNED_ORIGINAL_RUNTIME_SEED_MANIFEST_SHA256",
                manifest_hash,
            ):
                bundle = OriginalRuntimeSeedLoader(
                    project_root=PROJECT_ROOT, seed_dir=root
                ).load()
        self.assertEqual("ORIGINAL_RUNTIME_SEED_V1", bundle.seed_id)
        self.assertEqual(1, len(bundle.observations))
        self.assertEqual(1, len(bundle.resolutions))
        self.assertEqual(_observation().payload_json, bundle.observations[0].payload_json)
        self.assertEqual(_resolution(_observation()).payload_json, bundle.resolutions[0].payload_json)

    def test_missing_payload_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "seed"
            manifest_hash = _write_fixture(root)
            (root / "observations.jsonl").unlink()
            with patch.object(seed_module, "PINNED_ORIGINAL_RUNTIME_SEED_MANIFEST_SHA256", manifest_hash):
                with self.assertRaisesRegex(ValueError, "ORIGINAL_RUNTIME_SEED_PAYLOAD_MISSING"):
                    OriginalRuntimeSeedLoader(project_root=PROJECT_ROOT, seed_dir=root).load()

    def test_payload_hash_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "seed"
            manifest_hash = _write_fixture(root)
            with (root / "observations.jsonl").open("ab") as stream:
                stream.write(b"\n")
            with patch.object(seed_module, "PINNED_ORIGINAL_RUNTIME_SEED_MANIFEST_SHA256", manifest_hash):
                with self.assertRaisesRegex(ValueError, "ORIGINAL_RUNTIME_SEED_PAYLOAD_HASH_MISMATCH"):
                    OriginalRuntimeSeedLoader(project_root=PROJECT_ROOT, seed_dir=root).load()

    def test_non_original_manifest_fails_closed_after_authenticity_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "seed"
            _write_fixture(root)
            manifest_path = root / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["provenance_classification"] = "RECOVERED"
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
            )
            with patch.object(seed_module, "PINNED_ORIGINAL_RUNTIME_SEED_MANIFEST_SHA256", _sha256(manifest_path)):
                with self.assertRaisesRegex(ValueError, "ORIGINAL_RUNTIME_SEED_PROVENANCE_INVALID"):
                    OriginalRuntimeSeedLoader(project_root=PROJECT_ROOT, seed_dir=root).load()

    def test_duplicate_durable_identity_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "seed"
            manifest_hash = _write_fixture(root, duplicate=True)
            with patch.object(seed_module, "PINNED_ORIGINAL_RUNTIME_SEED_MANIFEST_SHA256", manifest_hash):
                with self.assertRaisesRegex(ValueError, "ORIGINAL_RUNTIME_SEED_OBSERVATION_IDENTITY_CONFLICT"):
                    OriginalRuntimeSeedLoader(project_root=PROJECT_ROOT, seed_dir=root).load()

    def test_conflicting_duplicate_observation_key_fails_in_loader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "seed"
            manifest_hash = _write_fixture(root, conflicting_duplicate=True)
            with patch.object(seed_module, "PINNED_ORIGINAL_RUNTIME_SEED_MANIFEST_SHA256", manifest_hash):
                with self.assertRaisesRegex(ValueError, "ORIGINAL_RUNTIME_SEED_OBSERVATION_IDENTITY_CONFLICT"):
                    OriginalRuntimeSeedLoader(project_root=PROJECT_ROOT, seed_dir=root).load()

    def test_frozen_contract_hash_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "seed"
            _write_fixture(root)
            manifest_path = root / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["frozen_contracts"][0]["sha256"] = "0" * 64
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
            )
            with patch.object(seed_module, "PINNED_ORIGINAL_RUNTIME_SEED_MANIFEST_SHA256", _sha256(manifest_path)):
                with self.assertRaisesRegex(ValueError, "ORIGINAL_RUNTIME_SEED_FROZEN_CONTRACT_HASH_MISMATCH"):
                    OriginalRuntimeSeedLoader(project_root=PROJECT_ROOT, seed_dir=root).load()

    def test_committed_seed_has_exact_accepted_counts_and_exclusions(self) -> None:
        bundle = OriginalRuntimeSeedLoader(project_root=PROJECT_ROOT).load()
        self.assertEqual(4_994, len(bundle.observations))
        self.assertEqual(1_850, len(bundle.resolutions))
        self.assertEqual(47, len({row.strategy_id for row in bundle.observations}))
        self.assertEqual({"HISTORICAL"}, {row.source_layer for row in bundle.observations})
        self.assertFalse(any(row.emitted for row in bundle.observations))
        self.assertFalse(any(row.evaluation_key for row in bundle.observations))
        self.assertFalse(any(row.signal_identity_key for row in bundle.observations))


class OriginalRuntimeSeedPackagingTests(unittest.TestCase):
    def test_packager_reproduces_committed_seed_byte_for_byte(self) -> None:
        from tools.build_original_runtime_seed_v1 import build_seed

        with tempfile.TemporaryDirectory() as temporary:
            generated = Path(temporary) / "original_runtime_seed_v1"
            build_seed(project_root=PROJECT_ROOT, output_dir=generated)
            self.assertEqual(
                sorted(path.name for path in SEED_DIR.iterdir()),
                sorted(path.name for path in generated.iterdir()),
            )
            for expected in sorted(SEED_DIR.iterdir()):
                self.assertEqual(
                    expected.read_bytes(), generated.joinpath(expected.name).read_bytes(), expected.name
                )


class OriginalRuntimeSeedIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SqliteStore.open(Path(self.temporary.name) / "runtime.sqlite3")
        self.store.migrate()
        self.repository = PerformanceRepository(self.store)
        self.engine = StrategyPerformanceEngine(project_root=PROJECT_ROOT, repository=self.repository)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_normal_bootstrap_does_not_call_old_ahr_loader(self) -> None:
        with patch(
            "src.performance_engine.PinnedAhrArtifactLoader.load",
            side_effect=AssertionError("old AHR path forbidden"),
        ), patch(
            "src.performance_historical.default_artifacts",
            side_effect=AssertionError("external research paths forbidden"),
        ):
            result = self.engine.bootstrap_historical(as_of_date="2026-08-15")
        self.assertEqual(4_994, result.observation_inserted)
        self.assertEqual(1_850, result.resolution_inserted)

    def test_second_seed_bootstrap_is_idempotent(self) -> None:
        first = self.engine.bootstrap_historical(as_of_date="2026-08-15")
        second = self.engine.bootstrap_historical(as_of_date="2026-08-15")
        self.assertEqual((4_994, 1_850, "INSERTED"), (
            first.observation_inserted, first.resolution_inserted, first.ingest_receipt_outcome
        ))
        self.assertEqual((4_994, 1_850, "REPLAYED"), (
            second.observation_replayed, second.resolution_replayed, second.ingest_receipt_outcome
        ))

    def test_conflicting_existing_original_row_preserves_immutable_failure(self) -> None:
        source = OriginalRuntimeSeedLoader(project_root=PROJECT_ROOT).load().observations[0]
        values = source.constructor_values()
        values["reason_code"] = values["reason_code"] + "_DRIFT"
        self.repository.append_observation(PerformanceObservation.create(**values))
        with self.assertRaisesRegex(ValueError, "PERFORMANCE_OBSERVATION_CONFLICT"):
            self.engine.bootstrap_historical(as_of_date="2026-08-15")

    def test_seed_bootstrap_introduces_no_recovered_forward_signal_or_paper_rows(self) -> None:
        self.engine.bootstrap_historical(as_of_date="2026-08-15")
        connection = self.store._connection
        self.assertEqual(0, connection.execute(
            "SELECT COUNT(*) FROM strategy_performance_observations WHERE source_layer='FORWARD' OR emitted=1"
        ).fetchone()[0])
        for table in ("strategy_reconstruction_status", "signals", "paper_intents", "paper_fills"):
            self.assertEqual(0, self.store.count(table), table)

    def test_incompatible_migration_version_fails_before_writing(self) -> None:
        self.store._connection.execute(
            "DELETE FROM schema_migrations WHERE version = 7"
        )
        self.store._connection.commit()
        with self.assertRaisesRegex(
            ValueError, "ORIGINAL_RUNTIME_SEED_SCHEMA_INCOMPATIBLE:6:7"
        ):
            self.engine.bootstrap_historical(as_of_date="2026-08-15")
        self.assertEqual(0, self.store.count("strategy_performance_observations"))


class OriginalRuntimeSeedParityTests(unittest.TestCase):
    def test_old_and_new_full_durable_content_matches_frozen_oracle(self) -> None:
        from tools.verify_original_runtime_seed_v1 import verify_seed

        expected = {
            "strategy_performance_observations": (4_994, "b041c250e0b3db6168c4fdb1915fd432f67fd58e33f4ca943338553902021866"),
            "strategy_performance_resolutions": (1_850, "11f330e0df82fed94204362127f9de0671038b34d3047c02369ee00dae8a0fdd"),
            "strategy_performance_ingest_runs": (1, "f255ba7e74945519a2553f20497ec1586d341943469f1135c1d23702fdf4a177"),
            "strategy_performance_materialization_revisions": (141, "0bb63936dac431a2ac96f25d585facbe25bd34bb878c7f7bdecaa659b09daf56"),
            "strategy_performance_aggregates": (141, "14b6ca53d20016a636387d2c09902626cb52a690f7f0f2dbaf0a034bb87e6c9b"),
            "strategy_performance_timeseries": (15_348, "17231f344a916f40d1108f06a8cffac5b34e1218d02de7930f01d9d3bd0adbf6"),
        }
        receipt = verify_seed(project_root=PROJECT_ROOT)
        self.assertEqual("PASS", receipt["status"])
        for table, (row_count, digest) in expected.items():
            comparison = receipt["tables"][table]
            self.assertTrue(comparison["equal"], table)
            self.assertEqual({"rows": row_count, "sha256": digest}, comparison["old"])
            self.assertEqual(comparison["old"], comparison["new"])
        for side in ("old", "new"):
            self.assertEqual(
                {
                    "emitted_observations": 0,
                    "forward_observations": 0,
                    "paper_fills": 0,
                    "paper_intents": 0,
                    "recovered_rows": 0,
                    "signals": 0,
                },
                receipt["guards"][side],
            )


if __name__ == "__main__":
    unittest.main()
