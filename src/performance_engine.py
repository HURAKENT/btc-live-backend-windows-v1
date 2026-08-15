from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from src.performance_historical import (
    HistoricalPerformanceObservationBuilder,
    HistoricalSettlementReconciler,
    PinnedAhrArtifactLoader,
)
from src.performance_metrics import build_metrics
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
from src.performance_repository import PerformanceRepository


CALCULATION_VERSION = "PERFORMANCE_METRICS_V1"
SOURCE_VIEWS = ("HISTORICAL", "FORWARD", "COMBINED")


@dataclass(frozen=True, slots=True)
class HistoricalBootstrapResult:
    input_count: int
    observation_inserted: int
    observation_replayed: int
    resolution_inserted: int
    resolution_replayed: int
    ingest_receipt_outcome: str


class StrategyPerformanceEngine:
    def __init__(self, *, project_root: Path, repository: PerformanceRepository) -> None:
        self.project_root = Path(project_root).resolve()
        self.repository = repository

    def bootstrap_historical(self) -> HistoricalBootstrapResult:
        bundle = PinnedAhrArtifactLoader(project_root=self.project_root).load()
        observations = HistoricalPerformanceObservationBuilder(
            project_root=self.project_root, bundle=bundle
        ).build()
        resolutions = HistoricalSettlementReconciler(
            project_root=self.project_root, bundle=bundle
        ).reconcile(observations)

        observation_results = [
            self.repository.append_observation(observation)
            for observation in observations
        ]
        resolution_results = [
            self.repository.append_resolution(resolution)
            for resolution in resolutions
        ]
        first_key = observations[0].observation_key
        last_key = observations[-1].observation_key
        completed_at = _date_epoch_ms(max(row.market_date for row in observations))
        ingest = PerformanceIngestRun.create(
            ingest_run_key=canonical_identity(
                "performance-ingest-historical",
                {"acceptance_sha256": bundle.acceptance_sha256,
                 "results_sha256": bundle.result_artifact_sha256},
            ),
            mode="HISTORICAL_BOOTSTRAP",
            source_artifact_class="PINNED_AHR_STRATEGY_RESULTS",
            source_sha256=bundle.result_artifact_sha256,
            acceptance_sha256=bundle.acceptance_sha256,
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
        receipt = self.repository.append_ingest_run(ingest)
        return HistoricalBootstrapResult(
            input_count=len(observations),
            observation_inserted=sum(row.inserted for row in observation_results),
            observation_replayed=sum(not row.inserted for row in observation_results),
            resolution_inserted=sum(row.inserted for row in resolution_results),
            resolution_replayed=sum(not row.inserted for row in resolution_results),
            ingest_receipt_outcome=receipt.outcome,
        )

    def refresh_all(self, *, as_of_date: date | str) -> dict[str, int]:
        observations = self.repository.read_observations()
        resolutions = self.repository.read_effective_resolutions()
        strategy_ids = tuple(sorted({row.strategy_id for row in observations}))
        outcomes = {"inserted": 0, "replayed": 0}
        for strategy_id in strategy_ids:
            strategy_observations = [
                row for row in observations if row.strategy_id == strategy_id
            ]
            for source_view in SOURCE_VIEWS:
                result = self._refresh_one(
                    strategy_id=strategy_id,
                    source_view=source_view,
                    observations=strategy_observations,
                    resolutions=resolutions,
                    as_of_date=as_of_date,
                )
                outcomes[result.outcome.lower()] += 1
        return outcomes

    def _refresh_one(
        self, *, strategy_id: str, source_view: str,
        observations: Sequence[PerformanceObservation],
        resolutions: Mapping[str, PerformanceResolution], as_of_date: date | str,
    ):
        observation_keys = {item.observation_key for item in observations}
        scoped_resolutions = {
            key: value for key, value in resolutions.items() if key in observation_keys
        }
        metrics = build_metrics(
            observations, scoped_resolutions,
            source_view=source_view, as_of_date=as_of_date
        )
        ledger_rows = sorted(
            [(row.observation_key, row.payload_sha256) for row in observations]
            + [(row.resolution_key, row.payload_sha256)
               for row in scoped_resolutions.values()]
        )
        ledger_sha = canonical_sha256(ledger_rows)
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
        )
        revision = AggregateRevision.create(
            revision_key=revision_key, strategy_id=strategy_id,
            source_view=source_view, calculation_version=CALCULATION_VERSION,
            source_ledger_revision=len(ledger_rows), source_ledger_sha256=ledger_sha,
            generated_at_ms=_date_epoch_ms(str(as_of_date)),
            provenance={"as_of_date": str(as_of_date),
                        "calculation_contract": CALCULATION_VERSION},
            aggregate_count=len(aggregates), timeseries_count=len(timeseries),
            children_sha256=materialization_children_sha256(aggregates, timeseries),
        )
        return self.repository.publish_materialization(revision, aggregates, timeseries)

    @staticmethod
    def _timeseries(*, revision_key: str, strategy_id: str,
                    source_view: str, metrics: Mapping[str, object]) -> list[TimeseriesRow]:
        groups: list[tuple[str, Iterable[Mapping[str, object]], str]] = [
            ("CUMULATIVE", metrics["cumulative_series"], "observation_key"),
            ("MONTHLY", metrics["monthly_series"], "month"),
        ]
        rolling = metrics["rolling_series"]
        assert isinstance(rolling, Mapping)
        for label in ("30D", "90D", "365D"):
            groups.append((f"ROLLING_{label}", rolling[label], "observation_key"))
        rows: list[TimeseriesRow] = []
        for series_kind, points, period_field in groups:
            for point in points:
                period_key = str(point[period_field])
                rows.append(TimeseriesRow.create(
                    timeseries_key=canonical_identity(
                        "performance-timeseries", [revision_key, series_kind, period_key]
                    ),
                    revision_key=revision_key, strategy_id=strategy_id,
                    source_view=source_view, series_kind=series_kind,
                    period_key=period_key, calculation_version=CALCULATION_VERSION,
                    observation_count=int(point.get("resolved_signal_count", 1)),
                    observation_sha256=canonical_sha256(point), payload=point,
                ))
        return rows


def _date_epoch_ms(value: str) -> int:
    parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)
