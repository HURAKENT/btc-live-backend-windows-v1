from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Mapping

from src.performance_models import (
    CatchupClassification,
    canonical_identity,
    canonical_sha256,
)
from src.performance_repository import PerformanceRepository
from src.storage import SqliteReadStore, SqliteStore


POST_BASELINE_START_DATE = "2026-07-08"


@dataclass(frozen=True, slots=True)
class MarketEvidence:
    market_id: str | None
    source_event_identity: str | None
    classification: str
    reason_code: str
    provenance: Mapping[str, object]


class MarketCatchupClassifier:
    """Classify post-baseline market dates from persisted local evidence only."""

    def __init__(
        self,
        *,
        store: SqliteStore | SqliteReadStore,
        classified_at_ms: int,
    ) -> None:
        if type(store) not in (SqliteStore, SqliteReadStore):
            raise ValueError("INVALID_MARKET_CATCHUP_STORE")
        if type(classified_at_ms) is not int or classified_at_ms < 0:
            raise ValueError("INVALID_MARKET_CATCHUP_TIMESTAMP")
        self.store = store
        self.repository = PerformanceRepository(store)
        self.classified_at_ms = classified_at_ms

    def classify_range(
        self,
        *,
        start_date: str = POST_BASELINE_START_DATE,
        end_date: str,
    ) -> list[CatchupClassification]:
        return self.classify_dates(_date_range(start_date, end_date))

    def classify_dates(self, market_dates: Iterable[str]) -> list[CatchupClassification]:
        existing = {
            row.market_date: row
            for row in self.repository.read_effective_catchup_classifications()
        }
        rows: list[CatchupClassification] = []
        for market_date in market_dates:
            _require_date(market_date)
            evidence = self._classify_one(market_date)
            previous = existing.get(market_date)
            if previous is not None and _same_evidence(previous, evidence):
                rows.append(previous)
                continue
            revision = 1 if previous is None else previous.revision + 1
            supersedes = None if previous is None else previous.catchup_key
            key_coordinates = {
                "classification": evidence.classification,
                "classified_at_ms": self.classified_at_ms,
                "market_date": market_date,
                "market_id": evidence.market_id,
                "reason_code": evidence.reason_code,
                "revision": revision,
                "source_event_identity": evidence.source_event_identity,
                "supersedes_catchup_key": supersedes,
            }
            rows.append(
                CatchupClassification.create(
                    catchup_key=canonical_identity("catchup", key_coordinates),
                    market_date=market_date,
                    classification=evidence.classification,
                    reason_code=evidence.reason_code,
                    market_id=evidence.market_id,
                    source_event_identity=evidence.source_event_identity,
                    classified_at_ms=self.classified_at_ms,
                    provenance=evidence.provenance,
                    revision=revision,
                    supersedes_catchup_key=supersedes,
                )
            )
        return rows

    def _classify_one(self, market_date: str) -> MarketEvidence:
        absence = self._absence_event(market_date)
        market = self._market_for_date(market_date)
        resolution = self._resolution_event(market_date)
        if resolution is not None and absence is not None:
            raise ValueError("MARKET_CATCHUP_CONTRADICTORY_EVIDENCE")
        if absence is not None and market is not None:
            raise ValueError("MARKET_CATCHUP_CONTRADICTORY_EVIDENCE")
        if resolution is not None:
            if (
                market is not None
                and resolution["market_id"] is not None
                and resolution["market_id"] != market["market_id"]
            ):
                raise ValueError("MARKET_CATCHUP_CONTRADICTORY_EVIDENCE")
            return MarketEvidence(
                market_id=(
                    resolution["market_id"]
                    if resolution["market_id"] is not None
                    else None if market is None else market["market_id"]
                ),
                source_event_identity=resolution["natural_key"],
                classification="RESOLVED",
                reason_code="PERSISTED_PUBLIC_RESOLUTION",
                provenance={
                    "classifier": "MarketCatchupClassifier",
                    "event_id": resolution["event_id"],
                    "event_type": resolution["event_type"],
                    "payload_sha256": resolution["payload_sha256"],
                },
            )
        if absence is not None:
            return MarketEvidence(
                market_id=None,
                source_event_identity=absence["natural_key"],
                classification="EXPECTED_ABSENT",
                reason_code="PERSISTED_EXPECTED_ABSENCE",
                provenance={
                    "classifier": "MarketCatchupClassifier",
                    "event_id": absence["event_id"],
                    "event_type": absence["event_type"],
                    "payload_sha256": absence["payload_sha256"],
                },
            )

        if market is None:
            return MarketEvidence(
                market_id=None,
                source_event_identity=None,
                classification="DATA_GAP",
                reason_code="MISSING_PERSISTED_MARKET_EVIDENCE",
                provenance={
                    "classifier": "MarketCatchupClassifier",
                    "market_date": market_date,
                },
            )

        payload = _loads(market["payload_json"])
        if _is_resolved(payload):
            return MarketEvidence(
                market_id=market["market_id"],
                source_event_identity=None,
                classification="RESOLVED",
                reason_code="PERSISTED_MARKET_RESOLVED",
                provenance={
                    "classifier": "MarketCatchupClassifier",
                    "market_payload_sha256": market["payload_sha256"],
                },
            )
        return MarketEvidence(
            market_id=market["market_id"],
            source_event_identity=None,
            classification="PENDING",
            reason_code="PERSISTED_MARKET_PENDING",
            provenance={
                "classifier": "MarketCatchupClassifier",
                "market_payload_sha256": market["payload_sha256"],
            },
        )

    def _market_for_date(self, market_date: str) -> dict[str, Any] | None:
        rows = self.store.rows(
            """
            SELECT market_id, payload_json, payload_sha256
            FROM market_catalog
            ORDER BY updated_at_ms DESC, market_id ASC
            """
        )
        matches = []
        for row in rows:
            payload = _loads(row[1])
            if _payload_market_date(payload) == market_date:
                matches.append(
                    {
                        "market_id": row[0],
                        "payload_json": row[1],
                        "payload_sha256": row[2],
                    }
                )
        if len(matches) > 1:
            raise ValueError("MARKET_CATCHUP_AMBIGUOUS_MARKET_DATE")
        return matches[0] if matches else None

    def _absence_event(self, market_date: str) -> dict[str, Any] | None:
        rows = self.store.rows(
            """
            SELECT event_id, natural_key, event_type, payload_json, payload_sha256
            FROM source_events
            WHERE event_type IN (
                'BTC_DAILY_RANGE_MARKET_ABSENT',
                'POLYMARKET_DAILY_RANGE_MARKET_ABSENT'
            )
            ORDER BY event_id ASC
            """
        )
        matches = []
        for row in rows:
            payload = _loads(row[3])
            if _payload_market_date(payload) == market_date:
                matches.append(
                    {
                        "event_id": row[0],
                        "natural_key": row[1],
                        "event_type": row[2],
                        "payload_sha256": row[4],
                    }
                )
        if len(matches) > 1:
            raise ValueError("MARKET_CATCHUP_AMBIGUOUS_ABSENCE_EVIDENCE")
        return matches[0] if matches else None

    def _resolution_event(self, market_date: str) -> dict[str, Any] | None:
        rows = self.store.rows(
            """
            SELECT event_id, natural_key, event_type, payload_json, payload_sha256
            FROM source_events
            WHERE event_type IN (
                'POLYMARKET_MARKET_RESOLVED',
                'POLYMARKET_DAILY_RANGE_MARKET_RESOLVED',
                'MARKET_RESOLVED'
            )
            ORDER BY event_id ASC
            """
        )
        matches = []
        for row in rows:
            payload = _loads(row[3])
            if _payload_market_date(payload) != market_date:
                continue
            if payload.get("resolved") is not True:
                raise ValueError("MARKET_CATCHUP_INVALID_RESOLUTION_EVIDENCE")
            count = payload.get("winning_bucket_count", 1)
            winner = payload.get("winning_bucket_identity") or payload.get(
                "winning_bucket_id"
            )
            if count != 1 or not isinstance(winner, str) or not winner:
                raise ValueError("MARKET_CATCHUP_AMBIGUOUS_RESOLUTION_EVIDENCE")
            matches.append(
                {
                    "event_id": row[0],
                    "natural_key": row[1],
                    "event_type": row[2],
                    "market_id": payload.get("market_id") or payload.get("event_id"),
                    "payload_sha256": row[4],
                    "supersedes_source_event_natural_key": payload.get(
                        "supersedes_source_event_natural_key"
                    ),
                    "winning_bucket_identity": winner,
                }
            )
        if not matches:
            return None
        current = matches[0]
        for candidate in matches[1:]:
            supersedes = candidate["supersedes_source_event_natural_key"]
            if supersedes == current["natural_key"]:
                current = candidate
                continue
            if (
                candidate["winning_bucket_identity"]
                == current["winning_bucket_identity"]
                and supersedes is None
            ):
                continue
            raise ValueError("MARKET_CATCHUP_AMBIGUOUS_RESOLUTION_EVIDENCE")
        return current


