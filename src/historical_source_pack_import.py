from __future__ import annotations

import csv
import hashlib
import io
import json
import zlib
import zipfile
from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Iterator

from src.binance_provider import BINANCE_INTERVAL, BINANCE_SYMBOL, _canonical_closed_kline_payload
from src.data_completion import DataCompletionLedger, ImportCommitResult, ImportRunRecord, SourceRangeRecord
from src.fixed_point import ProbabilityMicros
from src.models import SourceEvent, payload_sha256
from src.storage import SqliteStore


_MANIFEST = "reports/output_hash_manifest.csv"
_REQUIRED_NORMALIZED_MEMBERS = (
    "normalized/markets.csv",
    "normalized/binance_btcusdt_1m.csv",
    "normalized/price_history.csv",
    "normalized/settlements.csv",
)
_SCHEMAS = {
    "normalized/markets.csv": (
        "market_date", "event_match_method", "event_id", "event_slug", "event_title",
        "event_end_date", "market_id", "condition_id", "market_slug", "question",
        "group_item_title", "group_item_threshold", "lower_bound", "upper_bound",
        "lower_inclusive", "upper_inclusive", "outcomes", "yes_token_id", "no_token_id",
        "volume_num", "liquidity_num", "enable_order_book", "order_min_size", "tick_size",
        "description", "resolution_source", "closed", "automatically_resolved", "source_batch_id",
    ),
    "normalized/binance_btcusdt_1m.csv": (
        "symbol", "open_time_ms", "open_time_utc", "open", "high", "low", "close",
        "volume", "close_time_ms", "close_time_utc", "quote_asset_volume", "trade_count",
        "taker_buy_base_volume", "taker_buy_quote_volume",
    ),
    "normalized/price_history.csv": (
        "market_date", "event_slug", "event_end_date", "market_id", "condition_id",
        "group_item_title", "token_id", "timestamp", "timestamp_utc", "price", "source_batch_id",
    ),
}
_SETTLEMENT_SCHEMAS = (
    ("market_date", "event_end_utc", "binance_close", "derived_winner_bucket"),
    (
        "market_date", "event_end_utc", "binance_close", "derived_winner_bucket",
        "source_batch_id", "independent_winner_bucket", "independent_match",
    ),
)


@dataclass(frozen=True, slots=True)
class AuthoritativeSourcePack:
    pack_id: str
    filename: str
    sha256: str
    member_set_sha256: str
    universe_dates_sha256: str
    market_identity_sequence_sha256: str


AUTHORITATIVE_SOURCE_PACKS = (
    AuthoritativeSourcePack(
        "HISTORICAL_70_V1_10",
        "btc_daily_range_merged_70_v1_10.zip",
        "911f4108cd8f6fe8d14c3bcbc435062caa0fa850c9f51a9712687507ac8691ce",
        "065b7294bd0b1ca9bf1726891598ebce9160d281043c803ae5430d8d02985c32",
        "c5d57431c006a6be71c6c7ec2f2e3c780339a29cfcc179febf59ebbcd6ec08ca",
        "432e35c6d4b5f73d8b8f8dd66779052854551746d0d13409f11144c3335dfb71",
    ),
    AuthoritativeSourcePack(
        "VALIDATION_100_V2_2",
        "btc_daily_range_validation_100_merged_v2_2(1).zip",
        "cfcf8b47acb87c3efec9a0d0f86f86dd83c7eeed919975c80dac9094ed7d1dc2",
        "c41cbe90df0afe1c749af23f1f3ed9e4e45b9a08b1f51db3a265deb8bee4367a",
        "a0978f29c94555927c03ad4116bbf53c6d78a772501937a377df672a4fd5a782",
        "12ebad4dd4122b5b0b636f657f912503cca3a4a3fd150c4e329bc52c94f5d01a",
    ),
)


@dataclass(frozen=True, slots=True)
class SourceRangePlan:
    source: str
    row_count: int
    start_ms: int
    end_ms: int
    natural_key_sequence_sha256: str


