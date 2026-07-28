from __future__ import annotations

import csv
import dataclasses
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from src.config import RuntimeConfig, load_runtime_config, validate_runtime_config
from src.registry_lock import build_executable_gap_report, verify_registry_lock


CONFIG_PATH = Path("config/c0_c1_frozen_config.json")


class ConfigTests(unittest.TestCase):
    def test_bind_is_loopback_only(self):
        cfg = load_runtime_config(CONFIG_PATH)
        self.assertEqual(cfg.bind_host, "127.0.0.1")
        self.assertEqual(cfg.bind_port, 8767)

    def test_forbidden_features_are_disabled(self):
        cfg = load_runtime_config(CONFIG_PATH)
        self.assertFalse(cfg.real_orders_enabled)
        self.assertFalse(cfg.wallet_enabled)
        self.assertFalse(cfg.paper_enabled)
        self.assertFalse(cfg.dashboard_enabled)

    def test_runtime_config_is_immutable(self):
        cfg = load_runtime_config(CONFIG_PATH)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            cfg.bind_port = 9000

    def test_non_loopback_bind_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "BIND_HOST_MUST_BE_LOOPBACK"):
            validate_runtime_config(self._valid_config(bind_host="0.0.0.0"))

    def test_port_outside_allowed_range_is_rejected(self):
        for port in (1023, 65536):
            with self.subTest(port=port):
                with self.assertRaisesRegex(ValueError, "BIND_PORT_OUT_OF_RANGE"):
                    validate_runtime_config(self._valid_config(bind_port=port))

    def test_real_orders_are_rejected_when_enabled(self):
        with self.assertRaisesRegex(ValueError, "REAL_ORDERS_MUST_BE_DISABLED"):
            validate_runtime_config(self._valid_config(real_orders_enabled=True))

    def test_wallet_is_rejected_when_enabled(self):
        with self.assertRaisesRegex(ValueError, "WALLET_MUST_BE_DISABLED"):
            validate_runtime_config(self._valid_config(wallet_enabled=True))

    def test_paper_execution_is_rejected_when_enabled(self):
        with self.assertRaisesRegex(ValueError, "PAPER_EXECUTION_MUST_BE_DISABLED"):
            validate_runtime_config(self._valid_config(paper_enabled=True))

    def test_dashboard_is_rejected_when_enabled(self):
        with self.assertRaisesRegex(ValueError, "DASHBOARD_MUST_BE_DISABLED"):
            validate_runtime_config(self._valid_config(dashboard_enabled=True))

    def test_database_writer_count_must_be_one(self):
        with self.assertRaisesRegex(ValueError, "DATABASE_WRITER_COUNT_MUST_BE_ONE"):
            validate_runtime_config(self._valid_config(database_writer_count=2))

    def test_unknown_top_level_config_key_is_rejected(self):
        payload = self._valid_payload()
        payload["unexpected"] = "value"
        with self.assertRaisesRegex(ValueError, "UNKNOWN_CONFIG_KEYS"):
            self._load_payload(payload)

    def test_missing_top_level_config_key_is_rejected(self):
        payload = self._valid_payload()
        del payload["bind_port"]
        with self.assertRaisesRegex(ValueError, "MISSING_CONFIG_KEYS"):
            self._load_payload(payload)

    def test_incorrect_config_value_type_is_rejected(self):
        payload = self._valid_payload()
        payload["bind_port"] = "8767"
        with self.assertRaisesRegex(ValueError, "INVALID_CONFIG_TYPE: bind_port"):
            self._load_payload(payload)

    def test_runtime_config_direct_type_mismatches_are_rejected(self):
        cases = (
            ("bind_host", 127001),
            ("bind_port", 8767.0),
            ("bind_port", True),
            ("real_orders_enabled", 0),
            ("wallet_enabled", 0),
            ("paper_enabled", 0),
            ("dashboard_enabled", 0),
            ("database_writer_count", True),
            ("database_writer_count", 1.0),
        )

        for field, value in cases:
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError) as caught:
                    validate_runtime_config(self._valid_config(**{field: value}))
                self.assertEqual(str(caught.exception), f"INVALID_CONFIG_TYPE: {field}")

    @staticmethod
    def _valid_payload():
        return {
            "bind_host": "127.0.0.1",
            "bind_port": 8767,
            "real_orders_enabled": False,
            "wallet_enabled": False,
            "paper_enabled": False,
            "dashboard_enabled": False,
            "database_writer_count": 1,
        }

    def _load_payload(self, payload):
        with tempfile.TemporaryDirectory() as temp_directory:
            path = Path(temp_directory, "config.json")
            path.write_text(json.dumps(payload), encoding="utf-8")
            return load_runtime_config(path)

    @staticmethod
    def _valid_config(**overrides):
        values = ConfigTests._valid_payload()
        values.update(overrides)
        return RuntimeConfig(**values)