def catchup_ledger_sha256(rows: list[CatchupClassification]) -> str:
    return canonical_sha256([json.loads(row.payload_json) for row in rows])


def _date_range(start_date: str, end_date: str) -> list[str]:
    _require_date(start_date)
    _require_date(end_date)
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if end < start:
        raise ValueError("INVALID_MARKET_CATCHUP_DATE_RANGE")
    values: list[str] = []
    current = start
    while current <= end:
        values.append(current.isoformat())
        current += timedelta(days=1)
    return values


def _loads(payload_json: str) -> Mapping[str, Any]:
    payload = json.loads(payload_json)
    if not isinstance(payload, Mapping):
        raise ValueError("INVALID_MARKET_EVIDENCE_PAYLOAD")
    return payload


def _payload_market_date(payload: Mapping[str, Any]) -> str | None:
    for key in ("market_date", "date"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value[:10]
    resolution = payload.get("resolution_utc")
    if isinstance(resolution, str) and len(resolution) >= 10:
        return resolution[:10]
    return None


def _is_resolved(payload: Mapping[str, Any]) -> bool:
    if payload.get("resolved") is True:
        return True
    if payload.get("winning_bucket_identity") or payload.get("winning_bucket_id"):
        return True
    status = payload.get("status")
    return isinstance(status, str) and status.upper() in {"RESOLVED", "SETTLED"}


def _same_evidence(
    previous: CatchupClassification,
    evidence: MarketEvidence,
) -> bool:
    return (
        previous.classification == evidence.classification
        and previous.reason_code == evidence.reason_code
        and previous.market_id == evidence.market_id
        and previous.source_event_identity == evidence.source_event_identity
        and dict(previous.provenance) == dict(evidence.provenance)
    )


def _require_date(value: str) -> None:
    if type(value) is not str:
        raise ValueError("INVALID_MARKET_CATCHUP_DATE")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError("INVALID_MARKET_CATCHUP_DATE") from None
    if parsed.isoformat() != value:
        raise ValueError("INVALID_MARKET_CATCHUP_DATE")
