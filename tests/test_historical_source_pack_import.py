from __future__ import annotations

import csv
import dataclasses
import hashlib
import io
import json
import tempfile
import tracemalloc
import unittest
from unittest import mock
import warnings
import zipfile
from pathlib import Path

from src.storage import SqliteStore


def _build_pack(
    path: Path,
    *,
    corrupt_manifest: bool = False,
    bad_price: bool = False,
    omit_member: str | None = None,
    unmanifested_member: bool = False,
    bad_market_schema: bool = False,
    price_row_count: int = 1,
) -> str:
    price_rows = "".join(
        f"2026-01-01,s,2026-01-01T16:00:00+00:00,2,c,b,yes,{60 + index},x,{'bad' if bad_price and index == price_row_count - 1 else '0.25'},b1\n"
        for index in range(price_row_count)
    )
    members = {
        "normalized/markets.csv": (
            ("wrong," if bad_market_schema else "")
            + "market_date,event_match_method,event_id,event_slug,event_title,event_end_date,market_id,condition_id,market_slug,question,group_item_title,group_item_threshold,lower_bound,upper_bound,lower_inclusive,upper_inclusive,outcomes,yes_token_id,no_token_id,volume_num,liquidity_num,enable_order_book,order_min_size,tick_size,description,resolution_source,closed,automatically_resolved,source_batch_id\n"
            "2026-01-01,exact,1,s,t,2026-01-01T16:00:00+00:00,2,c,ms,q,b,1,0,1,True,False,yes-no,yes,no,1,1,True,5,0.01,d,r,True,True,b1\n"
        ).encode(),
        "normalized/binance_btcusdt_1m.csv": (
            "symbol,open_time_ms,open_time_utc,open,high,low,close,volume,close_time_ms,close_time_utc,quote_asset_volume,trade_count,taker_buy_base_volume,taker_buy_quote_volume\n"
            "BTCUSDT,60000,x,1,2,0.5,1.5,10,119999,x,15,3,4,6\n"
        ).encode(),
        "normalized/price_history.csv": (
            "market_date,event_slug,event_end_date,market_id,condition_id,group_item_title,token_id,timestamp,timestamp_utc,price,source_batch_id\n"
            + price_rows
        ).encode(),
        "normalized/settlements.csv": (
            "market_date,event_end_utc,binance_close,derived_winner_bucket\n"
            "2026-01-01,2026-01-01T16:00:00+00:00,1.5,b\n"
        ).encode(),
        "README.md": b"synthetic source pack\n",
    }
    if omit_member is not None:
        members.pop(omit_member)
    rows = ["relative_path,size_bytes,sha256"]
    for name, raw in sorted(members.items()):
        digest = hashlib.sha256(raw).hexdigest()
        if corrupt_manifest and name == "normalized/price_history.csv":
            digest = "0" * 64
        rows.append(f"{name},{len(raw)},{digest}")
    members["reports/output_hash_manifest.csv"] = ("\n".join(rows) + "\n").encode()
    if unmanifested_member:
        members["unexpected.txt"] = b"not in manifest"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in members.items():
            archive.writestr(name, raw)
    return hashlib.sha256(path.read_bytes()).hexdigest()


