from __future__ import annotations

import dataclasses
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.performance_models import (
    AggregateRevision,
    AggregateRow,
    CatchupClassification,
    PerformanceCursor,
    PerformanceIngestRun,
    PerformanceObservation,
    PerformanceResolution,
    TimeseriesRow,
    canonical_identity,
    materialization_children_sha256,
)
from src.performance_repository import PerformanceRepository
from src.models import SignalRecord
from src.storage import SqliteStore


class PerformanceRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SqliteStore.open(Path(self.temp.name) / "runtime.sqlite3")
        self.store.migrate()
        self.repository = PerformanceRepository(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    @staticmethod
    def observation(
        *,
        observation_key: str = "obs:h:1",
        logical_decision_key: str = "decision:1",
        source_layer: str = "HISTORICAL",
        accepted: bool = True,
        emitted: bool = False,
        reason_code: str = "ACCEPTED",
        performance_price_micros: int | None = 695_000,
        scoring_status: str = "RESOLUTION_PENDING",
        scoring_reason_code: str = "SETTLEMENT_PENDING",
        market_date: str = "2026-01-03",
        checkpoint_minutes: int = 60,
    ) -> PerformanceObservation:
        return PerformanceObservation.create(
            observation_key=observation_key,
            logical_decision_key=logical_decision_key,
            source_layer=source_layer,
            strategy_id="NO_A0",
            strategy_version="V1",
            family="CONTROL",
            registry_index=1,
            activation_status="ENABLED_RESEARCH",
            activation_reason_code="CANONICAL_V1",
            market_id=f"btc-range-{market_date}",
            market_date=market_date,
            evaluation_key=f"evaluation:{source_layer}:{observation_key}",
            signal_identity_key=(
                f"signal:{observation_key}" if emitted else None
            ),
            parent_strategy_id=None,
            source_decision_identity=None,
            checkpoint_minutes=checkpoint_minutes,
            horizon="T60",
            side="NO",
            selected_buckets=("bucket-3",),
            accepted=accepted,
            emitted=emitted,
            reason_code=reason_code,
            reference_price_micros=(665_000 if accepted else None),
            performance_price_micros=(
                performance_price_micros if accepted else None
            ),
            performance_price_basis=(
                "CONTRACT_STRESSED_REFERENCE_RECONSTRUCTED"
                if accepted and performance_price_micros is not None
                else "NOT_APPLICABLE"
            ),
            scoring_status=(scoring_status if accepted else "REJECTED"),
            scoring_reason_code=(
                scoring_reason_code if accepted else "EVALUATOR_REJECTED"
            ),
            observed_at_ms=100,
            source_created_at_ms=None,
            provenance_run_id="20260814T205244105513Z",
            source_result_sha256="a" * 64,
            input_sha256="b" * 64,
        )

    @staticmethod
    def resolution(
        observation: PerformanceObservation,
        *,
        resolution_key: str = "resolution:1",
        revision: int = 1,
        supersedes_resolution_key: str | None = None,
        won: bool = True,
    ) -> PerformanceResolution:
        return PerformanceResolution.create_for_observation(
            observation,
            resolution_key=resolution_key,
            revision=revision,
            supersedes_resolution_key=supersedes_resolution_key,
            settlement_identity=f"settlement:{revision}",
            settlement_source_event_id=None,
            winning_bucket_identity="bucket-7",
            won=won,
            resolution_date=observation.market_date,
            resolved_at_ms=None,
            provenance_json={"artifact_sha256": "c" * 64},
        )

    def test_migration_adds_all_performance_tables_and_count_allowlist(self) -> None:
        names = {
            row[0]
            for row in self.store.rows(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        expected = {
            "strategy_performance_observations",
            "strategy_performance_resolutions",
            "strategy_performance_ingest_runs",
            "strategy_performance_cursors",
            "strategy_performance_catchup",
            "strategy_performance_materialization_revisions",
            "strategy_performance_aggregates",
            "strategy_performance_timeseries",
        }
        self.assertTrue(expected <= names)
        for table in expected:
            self.assertEqual(self.store.count(table), 0)

    def test_immutable_observation_replays_exact_payload_and_conflicts_on_drift(self) -> None:
        observation = self.observation()
        self.assertEqual(
            self.repository.append_observation(observation).outcome,
            "INSERTED",
        )
        self.assertEqual(
            self.repository.append_observation(observation).outcome,
            "REPLAYED",
        )

        changed = self.observation(reason_code="DRIFTED")
        with self.assertRaisesRegex(
            ValueError,
            "PERFORMANCE_OBSERVATION_CONFLICT",
        ):
            self.repository.append_observation(changed)

        self.assertEqual(self.store.count("strategy_performance_observations"), 1)

    def test_observation_identity_is_canonical_and_cross_layer_rows_stay_separate(self) -> None:
        first = canonical_identity("observation", {"b": 2, "a": 1})
        second = canonical_identity("observation", {"a": 1, "b": 2})
        self.assertEqual(first, second)

        historical = self.observation()
        forward = self.observation(
            observation_key="obs:f:1",
            source_layer="FORWARD",
            emitted=False,
        )
        self.repository.append_observation(historical)
        self.repository.append_observation(forward)
        rows = self.repository.read_observations(strategy_id="NO_A0")
        self.assertEqual([row.source_layer for row in rows], ["HISTORICAL", "FORWARD"])

        duplicate_layer = self.observation(observation_key="obs:h:duplicate")
        with self.assertRaisesRegex(
            ValueError,
            "PERFORMANCE_OBSERVATION_CONFLICT",
        ):
            self.repository.append_observation(duplicate_layer)

    def test_observation_domain_is_frozen_and_rejected_rows_have_no_economics(self) -> None:
        rejected = self.observation(accepted=False, reason_code="FILTER_REJECTED")
        self.assertEqual(rejected.shares_micros, 0)
        self.assertIsNone(rejected.performance_price_micros)
        self.assertEqual(rejected.scoring_status, "REJECTED")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            rejected.reason_code = "MUTATED"  # type: ignore[misc]

        with self.assertRaisesRegex(ValueError, "INVALID_OBSERVATION_ECONOMICS"):
            PerformanceObservation.create(
                **{
                    **rejected.constructor_values(),
                    "accepted": False,
                    "performance_price_micros": 1,
                }
            )

        cursor = PerformanceCursor.create(
            cursor_name="immutable",
            cursor={"position": {"evaluation_id": 1}},
            updated_at_ms=1,
        )
        with self.assertRaises(TypeError):
            cursor.cursor["position"]["evaluation_id"] = 2  # type: ignore[index]

    def test_resolution_replay_and_explicit_revision_supersession(self) -> None:
        observation = self.observation()
        self.repository.append_observation(observation)
        original = self.resolution(observation)
        self.assertEqual(self.repository.append_resolution(original).outcome, "INSERTED")
        self.assertEqual(self.repository.append_resolution(original).outcome, "REPLAYED")

        correction = self.resolution(
            observation,
            resolution_key="resolution:2",
            revision=2,
            supersedes_resolution_key=original.resolution_key,
            won=False,
        )
        self.repository.append_resolution(correction)
        effective = self.repository.read_effective_resolutions()
        self.assertEqual(effective[observation.observation_key].resolution_key, "resolution:2")
        self.assertEqual(self.store.count("strategy_performance_resolutions"), 2)

        with self.assertRaisesRegex(ValueError, "PERFORMANCE_RESOLUTION_SEQUENCE_CONFLICT"):
            self.repository.append_resolution(
                self.resolution(
                    observation,
                    resolution_key="resolution:4",
                    revision=4,
                    supersedes_resolution_key=correction.resolution_key,
                )
            )

    def test_cursor_advance_and_observation_inserts_share_one_transaction(self) -> None:
        cursor1 = PerformanceCursor.create(
            cursor_name="forward-evaluations",
            cursor={"evaluation_id": 10},
            updated_at_ms=10,
        )
        first = self.observation(
            observation_key="obs:f:10",
            logical_decision_key="decision:10",
            source_layer="FORWARD",
            emitted=False,
        )
        results = self.repository.append_observations_and_advance_cursor(
            [first], cursor1
        )
        self.assertEqual([result.outcome for result in results], ["INSERTED"])
        self.assertEqual(self.repository.read_cursor("forward-evaluations"), cursor1)

        cursor2 = PerformanceCursor.create(
            cursor_name="forward-evaluations",
            cursor={"evaluation_id": 20},
            updated_at_ms=20,
        )
        second = self.observation(
            observation_key="obs:f:20",
            logical_decision_key="decision:20",
            source_layer="FORWARD",
            emitted=False,
        )
        conflicting = self.observation(
            observation_key="obs:f:10",
            logical_decision_key="decision:10",
            source_layer="FORWARD",
            emitted=False,
            reason_code="DRIFTED",
        )
        with self.assertRaisesRegex(ValueError, "PERFORMANCE_OBSERVATION_CONFLICT"):
            self.repository.append_observations_and_advance_cursor(
                [second, conflicting], cursor2
            )
        self.assertEqual(self.repository.read_cursor("forward-evaluations"), cursor1)
        self.assertEqual(self.store.count("strategy_performance_observations"), 1)

    def test_unscorable_observation_cannot_be_resolved_or_persisted_as_resolution(self) -> None:
        unscorable = self.observation(
            observation_key="obs:h:unscorable",
            logical_decision_key="decision:unscorable",
            performance_price_micros=695_000,
            scoring_status="UNSCORABLE",
            scoring_reason_code="UNSCORABLE_AMBIGUOUS_BUCKET",
        )
        with self.assertRaisesRegex(ValueError, "UNSCORABLE_PERFORMANCE_OBSERVATION"):
            self.resolution(unscorable)

        pending = self.observation(
            observation_key="obs:h:pending-template",
            logical_decision_key="decision:pending-template",
            performance_price_micros=695_000,
        )
        forged = dataclasses.replace(
            self.resolution(pending),
            resolution_key="resolution:forged-unscorable",
            observation_key=unscorable.observation_key,
        )
        self.repository.append_observation(unscorable)
        with self.assertRaisesRegex(
            ValueError,
            "PERFORMANCE_RESOLUTION_OBSERVATION_UNSCORABLE",
        ):
            self.repository.append_resolution(forged)

    def test_emitted_observation_requires_matching_persisted_signal(self) -> None:
        emitted = self.observation(
            observation_key="obs:f:emitted",
            logical_decision_key="decision:emitted",
            source_layer="FORWARD",
            emitted=True,
        )
        with self.assertRaisesRegex(
            ValueError,
            "PERFORMANCE_EMITTED_SIGNAL_MISSING",
        ):
            self.repository.append_observation(emitted)

        self.store.commit_signal_and_outbox(
            SignalRecord(
                identity_key=emitted.signal_identity_key,
                evaluation_key=emitted.evaluation_key,
                strategy_id=emitted.strategy_id,
                signal_type="STRICT_A_SIGNAL_V1",
                payload_json="{}",
                created_at_ms=99,
            ),
            topic="signal.created",
        )
        self.assertEqual(
            self.repository.append_observation(emitted).outcome,
            "INSERTED",
        )
        foreign_keys = self.store.rows(
            "PRAGMA foreign_key_list(strategy_performance_observations)"
        )
        self.assertTrue(
            any(
                row[2] == "signals"
                and row[3] == "signal_identity_key"
                and row[4] == "identity_key"
                for row in foreign_keys
            )
        )

    def test_ingest_and_catchup_records_replay_but_never_overwrite(self) -> None:
        ingest = PerformanceIngestRun.create(
            ingest_run_key="ingest:1",
            mode="FORWARD_INCREMENTAL",
            source_artifact_class="SQLITE_COMMITTED_EVALUATIONS",
            source_sha256="d" * 64,
            acceptance_sha256=None,
            source_schema_version="STRATEGY_EVALUATION_V1",
            input_count=3,
            inserted_count=1,
            replayed_count=1,
            rejected_count=1,
            accepted_count=2,
            unscorable_count=0,
            conflict_count=0,
            first_source_identity="evaluation:1",
            last_source_identity="evaluation:3",
            first_cursor_json={"evaluation_id": 1},
            last_cursor_json={"evaluation_id": 3},
            status="COMPLETE",
            started_at_ms=1,
            completed_at_ms=2,
        )
        self.assertEqual(self.repository.append_ingest_run(ingest).outcome, "INSERTED")
        self.assertEqual(self.repository.append_ingest_run(ingest).outcome, "REPLAYED")
        changed_ingest = dataclasses.replace(ingest, inserted_count=2)
        with self.assertRaisesRegex(ValueError, "PERFORMANCE_INGEST_RUN_CONFLICT"):
            self.repository.append_ingest_run(changed_ingest)
        self.assertEqual(self.repository.read_ingest_runs(), [ingest])

        classification = CatchupClassification.create(
            catchup_key="catchup:2026-08-01",
            market_date="2026-08-01",
            classification="EXPECTED_ABSENT",
            reason_code="NO_CANONICAL_MARKET",
            market_id=None,
            source_event_identity=None,
            classified_at_ms=3,
            provenance={"calendar": "BTC_DAILY_RANGE"},
        )
        self.assertEqual(
            self.repository.append_catchup_classification(classification).outcome,
            "INSERTED",
        )
        self.assertEqual(
            self.repository.append_catchup_classification(classification).outcome,
            "REPLAYED",
        )
        with self.assertRaisesRegex(ValueError, "PERFORMANCE_CATCHUP_CONFLICT"):
            self.repository.append_catchup_classification(
                dataclasses.replace(classification, reason_code="DATA_CHANGED")
            )
        self.assertEqual(
            self.repository.read_catchup_classifications(),
            [classification],
        )

        resolved = dataclasses.replace(
            classification,
            catchup_key="catchup:2026-08-01:2",
            revision=2,
            supersedes_catchup_key=classification.catchup_key,
            classification="RESOLVED",
            reason_code="CANONICAL_SETTLEMENT_AVAILABLE",
            market_id="btc-range-2026-08-01",
            classified_at_ms=4,
        )
        self.repository.append_catchup_classification(resolved)
        self.assertEqual(
            self.repository.read_effective_catchup_classifications(),
            [resolved],
        )
        self.assertEqual(
            self.store.count("strategy_performance_catchup"),
            2,
        )

    def test_materialization_publication_is_atomic_and_keeps_prior_current(self) -> None:
        revision1 = AggregateRevision.create(
            revision_key="materialization:NO_A0:HISTORICAL:1",
            strategy_id="NO_A0",
            source_view="HISTORICAL",
            calculation_version="PERFORMANCE_V1",
            source_ledger_revision=1,
            source_ledger_sha256="e" * 64,
            generated_at_ms=10,
            provenance={"input_count": 1},
            aggregate_count=1,
            timeseries_count=1,
            children_sha256="0" * 64,
        )
        aggregate1 = AggregateRow.create(
            aggregate_key="aggregate:1",
            revision_key=revision1.revision_key,
            strategy_id="NO_A0",
            source_view="HISTORICAL",
            window_kind="ALL",
            window_key="ALL",
            calculation_version="PERFORMANCE_V1",
            payload={"pnl_usd_micros": 10},
        )
        series1 = TimeseriesRow.create(
            timeseries_key="series:1",
            revision_key=revision1.revision_key,
            strategy_id="NO_A0",
            source_view="HISTORICAL",
            series_kind="CUMULATIVE",
            period_key="2026-01-03:60:obs:h:1",
            calculation_version="PERFORMANCE_V1",
            observation_count=1,
            observation_sha256="f" * 64,
            payload={"cumulative_pnl_usd_micros": 10},
        )
        revision1 = dataclasses.replace(
            revision1,
            children_sha256=materialization_children_sha256(
                [aggregate1],
                [series1],
            ),
        )
        self.repository.publish_materialization(revision1, [aggregate1], [series1])

        revision2 = dataclasses.replace(
            revision1,
            revision_key="materialization:NO_A0:HISTORICAL:2",
            source_ledger_revision=2,
            generated_at_ms=20,
        )
        aggregate2 = dataclasses.replace(
            aggregate1,
            aggregate_key="aggregate:2",
            revision_key=revision2.revision_key,
        )
        duplicate_series = [
            dataclasses.replace(series1, revision_key=revision2.revision_key),
            dataclasses.replace(series1, revision_key=revision2.revision_key),
        ]
        with self.assertRaisesRegex(ValueError, "PERFORMANCE_MATERIALIZATION_INCOMPLETE"):
            self.repository.publish_materialization(
                revision2, [aggregate2], duplicate_series
            )

        current = self.repository.read_current_materialization(
            strategy_id="NO_A0",
            source_view="HISTORICAL",
            calculation_version="PERFORMANCE_V1",
        )
        self.assertEqual(current["revision"].revision_key, revision1.revision_key)
        self.assertEqual(current["aggregates"][0].payload, {"pnl_usd_micros": 10})
        self.assertEqual(self.store.count("strategy_performance_materialization_revisions"), 1)

        changed_aggregate = dataclasses.replace(
            aggregate1,
            payload={"pnl_usd_micros": 999},
        )
        with self.assertRaisesRegex(ValueError, "PERFORMANCE_MATERIALIZATION_INCOMPLETE"):
            self.repository.publish_materialization(
                revision1, [changed_aggregate], [series1]
            )

    def test_read_only_store_can_open_repository_but_cannot_cross_writer_boundary(self) -> None:
        observation = self.observation()
        self.repository.append_observation(observation)
        read_store = self.store.open_read_store()
        try:
            repository = read_store.performance_repository()
            self.assertEqual(repository.read_observations(), [observation])
            with self.assertRaisesRegex(
                ValueError,
                "PERFORMANCE_REPOSITORY_READ_ONLY",
            ):
                repository.append_observation(
                    self.observation(
                        observation_key="obs:h:2",
                        logical_decision_key="decision:2",
                    )
                )
        finally:
            read_store.close()

    def test_materialization_refuses_empty_or_missing_all_aggregate(self) -> None:
        empty = AggregateRevision.create(
            revision_key="materialization:NO_A0:FORWARD:empty",
            strategy_id="NO_A0",
            source_view="FORWARD",
            calculation_version="PERFORMANCE_V1",
            source_ledger_revision=0,
            source_ledger_sha256="0" * 64,
            generated_at_ms=1,
            provenance={"input_count": 0},
            aggregate_count=0,
            timeseries_count=0,
            children_sha256=materialization_children_sha256([], []),
        )
        with self.assertRaisesRegex(
            ValueError,
            "PERFORMANCE_MATERIALIZATION_INCOMPLETE",
        ):
            self.repository.publish_materialization(empty, [], [])

        monthly = AggregateRow.create(
            aggregate_key="aggregate:monthly-only",
            revision_key="materialization:NO_A0:FORWARD:monthly-only",
            strategy_id="NO_A0",
            source_view="FORWARD",
            window_kind="MONTHLY",
            window_key="2026-08",
            calculation_version="PERFORMANCE_V1",
            payload={"resolved_signal_count": 0},
        )
        missing_all = dataclasses.replace(
            empty,
            revision_key=monthly.revision_key,
            source_ledger_revision=1,
            aggregate_count=1,
            children_sha256=materialization_children_sha256([monthly], []),
        )
        with self.assertRaisesRegex(
            ValueError,
            "PERFORMANCE_MATERIALIZATION_INCOMPLETE",
        ):
            self.repository.publish_materialization(
                missing_all,
                [monthly],
                [],
            )

    def test_database_constraints_reject_invalid_share_and_signed_pnl_equations(self) -> None:
        observation = self.observation()
        self.repository.append_observation(observation)
        columns = self.store.rows("PRAGMA table_info(strategy_performance_resolutions)")
        self.assertTrue(columns)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store._connection.execute(
                """
                INSERT INTO strategy_performance_resolutions(
                    resolution_key, observation_key, revision,
                    supersedes_resolution_key, settlement_identity,
                    settlement_source_event_id, winning_bucket_identity, won,
                    shares_micros, cost_usd_micros, gross_payout_usd_micros,
                    pnl_usd_micros, turnover_usd_micros, resolution_date,
                    resolved_at_ms, provenance_json, payload_json, payload_sha256
                ) VALUES (?, ?, 1, NULL, 'settlement', NULL, 'bucket', 1,
                          4000000, 3475000, 5000000, 999, 3475000,
                          '2026-01-03', NULL, '{}', '{}', ?)
                """,
                ("bad-resolution", observation.observation_key, "0" * 64),
            )


if __name__ == "__main__":
    unittest.main()