@dataclass(frozen=True, slots=True)
class SourcePackPlan:
    pack_id: str
    path: Path
    outer_sha256: str
    manifest_sha256: str
    schema_mapping_sha256: str
    market_row_count: int
    binance_row_count: int
    price_row_count: int
    settlement_row_count: int
    universe_dates: frozenset[str]
    ranges: tuple[SourceRangePlan, ...]
    member_set_sha256: str
    universe_dates_sha256: str
    market_identity_sequence_sha256: str
    markets_per_date_min: int
    markets_per_date_max: int
    silent_drop_count: int = 0

    @property
    def universe_date_count(self) -> int:
        return len(self.universe_dates)

    @property
    def declared_row_count(self) -> int:
        return sum(item.row_count for item in self.ranges)


@dataclass(frozen=True, slots=True)
class SourcePackImportResult:
    import_run_row_id: int
    inserted: bool
    processed_row_count: int
    max_materialized_rows: int


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _hash_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member_names(archive: zipfile.ZipFile) -> tuple[str, ...]:
    names = tuple(item.filename for item in archive.infolist())
    folded: set[str] = set()
    for name in names:
        pure = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or pure.is_absolute()
            or ".." in pure.parts
            or (pure.parts and ":" in pure.parts[0])
        ):
            raise ValueError("SOURCE_PACK_PATH_TRAVERSAL")
        key = name.casefold()
        if key in folded:
            raise ValueError("SOURCE_PACK_DUPLICATE_MEMBER")
        folded.add(key)
    return names


def _read_manifest(archive: zipfile.ZipFile) -> dict[str, tuple[int, str]]:
    try:
        raw = archive.read(_MANIFEST)
    except KeyError:
        raise ValueError("SOURCE_PACK_REQUIRED_MEMBER_MISSING") from None
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    if tuple(reader.fieldnames or ()) != ("relative_path", "size_bytes", "sha256"):
        raise ValueError("SOURCE_PACK_MANIFEST_SCHEMA_MISMATCH")
    result: dict[str, tuple[int, str]] = {}
    for row in reader:
        name = row["relative_path"]
        if name in result:
            raise ValueError("SOURCE_PACK_MANIFEST_DUPLICATE_MEMBER")
        try:
            size = int(row["size_bytes"])
        except (TypeError, ValueError):
            raise ValueError("SOURCE_PACK_MANIFEST_INVALID") from None
        sha = row["sha256"]
        if size < 0 or len(sha) != 64 or any(char not in "0123456789abcdef" for char in sha):
            raise ValueError("SOURCE_PACK_MANIFEST_INVALID")
        result[name] = (size, sha)
    return result


def _verify_member_hashes(
    archive: zipfile.ZipFile,
    manifest: dict[str, tuple[int, str]],
) -> None:
    for name, (expected_size, expected_sha) in manifest.items():
        try:
            info = archive.getinfo(name)
            digest = hashlib.sha256()
            size = 0
            with archive.open(info) as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
        except (KeyError, zipfile.BadZipFile, RuntimeError, EOFError, OSError, zlib.error):
            raise ValueError("SOURCE_PACK_CRC_ERROR") from None
        if size != expected_size or digest.hexdigest() != expected_sha:
            raise ValueError("SOURCE_PACK_MEMBER_HASH_MISMATCH")


def _iter_rows(archive: zipfile.ZipFile, member: str) -> Iterator[dict[str, str]]:
    with archive.open(member) as binary:
        with io.TextIOWrapper(binary, encoding="utf-8-sig", newline="") as text:
            reader = csv.DictReader(text)
            fields = tuple(reader.fieldnames or ())
            expected = _SETTLEMENT_SCHEMAS if member == "normalized/settlements.csv" else (_SCHEMAS[member],)
            if fields not in expected:
                raise ValueError("SOURCE_PACK_CSV_SCHEMA_MISMATCH")
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("SOURCE_PACK_CSV_ROW_SHAPE_INVALID")
                yield row