class RegistryLockTests(unittest.TestCase):
    LOCK_PATH = Path("contract/STRATEGY_REGISTRY_47_LIVE_BACKEND_LOCK.json")
    REGISTRY_JSON_PATH = Path("registry/STRATEGY_REGISTRY_47.json")
    REGISTRY_CSV_PATH = Path("registry/STRATEGY_REGISTRY_47.csv")

    def test_canonical_registry_passes_verification(self):
        report = self._verify()
        self.assertEqual(report.ordered_strategy_ids, self._locked_strategy_ids())

    def test_strategy_count_is_47(self):
        self.assertEqual(self._verify().strategy_count, 47)

    def test_unique_strategy_ids_are_47(self):
        self.assertEqual(self._verify().unique_strategy_ids, 47)

    def test_v1_count_is_34(self):
        self.assertEqual(self._verify().v1_count, 34)

    def test_v2_count_is_13(self):
        self.assertEqual(self._verify().v2_count, 13)

    def test_json_hash_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path, json_path, csv_path = self._copy_inputs(Path(directory))
            json_path.write_bytes(json_path.read_bytes() + b"\n")
            self._assert_lock_error(
                "REGISTRY_JSON_HASH_MISMATCH", lock_path, json_path, csv_path
            )

    def test_csv_hash_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path, json_path, csv_path = self._copy_inputs(Path(directory))
            csv_path.write_bytes(csv_path.read_bytes() + b"\r\n")
            self._assert_lock_error(
                "REGISTRY_CSV_HASH_MISMATCH", lock_path, json_path, csv_path
            )

    def test_count_other_than_47_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path, json_path, csv_path = self._copy_inputs(Path(directory))
            lock = self._read_json(lock_path)
            lock["strategy_count"] = 46
            self._write_json(lock_path, lock)
            self._assert_lock_error(
                "REGISTRY_COUNT_MISMATCH", lock_path, json_path, csv_path
            )

    def test_duplicate_strategy_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path, json_path, csv_path = self._copy_inputs(Path(directory))
            registry = self._read_json(json_path)
            registry["strategies"][1]["strategy_id"] = registry["strategies"][0][
                "strategy_id"
            ]
            self._write_json(json_path, registry)
            self._refresh_lock_hashes(lock_path, json_path, csv_path)
            self._assert_lock_error(
                "DUPLICATE_STRATEGY_ID", lock_path, json_path, csv_path
            )

    def test_missing_canonical_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path, json_path, csv_path = self._copy_inputs(Path(directory))
            registry = self._read_json(json_path)
            registry["strategies"].pop()
            self._write_json(json_path, registry)
            self._refresh_lock_hashes(lock_path, json_path, csv_path)
            self._assert_lock_error(
                "MISSING_REGISTRY_ID", lock_path, json_path, csv_path
            )

    def test_unexpected_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path, json_path, csv_path = self._copy_inputs(Path(directory))
            registry = self._read_json(json_path)
            unexpected = dict(registry["strategies"][-1])
            unexpected["strategy_id"] = "UNEXPECTED_TEST_STRATEGY"
            registry["strategies"].append(unexpected)
            self._write_json(json_path, registry)
            self._refresh_lock_hashes(lock_path, json_path, csv_path)
            self._assert_lock_error(
                "UNEXPECTED_REGISTRY_ID", lock_path, json_path, csv_path
            )

    def test_missing_or_invalid_lineage_is_rejected(self):
        cases = (
            ("json_missing", "REGISTRY_LINEAGE_MISSING"),
            ("json_invalid", "REGISTRY_LINEAGE_MISMATCH"),
            ("csv_missing", "REGISTRY_LINEAGE_MISSING"),
            ("csv_invalid", "REGISTRY_LINEAGE_MISMATCH"),
        )
        for mutation, expected_code in cases:
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as directory:
                    lock_path, json_path, csv_path = self._copy_inputs(Path(directory))
                    if mutation.startswith("json"):
                        registry = self._read_json(json_path)
                        if mutation.endswith("missing"):
                            del registry["strategies"][0]["version"]
                        else:
                            registry["strategies"][0]["version"] = "V3"
                        self._write_json(json_path, registry)
                    else:
                        fieldnames, rows = self._read_csv(csv_path)
                        rows[0]["version"] = "" if mutation.endswith("missing") else "V3"
                        self._write_csv(csv_path, fieldnames, rows)
                    self._refresh_lock_hashes(lock_path, json_path, csv_path)
                    self._assert_lock_error(
                        expected_code, lock_path, json_path, csv_path
                    )

    def test_invalid_top_level_shape_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path, json_path, csv_path = self._copy_inputs(Path(directory))
            self._write_json(json_path, [])
            self._refresh_lock_hashes(lock_path, json_path, csv_path)
            self._assert_lock_error(
                "INVALID_REGISTRY_SHAPE", lock_path, json_path, csv_path
            )

    def test_invalid_primitive_type_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path, json_path, csv_path = self._copy_inputs(Path(directory))
            lock = self._read_json(lock_path)
            lock["strategy_count"] = True
            self._write_json(lock_path, lock)
            self._assert_lock_error(
                "INVALID_REGISTRY_TYPE", lock_path, json_path, csv_path
            )

    def test_gap_report_has_no_executable_strategies(self):
        report = self._gap_report()
        self.assertEqual(report["executable_now"], 0)

    def test_gap_report_requires_parity_for_all_47(self):
        report = self._gap_report()
        self.assertEqual(report["requires_parity"], 47)

    def test_all_gap_rows_are_locked_but_non_executable(self):
        strategies = self._gap_report()["strategies"]
        self.assertEqual(len(strategies), 47)
        expected_status = {
            "registry_locked": True,
            "exact_rule_source_locked": False,
            "historical_parity": "NOT_RUN",
            "runtime_status": "DISABLED_EXECUTABLE_RULE_GAP",
        }
        for row in strategies:
            with self.subTest(strategy_id=row["strategy_id"]):
                self.assertEqual(
                    {key: row[key] for key in expected_status}, expected_status
                )

    def test_gap_report_order_matches_lock(self):
        strategy_ids = [
            row["strategy_id"] for row in self._gap_report()["strategies"]
        ]
        self.assertEqual(tuple(strategy_ids), self._locked_strategy_ids())

    def _verify(self):
        return verify_registry_lock(
            self.LOCK_PATH, self.REGISTRY_JSON_PATH, self.REGISTRY_CSV_PATH
        )

    def _gap_report(self):
        return build_executable_gap_report(
            self.LOCK_PATH, self.REGISTRY_JSON_PATH, self.REGISTRY_CSV_PATH
        )

    def _locked_strategy_ids(self):
        return tuple(self._read_json(self.LOCK_PATH)["strategy_ids"])

    @classmethod
    def _copy_inputs(cls, directory):
        lock_path = directory / cls.LOCK_PATH.name
        json_path = directory / cls.REGISTRY_JSON_PATH.name
        csv_path = directory / cls.REGISTRY_CSV_PATH.name
        lock_path.write_bytes(cls.LOCK_PATH.read_bytes())
        json_path.write_bytes(cls.REGISTRY_JSON_PATH.read_bytes())
        csv_path.write_bytes(cls.REGISTRY_CSV_PATH.read_bytes())
        return lock_path, json_path, csv_path

    @staticmethod
    def _read_json(path):
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_json(path, payload):
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    @staticmethod
    def _read_csv(path):
        with io.StringIO(path.read_bytes().decode("utf-8-sig"), newline="") as stream:
            reader = csv.DictReader(stream)
            return reader.fieldnames, list(reader)

    @staticmethod
    def _write_csv(path, fieldnames, rows):
        with io.StringIO(newline="") as stream:
            writer = csv.DictWriter(
                stream, fieldnames=fieldnames, lineterminator="\r\n"
            )
            writer.writeheader()
            writer.writerows(rows)
            encoded = stream.getvalue().encode("utf-8")
        path.write_bytes(bytes((0xEF, 0xBB, 0xBF)) + encoded)

    @classmethod
    def _refresh_lock_hashes(cls, lock_path, json_path, csv_path):
        lock = cls._read_json(lock_path)
        lock["registry_json"]["sha256"] = hashlib.sha256(json_path.read_bytes()).hexdigest()
        lock["registry_csv"]["sha256"] = hashlib.sha256(csv_path.read_bytes()).hexdigest()
        cls._write_json(lock_path, lock)

    def _assert_lock_error(self, code, lock_path, json_path, csv_path):
        with self.assertRaises(ValueError) as caught:
            verify_registry_lock(lock_path, json_path, csv_path)
        self.assertTrue(
            str(caught.exception).startswith(code),
            f"{str(caught.exception)!r} does not start with {code!r}",
        )


if __name__ == "__main__":
    unittest.main()
