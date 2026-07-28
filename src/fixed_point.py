from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Self


_MICROS_PER_UNIT = Decimal(1_000_000)
_MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807


def _decimal_to_micros(
    value: str | int | Decimal,
    *,
    range_code: str,
    maximum: int,
) -> int:
    if type(value) is float:
        raise ValueError("FIXED_POINT_FLOAT_FORBIDDEN")
    if type(value) not in (str, int, Decimal):
        raise ValueError("INVALID_FIXED_POINT_TYPE")

    try:
        decimal_value = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError("INVALID_FIXED_POINT_DECIMAL") from None

    if not decimal_value.is_finite():
        raise ValueError("INVALID_FIXED_POINT_DECIMAL")

    scaled = decimal_value * _MICROS_PER_UNIT
    integral = scaled.to_integral_value()
    if scaled != integral:
        raise ValueError("FIXED_POINT_PRECISION_LOSS")

    micros = int(integral)
    if not 0 <= micros <= maximum:
        raise ValueError(range_code)
    return micros


def probability_to_micros(value: str | int | Decimal) -> int:
    return _decimal_to_micros(
        value,
        range_code="PROBABILITY_OUT_OF_RANGE",
        maximum=1_000_000,
    )


@dataclass(frozen=True, slots=True)
class ProbabilityMicros:
    value: int

    def __post_init__(self) -> None:
        if type(self.value) is not int:
            raise ValueError("INVALID_PROBABILITY_MICROS_TYPE")
        if not 0 <= self.value <= 1_000_000:
            raise ValueError("PROBABILITY_OUT_OF_RANGE")

    @classmethod
    def from_decimal(cls, value: str | int | Decimal) -> Self:
        return cls(probability_to_micros(value))


@dataclass(frozen=True, slots=True)
class SharesMicros:
    value: int

    def __post_init__(self) -> None:
        if type(self.value) is not int:
            raise ValueError("INVALID_SHARES_MICROS_TYPE")
        if not 0 <= self.value <= _MAX_SQLITE_INTEGER:
            raise ValueError("SHARES_OUT_OF_RANGE")

    @classmethod
    def from_decimal(cls, value: str | int | Decimal) -> Self:
        return cls(
            _decimal_to_micros(
                value,
                range_code="SHARES_OUT_OF_RANGE",
                maximum=_MAX_SQLITE_INTEGER,
            )
        )


@dataclass(frozen=True, slots=True)
class UsdMicros:
    value: int

    def __post_init__(self) -> None:
        if type(self.value) is not int:
            raise ValueError("INVALID_USD_MICROS_TYPE")
        if not 0 <= self.value <= _MAX_SQLITE_INTEGER:
            raise ValueError("USD_OUT_OF_RANGE")

    @classmethod
    def from_decimal(cls, value: str | int | Decimal) -> Self:
        return cls(
            _decimal_to_micros(
                value,
                range_code="USD_OUT_OF_RANGE",
                maximum=_MAX_SQLITE_INTEGER,
            )
        )
