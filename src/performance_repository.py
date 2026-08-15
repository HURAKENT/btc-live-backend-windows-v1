from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Sequence

from src.performance_models import (
    AggregateRevision,
    AggregateRow,
    CatchupClassification,
    PerformanceCursor,
    PerformanceIngestRun,
    PerformanceObservation,
    PerformanceResolution,
    TimeseriesRow,
    canonical_json,
    five_share_economics,
    materialization_children_sha256,
)
from src.storage import SqliteReadStore, SqliteStore


@dataclass(frozen=True, slots=True)
class RepositoryWriteResult:
    identity_key: str
    outcome: str

    @property
    def inserted(self) -> bool:
        return self.outcome == "INSERTED"


class PerformanceRepository:
    """Performance ledger access over the existing SqliteStore writer."""

    def __init__(self, store: SqliteStore | SqliteReadStore) -> None:
        if type(store) not in (SqliteStore, SqliteReadStore):
            raise ValueError("INVALID_PERFORMANCE_STORE")
        self._store = store
        self._connection = store._connection
        self._writable = type(store) is SqliteStore

    def _require_writer(self) -> None:
        if not self._writable:
            raise ValueError("PERFORMANCE_REPOSITORY_READ_ONLY")

    def append_observation(self, observation: PerformanceObservation) -> RepositoryWriteResult:
        self._require_writer()
        if type(observation) is not PerformanceObservation:
            raise ValueError("INVALID_PERFORMANCE_OBSERVATION_TYPE")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            result = self._append_observation_uncommitted(observation)
            self._connection.commit()
            return result
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def append_observations_and_advance_cursor(
        self,
        observations: Sequence[PerformanceObservation],
        cursor: PerformanceCursor,
    ) -> list[RepositoryWriteResult]:
        self._require_writer()
        if type(observations) not in (list, tuple) or any(
            type(observation) is not PerformanceObservation for observation in observations
        ):
            raise ValueError("INVALID_PERFORMANCE_OBSERVATIONS")
        if type(cursor) is not PerformanceCursor:
            raise ValueError("INVALID_PERFORMANCE_CURSOR_TYPE")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            results = [
                self._append_observation_uncommitted(observation)
                for observation in observations
            ]
            self._advance_cursor_uncommitted(cursor)
            self._connection.commit()
            return results
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def append_observation_batch(
        self, observations: Sequence[PerformanceObservation]
    ) -> list[RepositoryWriteResult]:
        """Persist a complete immutable source batch in one writer transaction."""
        self._require_writer()
        if type(observations) not in (list, tuple) or any(
            type(row) is not PerformanceObservation for row in observations
        ):
            raise ValueError("INVALID_PERFORMANCE_OBSERVATIONS")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            results = [self._append_observation_uncommitted(row) for row in observations]
            self._connection.commit()
            return results
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def _append_observation_uncommitted(
        self, observation: PerformanceObservation
    ) -> RepositoryWriteResult:
        stored = self._connection.execute(
            """
            SELECT payload_json, payload_sha256
            FROM strategy_performance_observations
            WHERE observation_key = ?
            """,
            (observation.observation_key,),
        ).fetchone()
        if stored is not None:
            if tuple(stored) != (observation.payload_json, observation.payload_sha256):
                raise ValueError("PERFORMANCE_OBSERVATION_CONFLICT")
            return RepositoryWriteResult(observation.observation_key, "REPLAYED")

        if observation.emitted:
            signal = self._connection.execute(
                """
                SELECT evaluation_key, strategy_id
                FROM signals WHERE identity_key = ?
                """,
                (observation.signal_identity_key,),
            ).fetchone()
            if signal is None:
                raise ValueError("PERFORMANCE_EMITTED_SIGNAL_MISSING")
            if tuple(signal) != (
                observation.evaluation_key,
                observation.strategy_id,
            ):
                raise ValueError("PERFORMANCE_EMITTED_SIGNAL_CONFLICT")

        collision = self._connection.execute(
            """
            SELECT observation_key FROM strategy_performance_observations
            WHERE (source_layer = ? AND logical_decision_key = ?)
               OR (? IS NOT NULL AND signal_identity_key = ?)
            LIMIT 1
            """,
            (
                observation.source_layer,
                observation.logical_decision_key,
                observation.signal_identity_key,
                observation.signal_identity_key,
            ),
        ).fetchone()
        if collision is not None:
            raise ValueError("PERFORMANCE_OBSERVATION_CONFLICT")
        try:
            self._connection.execute(
                """
                INSERT INTO strategy_performance_observations(
                    observation_key, logical_decision_key, decision_semantic_sha256,
                    schema_version, source_layer, strategy_id, strategy_version,
                    family, registry_index, activation_status, activation_reason_code,
                    market_id, market_date, evaluation_key, signal_identity_key,
                    parent_strategy_id, source_decision_identity, checkpoint_minutes,
                    horizon, side, selected_buckets_json,
                    selected_bucket_identity_sha256, accepted, emitted, reason_code,
                    reference_price_micros, performance_price_micros,
                    performance_price_basis, shares_micros, scoring_status,
                    scoring_reason_code, observed_at_ms, source_created_at_ms,
                    provenance_run_id, source_result_sha256, input_sha256,
                    payload_json, payload_sha256
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    observation.observation_key,
                    observation.logical_decision_key,
                    observation.decision_semantic_sha256,
                    observation.schema_version,
                    observation.source_layer,
                    observation.strategy_id,
                    observation.strategy_version,
                    observation.family,
                    observation.registry_index,
                    observation.activation_status,
                    observation.activation_reason_code,
                    observation.market_id,
                    observation.market_date,
                    observation.evaluation_key,
                    observation.signal_identity_key,
                    observation.parent_strategy_id,
                    observation.source_decision_identity,
                    observation.checkpoint_minutes,
                    observation.horizon,
                    observation.side,
                    observation.selected_buckets_json,
                    observation.selected_bucket_identity_sha256,
                    int(observation.accepted),
                    int(observation.emitted),
                    observation.reason_code,
                    observation.reference_price_micros,
                    observation.performance_price_micros,
                    observation.performance_price_basis,
                    observation.shares_micros,
                    observation.scoring_status,
                    observation.scoring_reason_code,
                    observation.observed_at_ms,
                    observation.source_created_at_ms,
                    observation.provenance_run_id,
                    observation.source_result_sha256,
                    observation.input_sha256,
                    observation.payload_json,
                    observation.payload_sha256,
                ),
            )
        except sqlite3.IntegrityError:
            raise ValueError("PERFORMANCE_OBSERVATION_CONFLICT") from None
        return RepositoryWriteResult(observation.observation_key, "INSERTED")

    def read_observations(
        self,
        *,
        strategy_id: str | None = None,
        source_layer: str | None = None,
    ) -> list[PerformanceObservation]:
        clauses: list[str] = []
        parameters: list[object] = []
        if strategy_id is not None:
            clauses.append("strategy_id = ?")
            parameters.append(strategy_id)
        if source_layer is not None:
            if source_layer not in {"HISTORICAL", "FORWARD"}:
                raise ValueError("INVALID_PERFORMANCE_SOURCE_LAYER")
            clauses.append("source_layer = ?")
            parameters.append(source_layer)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"""
            SELECT payload_json
            FROM strategy_performance_observations
            {where}
            ORDER BY strategy_id ASC,
                CASE source_layer WHEN 'HISTORICAL' THEN 0 ELSE 1 END ASC,
                market_date ASC, checkpoint_minutes DESC, observation_key ASC
            """,
            tuple(parameters),
        ).fetchall()
        return [_observation_from_payload(row[0]) for row in rows]

    def append_resolution(self, resolution: PerformanceResolution) -> RepositoryWriteResult:
        self._require_writer()
        if type(resolution) is not PerformanceResolution:
            raise ValueError("INVALID_PERFORMANCE_RESOLUTION_TYPE")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            result = self._append_resolution_uncommitted(resolution)
            self._connection.commit()
            return result
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def _append_resolution_uncommitted(
        self, resolution: PerformanceResolution
    ) -> RepositoryWriteResult:
        stored = self._connection.execute(
            """
            SELECT payload_json, payload_sha256
            FROM strategy_performance_resolutions WHERE resolution_key = ?
            """,
            (resolution.resolution_key,),
        ).fetchone()
        if stored is not None:
            if tuple(stored) != (resolution.payload_json, resolution.payload_sha256):
                raise ValueError("PERFORMANCE_RESOLUTION_CONFLICT")
            return RepositoryWriteResult(resolution.resolution_key, "REPLAYED")

        observation_row = self._connection.execute(
            """
            SELECT accepted, performance_price_micros, scoring_status
            FROM strategy_performance_observations WHERE observation_key = ?
            """,
            (resolution.observation_key,),
        ).fetchone()
        if observation_row is None:
            raise ValueError("PERFORMANCE_RESOLUTION_OBSERVATION_MISSING")
        if (
            observation_row[0] != 1
            or observation_row[1] is None
            or observation_row[1] <= 0
            or observation_row[2] != "RESOLUTION_PENDING"
        ):
            raise ValueError("PERFORMANCE_RESOLUTION_OBSERVATION_UNSCORABLE")
        expected_economics = five_share_economics(observation_row[1], won=resolution.won)
        if any(
            getattr(resolution, name) != expected
            for name, expected in expected_economics.items()
        ):
            raise ValueError("PERFORMANCE_RESOLUTION_ECONOMICS_CONFLICT")

        previous = self._connection.execute(
            """
            SELECT resolution_key, revision
            FROM strategy_performance_resolutions
            WHERE observation_key = ?
              AND resolution_key NOT IN (
                  SELECT supersedes_resolution_key
                  FROM strategy_performance_resolutions
                  WHERE supersedes_resolution_key IS NOT NULL
              )
            """,
            (resolution.observation_key,),
        ).fetchall()
        if not previous:
            sequence_valid = (
                resolution.revision == 1
                and resolution.supersedes_resolution_key is None
            )
        else:
            sequence_valid = (
                len(previous) == 1
                and resolution.revision == previous[0][1] + 1
                and resolution.supersedes_resolution_key == previous[0][0]
            )
        if not sequence_valid:
            raise ValueError("PERFORMANCE_RESOLUTION_SEQUENCE_CONFLICT")
        try:
            self._connection.execute(
                """
                INSERT INTO strategy_performance_resolutions(
                    resolution_key, observation_key, revision,
                    supersedes_resolution_key, settlement_identity,
                    settlement_source_event_id, winning_bucket_identity, won,
                    shares_micros, cost_usd_micros, gross_payout_usd_micros,
                    pnl_usd_micros, turnover_usd_micros, resolution_date,
                    resolved_at_ms, provenance_json, payload_json, payload_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolution.resolution_key,
                    resolution.observation_key,
                    resolution.revision,
                    resolution.supersedes_resolution_key,
                    resolution.settlement_identity,
                    resolution.settlement_source_event_id,
                    resolution.winning_bucket_identity,
                    int(resolution.won),
                    resolution.shares_micros,
                    resolution.cost_usd_micros,
                    resolution.gross_payout_usd_micros,
                    resolution.pnl_usd_micros,
                    resolution.turnover_usd_micros,
                    resolution.resolution_date,
                    resolution.resolved_at_ms,
                    resolution.provenance_json,
                    resolution.payload_json,
                    resolution.payload_sha256,
                ),
            )
        except sqlite3.IntegrityError:
            raise ValueError("PERFORMANCE_RESOLUTION_CONFLICT") from None
        return RepositoryWriteResult(resolution.resolution_key, "INSERTED")

    def read_effective_resolutions(self) -> dict[str, PerformanceResolution]:
        rows = self._connection.execute(
            """
            SELECT resolution.payload_json
            FROM strategy_performance_resolutions AS resolution
            WHERE NOT EXISTS (
                SELECT 1 FROM strategy_performance_resolutions AS successor
                WHERE successor.supersedes_resolution_key = resolution.resolution_key
            )
            ORDER BY resolution.observation_key ASC
            """
        ).fetchall()
        resolutions = [_resolution_from_payload(row[0]) for row in rows]
        if len({resolution.observation_key for resolution in resolutions}) != len(resolutions):
            raise ValueError("MULTIPLE_EFFECTIVE_PERFORMANCE_RESOLUTIONS")
        return {resolution.observation_key: resolution for resolution in resolutions}

    def append_ingest_run(self, ingest: PerformanceIngestRun) -> RepositoryWriteResult:
        self._require_writer()
        if type(ingest) is not PerformanceIngestRun:
            raise ValueError("INVALID_PERFORMANCE_INGEST_RUN_TYPE")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            stored = self._connection.execute(
                "SELECT payload_json, payload_sha256 FROM strategy_performance_ingest_runs WHERE ingest_run_key = ?",
                (ingest.ingest_run_key,),
            ).fetchone()
            if stored is not None:
                if tuple(stored) != (ingest.payload_json, ingest.payload_sha256):
                    raise ValueError("PERFORMANCE_INGEST_RUN_CONFLICT")
                self._connection.commit()
                return RepositoryWriteResult(ingest.ingest_run_key, "REPLAYED")
            self._connection.execute(
                """
                INSERT INTO strategy_performance_ingest_runs(
                    ingest_run_key, schema_version, mode, source_artifact_class,
                    source_sha256, acceptance_sha256, source_schema_version,
                    input_count, inserted_count, replayed_count, rejected_count,
                    accepted_count, unscorable_count, conflict_count,
                    first_source_identity, last_source_identity, first_cursor_json,
                    last_cursor_json, status, started_at_ms, completed_at_ms,
                    payload_json, payload_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ingest.ingest_run_key, ingest.schema_version, ingest.mode,
                    ingest.source_artifact_class, ingest.source_sha256,
                    ingest.acceptance_sha256, ingest.source_schema_version,
                    ingest.input_count, ingest.inserted_count, ingest.replayed_count,
                    ingest.rejected_count, ingest.accepted_count,
                    ingest.unscorable_count, ingest.conflict_count,
                    ingest.first_source_identity, ingest.last_source_identity,
                    _optional_json(ingest.first_cursor_json),
                    _optional_json(ingest.last_cursor_json), ingest.status,
                    ingest.started_at_ms, ingest.completed_at_ms,
                    ingest.payload_json, ingest.payload_sha256,
                ),
            )
            self._connection.commit()
            return RepositoryWriteResult(ingest.ingest_run_key, "INSERTED")
        except sqlite3.IntegrityError:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise ValueError("PERFORMANCE_INGEST_RUN_CONFLICT") from None
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def append_resolutions_and_ingest_run(
        self,
        resolutions: Sequence[PerformanceResolution],
        ingest: PerformanceIngestRun,
    ) -> tuple[list[RepositoryWriteResult], RepositoryWriteResult]:
        """Complete reconciliation and its ingest receipt atomically."""
        self._require_writer()
        if type(resolutions) not in (list, tuple) or any(
            type(row) is not PerformanceResolution for row in resolutions
        ):
            raise ValueError("INVALID_PERFORMANCE_RESOLUTIONS")
        if type(ingest) is not PerformanceIngestRun:
            raise ValueError("INVALID_PERFORMANCE_INGEST_RUN_TYPE")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            resolution_results = [
                self._append_resolution_uncommitted(row) for row in resolutions
            ]
            stored = self._connection.execute(
                "SELECT payload_json, payload_sha256 FROM strategy_performance_ingest_runs WHERE ingest_run_key = ?",
                (ingest.ingest_run_key,),
            ).fetchone()
            if stored is not None:
                if tuple(stored) != (ingest.payload_json, ingest.payload_sha256):
                    raise ValueError("PERFORMANCE_INGEST_RUN_CONFLICT")
                ingest_result = RepositoryWriteResult(ingest.ingest_run_key, "REPLAYED")
            else:
                self._insert_ingest_run_uncommitted(ingest)
                ingest_result = RepositoryWriteResult(ingest.ingest_run_key, "INSERTED")
            self._connection.commit()
            return resolution_results, ingest_result
        except sqlite3.IntegrityError:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise ValueError("PERFORMANCE_INGEST_RUN_CONFLICT") from None
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def _insert_ingest_run_uncommitted(self, ingest: PerformanceIngestRun) -> None:
        self._connection.execute(
            """
            INSERT INTO strategy_performance_ingest_runs(
                ingest_run_key, schema_version, mode, source_artifact_class,
                source_sha256, acceptance_sha256, source_schema_version,
                input_count, inserted_count, replayed_count, rejected_count,
                accepted_count, unscorable_count, conflict_count,
                first_source_identity, last_source_identity, first_cursor_json,
                last_cursor_json, status, started_at_ms, completed_at_ms,
                payload_json, payload_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ingest.ingest_run_key, ingest.schema_version, ingest.mode,
                ingest.source_artifact_class, ingest.source_sha256,
                ingest.acceptance_sha256, ingest.source_schema_version,
                ingest.input_count, ingest.inserted_count, ingest.replayed_count,
                ingest.rejected_count, ingest.accepted_count,
                ingest.unscorable_count, ingest.conflict_count,
                ingest.first_source_identity, ingest.last_source_identity,
                _optional_json(ingest.first_cursor_json),
                _optional_json(ingest.last_cursor_json), ingest.status,
                ingest.started_at_ms, ingest.completed_at_ms,
                ingest.payload_json, ingest.payload_sha256,
            ),
        )

    def append_catchup_classification(
        self, classification: CatchupClassification
    ) -> RepositoryWriteResult:
        self._require_writer()
        if type(classification) is not CatchupClassification:
            raise ValueError("INVALID_PERFORMANCE_CATCHUP_TYPE")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            stored = self._connection.execute(
                "SELECT payload_json, payload_sha256 FROM strategy_performance_catchup WHERE catchup_key = ?",
                (classification.catchup_key,),
            ).fetchone()
            if stored is not None:
                if tuple(stored) != (classification.payload_json, classification.payload_sha256):
                    raise ValueError("PERFORMANCE_CATCHUP_CONFLICT")
                self._connection.commit()
                return RepositoryWriteResult(classification.catchup_key, "REPLAYED")
            previous = self._connection.execute(
                """
                SELECT current.catchup_key, current.revision
                FROM strategy_performance_catchup AS current
                WHERE current.market_date = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM strategy_performance_catchup AS successor
                      WHERE successor.supersedes_catchup_key = current.catchup_key
                  )
                """,
                (classification.market_date,),
            ).fetchall()
            if not previous:
                sequence_valid = (
                    classification.revision == 1
                    and classification.supersedes_catchup_key is None
                )
            else:
                sequence_valid = (
                    len(previous) == 1
                    and classification.revision == previous[0][1] + 1
                    and classification.supersedes_catchup_key == previous[0][0]
                )
            if not sequence_valid:
                raise ValueError("PERFORMANCE_CATCHUP_SEQUENCE_CONFLICT")
            self._connection.execute(
                """
                INSERT INTO strategy_performance_catchup(
                    catchup_key, schema_version, revision,
                    supersedes_catchup_key, market_date, classification,
                    reason_code, market_id, source_event_identity,
                    classified_at_ms, provenance_json, payload_json,
                    payload_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    classification.catchup_key, classification.schema_version,
                    classification.revision,
                    classification.supersedes_catchup_key,
                    classification.market_date, classification.classification,
                    classification.reason_code, classification.market_id,
                    classification.source_event_identity, classification.classified_at_ms,
                    classification.provenance_json, classification.payload_json,
                    classification.payload_sha256,
                ),
            )
            self._connection.commit()
            return RepositoryWriteResult(classification.catchup_key, "INSERTED")
        except sqlite3.IntegrityError:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise ValueError("PERFORMANCE_CATCHUP_CONFLICT") from None
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def read_ingest_runs(self) -> list[PerformanceIngestRun]:
        rows = self._connection.execute(
            """
            SELECT payload_json FROM strategy_performance_ingest_runs
            ORDER BY started_at_ms ASC, ingest_run_key ASC
            """
        ).fetchall()
        return [PerformanceIngestRun.create(**json.loads(row[0])) for row in rows]

    def read_catchup_classifications(self) -> list[CatchupClassification]:
        rows = self._connection.execute(
            """
            SELECT payload_json FROM strategy_performance_catchup
            ORDER BY market_date ASC, revision ASC, catchup_key ASC
            """
        ).fetchall()
        values: list[CatchupClassification] = []
        for row in rows:
            payload = json.loads(row[0])
            values.append(CatchupClassification.create(**payload))
        return values

    def read_effective_catchup_classifications(
        self,
    ) -> list[CatchupClassification]:
        rows = self._connection.execute(
            """
            SELECT current.payload_json
            FROM strategy_performance_catchup AS current
            WHERE NOT EXISTS (
                SELECT 1 FROM strategy_performance_catchup AS successor
                WHERE successor.supersedes_catchup_key = current.catchup_key
            )
            ORDER BY current.market_date ASC, current.catchup_key ASC
            """
        ).fetchall()
        return [
            CatchupClassification.create(**json.loads(row[0]))
            for row in rows
        ]

    def read_cursor(self, cursor_name: str) -> PerformanceCursor | None:
        if type(cursor_name) is not str or not cursor_name:
            raise ValueError("INVALID_PERFORMANCE_CURSOR_NAME")
        row = self._connection.execute(
            """
            SELECT cursor_json, updated_at_ms
            FROM strategy_performance_cursors WHERE cursor_name = ?
            """,
            (cursor_name,),
        ).fetchone()
        if row is None:
            return None
        return PerformanceCursor.create(
            cursor_name=cursor_name,
            cursor=json.loads(row[0]),
            updated_at_ms=row[1],
        )

    def _advance_cursor_uncommitted(self, cursor: PerformanceCursor) -> bool:
        stored = self._connection.execute(
            """
            SELECT cursor_json, cursor_sha256, updated_at_ms
            FROM strategy_performance_cursors WHERE cursor_name = ?
            """,
            (cursor.cursor_name,),
        ).fetchone()
        if stored is None:
            self._connection.execute(
                """
                INSERT INTO strategy_performance_cursors(
                    cursor_name, schema_version, cursor_json, cursor_sha256, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    cursor.cursor_name, cursor.schema_version, cursor.cursor_json,
                    cursor.cursor_sha256, cursor.updated_at_ms,
                ),
            )
            return True
        stored_json, stored_sha256, stored_timestamp = stored
        if cursor.updated_at_ms < stored_timestamp:
            raise ValueError("PERFORMANCE_CURSOR_REGRESSION")
        if cursor.updated_at_ms == stored_timestamp:
            if (cursor.cursor_json, cursor.cursor_sha256) != (stored_json, stored_sha256):
                raise ValueError("PERFORMANCE_CURSOR_CONFLICT")
            return False
        self._connection.execute(
            """
            UPDATE strategy_performance_cursors
            SET schema_version = ?, cursor_json = ?, cursor_sha256 = ?, updated_at_ms = ?
            WHERE cursor_name = ?
            """,
            (
                cursor.schema_version, cursor.cursor_json, cursor.cursor_sha256,
                cursor.updated_at_ms, cursor.cursor_name,
            ),
        )
        return True

    def publish_materialization(
        self,
        revision: AggregateRevision,
        aggregates: Sequence[AggregateRow],
        timeseries: Sequence[TimeseriesRow],
    ) -> RepositoryWriteResult:
        return self.publish_materialization_batch(
            [(revision, aggregates, timeseries)]
        )[0]

    def publish_materialization_batch(
        self,
        materializations: Sequence[
            tuple[AggregateRevision, Sequence[AggregateRow], Sequence[TimeseriesRow]]
        ],
    ) -> list[RepositoryWriteResult]:
        """Publish a complete multi-view rebuild with one all-or-nothing cutover."""
        self._require_writer()
        if type(materializations) not in (list, tuple) or not materializations:
            raise ValueError("INVALID_PERFORMANCE_MATERIALIZATION_BATCH")
        seen_scopes: set[tuple[str, str, str]] = set()
        for revision, aggregates, timeseries in materializations:
            self._validate_materialization(revision, aggregates, timeseries)
            scope = (
                revision.strategy_id,
                revision.source_view,
                revision.calculation_version,
            )
            if scope in seen_scopes:
                raise ValueError("PERFORMANCE_MATERIALIZATION_BATCH_DUPLICATE_SCOPE")
            seen_scopes.add(scope)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            results = [
                self._publish_materialization_uncommitted(revision, aggregates, timeseries)
                for revision, aggregates, timeseries in materializations
            ]
            self._connection.commit()
            return results
        except sqlite3.IntegrityError:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise ValueError("PERFORMANCE_MATERIALIZATION_CONFLICT") from None
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    @staticmethod
    def _validate_materialization(
        revision: AggregateRevision,
        aggregates: Sequence[AggregateRow],
        timeseries: Sequence[TimeseriesRow],
    ) -> None:
        if type(revision) is not AggregateRevision:
            raise ValueError("INVALID_MATERIALIZATION_REVISION_TYPE")
        if type(aggregates) not in (list, tuple) or any(type(row) is not AggregateRow for row in aggregates):
            raise ValueError("INVALID_MATERIALIZATION_AGGREGATES")
        if type(timeseries) not in (list, tuple) or any(type(row) is not TimeseriesRow for row in timeseries):
            raise ValueError("INVALID_MATERIALIZATION_TIMESERIES")
        for row in [*aggregates, *timeseries]:
            if (
                row.revision_key != revision.revision_key
                or row.strategy_id != revision.strategy_id
                or row.source_view != revision.source_view
                or row.calculation_version != revision.calculation_version
            ):
                raise ValueError("PERFORMANCE_MATERIALIZATION_SCOPE_CONFLICT")
        if (
            len(aggregates) != revision.aggregate_count
            or len(timeseries) != revision.timeseries_count
            or sum(
                row.window_kind == "ALL" and row.window_key == "ALL"
                for row in aggregates
            ) != 1
            or materialization_children_sha256(aggregates, timeseries)
            != revision.children_sha256
        ):
            raise ValueError("PERFORMANCE_MATERIALIZATION_INCOMPLETE")

    def _publish_materialization_uncommitted(
        self,
        revision: AggregateRevision,
        aggregates: Sequence[AggregateRow],
        timeseries: Sequence[TimeseriesRow],
    ) -> RepositoryWriteResult:
        stored = self._connection.execute(
            "SELECT payload_json, payload_sha256 FROM strategy_performance_materialization_revisions WHERE revision_key = ?",
            (revision.revision_key,),
        ).fetchone()
        if stored is not None:
            if (
                tuple(stored) != (revision.payload_json, revision.payload_sha256)
                or not self._stored_materialization_matches(
                    revision.revision_key, aggregates, timeseries
                )
            ):
                raise ValueError("PERFORMANCE_MATERIALIZATION_CONFLICT")
            return RepositoryWriteResult(revision.revision_key, "REPLAYED")
        self._connection.execute(
                """
                INSERT INTO strategy_performance_materialization_revisions(
                    revision_key, schema_version, strategy_id, source_view,
                    calculation_version, source_ledger_revision,
                    source_ledger_sha256, generated_at_ms, aggregate_count,
                    timeseries_count, children_sha256, status, is_current,
                    payload_json, payload_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'COMPLETE', 0, ?, ?)
                """,
                (
                    revision.revision_key, revision.schema_version,
                    revision.strategy_id, revision.source_view,
                    revision.calculation_version, revision.source_ledger_revision,
                    revision.source_ledger_sha256, revision.generated_at_ms,
                    revision.aggregate_count, revision.timeseries_count,
                    revision.children_sha256,
                    revision.payload_json, revision.payload_sha256,
                ),
            )
        for row in aggregates:
            self._connection.execute(
                    """
                    INSERT INTO strategy_performance_aggregates(
                        aggregate_key, schema_version, revision_key, strategy_id,
                        source_view, window_kind, window_key, calculation_version,
                        payload_json, payload_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row.aggregate_key, row.schema_version, row.revision_key,
                        row.strategy_id, row.source_view, row.window_kind,
                        row.window_key, row.calculation_version, row.payload_json,
                        row.payload_sha256,
                    ),
                )
        for row in timeseries:
            self._connection.execute(
                    """
                    INSERT INTO strategy_performance_timeseries(
                        timeseries_key, schema_version, revision_key, strategy_id,
                        source_view, series_kind, period_key, calculation_version,
                        observation_count, observation_sha256, payload_json,
                        payload_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row.timeseries_key, row.schema_version, row.revision_key,
                        row.strategy_id, row.source_view, row.series_kind,
                        row.period_key, row.calculation_version,
                        row.observation_count, row.observation_sha256,
                        row.payload_json, row.payload_sha256,
                    ),
                )
        self._connection.execute(
                """
                UPDATE strategy_performance_materialization_revisions
                SET is_current = 0
                WHERE strategy_id = ? AND source_view = ?
                  AND calculation_version = ? AND is_current = 1
                """,
                (revision.strategy_id, revision.source_view, revision.calculation_version),
            )
        self._connection.execute(
            "UPDATE strategy_performance_materialization_revisions SET is_current = 1 WHERE revision_key = ?",
            (revision.revision_key,),
        )
        return RepositoryWriteResult(revision.revision_key, "INSERTED")

    def _stored_materialization_matches(
        self,
        revision_key: str,
        aggregates: Sequence[AggregateRow],
        timeseries: Sequence[TimeseriesRow],
    ) -> bool:
        stored_aggregates = sorted(
            self._connection.execute(
                """
                SELECT aggregate_key, schema_version, revision_key, strategy_id,
                       source_view, window_kind, window_key, calculation_version,
                       payload_json, payload_sha256
                FROM strategy_performance_aggregates WHERE revision_key = ?
                """,
                (revision_key,),
            ).fetchall()
        )
        expected_aggregates = sorted(
            (
                row.aggregate_key, row.schema_version, row.revision_key,
                row.strategy_id, row.source_view, row.window_kind, row.window_key,
                row.calculation_version, row.payload_json, row.payload_sha256,
            )
            for row in aggregates
        )
        stored_timeseries = sorted(
            self._connection.execute(
                """
                SELECT timeseries_key, schema_version, revision_key, strategy_id,
                       source_view, series_kind, period_key, calculation_version,
                       observation_count, observation_sha256, payload_json,
                       payload_sha256
                FROM strategy_performance_timeseries WHERE revision_key = ?
                """,
                (revision_key,),
            ).fetchall()
        )
        expected_timeseries = sorted(
            (
                row.timeseries_key, row.schema_version, row.revision_key,
                row.strategy_id, row.source_view, row.series_kind, row.period_key,
                row.calculation_version, row.observation_count,
                row.observation_sha256, row.payload_json, row.payload_sha256,
            )
            for row in timeseries
        )
        return (
            stored_aggregates == expected_aggregates
            and stored_timeseries == expected_timeseries
        )

    def read_current_materialization(
        self,
        *,
        strategy_id: str,
        source_view: str,
        calculation_version: str,
    ) -> dict[str, object] | None:
        row = self._connection.execute(
            """
            SELECT revision_key, payload_json
            FROM strategy_performance_materialization_revisions
            WHERE strategy_id = ? AND source_view = ?
              AND calculation_version = ? AND status = 'COMPLETE' AND is_current = 1
            """,
            (strategy_id, source_view, calculation_version),
        ).fetchone()
        if row is None:
            return None
        revision = _aggregate_revision_from_payload(row[1])
        aggregate_rows = self._connection.execute(
            """
            SELECT aggregate_key, schema_version, revision_key, strategy_id,
                   source_view, window_kind, window_key, calculation_version,
                   payload_json
            FROM strategy_performance_aggregates
            WHERE revision_key = ? ORDER BY window_kind ASC, window_key ASC
            """,
            (row[0],),
        ).fetchall()
        timeseries_rows = self._connection.execute(
            """
            SELECT timeseries_key, schema_version, revision_key, strategy_id,
                   source_view, series_kind, period_key, calculation_version,
                   observation_count, observation_sha256, payload_json
            FROM strategy_performance_timeseries
            WHERE revision_key = ? ORDER BY series_kind ASC, period_key ASC
            """,
            (row[0],),
        ).fetchall()
        return {
            "revision": revision,
            "aggregates": [
                AggregateRow.create(
                    aggregate_key=value[0], schema_version=value[1],
                    revision_key=value[2], strategy_id=value[3],
                    source_view=value[4], window_kind=value[5], window_key=value[6],
                    calculation_version=value[7], payload=json.loads(value[8]),
                )
                for value in aggregate_rows
            ],
            "timeseries": [
                TimeseriesRow.create(
                    timeseries_key=value[0], schema_version=value[1],
                    revision_key=value[2], strategy_id=value[3],
                    source_view=value[4], series_kind=value[5], period_key=value[6],
                    calculation_version=value[7], observation_count=value[8],
                    observation_sha256=value[9], payload=json.loads(value[10]),
                )
                for value in timeseries_rows
            ],
        }


def _observation_from_payload(payload_json: str) -> PerformanceObservation:
    payload = json.loads(payload_json)
    payload["selected_buckets"] = tuple(payload.pop("selected_buckets"))
    payload.pop("decision_semantic_sha256")
    payload.pop("selected_bucket_identity_sha256")
    return PerformanceObservation.create(**payload)


def _resolution_from_payload(payload_json: str) -> PerformanceResolution:
    payload = json.loads(payload_json)
    payload["provenance_json"] = canonical_json(payload.pop("provenance"))
    return PerformanceResolution(**payload)


def _aggregate_revision_from_payload(payload_json: str) -> AggregateRevision:
    payload = json.loads(payload_json)
    return AggregateRevision.create(**payload)


def _optional_json(value: object | None) -> str | None:
    return None if value is None else canonical_json(value)
