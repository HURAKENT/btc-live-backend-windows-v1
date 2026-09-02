from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from src.performance_historical import (
    HistoricalPerformanceObservationBuilder,
    HistoricalSettlementReconciler,
    PinnedAhrArtifactLoader,
)
from src.original_runtime_seed import OriginalRuntimeSeedLoader
from src.performance_metrics import build_metrics, effective_observations_for_view
from src.performance_models import (
    AggregateRevision,
    AggregateRow,
    PerformanceIngestRun,
    PerformanceObservation,
    PerformanceResolution,
    TimeseriesRow,
    canonical_identity,
    canonical_sha256,
    materialization_children_sha256,
)
from src.performance_repository import PerformanceRepository, RepositoryWriteResult


CALCULATION_VERSION = "PERFORMANCE_METRICS_V2"
SOURCE_VIEWS = ("HISTORICAL", "FORWARD", "COMBINED")


@dataclass(frozen=True, slots=True)
class HistoricalBootstrapResult:
    input_count: int
    observation_inserted: int
    observation_replayed: int
    resolution_inserted: int
    resolution_replayed: int
    ingest_receipt_outcome: str
    materialization_inserted: int
    materialization_replayed: int


PerformanceIngestReceipt = HistoricalBootstrapResult


class StrategyPerformanceEngine:
    def __init__(self, *, project_root: Path, repository: PerformanceRepository) -> None:
        self.project_root = Path(project_root).resolve()
        self.repository = repository

    def bootstrap_historical(
        self, *, as_of_date: date | str = "2026-08-15"
    ) -> HistoricalBootstrapResult:
        seed = OriginalRuntimeSeedLoader(project_root=self.project_root).load()
        self._validate_seed_schema(seed.expected_migration_version)
        observation_results = self.repository.append_observation_batch(
            seed.observations
        )
        return self._complete_historical_bootstrap(
            observations=seed.observations,
            resolutions=seed.resolutions,
            observation_results=observation_results,
            acceptance_sha256=seed.acceptance_sha256,
            result_artifact_sha256=seed.result_artifact_sha256,
            as_of_date=as_of_date,
        )

    def bootstrap_historical_from_ahr(
        self, *, as_of_date: date | str = "2026-08-15"
    ) -> HistoricalBootstrapResult:
        """Explicit research/parity path using all pinned AHR source datasets."""
        bundle = PinnedAhrArtifactLoader(project_root=self.project_root).load()
        observations = HistoricalPerformanceObservationBuilder(
            project_root=self.project_root, bundle=bundle
        ).build()
        observation_results = self.repository.append_observation_batch(observations)
        built_keys = {row.observation_key for row in observations}
        persisted = tuple(
            row for row in self.repository.read_observations(source_layer="HISTORICAL")
            if row.observation_key in built_keys
        )
        if len(persisted) != len(observations):
            raise ValueError("PERFORMANCE_HISTORICAL_PERSISTED_SCOPE_INCOMPLETE")
        resolutions = HistoricalSettlementReconciler(
            project_root=self.project_root, bundle=bundle
        ).reconcile(persisted)
        return self._complete_historical_bootstrap(
            observations=observations,
            resolutions=resolutions,
            observation_results=observation_results,
            acceptance_sha256=bundle.acceptance_sha256,
            result_artifact_sha256=bundle.result_artifact_sha256,
            as_of_date=as_of_date,
        )

    def _complete_historical_bootstrap(
        self,
        *,
        observations: Sequence[PerformanceObservation],
        resolutions: Sequence[PerformanceResolution],
        observation_results: Sequence[RepositoryWriteResult],
        acceptance_sha256: str,
        result_artifact_sha256: str,
        as_of_date: date | str,
    ) -> HistoricalBootstrapResult:
        first_key = observations[0].observation_key
        last_key = observations[-1].observation_key
        completed_at = _date_epoch_ms(max(row.market_date for row in observations))
        ingest = PerformanceIngestRun.create(
            ingest_run_key=canonical_identity(
                "performance-ingest-historical",
                {"acceptance_sha256": acceptance_sha256,
                 "results_sha256": result_artifact_sha256},
            ),
            mode="HISTORICAL_BOOTSTRAP",
            source_artifact_class="PINNED_AHR_STRATEGY_RESULTS",
            source_sha256=result_artifact_sha256,
            acceptance_sha256=acceptance_sha256,
            source_schema_version="AHR_1_RESULT_RECORD_V1_V2",
            input_count=len(observations), inserted_count=len(observations),
            replayed_count=0,
            rejected_count=sum(not row.accepted for row in observations),
            accepted_count=sum(row.accepted for row in observations),
            unscorable_count=sum(row.scoring_status == "UNSCORABLE" for row in observations),
            conflict_count=0, first_source_identity=first_key,
            last_source_identity=last_key, first_cursor_json=None,
            last_cursor_json={"market_date": max(row.market_date for row in observations),
                              "result_count": len(observations)},
            status="COMPLETE", started_at_ms=completed_at,
            completed_at_ms=completed_at,
        )
        resolution_results, receipt = self.repository.append_resolutions_and_ingest_run(
            resolutions, ingest
        )
        materializations = self.refresh_all(as_of_date=as_of_date)
        return HistoricalBootstrapResult(
            input_count=len(observations),
            observation_inserted=sum(row.inserted for row in observation_results),
            observation_replayed=sum(not row.inserted for row in observation_results),
            resolution_inserted=sum(row.inserted for row in resolution_results),
            resolution_replayed=sum(not row.inserted for row in resolution_results),
            ingest_receipt_outcome=receipt.outcome,
            materialization_inserted=materializations["inserted"],
            materialization_replayed=materializations["replayed"],
        )

    def _validate_seed_schema(self, expected_migration_version: int) -> None:
        actual = self.repository._connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
        if actual != expected_migration_version:
            raise ValueError(
                f"ORIGINAL_RUNTIME_SEED_SCHEMA_INCOMPATIBLE:{actual}:"
                f"{expected_migration_version}"
            )

    def refresh_all(self, *, as_of_date: date | str) -> dict[str, int]:
        observations = self.repository.read_observations()
        resolutions = self.repository.read_effective_resolutions()
        strategy_ids = tuple(sorted({row.strategy_id for row in observations}))
        materializations = []
        for strategy_id in strategy_ids:
            strategy_observations = [
                row for row in observations if row.strategy_id == strategy_id
            ]
            for source_view in SOURCE_VIEWS:
                materializations.append(self._build_one_materialization(
                    strategy_id=strategy_id,
                    source_view=source_view,
                    observations=strategy_observations,
                    resolutions=resolutions,
                    as_of_date=as_of_date,
                ))
        results = self.repository.publish_materialization_batch(materializations)
        outcomes = {"inserted": 0, "replayed": 0}
        for result in results:
            outcomes[result.outcome.lower()] += 1
        return outcomes

    def _build_one_materialization(
        self, *, strategy_id: str, source_view: str,
        observations: Sequence[PerformanceObservation],
        resolutions: Mapping[str, PerformanceResolution], as_of_date: date | str,
    ):
        if source_view == "COMBINED":
            ledger_observations = list(observations)
        else:
            ledger_observations = [
                row for row in observations if row.source_layer == source_view
            ]
        effective_observations = effective_observations_for_view(
            ledger_observations, source_view=source_view
        )
        observation_keys = {item.observation_key for item in ledger_observations}
        scoped_resolutions = {
            key: value for key, value in resolutions.items() if key in observation_keys
        }
        metrics = build_metrics(
            ledger_observations, scoped_resolutions,
            source_view=source_view, as_of_date=as_of_date
        )
        ledger_rows = sorted(
            [(row.observation_key, row.payload_sha256) for row in ledger_observations]
            + [(row.resolution_key, row.payload_sha256)
               for row in scoped_resolutions.values()]
        )
        ledger_sha = canonical_sha256(ledger_rows)
        materialization_input_sha = canonical_sha256(
            {"as_of_date": str(as_of_date), "source_ledger_sha256": ledger_sha}
        )
        ledger_revision = int(materialization_input_sha[:15], 16)
        revision_key = canonical_identity(
            "performance-materialization",
            {"calculation_version": CALCULATION_VERSION,
             "source_ledger_sha256": ledger_sha,
             "source_view": source_view, "strategy_id": strategy_id,
             "as_of_date": str(as_of_date)},
        )
        scalar_payload = {key: value for key, value in metrics.items()
                          if key not in {"cumulative_series", "monthly_series", "rolling_series"}}
        aggregates = [AggregateRow.create(
            aggregate_key=canonical_identity("performance-aggregate", [revision_key, "ALL", "ALL"]),
            revision_key=revision_key, strategy_id=strategy_id,
            source_view=source_view, window_kind="ALL", window_key="ALL",
            calculation_version=CALCULATION_VERSION, payload=scalar_payload,
        )]
        timeseries = self._timeseries(
            revision_key=revision_key, strategy_id=strategy_id,
            source_view=source_view, metrics=metrics,
            effective_observations=effective_observations,
            resolutions=scoped_resolutions,
        )
        revision = AggregateRevision.create(
            revision_key=revision_key, strategy_id=strategy_id,
            source_view=source_view, calculation_version=CALCULATION_VERSION,
            source_ledger_revision=ledger_revision, source_ledger_sha256=ledger_sha,
            generated_at_ms=_date_epoch_ms(str(as_of_date)),
            provenance={"as_of_date": str(as_of_date),
                        "calculation_contract": CALCULATION_VERSION},
            aggregate_count=len(aggregates), timeseries_count=len(timeseries),
            children_sha256=materialization_children_sha256(aggregates, timeseries),
        )
        return (revision, aggregates, timeseries)

    @staticmethod
    def _timeseries(*, revision_key: str, strategy_id: str,
                    source_view: str, metrics: Mapping[str, object],
                    effective_observations: Sequence[PerformanceObservation],
                    resolutions: Mapping[str, PerformanceResolution]) -> list[TimeseriesRow]:
        groups: list[tuple[str, Iterable[Mapping[str, object]], str]] = [
            ("CUMULATIVE", metrics["cumulative_series"], "observation_key"),
            ("MONTHLY", metrics["monthly_series"], "month"),
        ]
        rolling = metrics["rolling_series"]
        assert isinstance(rolling, Mapping)
        for label in ("30D", "90D", "365D"):
            groups.append((f"ROLLING_{label}", rolling[label], "observation_key"))
        rows: list[TimeseriesRow] = []
        resolved = [
            row for row in effective_observations if row.observation_key in resolutions
        ]
        for series_kind, points, period_field in groups:
            for point_index, point in enumerate(points):
                period_key = str(point[period_field])
                membership = _timeseries_membership(
                    series_kind, point, point_index, resolved
                )
                rows.append(TimeseriesRow.create(
                    timeseries_key=canonical_identity(
                        "performance-timeseries", [revision_key, series_kind, period_key]
                    ),
                    revision_key=revision_key, strategy_id=strategy_id,
                    source_view=source_view, series_kind=series_kind,
                    period_key=period_key, calculation_version=CALCULATION_VERSION,
                    observation_count=len(membership),
                    observation_sha256=canonical_sha256(membership), payload=point,
                ))
        return rows


def bootstrap_historical_performance(
    *, project_root: Path, repository: PerformanceRepository,
    as_of_date: date | str,
) -> PerformanceIngestReceipt:
    return StrategyPerformanceEngine(
        project_root=project_root, repository=repository
    ).bootstrap_historical(as_of_date=as_of_date)


def _timeseries_membership(
    series_kind: str,
    point: Mapping[str, object],
    point_index: int,
    resolved: Sequence[PerformanceObservation],
) -> list[str]:
    if series_kind == "CUMULATIVE":
        members = resolved[: point_index + 1]
    elif series_kind == "MONTHLY":
        month = str(point["month"])
        members = [row for row in resolved if row.market_date[:7] == month]
    elif series_kind.startswith("ROLLING_"):
        days = int(series_kind.removeprefix("ROLLING_").removesuffix("D"))
        endpoint = date.fromisoformat(str(point["as_of_date"]))
        start = endpoint - timedelta(days=days - 1)
        members = [
            row for row in resolved[: point_index + 1]
            if start <= date.fromisoformat(row.market_date) <= endpoint
        ]
    else:
        raise ValueError("INVALID_TIMESERIES_KIND")
    return sorted(row.observation_key for row in members)


def _date_epoch_ms(value: str) -> int:
    parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)
