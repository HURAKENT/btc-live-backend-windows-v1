from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = PROJECT_ROOT / "scripts" / "RUN_AUTONOMOUS_HISTORICAL_REVALIDATION.ps1"


class HistoricalRevalidationLauncherTests(unittest.TestCase):
    def test_launcher_has_fail_closed_operator_contract(self) -> None:
        text = LAUNCHER.read_text(encoding="utf-8")

        self.assertIn("Set-StrictMode -Version Latest", text)
        self.assertIn("$ErrorActionPreference = 'Stop'", text)
        self.assertIn("$ProjectRoot = Split-Path -Parent $PSScriptRoot", text)
        self.assertIn(".venv\\Scripts\\python.exe", text)
        self.assertIn("'--smoke'", text)
        self.assertIn("'--resume'", text)
        self.assertIn("'--pyarrow-path'", text)

    def test_launcher_contains_no_forbidden_mutation_or_network_surface(self) -> None:
        lowered = LAUNCHER.read_text(encoding="utf-8").lower()
        for forbidden in (
            "executionpolicy bypass",
            "pip install",
            "binance.com",
            "polymarket.com",
            "register-scheduledtask",
            "unregister-scheduledtask",
            "new-scheduledtask",
            "wallet",
            "private key",
            "real order",
            "database-path",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, lowered)

    def test_launcher_captures_native_exit_code_explicitly(self) -> None:
        text = LAUNCHER.read_text(encoding="utf-8")

        self.assertIn("$PythonProcess = Start-Process", text)
        self.assertIn("-Wait -NoNewWindow -PassThru", text)
        self.assertIn("exit $PythonProcess.ExitCode", text)
        self.assertNotIn("exit $LASTEXITCODE", text)


if __name__ == "__main__":
    unittest.main()
