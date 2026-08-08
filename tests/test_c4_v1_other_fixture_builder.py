from __future__ import annotations

import importlib.util
import unittest


class C4V1OtherFixtureBuilderTests(unittest.TestCase):
    def test_builder_module_exists(self) -> None:
        self.assertIsNotNone(
            importlib.util.find_spec("tools.build_c4_v1_other_parity_fixture")
        )


if __name__ == "__main__":
    unittest.main()
