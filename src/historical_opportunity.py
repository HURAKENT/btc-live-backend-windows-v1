from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from src.strategy_dispatch import StrategyDispatcher
from src.strategy_v1 import V1_IDENTITY_POLICIES
from src.strategy_v1_parity import historical_identity_bindings


_TOLERANCE = 1e-9
_EXPECTED_COUNTS = {
    "historical": 170,
    "u1": 131,
    "u2": 82,
    "early_confidence": 136,
    "early_confidence_u1": 106,
    "early_confidence_u2": 69,
}
_V1_PARITY_FIXTURES = {
    "PARITY_EARLY_CONFIDENCE": "V1_EARLY_CONFIDENCE_FULL_DECISION_PARITY.jsonl",
    "PARITY_EARLY_HORIZON": "V1_EARLY_HORIZON_FULL_DECISION_PARITY.jsonl",
    "PARITY_CONFIRMATION": "V1_CONFIRMATION_BASKET_FULL_DECISION_PARITY.jsonl",
    "PARITY_BASKET": "V1_CONFIRMATION_BASKET_FULL_DECISION_PARITY.jsonl",
}


@dataclass(frozen=True, slots=True)
class HistoricalOpportunityMap:
    dispatcher: StrategyDispatcher
    historical_dates: tuple[str, ...]
    u1_dates: frozenset[str]
    u2_dates: frozenset[str]
    early_confidence_dates: frozenset[str]
    confirmation_dates: frozenset[str]

    @classmethod
    def reproduce(
        cls,
        *,
        dispatcher: StrategyDispatcher,
        matrix: Mapping[tuple[str, int], tuple[dict[str, Any], ...]],
        confirmation_dates: frozenset[str],
    ) -> "HistoricalOpportunityMap":
        historical_dates = tuple(sorted({market_date for market_date, _ in matrix}))
        u1_dates: set[str] = set()
        u2_dates: set[str] = set()
        for market_date in historical_dates:
            for checkpoint in (60, 30):
                buckets = matrix.get((market_date, checkpoint))
                if buckets is None or len(buckets) != 11:
                    raise ValueError(
                        f"AHR_OPPORTUNITY_CHECKPOINT_MISSING:{market_date}:{checkpoint}"
                    )
                selected = min(
                    buckets,
                    key=lambda row: (
                        -(float(row["model_p"]) - float(row["q_yes"])),
                        int(row["bucket_index"]),
                    ),
                )
                edge = float(selected["model_p"]) - float(selected["q_yes"])
                q_yes = float(selected["q_yes"])
                if edge < 0.02 - _TOLERANCE or q_yes > 0.97 + _TOLERANCE:
                    continue
                u1_dates.add(market_date)
                maximum = max(float(row["q_yes"]) for row in buckets)
                favorites = tuple(
                    row
                    for row in buckets
                    if abs(float(row["q_yes"]) - maximum) <= _TOLERANCE
                )
                if (
                    len(favorites) == 1
                    and int(favorites[0]["bucket_index"])
                    == int(selected["bucket_index"])
                ):
                    u2_dates.add(market_date)
                break
        early_confidence = (
            frozenset(historical_dates[34:])
            if len(historical_dates) == 170
            else frozenset(historical_dates)
        )
        return cls(
            dispatcher=dispatcher,
            historical_dates=historical_dates,
            u1_dates=frozenset(u1_dates),
            u2_dates=frozenset(u2_dates),
            early_confidence_dates=early_confidence,
            confirmation_dates=confirmation_dates,
        )

    def counts(self) -> dict[str, int]:
        early = self.early_confidence_dates
        return {
            "historical": len(self.historical_dates),
            "u1": len(self.u1_dates),
            "u2": len(self.u2_dates),
            "early_confidence": len(early),
            "early_confidence_u1": len(early & self.u1_dates),
            "early_confidence_u2": len(early & self.u2_dates),
        }

    def validate_canonical_counts(self) -> dict[str, int]:
        observed = self.counts()
        if observed != _EXPECTED_COUNTS:
            raise ValueError(
                f"AHR_OPPORTUNITY_COUNT_MISMATCH:{observed}:{_EXPECTED_COUNTS}"
            )
        if not self.u2_dates.issubset(self.u1_dates):
            raise ValueError("AHR_OPPORTUNITY_U2_NOT_SUBSET_U1")
        return observed

    def parity_fixture_name(self, strategy_id: str) -> str:
        binding = self.dispatcher.binding(strategy_id)
        try:
            return _V1_PARITY_FIXTURES[binding.parity_artifact_id]
        except KeyError:
            raise ValueError(
                f"AHR_PARITY_POPULATION_UNKNOWN:{strategy_id}:"
                f"{binding.parity_artifact_id}"
            ) from None

    def component_universes(
        self,
        *,
        strategy_id: str,
        market_date: str,
        checkpoint_minutes: int,
    ) -> tuple[str, ...]:
        binding = self.dispatcher.binding(strategy_id)
        if binding.version != "V1":
            return ()
        policy = V1_IDENTITY_POLICIES.get(strategy_id)
        if policy is None or checkpoint_minutes not in policy.checkpoints:
            return ()
        if market_date not in self.historical_dates:
            return ()
        parity_population = binding.parity_artifact_id
        if (
            parity_population == "PARITY_EARLY_CONFIDENCE"
            and market_date not in self.early_confidence_dates
        ):
            return ()
        if (
            parity_population == "PARITY_CONFIRMATION"
            and market_date not in self.confirmation_dates
        ):
            return ()
        if parity_population not in _V1_PARITY_FIXTURES:
            raise ValueError(
                f"AHR_PARITY_POPULATION_UNKNOWN:{strategy_id}:{parity_population}"
            )

        historical = historical_identity_bindings().get(strategy_id)
        if historical is None:
            return ("ALL",)
        if historical.universe == "ALL":
            return ("ALL",)
        if historical.universe == "U1":
            return ("U1",) if market_date in self.u1_dates else ()
        if historical.universe == "U2":
            return ("U2",) if market_date in self.u2_dates else ()
        if historical.policy == "EQUALITY" and historical.universe == "U1|U2":
            output: list[str] = []
            if market_date in self.u1_dates:
                output.append("U1")
            if market_date in self.u2_dates:
                output.append("U2")
            return tuple(output)
        raise ValueError(f"AHR_OPPORTUNITY_UNIVERSE_UNKNOWN:{strategy_id}")

    def is_applicable(
        self,
        *,
        strategy_id: str,
        market_date: str,
        checkpoint_minutes: int,
    ) -> bool:
        return bool(
            self.component_universes(
                strategy_id=strategy_id,
                market_date=market_date,
                checkpoint_minutes=checkpoint_minutes,
            )
        )
