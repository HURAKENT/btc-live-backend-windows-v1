from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = PROJECT_ROOT / "tools" / "polymarket_ws_wire_shape_probe.py"


def _load_probe():
    if not TOOL_PATH.is_file():
        return None
    spec = importlib.util.spec_from_file_location(
        "_polymarket_ws_wire_shape_probe_test",
        TOOL_PATH,
    )
    if spec is None or spec.loader is None:
        raise AssertionError("probe import specification is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROBE = _load_probe()
PROBE_AVAILABLE = PROBE is not None


def _market_book(**overrides):
    payload = {
        "event_type": "book",
        "asset_id": "raw-asset-yes-123456789",
        "market": "raw-market-123456789",
        "timestamp": "1785556800000",
        "hash": "book-hash-1",
        "bids": [],
        "asks": [],
    }
    payload.update(overrides)
    return payload


def _sample_report():
    return PROBE.build_probe_report(
        probe_metadata={
            "probe_id": "PROBE-TEST",
            "generated_at_utc": "2026-07-29T00:00:00Z",
            "source_commit": "a" * 40,
            "python_version": "3.12.4",
            "aiohttp_version": "3.14.3",
        },
        discovery_summary={
            "selected": True,
            "event_identity_sha256": "1" * 64,
            "market_count": 11,
            "asset_count": 22,
            "selected_market_identity_sha256": "2" * 64,
            "selected_market_order_index": 0,
            "subscribed_asset_count": 2,
            "subscribed_asset_set_sha256": "3" * 64,
            "token_role_count": {"YES": 1, "NO": 1},
        },
        subscription_summary={
            "sent": True,
            "asset_count": 2,
            "payload_sha256": "4" * 64,
        },
        frames=[
            PROBE.summarize_ws_frame(
                sequence=1,
                transport_type="TEXT",
                raw_bytes=json.dumps(_market_book()).encode("utf-8"),
                monotonic_offset_ms=1,
            )
        ],
        counters={
            "gamma_discovery_sequences": 1,
            "websocket_connections": 1,
            "subscriptions_sent": 1,
            "frames_received": 1,
            "inbound_bytes": 1,
            "reconnect_attempts": 0,
        },
    )


class ProbeExistenceTests(unittest.TestCase):
    def test_tool_exists_and_imports(self):
        self.assertTrue(TOOL_PATH.is_file())
        self.assertIsNotNone(PROBE)


class DirectExecutionTests(unittest.TestCase):
    def test_real_direct_help_succeeds(self):
        result = subprocess.run(
            [sys.executable, str(TOOL_PATH), "--help"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

        self.assertEqual(
            result.returncode,
            0,
            result.stdout + result.stderr,
        )
        self.assertIn("usage:", result.stdout.lower())
        self.assertNotIn("Traceback", result.stderr)

    def test_real_direct_invalid_config_exits_thirty_without_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "probe.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(TOOL_PATH),
                    "--output",
                    str(output.resolve()),
                    "--max-frames",
                    "11",
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )

            self.assertEqual(
                result.returncode,
                30,
                result.stdout + result.stderr,
            )
            self.assertIn("INVALID_MAX_FRAMES", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse(output.exists())

    def test_module_import_does_not_change_sys_path(self):
        program = (
            "import json, sys\n"
            "before = list(sys.path)\n"
            "import tools.polymarket_ws_wire_shape_probe\n"
            "print(json.dumps({'same': before == sys.path}))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

        self.assertEqual(
            result.returncode,
            0,
            result.stdout + result.stderr,
        )
        self.assertEqual(json.loads(result.stdout), {"same": True})
        self.assertNotIn("Traceback", result.stderr)


@unittest.skipUnless(PROBE_AVAILABLE, "probe implementation is not present")
class StructuralClassificationTests(unittest.TestCase):
    def test_single_market_data_object(self):
        result = PROBE.classify_json_structure(_market_book())
        self.assertEqual(result["classification"], "SINGLE_MARKET_OBJECT")

    def test_single_service_object(self):
        result = PROBE.classify_json_structure(
            {"type": "subscribed", "message": "ok"}
        )
        self.assertEqual(result["classification"], "SINGLE_SERVICE_OBJECT")

    def test_unknown_object(self):
        result = PROBE.classify_json_structure({"future": {"shape": 1}})
        self.assertEqual(result["classification"], "SINGLE_UNKNOWN_OBJECT")

    def test_array_of_market_data_objects(self):
        result = PROBE.classify_json_structure(
            [_market_book(), _market_book(hash="book-hash-2")]
        )
        self.assertEqual(result["classification"], "ARRAY_OF_MARKET_OBJECTS")
        self.assertEqual(result["structure"]["object_element_count"], 2)

    def test_array_of_service_objects(self):
        result = PROBE.classify_json_structure(
            [{"type": "subscribed"}, {"type": "connected"}]
        )
        self.assertEqual(result["classification"], "ARRAY_OF_SERVICE_OBJECTS")

    def test_array_of_unknown_objects(self):
        result = PROBE.classify_json_structure([{"a": 1}, {"b": 2}])
        self.assertEqual(
            result["classification"],
            "ARRAY_OF_OBJECTS_UNKNOWN",
        )

    def test_mixed_array(self):
        result = PROBE.classify_json_structure([_market_book(), 1, None])
        self.assertEqual(result["classification"], "ARRAY_MIXED")
        self.assertTrue(result["structure"]["mixed_type"])

    def test_empty_array(self):
        result = PROBE.classify_json_structure([])
        self.assertEqual(result["classification"], "ARRAY_EMPTY")
        self.assertTrue(result["structure"]["empty"])

    def test_json_string(self):
        result = PROBE.classify_json_structure("provider value")
        self.assertEqual(result["classification"], "JSON_PRIMITIVE")
        self.assertEqual(result["top_level_json_type"], "string")

    def test_json_number(self):
        result = PROBE.classify_json_structure(3)
        self.assertEqual(result["top_level_json_type"], "number")

    def test_json_boolean(self):
        result = PROBE.classify_json_structure(True)
        self.assertEqual(result["top_level_json_type"], "boolean")

    def test_json_null(self):
        result = PROBE.classify_json_structure(None)
        self.assertEqual(result["top_level_json_type"], "null")

    def test_malformed_json_text(self):
        result = PROBE.summarize_ws_frame(
            sequence=1,
            transport_type="TEXT",
            raw_bytes=b"{",
            monotonic_offset_ms=5,
        )
        self.assertEqual(result["classification"], "NON_JSON_TEXT")
        self.assertEqual(result["json_decode_status"], "ERROR")
        self.assertNotIn("raw_content", result)
        self.assertNotIn("payload_excerpt", result)

    def test_binary_frame(self):
        result = PROBE.summarize_ws_frame(
            sequence=1,
            transport_type="BINARY",
            raw_bytes=b"\x00\x01",
            monotonic_offset_ms=5,
        )
        self.assertEqual(result["classification"], "BINARY_FRAME")

    def test_control_frame(self):
        result = PROBE.summarize_ws_frame(
            sequence=1,
            transport_type="PONG",
            raw_bytes=b"",
            monotonic_offset_ms=5,
        )
        self.assertEqual(result["classification"], "CONTROL_FRAME")

    def test_arbitrary_string_values_are_not_persisted(self):
        secret_value = "arbitrary-provider-value-must-not-survive"
        result = PROBE.classify_json_structure(
            {"event_type": "book", "note": secret_value}
        )
        self.assertNotIn(secret_value, json.dumps(result))

    def test_allowed_discriminator_is_bounded(self):
        self.assertEqual(PROBE.sanitize_discriminator("book"), "book")
        self.assertIsNone(PROBE.sanitize_discriminator("x" * 65))
        self.assertIsNone(PROBE.sanitize_discriminator("line\nbreak"))
        self.assertIsNone(PROBE.sanitize_discriminator("9" * 40))

    def test_object_key_order_does_not_change_structural_hash(self):
        left = PROBE.summarize_ws_frame(
            sequence=1,
            transport_type="TEXT",
            raw_bytes=b'{"event_type":"book","asset_id":"x","hash":"h",'
            b'"timestamp":"1","bids":[],"asks":[]}',
            monotonic_offset_ms=1,
        )
        right = PROBE.summarize_ws_frame(
            sequence=1,
            transport_type="TEXT",
            raw_bytes=b'{"asks":[],"bids":[],"timestamp":"1","hash":"h",'
            b'"asset_id":"x","event_type":"book"}',
            monotonic_offset_ms=1,
        )
        self.assertEqual(
            left["structural_summary_sha256"],
            right["structural_summary_sha256"],
        )

    def test_array_structural_hash_policy_is_deterministic(self):
        first = [_market_book(), {"type": "subscribed"}]
        second = [
            dict(reversed(list(_market_book().items()))),
            {"type": "subscribed"},
        ]
        left = PROBE.classify_json_structure(first)
        right = PROBE.classify_json_structure(second)
        self.assertEqual(
            left["structural_summary_sha256"],
            right["structural_summary_sha256"],
        )


@unittest.skipUnless(PROBE_AVAILABLE, "probe implementation is not present")
class BoundAndSecurityTests(unittest.TestCase):
    def test_frame_larger_than_one_mib_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "FRAME_BYTES_BOUND"):
            PROBE.enforce_frame_bounds(
                frame_count=1,
                frame_bytes=PROBE.MAX_FRAME_BYTES + 1,
                total_bytes=PROBE.MAX_FRAME_BYTES + 1,
                max_frames=10,
            )

    def test_total_byte_bound_is_enforced(self):
        with self.assertRaisesRegex(ValueError, "TOTAL_BYTES_BOUND"):
            PROBE.enforce_frame_bounds(
                frame_count=4,
                frame_bytes=1,
                total_bytes=PROBE.MAX_TOTAL_BYTES + 1,
                max_frames=10,
            )

    def test_maximum_ten_frames_is_enforced(self):
        with self.assertRaisesRegex(ValueError, "FRAME_COUNT_BOUND"):
            PROBE.enforce_frame_bounds(
                frame_count=11,
                frame_bytes=1,
                total_bytes=11,
                max_frames=10,
            )

    def test_timeout_maximum_thirty_is_enforced(self):
        with self.assertRaisesRegex(ValueError, "INVALID_TIMEOUT_SECONDS"):
            PROBE.validate_bounds(max_frames=10, timeout_seconds=31)

    def test_reconnect_counter_remains_zero(self):
        report = _sample_report()
        self.assertEqual(report["bounds"]["reconnect_attempts"], 0)

    def test_raw_frame_is_absent_from_report(self):
        raw = json.dumps(_market_book()).encode("utf-8")
        report = _sample_report()
        serialized = PROBE.canonical_report_bytes(report)
        self.assertNotIn(raw, serialized)
        self.assertNotIn('"raw_frame":', serialized.decode("utf-8"))
        self.assertNotIn('"raw_payload":', serialized.decode("utf-8"))

    def test_raw_asset_id_is_rejected(self):
        raw_asset = "raw-asset-yes-123456789"
        report = _sample_report()
        report["leak"] = raw_asset
        report["report_sha256"] = hashlib.sha256(
            PROBE._canonical_bytes(
                {
                    key: value
                    for key, value in report.items()
                    if key != "report_sha256"
                }
            )
        ).hexdigest()
        with self.assertRaisesRegex(ValueError, "RAW_IDENTIFIER"):
            PROBE.validate_report_security(
                report=report,
                forbidden_identifiers={raw_asset},
                raw_frames=[],
            )

    def test_raw_market_and_event_ids_are_rejected(self):
        for identifier in ("event-733270", "market-3030283"):
            report = _sample_report()
            report["leak"] = identifier
            report["report_sha256"] = hashlib.sha256(
                PROBE._canonical_bytes(
                    {
                        key: value
                        for key, value in report.items()
                        if key != "report_sha256"
                    }
                )
            ).hexdigest()
            with self.subTest(identifier=identifier):
                with self.assertRaisesRegex(ValueError, "RAW_IDENTIFIER"):
                    PROBE.validate_report_security(
                        report=report,
                        forbidden_identifiers={identifier},
                        raw_frames=[],
                    )

    def test_report_formatting_is_deterministic(self):
        report = _sample_report()
        first = PROBE.canonical_report_bytes(report)
        second = PROBE.canonical_report_bytes(
            json.loads(first.decode("utf-8"))
        )
        self.assertEqual(first, second)
        self.assertTrue(first.endswith(b"\n"))

    def test_report_sha_validates(self):
        report = _sample_report()
        self.assertTrue(PROBE.verify_report_sha256(report))

    def test_nan_and_infinity_are_rejected(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    PROBE.canonical_report_bytes({"value": value})

    def test_import_has_no_network_side_effect(self):
        self.assertFalse(PROBE.IMPORT_PERFORMED_NETWORK_IO)

    def test_tool_never_imports_or_opens_sqlite(self):
        source = TOOL_PATH.read_text(encoding="utf-8")
        self.assertNotIn("sqlite3", source)
        self.assertNotIn("SqliteStore", source)
        self.assertFalse(PROBE.SQLITE_USED)

    def test_no_auth_order_or_wallet_surface(self):
        report = _sample_report()
        self.assertFalse(report["scope"]["auth_used"])
        self.assertFalse(report["scope"]["order_surface_used"])
        self.assertFalse(report["scope"]["wallet_surface_used"])

    def test_cli_rejects_max_frames_above_ten(self):
        self.assertEqual(
            PROBE.main(
                [
                    "--output",
                    str((PROJECT_ROOT.parent / "probe.json").resolve()),
                    "--max-frames",
                    "11",
                ]
            ),
            30,
        )

    def test_cli_rejects_timeout_above_thirty(self):
        self.assertEqual(
            PROBE.main(
                [
                    "--output",
                    str((PROJECT_ROOT.parent / "probe.json").resolve()),
                    "--timeout-seconds",
                    "31",
                ]
            ),
            30,
        )

    def test_production_contract_constants_are_exact(self):
        production = (
            PROJECT_ROOT / "src" / "polymarket_provider.py"
        ).read_text(encoding="utf-8")
        runtime = (
            PROJECT_ROOT / "src" / "runtime_orchestrator.py"
        ).read_text(encoding="utf-8")
        self.assertIn(PROBE.MARKET_WEBSOCKET_URL, production)
        self.assertIn(PROBE.GAMMA_EVENTS_URL, runtime)
        self.assertEqual(
            PROBE.SUBSCRIPTION_STATIC_FIELDS,
            {"type": "market", "custom_feature_enabled": True},
        )


if __name__ == "__main__":
    unittest.main()
