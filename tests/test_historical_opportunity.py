from __future__ import annotations

import unittest
from pathlib import Path

from src.historical_input_builder import HistoricalInputBuilder
from src.historical_revalidation import default_artifacts


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class HistoricalOpportunityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.builder = HistoricalInputBuilder(
            project_root=PROJECT_ROOT,
            artifacts=default_artifacts(),
        )
        cls.opportunities = cls.builder.opportunity_map()

    def test_canonical_population_invariants(self) -> None:
        self.assertEqual(
            self.opportunities.validate_canonical_counts(),
            {
                "historical": 170,
                "u1": 131,
                "u2": 82,
                "early_confidence": 136,
                "early_confidence_u1": 106,
                "early_confidence_u2": 69,
            },
        )

    def test_t18_january_15_is_u1_but_not_early_confidence(self) -> None:
        self.assertIn("2026-01-15", self.opportunities.u1_dates)
        self.assertNotIn("2026-01-15", self.opportunities.early_confidence_dates)
        self.assertFalse(
            self.opportunities.is_applicable(
                strategy_id="NO_FADE_P1_U1_T18",
                market_date="2026-01-15",
                checkpoint_minutes=1080,
            )
        )

    def test_equality_identity_preserves_both_component_populations(self) -> None:
        component_count = sum(
            len(
                self.opportunities.component_universes(
                    strategy_id="NO_FADE_P1_T60_U1_EQUALS_U2",
                    market_date=market_date,
                    checkpoint_minutes=60,
                )
            )
            for market_date in self.opportunities.historical_dates
        )

        self.assertEqual(component_count, 175)
        both = next(
            market_date
            for market_date in self.opportunities.historical_dates
            if market_date in self.opportunities.early_confidence_dates
            and market_date in self.opportunities.u2_dates
        )
        self.assertEqual(
            self.opportunities.component_universes(
                strategy_id="NO_FADE_P1_T60_U1_EQUALS_U2",
                market_date=both,
                checkpoint_minutes=60,
            ),
            ("U1", "U2"),
        )

    def test_current_binding_exposes_authoritative_parity_population(self) -> None:
        self.assertEqual(
            self.builder.dispatcher.binding(
                "NO_FADE_P1_U1_T18"
            ).parity_artifact_id,
            "PARITY_EARLY_CONFIDENCE",
        )
        self.assertEqual(
            self.builder.dispatcher.binding(
                "NO_FADE_P2_U1_T60"
            ).parity_artifact_id,
            "PARITY_EARLY_HORIZON",
        )


if __name__ == "__main__":
    unittest.main()