class HistoricalSourcePackImportTests(unittest.TestCase):
    def test_exact_two_authoritative_source_packs_are_frozen(self) -> None:
        from src.historical_source_pack_import import AUTHORITATIVE_SOURCE_PACKS

        self.assertEqual(
            tuple(
                (
                    item.pack_id,
                    item.filename,
                    item.sha256,
                    item.member_set_sha256,
                    item.universe_dates_sha256,
                    item.market_identity_sequence_sha256,
                )
                for item in AUTHORITATIVE_SOURCE_PACKS
            ),
            (
                ("HISTORICAL_70_V1_10", "btc_daily_range_merged_70_v1_10.zip", "911f4108cd8f6fe8d14c3bcbc435062caa0fa850c9f51a9712687507ac8691ce", "065b7294bd0b1ca9bf1726891598ebce9160d281043c803ae5430d8d02985c32", "c5d57431c006a6be71c6c7ec2f2e3c780339a29cfcc179febf59ebbcd6ec08ca", "432e35c6d4b5f73d8b8f8dd66779052854551746d0d13409f11144c3335dfb71"),
                ("VALIDATION_100_V2_2", "btc_daily_range_validation_100_merged_v2_2(1).zip", "cfcf8b47acb87c3efec9a0d0f86f86dd83c7eeed919975c80dac9094ed7d1dc2", "c41cbe90df0afe1c749af23f1f3ed9e4e45b9a08b1f51db3a265deb8bee4367a", "a0978f29c94555927c03ad4116bbf53c6d78a772501937a377df672a4fd5a782", "12ebad4dd4122b5b0b636f657f912503cca3a4a3fd150c4e329bc52c94f5d01a"),
            ),
        )

    def test_verified_plan_rejects_pack_replacement_before_any_database_write(self) -> None:
        from src.historical_source_pack_import import import_source_pack, verify_source_pack

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "pack.zip"
            digest = _build_pack(path)
            plan = verify_source_pack(path, pack_id="S", expected_outer_sha256=digest)
            _build_pack(path, price_row_count=2)
            store = SqliteStore.open(root / "data.sqlite3")
            try:
                store.migrate()
                with self.assertRaisesRegex(ValueError, "SOURCE_PACK_CHANGED_AFTER_VERIFICATION"):
                    import_source_pack(store, plan)
                self.assertEqual(store.count("data_import_runs"), 0)
                self.assertEqual(store.count("source_events"), 0)
            finally:
                store.close()

    def test_distinct_import_run_records_exact_replays_and_replays_itself(self) -> None:
        from src.historical_source_pack_import import import_source_pack, verify_source_pack

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "pack.zip"
            digest = _build_pack(path)
            first_plan = verify_source_pack(path, pack_id="A", expected_outer_sha256=digest)
            second_plan = verify_source_pack(path, pack_id="B", expected_outer_sha256=digest)
            store = SqliteStore.open(root / "data.sqlite3")
            try:
                store.migrate()
                first = import_source_pack(store, first_plan)
                second = import_source_pack(store, second_plan)
                replay = import_source_pack(store, second_plan)
                self.assertTrue(first.inserted)
                self.assertTrue(second.inserted)
                self.assertFalse(replay.inserted)
                counters = store.rows(
                    "SELECT inserted_row_count,replayed_row_count FROM data_import_runs ORDER BY import_run_row_id"
                )
                self.assertEqual(counters, [(4, 0), (0, 4)])
                self.assertEqual(
                    store.scalar(
                        "SELECT COUNT(*) FROM data_import_run_events WHERE import_run_id LIKE 'historical-source-pack:B:%' AND outcome='REPLAYED'"
                    ),
                    4,
                )
            finally:
                store.close()

    def test_existing_run_replay_verifies_full_run_and_range_provenance(self) -> None:
        from src.historical_source_pack_import import import_source_pack, verify_source_pack

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "pack.zip"
            digest = _build_pack(path)
            plan = verify_source_pack(path, pack_id="A", expected_outer_sha256=digest)
            store = SqliteStore.open(root / "data.sqlite3")
            try:
                store.migrate()
                import_source_pack(store, plan)
                store._connection.execute(
                    "UPDATE data_import_runs SET source_path_fingerprint=?",
                    ("0" * 64,),
                )
                store._connection.commit()
                with self.assertRaisesRegex(ValueError, "DATA_IMPORT_RUN_CONFLICT"):
                    import_source_pack(store, plan)
            finally:
                store.close()

    def test_verified_synthetic_pack_streams_atomically_and_replays_exactly(self) -> None:
        from src.historical_source_pack_import import import_source_pack, verify_source_pack

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pack_path = root / "pack.zip"
            expected_sha = _build_pack(pack_path)
            plan = verify_source_pack(pack_path, pack_id="SYNTHETIC", expected_outer_sha256=expected_sha)
            self.assertEqual(plan.declared_row_count, 4)
            store = SqliteStore.open(root / "data.sqlite3")
            try:
                store.migrate()
                first = import_source_pack(store, plan)
                replay = import_source_pack(store, plan)
                self.assertTrue(first.inserted)
                self.assertFalse(replay.inserted)
                self.assertEqual(store.count("data_import_runs"), 1)
                self.assertEqual(store.count("source_events"), 4)
                self.assertEqual(store.count("data_import_run_events"), 4)
                self.assertEqual(store.integrity_report()["status"], "PASS")
            finally:
                store.close()

    def test_outer_or_inner_hash_mismatch_fails_before_database_write(self) -> None:
        from src.historical_source_pack_import import verify_source_pack

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pack.zip"
            digest = _build_pack(path)
            with self.assertRaisesRegex(ValueError, "SOURCE_PACK_OUTER_HASH_MISMATCH"):
                verify_source_pack(path, pack_id="S", expected_outer_sha256="0" * 64)
            digest = _build_pack(path, corrupt_manifest=True)
            with self.assertRaisesRegex(ValueError, "SOURCE_PACK_MEMBER_HASH_MISMATCH"):
                verify_source_pack(path, pack_id="S", expected_outer_sha256=digest)

    def test_zip_path_traversal_and_duplicate_entries_fail_closed(self) -> None:
        from src.historical_source_pack_import import verify_source_pack

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "traversal.zip"
            digest = _build_pack(path)
            with zipfile.ZipFile(path, "a") as archive:
                archive.writestr("../escape.txt", b"escape")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError, "SOURCE_PACK_PATH_TRAVERSAL"):
                verify_source_pack(path, pack_id="S", expected_outer_sha256=digest)

            path = root / "duplicate.zip"
            _build_pack(path)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(path, "a") as archive:
                    archive.writestr("normalized/markets.csv", b"duplicate")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError, "SOURCE_PACK_DUPLICATE_MEMBER"):
                verify_source_pack(path, pack_id="S", expected_outer_sha256=digest)

    def test_crc_corruption_fails_closed(self) -> None:
        from src.historical_source_pack_import import verify_source_pack

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "crc.zip"
            _build_pack(path)
            with zipfile.ZipFile(path) as archive:
                info = archive.getinfo("normalized/markets.csv")
                offset = info.header_offset
            raw = bytearray(path.read_bytes())
            name_length = int.from_bytes(raw[offset + 26 : offset + 28], "little")
            extra_length = int.from_bytes(raw[offset + 28 : offset + 30], "little")
            data_offset = offset + 30 + name_length + extra_length
            raw[data_offset + max(1, info.compress_size // 2)] ^= 1
            path.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            with self.assertRaisesRegex(ValueError, "SOURCE_PACK_CRC_ERROR"):
                verify_source_pack(path, pack_id="S", expected_outer_sha256=digest)

    def test_required_members_manifest_completeness_and_exact_schemas(self) -> None:
        from src.historical_source_pack_import import verify_source_pack

        cases = (
            ({"omit_member": "normalized/settlements.csv"}, "SOURCE_PACK_REQUIRED_MEMBER_MISSING"),
            ({"unmanifested_member": True}, "SOURCE_PACK_MANIFEST_MEMBER_SET_MISMATCH"),
            ({"bad_market_schema": True}, "SOURCE_PACK_CSV_SCHEMA_MISMATCH"),
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, (options, error) in enumerate(cases):
                with self.subTest(error=error):
                    path = Path(directory) / f"case-{index}.zip"
                    digest = _build_pack(path, **options)
                    with self.assertRaisesRegex(ValueError, error):
                        verify_source_pack(path, pack_id="S", expected_outer_sha256=digest)

    def test_streaming_import_has_constant_materialization_bound(self) -> None:
        from src.historical_source_pack_import import import_source_pack, verify_source_pack

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "large.zip"
            digest = _build_pack(path, price_row_count=20_000)
            plan = verify_source_pack(path, pack_id="S", expected_outer_sha256=digest)
            self.assertFalse(hasattr(plan, "events"))
            store = SqliteStore.open(root / "data.sqlite3")
            try:
                store.migrate()
                tracemalloc.start()
                result = import_source_pack(store, plan)
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                self.assertTrue(result.inserted)
                self.assertLess(peak, 24 * 1024 * 1024)
                self.assertEqual(result.max_materialized_rows, 1)
                self.assertEqual(result.processed_row_count, 20_003)
            finally:
                store.close()

    @unittest.skipUnless(
        Path("/mnt/c/Users/gegos/Downloads/btc_daily_range_merged_70_v1_10.zip").exists()
        and Path("/mnt/c/Users/gegos/Downloads/btc_daily_range_validation_100_merged_v2_2(1).zip").exists(),
        "authoritative local source packs unavailable",
    )
    def test_authoritative_70_and_100_universes_are_disjoint_with_zero_silent_drops(self) -> None:
        from src.historical_source_pack_import import verify_authoritative_source_packs

        plans = verify_authoritative_source_packs(Path("/mnt/c/Users/gegos/Downloads"))
        self.assertEqual(tuple(plan.universe_date_count for plan in plans), (70, 100))
        self.assertEqual(tuple(plan.market_row_count for plan in plans), (770, 1100))
        self.assertEqual(tuple(plan.price_row_count for plan in plans), (2_853_424, 4_144_919))
        self.assertEqual(tuple(plan.binance_row_count for plan in plans), (113_221, 163_621))
        self.assertEqual(tuple(plan.settlement_row_count for plan in plans), (70, 100))
        self.assertEqual(tuple(plan.silent_drop_count for plan in plans), (0, 0))
        self.assertEqual(
            tuple(plan.member_set_sha256 for plan in plans),
            tuple(item.member_set_sha256 for item in __import__("src.historical_source_pack_import", fromlist=["AUTHORITATIVE_SOURCE_PACKS"]).AUTHORITATIVE_SOURCE_PACKS),
        )
        self.assertEqual(
            tuple(plan.universe_dates_sha256 for plan in plans),
            tuple(item.universe_dates_sha256 for item in __import__("src.historical_source_pack_import", fromlist=["AUTHORITATIVE_SOURCE_PACKS"]).AUTHORITATIVE_SOURCE_PACKS),
        )
        self.assertEqual(tuple(plan.markets_per_date_min for plan in plans), (11, 11))
        self.assertEqual(tuple(plan.markets_per_date_max for plan in plans), (11, 11))
        self.assertEqual(sum(plan.declared_row_count for plan in plans), 7_277_225)
        self.assertTrue(plans[0].universe_dates.isdisjoint(plans[1].universe_dates))

    def test_late_invalid_row_rolls_back_whole_pack(self) -> None:
        from src import historical_source_pack_import as source_pack
        from src.historical_source_pack_import import import_source_pack, verify_source_pack

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pack_path = root / "pack.zip"
            digest = _build_pack(pack_path)
            plan = verify_source_pack(pack_path, pack_id="SYNTHETIC", expected_outer_sha256=digest)
            events = list(source_pack._iter_events(plan))
            events[-1] = dataclasses.replace(
                events[-1],
                source_timestamp_ms=max(item.end_ms for item in plan.ranges),
            )
            store = SqliteStore.open(root / "data.sqlite3")
            try:
                store.migrate()
                with mock.patch.object(source_pack, "_iter_events", return_value=iter(events)):
                    with self.assertRaisesRegex(ValueError, "SOURCE_PACK_EVENT_OUTSIDE_RANGE"):
                        import_source_pack(store, plan)
                self.assertEqual(store.count("data_import_runs"), 0)
                self.assertEqual(store.count("source_events"), 0)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
