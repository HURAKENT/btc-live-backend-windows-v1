from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from src.config import RuntimeConfig, load_runtime_config, validate_runtime_config


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


if __name__ == "__main__":
    unittest.main()
