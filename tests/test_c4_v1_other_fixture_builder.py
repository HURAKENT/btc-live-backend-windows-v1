from __future__ import annotations

import importlib.util
import unittest


class C4V1OtherFixtureBuilderTests(unittest.TestCase):
    def test_builder_module_exists(self) -> None:
        self.assertIsNotNone(
            importlib.util.find_spec("tools.build_c4_v1_other_parity_fixture")
        )

    def test_builder_executes_validation_instead_of_being_an_import_stub(self) -> None:
        from tools.build_c4_v1_other_parity_fixture import build_records

        with self.assertRaisesRegex(
            ValueError, "C4_V1_OTHER_CONFIRMATION_DATE_COUNT"
        ):
            build_records(
                atlas_rows=[],
                market_rows=[],
                settlement_rows=[],
                coverage_rows=[],
                confirmation_dates=set(),
                confirmation_rows=[],
                basket_rows=[],
            )


if __name__ == "__main__":
    unittest.main()