def _utc_ms(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        result = int(parsed.timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("SOURCE_PACK_INVALID_TIMESTAMP") from None
    if result < 0:
        raise ValueError("SOURCE_PACK_INVALID_TIMESTAMP")
    return result


def _source_event(member: str, row: dict[str, str]) -> SourceEvent:
    if member == "normalized/binance_btcusdt_1m.csv":
        try:
            open_ms = int(row["open_time_ms"])
            close_ms = int(row["close_time_ms"])
            trades = int(row["trade_count"])
        except (TypeError, ValueError):
            raise ValueError("SOURCE_PACK_INVALID_BINANCE_ROW") from None
        if row["symbol"] != BINANCE_SYMBOL:
            raise ValueError("SOURCE_PACK_INVALID_BINANCE_ROW")
        payload = _canonical_closed_kline_payload(
            open_time_ms=open_ms,
            close_time_ms=close_ms,
            open_price=row["open"],
            high_price=row["high"],
            low_price=row["low"],
            close_price=row["close"],
            volume=row["volume"],
            quote_asset_volume=row["quote_asset_volume"],
            number_of_trades=trades,
            taker_buy_base_asset_volume=row["taker_buy_base_volume"],
            taker_buy_quote_asset_volume=row["taker_buy_quote_volume"],
        )
        return SourceEvent.binance_closed_kline(
            symbol=BINANCE_SYMBOL,
            interval=BINANCE_INTERVAL,
            open_time_ms=open_ms,
            payload=_canonical(payload).encode(),
            received_timestamp_ms=close_ms,
            recovery_origin="HISTORICAL_IMPORT",
        )
    if member == "normalized/price_history.csv":
        try:
            timestamp = int(row["timestamp"])
            decimal_price = Decimal(row["price"])
            price_micros = ProbabilityMicros.from_decimal(decimal_price).value
        except (TypeError, ValueError, InvalidOperation):
            raise ValueError("SOURCE_PACK_INVALID_PRICE") from None
        if timestamp <= 0:
            raise ValueError("SOURCE_PACK_INVALID_TIMESTAMP")
        provider_price = format(decimal_price, "f")
        payload = {
            "asset_id": row["token_id"],
            "event_type": "price_history",
            "price_micros": price_micros,
            "provider_point": {"p": provider_price, "t": timestamp},
            "timestamp_seconds": timestamp,
        }
        canonical = _canonical(payload)
        identity = hashlib.sha256(canonical.encode()).hexdigest()
        return SourceEvent(
            source="polymarket_history",
            natural_key=f"polymarket:{row['token_id']}:price_history:{timestamp}:{identity}",
            source_timestamp_ms=timestamp * 1000,
            received_timestamp_ms=timestamp * 1000,
            event_type="POLYMARKET_PRICE_HISTORY",
            payload_json=canonical,
            payload_sha256=payload_sha256(canonical),
            recovery_origin="HISTORICAL_IMPORT",
        )
    if member == "normalized/markets.csv":
        timestamp_ms = _utc_ms(row["event_end_date"])
        canonical = _canonical(row)
        return SourceEvent(
            source="historical_market_catalog",
            natural_key=f"historical-market:{row['condition_id']}:{row['market_id']}",
            source_timestamp_ms=timestamp_ms,
            received_timestamp_ms=timestamp_ms,
            event_type="HISTORICAL_MARKET_CATALOG",
            payload_json=canonical,
            payload_sha256=payload_sha256(canonical),
            recovery_origin="HISTORICAL_IMPORT",
        )
    if member == "normalized/settlements.csv":
        timestamp_ms = _utc_ms(row["event_end_utc"])
        canonical = _canonical(row)
        return SourceEvent(
            source="historical_settlement",
            natural_key=f"historical-settlement:{row['market_date']}",
            source_timestamp_ms=timestamp_ms,
            received_timestamp_ms=timestamp_ms,
            event_type="HISTORICAL_SETTLEMENT",
            payload_json=canonical,
            payload_sha256=payload_sha256(canonical),
            recovery_origin="HISTORICAL_IMPORT",
        )
    raise ValueError("SOURCE_PACK_UNKNOWN_MEMBER")


def _sequence_update(digest: hashlib._Hash, natural_key: str) -> None:
    raw = natural_key.encode("utf-8")
    digest.update(len(raw).to_bytes(8, "big"))
    digest.update(raw)


def _iter_events(
    plan: SourcePackPlan,
    archive: zipfile.ZipFile | None = None,
) -> Iterator[SourceEvent]:
    if archive is None:
        with zipfile.ZipFile(plan.path) as opened:
            yield from _iter_events(plan, opened)
        return
    else:
        for member in _REQUIRED_NORMALIZED_MEMBERS:
            for row in _iter_rows(archive, member):
                yield _source_event(member, row)


def verify_source_pack(
    path: Path,
    *,
    pack_id: str,
    expected_outer_sha256: str,
) -> SourcePackPlan:
    if not isinstance(path, Path) or type(pack_id) is not str or not pack_id:
        raise ValueError("INVALID_SOURCE_PACK_CONFIGURATION")
    actual_outer = _hash_file(path)
    if actual_outer != expected_outer_sha256:
        raise ValueError("SOURCE_PACK_OUTER_HASH_MISMATCH")
    try:
        with zipfile.ZipFile(path) as archive:
            names = _safe_member_names(archive)
            non_directory = {name for name in names if not name.endswith("/")}
            if any(name not in non_directory for name in _REQUIRED_NORMALIZED_MEMBERS):
                raise ValueError("SOURCE_PACK_REQUIRED_MEMBER_MISSING")
            manifest = _read_manifest(archive)
            if set(manifest) != non_directory - {_MANIFEST}:
                raise ValueError("SOURCE_PACK_MANIFEST_MEMBER_SET_MISMATCH")
            _verify_member_hashes(archive, manifest)

            counts: dict[str, int] = {}
            dates: set[str] = set()
            markets_per_date: dict[str, int] = {}
            market_identities: list[list[str]] = []
            range_state: dict[str, tuple[int, int, hashlib._Hash]] = {}
            for member in _REQUIRED_NORMALIZED_MEMBERS:
                count = 0
                for row in _iter_rows(archive, member):
                    event = _source_event(member, row)
                    count += 1
                    if member == "normalized/markets.csv":
                        dates.add(row["market_date"])
                        markets_per_date[row["market_date"]] = markets_per_date.get(row["market_date"], 0) + 1
                        market_identities.append(
                            [row["market_date"], row["condition_id"], row["market_id"], row["yes_token_id"], row["no_token_id"]]
                        )
                    state = range_state.get(event.source)
                    if state is None:
                        digest = hashlib.sha256()
                        range_state[event.source] = (event.source_timestamp_ms, event.source_timestamp_ms, digest)
                    else:
                        digest = state[2]
                        range_state[event.source] = (
                            min(state[0], event.source_timestamp_ms),
                            max(state[1], event.source_timestamp_ms),
                            digest,
                        )
                    _sequence_update(digest, event.natural_key)
                counts[member] = count
            ranges = tuple(
                SourceRangePlan(source, counts[member], state[0], state[1] + 1, state[2].hexdigest())
                for member, source in (
                    ("normalized/markets.csv", "historical_market_catalog"),
                    ("normalized/binance_btcusdt_1m.csv", "binance"),
                    ("normalized/price_history.csv", "polymarket_history"),
                    ("normalized/settlements.csv", "historical_settlement"),
                )
                for state in (range_state[source],)
            )
            if "reports/merge_audit.json" in manifest:
                audit = json.loads(archive.read("reports/merge_audit.json"))
                expected = (
                    int(audit.get("market_rows", -1)),
                    int(audit.get("binance_rows_unique", -1)),
                    int(audit.get("price_rows", -1)),
                    int(audit.get("settlements", -1)),
                )
                actual = tuple(counts[name] for name in _REQUIRED_NORMALIZED_MEMBERS)
                if expected != actual:
                    raise ValueError("SOURCE_PACK_AUDIT_ROW_COUNT_MISMATCH")
                universe_count = audit.get("replay_universe_dates", audit.get("validation_dates"))
                if universe_count != len(dates):
                    raise ValueError("SOURCE_PACK_UNIVERSE_COUNT_MISMATCH")
            schema_mapping = _canonical(
                {member: list(_SETTLEMENT_SCHEMAS if member.endswith("settlements.csv") else (_SCHEMAS[member],)) for member in _REQUIRED_NORMALIZED_MEMBERS}
            )
            return SourcePackPlan(
                pack_id=pack_id,
                path=path,
                outer_sha256=actual_outer,
                manifest_sha256=_hash_bytes(archive.read(_MANIFEST)),
                schema_mapping_sha256=hashlib.sha256(schema_mapping.encode()).hexdigest(),
                market_row_count=counts["normalized/markets.csv"],
                binance_row_count=counts["normalized/binance_btcusdt_1m.csv"],
                price_row_count=counts["normalized/price_history.csv"],
                settlement_row_count=counts["normalized/settlements.csv"],
                universe_dates=frozenset(dates),
                ranges=ranges,
                member_set_sha256=_hash_bytes(_canonical(sorted(non_directory)).encode()),
                universe_dates_sha256=_hash_bytes(_canonical(sorted(dates)).encode()),
                market_identity_sequence_sha256=_hash_bytes(_canonical(market_identities).encode()),
                markets_per_date_min=min(markets_per_date.values()),
                markets_per_date_max=max(markets_per_date.values()),
            )
    except zipfile.BadZipFile:
        raise ValueError("SOURCE_PACK_CRC_ERROR") from None


def verify_authoritative_source_packs(download_root: Path) -> tuple[SourcePackPlan, ...]:
    plans = tuple(
        verify_source_pack(
            download_root / item.filename,
            pack_id=item.pack_id,
            expected_outer_sha256=item.sha256,
        )
        for item in AUTHORITATIVE_SOURCE_PACKS
    )
    if not plans[0].universe_dates.isdisjoint(plans[1].universe_dates):
        raise ValueError("SOURCE_PACK_UNIVERSE_OVERLAP")
    for frozen, plan in zip(AUTHORITATIVE_SOURCE_PACKS, plans, strict=True):
        if (
            plan.member_set_sha256 != frozen.member_set_sha256
            or plan.universe_dates_sha256 != frozen.universe_dates_sha256
            or plan.market_identity_sequence_sha256 != frozen.market_identity_sequence_sha256
            or plan.markets_per_date_min != 11
            or plan.markets_per_date_max != 11
        ):
            raise ValueError("SOURCE_PACK_AUTHORITATIVE_UNIVERSE_MISMATCH")
    return plans


def _range_records(plan: SourcePackPlan, run_id: str) -> tuple[SourceRangeRecord, ...]:
    records = []
    for item in plan.ranges:
        scope = _canonical({"outer_sha256": plan.outer_sha256, "pack_id": plan.pack_id, "source": item.source})
        evidence = _canonical(
            {
                "manifest_sha256": plan.manifest_sha256,
                "natural_key_sequence_sha256": item.natural_key_sequence_sha256,
                "outer_sha256": plan.outer_sha256,
                "row_count": item.row_count,
            }
        )
        records.append(
            SourceRangeRecord(
                import_run_id=run_id,
                source=item.source,
                canonical_scope_json=scope,
                canonical_scope_sha256=hashlib.sha256(scope.encode()).hexdigest(),
                requested_start_ms=item.start_ms,
                requested_end_ms=item.end_ms,
                granularity_ms=1,
                contract_version="BTC_HISTORICAL_SOURCE_PACK_V1",
                status="COMPLETE",
                expected_row_count=item.row_count,
                observed_row_count=item.row_count,
                missing_row_count=0,
                reason_code=None,
                evidence_metadata_json=evidence,
                evidence_sha256=hashlib.sha256(evidence.encode()).hexdigest(),
                updated_at_ms=max(value.end_ms for value in plan.ranges),
            )
        )
    return tuple(records)


def import_source_pack(store: SqliteStore, plan: SourcePackPlan) -> SourcePackImportResult:
    if type(store) is not SqliteStore or type(plan) is not SourcePackPlan:
        raise ValueError("INVALID_SOURCE_PACK_IMPORT")
    ledger = DataCompletionLedger(store)
    run_id = f"historical-source-pack:{plan.pack_id}:{plan.outer_sha256}"
    ranges = _range_records(plan, run_id)
    expected_by_source = {item.source: item for item in plan.ranges}
    connection = store._connection

    def stream(
        archive: zipfile.ZipFile,
        consume,
    ) -> int:
        processed = 0
        digests = {source: hashlib.sha256() for source in expected_by_source}
        counts = {source: 0 for source in expected_by_source}
        for event in _iter_events(plan, archive):
            expected = expected_by_source.get(event.source)
            if expected is None:
                raise ValueError("SOURCE_PACK_EVENT_SOURCE_UNDECLARED")
            if not expected.start_ms <= event.source_timestamp_ms < expected.end_ms:
                raise ValueError("SOURCE_PACK_EVENT_OUTSIDE_RANGE")
            consume(event)
            processed += 1
            counts[event.source] += 1
            _sequence_update(digests[event.source], event.natural_key)
        if processed != plan.declared_row_count:
            raise ValueError("SOURCE_PACK_SILENT_DROP_DETECTED")
        for source, expected in expected_by_source.items():
            if counts[source] != expected.row_count or digests[source].hexdigest() != expected.natural_key_sequence_sha256:
                raise ValueError("SOURCE_PACK_SILENT_DROP_DETECTED")
        return processed

    try:
        with plan.path.open("rb") as raw_handle:
            digest = hashlib.sha256()
            for chunk in iter(lambda: raw_handle.read(1024 * 1024), b""):
                digest.update(chunk)
            if digest.hexdigest() != plan.outer_sha256:
                raise ValueError("SOURCE_PACK_CHANGED_AFTER_VERIFICATION")
            raw_handle.seek(0)
            with zipfile.ZipFile(raw_handle) as archive:
                names = _safe_member_names(archive)
                non_directory = {name for name in names if not name.endswith("/")}
                manifest = _read_manifest(archive)
                if set(manifest) != non_directory - {_MANIFEST}:
                    raise ValueError("SOURCE_PACK_CHANGED_AFTER_VERIFICATION")
                _verify_member_hashes(archive, manifest)
                if _hash_bytes(archive.read(_MANIFEST)) != plan.manifest_sha256:
                    raise ValueError("SOURCE_PACK_CHANGED_AFTER_VERIFICATION")

                connection.execute("BEGIN IMMEDIATE")
                existing = ledger._read_run(run_id)
                if existing is not None:
                    stored_run = ImportRunRecord(
                        **{
                            field.name: value
                            for field, value in zip(fields(ImportRunRecord), existing[1:], strict=True)
                        }
                    )
                    expected_run = ImportRunRecord(
                        import_run_id=run_id,
                        artifact_sha256=plan.outer_sha256,
                        source_path_fingerprint=hashlib.sha256(plan.path.name.encode()).hexdigest(),
                        dataset_start_ms=min(item.start_ms for item in plan.ranges),
                        dataset_end_ms=max(item.end_ms for item in plan.ranges),
                        declared_row_count=plan.declared_row_count,
                        schema_mapping_sha256=plan.schema_mapping_sha256,
                        inserted_row_count=stored_run.inserted_row_count,
                        replayed_row_count=stored_run.replayed_row_count,
                        conflict_row_count=0,
                        dropped_row_count=0,
                        status="COMPLETE",
                        created_at_ms=max(item.end_ms for item in plan.ranges),
                    )
                    row_id = ledger._verify_run(expected_run, existing)
                    stored_assessments = connection.execute(
                        "SELECT assessment_key FROM data_source_range_assessments "
                        "WHERE import_run_id=? ORDER BY assessment_key",
                        (run_id,),
                    ).fetchall()
                    if tuple(row[0] for row in stored_assessments) != tuple(
                        sorted(item.assessment_key for item in ranges)
                    ):
                        raise ValueError("DATA_IMPORT_RUN_RANGE_SET_CONFLICT")
                    for item in ranges:
                        definition = connection.execute(
                            "SELECT source,canonical_scope_json,canonical_scope_sha256,"
                            "requested_start_ms,requested_end_ms,granularity_ms,contract_version "
                            "FROM data_source_ranges WHERE range_key=?",
                            (item.range_key,),
                        ).fetchone()
                        expected_definition = (
                            item.source,
                            item.canonical_scope_json,
                            item.canonical_scope_sha256,
                            item.requested_start_ms,
                            item.requested_end_ms,
                            item.granularity_ms,
                            item.contract_version,
                        )
                        if definition is None or tuple(definition) != expected_definition:
                            raise ValueError("SOURCE_RANGE_CONFLICT")
                    inserted_links = 0
                    replayed_links = 0

                    def verify_existing(event: SourceEvent) -> None:
                        nonlocal inserted_links, replayed_links
                        will_insert, _ = ledger._classify_event(event)
                        if will_insert:
                            raise ValueError("SOURCE_PACK_REPLAY_LINK_CONFLICT")
                        link = connection.execute(
                            "SELECT l.outcome FROM data_import_run_events l JOIN source_events e ON e.event_id=l.event_id "
                            "WHERE l.import_run_id=? AND e.natural_key=?",
                            (run_id, event.natural_key),
                        ).fetchone()
                        if link is None or link[0] not in {"INSERTED", "REPLAYED"}:
                            raise ValueError("SOURCE_PACK_REPLAY_LINK_CONFLICT")
                        inserted_links += link[0] == "INSERTED"
                        replayed_links += link[0] == "REPLAYED"

                    processed = stream(archive, verify_existing)
                    if (
                        stored_run.artifact_sha256 != plan.outer_sha256
                        or stored_run.schema_mapping_sha256 != plan.schema_mapping_sha256
                        or stored_run.declared_row_count != plan.declared_row_count
                        or stored_run.inserted_row_count != inserted_links
                        or stored_run.replayed_row_count != replayed_links
                        or stored_run.conflict_row_count != 0
                        or stored_run.dropped_row_count != 0
                        or stored_run.status != "COMPLETE"
                    ):
                        raise ValueError("DATA_IMPORT_RUN_CONFLICT")
                    inserted = False
                else:
                    inserted_count = 0
                    replayed_count = 0

                    def classify(event: SourceEvent) -> None:
                        nonlocal inserted_count, replayed_count
                        will_insert, _ = ledger._classify_event(event)
                        inserted_count += will_insert
                        replayed_count += not will_insert

                    processed = stream(archive, classify)
                    run = ImportRunRecord(
                        import_run_id=run_id,
                        artifact_sha256=plan.outer_sha256,
                        source_path_fingerprint=hashlib.sha256(plan.path.name.encode()).hexdigest(),
                        dataset_start_ms=min(item.start_ms for item in plan.ranges),
                        dataset_end_ms=max(item.end_ms for item in plan.ranges),
                        declared_row_count=plan.declared_row_count,
                        schema_mapping_sha256=plan.schema_mapping_sha256,
                        inserted_row_count=inserted_count,
                        replayed_row_count=replayed_count,
                        conflict_row_count=0,
                        dropped_row_count=0,
                        status="COMPLETE",
                        created_at_ms=max(item.end_ms for item in plan.ranges),
                    )
                    inserted, row_id = ledger._insert_run(run)
                    for source_range in ranges:
                        ledger._insert_range(source_range)

                    actual_inserted = 0
                    actual_replayed = 0

                    def persist(event: SourceEvent) -> None:
                        nonlocal actual_inserted, actual_replayed
                        will_insert, _ = ledger._classify_event(event)
                        ledger._insert_event(run_id, event, will_insert)
                        actual_inserted += will_insert
                        actual_replayed += not will_insert

                    stream(archive, persist)
                    if (actual_inserted, actual_replayed) != (inserted_count, replayed_count):
                        raise ValueError("SOURCE_PACK_IMPORT_OUTCOME_CHANGED")
                connection.commit()
                return SourcePackImportResult(row_id, inserted, processed, 1)
    except (zipfile.BadZipFile, zlib.error):
        if connection.in_transaction:
            connection.rollback()
        raise ValueError("SOURCE_PACK_CRC_ERROR") from None
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
