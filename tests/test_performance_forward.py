from __future__ import annotations

import hashlib
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.performance_forward import (
    ForwardPerformanceObservationBuilder,
    ForwardPerformanceCycle,
    ForwardSettlementProjector,
)
from src.performance_models import FIVE_SHARES_MICROS, canonical_identity
from src.performance_repository import PerformanceRepository
from src.storage import SqliteStore


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ForwardPerformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SqliteStore.open(Path(self.temporary.name) / "runtime.sqlite3")
        self.store.migrate()
        self.repository = PerformanceRepository(self.store)
        self._market("btc-range-2026-07-08", "2026-07-08")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_builder_ingests_only_live_evaluations_and_advances_cursor_atomically(self) -> None:
        accepted_id = self._evaluation(
            evaluation_key="eval-live-accepted",
            status="SIGNAL",
            result={
                "accepted": True,
                "reason": "ACCEPTED",
                "side": "NO",
                "selected_bucket_indices": [1],
                "market_probability": 0.42,
                "stressed_reference_cost": 0.45,
            },
        )
        self._schedule("eval-live-accepted", accepted_id)
        self._signal("signal-live-accepted", "eval-live-accepted")
        recovered_id = self._evaluation(
            evaluation_key="eval-recovered",
            status="SIGNAL",
            origin="RECOVERED_AFTER_DOWNTIME",
            result={
                "accepted": True,
                "reason": "ACCEPTED",
                "side": "NO",
                "selected_bucket_indices": [2],
                "market_probability": 0.33,
                "stressed_reference_cost": 0.36,
            },
        )
        rejected_id = self._evaluation(
            evaluation_key="eval-live-rejected",
            status="NO_SIGNAL",
            result={
                "accepted": False,
                "reason": "FILTER_REJECTED",
                "side": "NO",
                "selected_bucket_indices": [],
                "market_probability": 0.40,
            },
        )
        self._schedule("eval-live-rejected", rejected_id, checkpoint_minutes=30)

        with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")):
            receipt = ForwardPerformanceCycle(
                store=self.store,
                repository=self.repository,
                observed_at_ms=10_000,
            ).run_once()

        observations = self.repository.read_observations(source_layer="FORWARD")
        self.assertEqual(receipt.observation_inserted_count, 2)
        self.assertEqual(receipt.last_evaluation_id, rejected_id)
        self.assertEqual(len(observations), 2)

        accepted = next(row for row in observations if row.evaluation_key == "eval-live-accepted")
        self.assertTrue(accepted.accepted)
        self.assertTrue(accepted.emitted)
        self.assertEqual(accepted.signal_identity_key, "signal-live-accepted")
        self.assertEqual(accepted.selected_buckets, (_sha("bucket-mid"),))
        self.assertEqual(
            accepted.logical_decision_key,
            canonical_identity(
                "performance-logical-decision",
                {
                    "checkpoint_minutes": 60,
                    "horizon": "T-60m",
                    "market_date": "2026-07-08",
                    "market_id": "btc-range-2026-07-08",
                    "parent_strategy_id": None,
                    "side": "NO",
                    "source_decision_identity": None,
                    "strategy_id": "NO_A0",
                    "strategy_version": "V1",
                },
            ),
        )
        self.assertEqual(accepted.performance_price_micros, 450_000)
        self.assertEqual(accepted.performance_price_basis, "FORWARD_CAPTURED_CONTRACT")
        self.assertEqual(accepted.scoring_status, "RESOLUTION_PENDING")
        self.assertEqual(accepted.shares_micros, FIVE_SHARES_MICROS)
        self.assertEqual(
            accepted.observation_key,
            canonical_identity(
                "forward-observation",
                {
                    "evaluation_id": accepted_id,
                    "evaluation_key": "eval-live-accepted",
                    "input_sha256": _sha(_canonical(self._input_payload(60))),
                    "result_index": 0,
                },
            ),
        )

        rejected = next(row for row in observations if row.evaluation_key == "eval-live-rejected")
        self.assertFalse(rejected.accepted)
        self.assertFalse(rejected.emitted)
        self.assertIsNone(rejected.signal_identity_key)
        self.assertEqual(rejected.shares_micros, 0)
        self.assertIsNone(rejected.performance_price_micros)
        self.assertEqual(rejected.performance_price_basis, "NOT_APPLICABLE")
        self.assertEqual(rejected.scoring_status, "REJECTED")

        cursor = self.repository.read_cursor("forward_performance:evaluations")
        self.assertIsNotNone(cursor)
        self.assertEqual(cursor.cursor["last_evaluation_id"], rejected_id)

    def test_infrastructure_evaluations_are_skipped_but_c6_missing_input_fails_closed(self) -> None:
        canary_id = self._evaluation(
            evaluation_key="canary-eval",
            strategy_id="CANARY_SYNC_READY_V1",
            strategy_version="1",
            status="SIGNAL",
            payload={
                "canary_infrastructure_only": True,
                "reason_code": "CANARY_READY",
                "status": "SIGNAL",
            },
        )
        canary_batch = ForwardPerformanceObservationBuilder(
            store=self.store,
            observed_at_ms=10_000,
        ).build_since(0)
        self.assertEqual(canary_batch.observations, [])
        self.assertEqual(canary_batch.last_evaluation_id, canary_id)

        self._evaluation(
            evaluation_key="eval-missing-schedule",
            status="SIGNAL",
            result={
                "accepted": True,
                "reason": "ACCEPTED",
                "side": "NO",
                "selected_bucket_indices": [0],
                "market_probability": 0.42,
                "stressed_reference_cost": 0.45,
            },
        )
        with self.assertRaisesRegex(
            ValueError,
            "FORWARD_EVALUATION_CHECKPOINT_EVIDENCE_MISSING",
        ):
            ForwardPerformanceObservationBuilder(
                store=self.store,
                observed_at_ms=10_000,
            ).build_since(canary_id)

    def test_unknown_schema_live_strategy_evaluation_is_not_silently_skipped(self) -> None:
        self._evaluation(
            evaluation_key="eval-unknown-schema",
            status="SIGNAL",
            payload={
                "schema_version": "UNKNOWN_LIVE_STRATEGY_EVALUATION_V1",
                "origin": "LIVE",
                "strategy_id": "NO_A0",
                "evaluation_key": "eval-unknown-schema",
                "results": {
                    "accepted": True,
                    "reason": "ACCEPTED",
                    "side": "NO",
                    "selected_bucket_indices": [0],
                    "market_probability": 0.42,
                    "stressed_reference_cost": 0.45,
                },
            },
        )

        with self.assertRaisesRegex(
            ValueError,
            "FORWARD_EVALUATION_CHECKPOINT_EVIDENCE_MISSING",
        ):
            ForwardPerformanceObservationBuilder(
                store=self.store,
                observed_at_ms=10_000,
            ).build_since(0)

    def test_canary_only_scan_advances_cursor_with_complete_ingest_receipt(self) -> None:
        canary_id = self._evaluation(
            evaluation_key="canary-only",
            strategy_id="CANARY_SYNC_READY_V1",
            strategy_version="1",
            status="SIGNAL",
            payload={"canary_infrastructure_only": True, "status": "SIGNAL"},
        )

        receipt = ForwardPerformanceCycle(
            store=self.store,
            repository=self.repository,
            observed_at_ms=10_000,
        ).run_once()

        self.assertEqual(receipt.last_evaluation_id, canary_id)
        self.assertEqual(receipt.ingest_receipt_outcome, "INSERTED")
        self.assertEqual(self.store.count("strategy_performance_ingest_runs"), 1)

    def test_persisted_checkpoint_hash_mismatch_fails_closed(self) -> None:
        evaluation_id = self._evaluation(
            evaluation_key="eval-bad-hash",
            status="SIGNAL",
            result={
                "accepted": True,
                "reason": "ACCEPTED",
                "side": "NO",
                "selected_bucket_indices": [0],
                "stressed_reference_cost": 0.45,
            },
        )
        self._schedule("eval-bad-hash", evaluation_id)
        self.store.rows(
            "UPDATE strategy_checkpoint_schedules SET input_snapshot_hash=? WHERE evaluation_id=?",
            ("f" * 64, evaluation_id),
        )

        with self.assertRaisesRegex(ValueError, "FORWARD_EVALUATION_INPUT_HASH_MISMATCH"):
            ForwardPerformanceObservationBuilder(
                store=self.store,
                observed_at_ms=10_000,
            ).build_since(0)

    def test_cycle_is_idempotent_and_missing_forward_price_is_unscorable(self) -> None:
        evaluation_id = self._evaluation(
            evaluation_key="eval-live-unpriced",
            status="SIGNAL",
            result={
                "accepted": True,
                "reason": "ACCEPTED",
                "side": "YES",
                "selected_bucket_indices": [0],
            },
        )
        self._schedule("eval-live-unpriced", evaluation_id)

        cycle = ForwardPerformanceCycle(
            store=self.store,
            repository=self.repository,
            observed_at_ms=10_000,
        )
        first = cycle.run_once()
        second = cycle.run_once()

        observations = self.repository.read_observations(source_layer="FORWARD")
        self.assertEqual(first.observation_inserted_count, 1)
        self.assertEqual(second.observation_inserted_count, 0)
        self.assertEqual(self.store.count("strategy_performance_observations"), 1)
        self.assertEqual(self.store.count("strategy_performance_ingest_runs"), 1)
        self.assertEqual(observations[0].scoring_status, "UNSCORABLE")
        self.assertEqual(observations[0].scoring_reason_code, "UNSCORABLE_MISSING_PRICE")
        self.assertIsNone(observations[0].performance_price_micros)
        self.assertEqual(observations[0].performance_price_basis, "UNAVAILABLE")

    def test_bucket_mapping_requires_canonical_market_bucket_universe(self) -> None:
        evaluation_id = self._evaluation(
            evaluation_key="eval-no-bucket-identity",
            status="SIGNAL",
            result={
                "accepted": True,
                "reason": "ACCEPTED",
                "side": "YES",
                "selected_bucket_indices": [0],
                "market_probability": 0.25,
                "stressed_reference_cost": 0.31,
            },
        )
        self._schedule("eval-no-bucket-identity", evaluation_id)
        payload = {"event_id": "btc-range-2026-07-08", "market_date": "2026-07-08"}
        payload_json = _canonical(payload)
        self.store.rows(
            "UPDATE market_catalog SET payload_json=?, payload_sha256=? WHERE market_id=?",
            (payload_json, _sha(payload_json), "btc-range-2026-07-08"),
        )

        with self.assertRaisesRegex(
            ValueError,
            "FORWARD_MARKET_BUCKET_UNIVERSE_MISSING",
        ):
            ForwardPerformanceObservationBuilder(
                store=self.store,
                observed_at_ms=10_000,
            ).build_since(0)

    def test_cycle_publishes_forward_materialization_and_post_baseline_gaps(self) -> None:
        evaluation_id = self._evaluation(
            evaluation_key="eval-materialize",
            status="SIGNAL",
            result={
                "accepted": True,
                "reason": "ACCEPTED",
                "side": "NO",
                "selected_bucket_indices": [1],
                "market_probability": 0.42,
                "stressed_reference_cost": 0.45,
            },
        )
        self._schedule("eval-materialize", evaluation_id)

        receipt = ForwardPerformanceCycle(
            store=self.store,
            repository=self.repository,
            observed_at_ms=10_000,
            catchup_end_date="2026-07-10",
        ).run_once()

        self.assertEqual(receipt.observation_inserted_count, 1)
        self.assertIsNotNone(
            self.repository.read_current_materialization(
                strategy_id="NO_A0",
                source_view="FORWARD",
                calculation_version="PERFORMANCE_METRICS_V2",
            )
        )
        self.assertEqual(
            [
                (row.market_date, row.classification)
                for row in self.repository.read_effective_catchup_classifications()
            ],
            [
                ("2026-07-08", "PENDING"),
                ("2026-07-09", "DATA_GAP"),
                ("2026-07-10", "DATA_GAP"),
            ],
        )

    def test_settlement_projector_uses_only_persisted_public_resolution_evidence(self) -> None:
        evaluation_id = self._evaluation(
            evaluation_key="eval-settle",
            status="SIGNAL",
            result={
                "accepted": True,
                "reason": "ACCEPTED",
                "side": "NO",
                "selected_bucket_indices": [1],
                "market_probability": 0.42,
                "stressed_reference_cost": 0.45,
            },
        )
        self._schedule("eval-settle", evaluation_id)
        self.repository.append_observations_and_advance_cursor(
            ForwardPerformanceObservationBuilder(
                store=self.store,
                observed_at_ms=10_000,
            ).build_since(0).observations,
            cursor=ForwardPerformanceObservationBuilder(
                store=self.store,
                observed_at_ms=10_000,
            ).build_since(0).cursor,
        )
        self._source_event(
            natural_key="raw-resolution:condition-00",
            event_type="POLYMARKET_MARKET_RESOLVED",
            payload={
                "event_type": "market_resolved",
                "market": "condition-00",
                "timestamp": 18_000,
            },
        )
        self._source_event(
            natural_key="resolution:wrong-date",
            event_type="POLYMARKET_MARKET_RESOLVED",
            payload={
                "market_id": "btc-range-2026-07-08",
                "market_date": "2026-07-09",
                "resolved": True,
                "winning_bucket_identity": "bucket-low",
                "winning_bucket_count": 1,
                "resolved_at_ms": 19_000,
            },
        )
        self._source_event(
            natural_key="resolution:btc-range-2026-07-08",
            event_type="POLYMARKET_MARKET_RESOLVED",
            payload={
                "market_id": "btc-range-2026-07-08",
                "market_date": "2026-07-08",
                "resolved": True,
                "winning_bucket_identity": "bucket-low",
                "winning_bucket_count": 1,
                "resolved_at_ms": 20_000,
            },
        )

        with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")):
            resolutions = ForwardSettlementProjector(
                store=self.store,
                repository=self.repository,
                reconciled_at_ms=30_000,
            ).project()

        self.assertEqual(len(resolutions), 1)
        self.repository.append_resolution(resolutions[0])
        self.assertTrue(resolutions[0].won)
        self.assertEqual(resolutions[0].shares_micros, FIVE_SHARES_MICROS)
        self.assertEqual(resolutions[0].cost_usd_micros, 2_250_000)
        self.assertEqual(resolutions[0].gross_payout_usd_micros, FIVE_SHARES_MICROS)

    def test_settlement_correction_supersedes_previous_effective_resolution(self) -> None:
        evaluation_id = self._evaluation(
            evaluation_key="eval-correction",
            status="SIGNAL",
            result={
                "accepted": True,
                "reason": "ACCEPTED",
                "side": "YES",
                "selected_bucket_indices": [0],
                "market_probability": 0.25,
                "stressed_reference_cost": 0.31,
            },
        )
        self._schedule("eval-correction", evaluation_id)
        batch = ForwardPerformanceObservationBuilder(
            store=self.store,
            observed_at_ms=10_000,
        ).build_since(0)
        self.repository.append_observations_and_advance_cursor(
            batch.observations,
            batch.cursor,
        )
        self._source_event(
            natural_key="resolution:first",
            event_type="POLYMARKET_MARKET_RESOLVED",
            payload={
                "market_id": "btc-range-2026-07-08",
                "market_date": "2026-07-08",
                "resolved": True,
                "winning_bucket_identity": "bucket-low",
                "winning_bucket_count": 1,
                "resolved_at_ms": 20_000,
            },
        )
        first = ForwardSettlementProjector(
            store=self.store,
            repository=self.repository,
            reconciled_at_ms=30_000,
        ).project()
        self.repository.append_resolution(first[0])
        self._source_event(
            natural_key="resolution:correction",
            event_type="POLYMARKET_MARKET_RESOLVED",
            payload={
                "market_id": "btc-range-2026-07-08",
                "market_date": "2026-07-08",
                "resolved": True,
                "winning_bucket_identity": "bucket-mid",
                "winning_bucket_count": 1,
                "resolved_at_ms": 40_000,
                "supersedes_source_event_natural_key": "resolution:first",
            },
        )

        correction = ForwardSettlementProjector(
            store=self.store,
            repository=self.repository,
            reconciled_at_ms=50_000,
        ).project()

        self.assertEqual(len(correction), 1)
        self.assertEqual(correction[0].revision, 2)
        self.assertEqual(correction[0].supersedes_resolution_key, first[0].resolution_key)
        self.assertEqual(correction[0].winning_bucket_identity, _sha("bucket-mid"))

    def test_same_winner_settlement_correction_preserves_revision_lineage(self) -> None:
        evaluation_id = self._evaluation(
            evaluation_key="eval-same-winner-correction",
            status="SIGNAL",
            result={
                "accepted": True,
                "reason": "ACCEPTED",
                "side": "YES",
                "selected_bucket_indices": [0],
                "market_probability": 0.25,
                "stressed_reference_cost": 0.31,
            },
        )
        self._schedule("eval-same-winner-correction", evaluation_id)
        batch = ForwardPerformanceObservationBuilder(
            store=self.store,
            observed_at_ms=10_000,
        ).build_since(0)
        self.repository.append_observations_and_advance_cursor(
            batch.observations,
            batch.cursor,
        )
        self._source_event(
            natural_key="resolution:same:first",
            event_type="POLYMARKET_DAILY_RANGE_MARKET_RESOLVED",
            payload={
                "market_id": "btc-range-2026-07-08",
                "market_date": "2026-07-08",
                "resolved": True,
                "winning_bucket_identity": "bucket-low",
                "winning_bucket_count": 1,
                "resolved_at_ms": 20_000,
            },
        )
        first = ForwardSettlementProjector(
            store=self.store,
            repository=self.repository,
            reconciled_at_ms=30_000,
        ).project()[0]
        self.repository.append_resolution(first)
        self._source_event(
            natural_key="resolution:same:correction",
            event_type="POLYMARKET_DAILY_RANGE_MARKET_RESOLVED",
            payload={
                "market_id": "btc-range-2026-07-08",
                "market_date": "2026-07-08",
                "resolved": True,
                "winning_bucket_identity": "bucket-low",
                "winning_bucket_count": 1,
                "resolved_at_ms": 40_000,
                "supersedes_source_event_natural_key": "resolution:same:first",
            },
        )

        correction = ForwardSettlementProjector(
            store=self.store,
            repository=self.repository,
            reconciled_at_ms=50_000,
        ).project()

        self.assertEqual(len(correction), 1)
        self.assertEqual(correction[0].revision, 2)
        self.assertEqual(correction[0].supersedes_resolution_key, first.resolution_key)
        self.assertEqual(correction[0].resolved_at_ms, 40_000)

    def test_ambiguous_resolution_evidence_fails_closed(self) -> None:
        evaluation_id = self._evaluation(
            evaluation_key="eval-ambiguous",
            status="SIGNAL",
            result={
                "accepted": True,
                "reason": "ACCEPTED",
                "side": "YES",
                "selected_bucket_indices": [0],
                "market_probability": 0.25,
                "stressed_reference_cost": 0.31,
            },
        )
        self._schedule("eval-ambiguous", evaluation_id)
        batch = ForwardPerformanceObservationBuilder(
            store=self.store,
            observed_at_ms=10_000,
        ).build_since(0)
        self.repository.append_observations_and_advance_cursor(
            batch.observations,
            batch.cursor,
        )
        self._source_event(
            natural_key="resolution:ambiguous",
            event_type="POLYMARKET_MARKET_RESOLVED",
            payload={
                "market_id": "btc-range-2026-07-08",
                "market_date": "2026-07-08",
                "resolved": True,
                "winning_bucket_identity": "bucket-low",
                "winning_bucket_count": 2,
            },
        )

        with self.assertRaisesRegex(ValueError, "FORWARD_RESOLUTION_AMBIGUOUS"):
            ForwardSettlementProjector(
                store=self.store,
                repository=self.repository,
                reconciled_at_ms=30_000,
            ).project()

    def test_contradictory_resolution_without_correction_lineage_fails_closed(self) -> None:
        evaluation_id = self._evaluation(
            evaluation_key="eval-contradictory",
            status="SIGNAL",
            result={
                "accepted": True,
                "reason": "ACCEPTED",
                "side": "YES",
                "selected_bucket_indices": [0],
                "stressed_reference_cost": 0.31,
            },
        )
        self._schedule("eval-contradictory", evaluation_id)
        batch = ForwardPerformanceObservationBuilder(
            store=self.store, observed_at_ms=10_000
        ).build_since(0)
        self.repository.append_observations_and_advance_cursor(
            batch.observations, batch.cursor
        )
        for key, winner in (("resolution:a", "bucket-low"), ("resolution:b", "bucket-mid")):
            self._source_event(
                natural_key=key,
                event_type="POLYMARKET_MARKET_RESOLVED",
                payload={
                    "market_id": "btc-range-2026-07-08",
                    "market_date": "2026-07-08",
                    "resolved": True,
                    "winning_bucket_identity": winner,
                    "winning_bucket_count": 1,
                },
            )

        with self.assertRaisesRegex(ValueError, "FORWARD_RESOLUTION_AMBIGUOUS"):
            ForwardSettlementProjector(
                store=self.store,
                repository=self.repository,
                reconciled_at_ms=30_000,
            ).project()

    def test_resolution_winner_outside_canonical_universe_fails_closed(self) -> None:
        evaluation_id = self._evaluation(
            evaluation_key="eval-impossible-winner",
            status="SIGNAL",
            result={
                "accepted": True,
                "reason": "ACCEPTED",
                "side": "NO",
                "selected_bucket_indices": [0],
                "stressed_reference_cost": 0.45,
            },
        )
        self._schedule("eval-impossible-winner", evaluation_id)
        batch = ForwardPerformanceObservationBuilder(
            store=self.store, observed_at_ms=10_000
        ).build_since(0)
        self.repository.append_observations_and_advance_cursor(
            batch.observations, batch.cursor
        )
        self._source_event(
            natural_key="resolution:impossible",
            event_type="POLYMARKET_MARKET_RESOLVED",
            payload={
                "market_id": "btc-range-2026-07-08",
                "market_date": "2026-07-08",
                "resolved": True,
                "winning_bucket_identity": "not-a-real-bucket",
                "winning_bucket_count": 1,
            },
        )

        with self.assertRaisesRegex(ValueError, "FORWARD_RESOLUTION_BUCKET_MAPPING_INVALID"):
            ForwardSettlementProjector(
                store=self.store,
                repository=self.repository,
                reconciled_at_ms=30_000,
            ).project()

    def _market(self, market_id: str, market_date: str) -> None:
        payload = {
            "event_id": market_id,
            "market_date": market_date,
            "resolution_utc": f"{market_date}T23:59:00+00:00",
            "outcomes": [
                "bucket-low", "bucket-mid", "bucket-high",
                *[f"bucket-{index}" for index in range(3, 11)],
            ],
        }
        payload_json = _canonical(payload)
        self.store.rows(
            """
            INSERT INTO market_catalog(market_id, payload_json, payload_sha256, updated_at_ms)
            VALUES (?, ?, ?, ?)
            """,
            (market_id, payload_json, _sha(payload_json), 1),
        )

    def _evaluation(
        self,
        *,
        evaluation_key: str,
        status: str,
        result: dict[str, object] | None = None,
        payload: dict[str, object] | None = None,
        origin: str = "LIVE",
        strategy_id: str = "NO_A0",
        strategy_version: str = "V1",
    ) -> int:
        if payload is None:
            payload = {
                "schema_version": "C6_CURRENT_REEVALUATION_V1",
                "origin": origin,
                "strategy_id": strategy_id,
                "evaluation_key": evaluation_key,
                "results": result,
            }
        payload_json = _canonical(payload)
        self.store.rows(
            """
            INSERT INTO strategy_evaluations(
                evaluation_key, strategy_id, strategy_version, status,
                input_snapshot_hash, evaluation_revision, execution_eligible,
                evaluated_at_ms, payload_json, origin,
                historical_signal_is_current_live_signal, current_reevaluation_required
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evaluation_key,
                strategy_id,
                strategy_version,
                status,
                "1" * 64,
                1,
                1 if origin == "LIVE" else 0,
                5_000,
                payload_json,
                origin,
                0,
                0,
            ),
        )
        return int(self.store.scalar(
            "SELECT evaluation_id FROM strategy_evaluations WHERE evaluation_key = ?",
            (evaluation_key,),
        ))

    def _schedule(
        self,
        evaluation_key: str,
        evaluation_id: int,
        *,
        capture_origin: str = "LIVE",
        checkpoint_minutes: int = 60,
        bucket_identity: bool = True,
    ) -> None:
        input_payload = self._input_payload(
            checkpoint_minutes,
            bucket_identity=bucket_identity,
        )
        input_payload_json = _canonical(input_payload)
        self.store.rows(
            """
            INSERT INTO strategy_checkpoint_schedules(
                schedule_key, evaluation_key, market_id, market_identity_sha256,
                resolution_ms, registry_index, strategy_id, strategy_version,
                rule_spec_sha256, input_schema_version, activation_status,
                activation_source_commit, c5_acceptance_sha256,
                c5_status_matrix_sha256, checkpoint_minutes, due_at_ms, state,
                capture_origin, input_snapshot_hash, input_payload_json,
                historical_depth_available, evaluation_id, created_at_ms,
                updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"schedule:{evaluation_key}",
                evaluation_key,
                "btc-range-2026-07-08",
                "2" * 64,
                2_000_000_000_000,
                4,
                "NO_A0",
                "V1",
                "3" * 64,
                "BTC_STRATEGY_EXECUTABLE_CHECKPOINT_INPUT_V1",
                "DISABLED_MISSING_EXECUTION_DATA",
                "commit",
                "4" * 64,
                "5" * 64,
                checkpoint_minutes,
                4_000,
                "COMPLETED",
                capture_origin,
                _sha(input_payload_json),
                input_payload_json,
                1,
                evaluation_id,
                1,
                1,
            ),
        )

    @staticmethod
    def _input_payload(
        checkpoint_minutes: int,
        *,
        bucket_identity: bool = True,
    ) -> dict[str, object]:
        buckets: list[dict[str, object]] = [
            {
                "bucket_index": index,
                **({"no_token_id": f"no-token-{index}"} if bucket_identity else {}),
            }
            for index in range(11)
        ]
        return {
            "schema_version": "BTC_STRATEGY_EXECUTABLE_CHECKPOINT_INPUT_V1",
            "market_id": "btc-range-2026-07-08",
            "market_date": "2026-07-08",
            "checkpoint_minutes": checkpoint_minutes,
            "buckets": buckets,
        }

    def _signal(self, identity_key: str, evaluation_key: str) -> None:
        payload_json = _canonical({"evaluation_key": evaluation_key, "strategy_id": "NO_A0"})
        self.store.rows(
            """
            INSERT INTO signals(
                identity_key, evaluation_key, strategy_id, signal_type,
                payload_json, created_at_ms, origin, execution_eligible,
                infrastructure_only
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                identity_key,
                evaluation_key,
                "NO_A0",
                "STRATEGY_EVALUATION_SIGNAL",
                payload_json,
                6_000,
                "LIVE",
                1,
                0,
            ),
        )

    def _source_event(
        self,
        *,
        natural_key: str,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        payload_json = _canonical(payload)
        self.store.rows(
            """
            INSERT INTO source_events(
                source, natural_key, source_timestamp_ms, received_timestamp_ms,
                event_type, payload_json, payload_sha256, recovery_origin,
                committed_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "polymarket",
                natural_key,
                20_000,
                20_000,
                event_type,
                payload_json,
                _sha(payload_json),
                "LIVE",
                20_000,
            ),
        )


if __name__ == "__main__":
    unittest.main()
