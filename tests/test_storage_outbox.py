from __future__ import annotations

import dataclasses
import unittest

from src.fixed_point import (
    ProbabilityMicros,
    SharesMicros,
    UsdMicros,
    probability_to_micros,
)
from src.models import (
    CanonicalSnapshot,
    OutboxEvent,
    SignalRecord,
    SourceEvent,
    StrategyEvaluation,
)


class DomainTests(unittest.TestCase):
    def test_probability_string_converts_exactly(self):
        self.assertEqual(probability_to_micros("0.456"), 456000)
        self.assertEqual(ProbabilityMicros.from_decimal("0.456").value, 456000)

    def test_probability_boundaries_are_allowed(self):
        self.assertEqual(probability_to_micros("0"), 0)
        self.assertEqual(probability_to_micros("1"), 1_000_000)

    def test_probability_outside_unit_interval_is_rejected(self):
        for value in ("1.001", "-0.000001"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "PROBABILITY_OUT_OF_RANGE"):
                    probability_to_micros(value)

    def test_float_is_rejected_at_fixed_point_boundaries(self):
        for fixed_type in (ProbabilityMicros, SharesMicros, UsdMicros):
            with self.subTest(fixed_type=fixed_type.__name__):
                with self.assertRaisesRegex(ValueError, "FIXED_POINT_FLOAT_FORBIDDEN"):
                    fixed_type.from_decimal(0.5)

    def test_shares_and_usd_are_stored_as_integer_micros(self):
        shares = SharesMicros.from_decimal("1.25")
        usd = UsdMicros.from_decimal("42.000001")
        self.assertEqual(shares.value, 1_250_000)
        self.assertEqual(usd.value, 42_000_001)
        self.assertIs(type(shares.value), int)
        self.assertIs(type(usd.value), int)

    def test_binance_closed_kline_natural_key_is_deterministic(self):
        event = SourceEvent.binance_closed_kline(
            symbol="BTCUSDT",
            interval="1m",
            open_time_ms=1000,
            payload=b"{}",
        )
        self.assertEqual(event.natural_key, "binance:BTCUSDT:1m:1000")

    def test_equivalent_payloads_have_the_same_canonical_hash(self):
        first = self._event(payload=b'{"b":2,"a":1}')
        second = self._event(payload=b'{ "a": 1, "b": 2 }')
        self.assertEqual(first.payload_json, '{"a":1,"b":2}')
        self.assertEqual(first.payload_sha256, second.payload_sha256)

    def test_changed_payload_has_a_different_hash(self):
        first = self._event(payload=b'{"close":"100"}')
        second = self._event(payload=b'{"close":"101"}')
        self.assertNotEqual(first.payload_sha256, second.payload_sha256)

    def test_committed_domain_records_are_immutable(self):
        event = self._event(payload=b"{}")
        snapshot = CanonicalSnapshot(
            snapshot_key="snapshot:1",
            source_event_ids=(1,),
            payload_json="{}",
            payload_sha256=event.payload_sha256,
            created_at_ms=1001,
            recovery_origin="LIVE",
        )
        signal = SignalRecord(
            identity_key="signal:1",
            evaluation_key="evaluation:1",
            strategy_id="CANARY_SYNC_READY_V1",
            signal_type="SYNC_READY",
            payload_json="{}",
            created_at_ms=1002,
        )
        outbox = OutboxEvent(
            event_id=1,
            topic="signal.created",
            payload_json="{}",
            created_at_ms=1002,
        )
        records_and_fields = (
            (event, "natural_key"),
            (snapshot, "snapshot_key"),
            (signal, "identity_key"),
            (outbox, "event_id"),
        )
        for record, field in records_and_fields:
            with self.subTest(record=type(record).__name__):
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    setattr(record, field, "changed")

    def test_recovered_origin_is_explicit(self):
        event = self._event(
            payload=b"{}",
            recovery_origin="RECOVERED_AFTER_DOWNTIME",
        )
        self.assertEqual(event.recovery_origin, "RECOVERED_AFTER_DOWNTIME")

    def test_execution_eligibility_is_an_explicit_boolean(self):
        evaluation = StrategyEvaluation(
            evaluation_key="evaluation:1",
            strategy_id="CANARY_SYNC_READY_V1",
            strategy_version="1",
            status="SIGNAL",
            input_snapshot_hash="a" * 64,
            evaluation_revision=1,
            execution_eligible=False,
            evaluated_at_ms=1002,
            payload_json="{}",
        )
        self.assertIs(evaluation.execution_eligible, False)

        with self.assertRaisesRegex(
            ValueError, "INVALID_EXECUTION_ELIGIBILITY_TYPE"
        ):
            dataclasses.replace(evaluation, execution_eligible=0)

    @staticmethod
    def _event(
        *,
        payload: bytes,
        recovery_origin: str = "LIVE",
    ) -> SourceEvent:
        return SourceEvent.binance_closed_kline(
            symbol="BTCUSDT",
            interval="1m",
            open_time_ms=1000,
            payload=payload,
            received_timestamp_ms=1001,
            recovery_origin=recovery_origin,
        )


if __name__ == "__main__":
    unittest.main()
