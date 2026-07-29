from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = PROJECT_ROOT / "scripts" / "RUN_C1_ACCEPTANCE_SAFE.ps1"
POWERSHELL = (
    Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    if sys.platform == "win32"
    else None
)


@unittest.skipUnless(sys.platform == "win32", "Windows launcher contract")
class Task14LauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.valid = cls._compile_stub("valid", "Python 3.12.4", "", 0)
        cls.wrong = cls._compile_stub("wrong", "Python 3.11.9", "", 0)
        cls.failed = cls._compile_stub(
            "failed",
            "",
            "deliberate child failure",
            7,
        )

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_valid_native_python_preflight_captures_version_and_exit(self):
        result = self._run(self.valid)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Python 3.12.4", result.stdout)
        self.assertIn("child exit code=0", result.stdout)
        self.assertNotIn("simulate_downtime.py", result.stdout)

    def test_wrong_native_python_version_returns_config_exit(self):
        result = self._run(self.wrong)
        self.assertEqual(result.returncode, 30)
        self.assertIn("Python 3.11.9", result.stdout + result.stderr)
        self.assertNotIn("simulate_downtime.py", result.stdout)

    def test_native_execution_failure_reports_path_exit_and_stderr(self):
        result = self._run(self.failed)
        diagnostic = result.stdout + result.stderr
        self.assertEqual(result.returncode, 30)
        self.assertIn(str(self.failed), diagnostic)
        self.assertIn("child exit 7", diagnostic)
        self.assertIn("deliberate child failure", diagnostic)

    def test_real_windows_venv_preflight_succeeds(self):
        result = self._run(Path(sys.executable))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Python 3.12.4", result.stdout)
        self.assertIn("RAW_VERSION_COUNT=1", result.stdout)
        self.assertIn("child exit code=0", result.stdout)

    def _run(self, executable: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(POWERSHELL),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(LAUNCHER),
                "-Mode",
                "Preflight",
                "-PythonPath",
                str(executable),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    @classmethod
    def _compile_stub(
        cls,
        name: str,
        stdout: str,
        stderr: str,
        exit_code: int,
    ) -> Path:
        path = cls.root / f"{name}.exe"
        source_path = cls.root / f"{name}.cs"
        source = (
            "using System; public static class Program { "
            "public static int Main(string[] args) { "
            f'Console.Out.WriteLine("{stdout}"); '
            f'Console.Error.WriteLine("{stderr}"); '
            f"return {exit_code}; "
            "} }"
        )
        source_path.write_text(source, encoding="utf-8")
        command = (
            f"Add-Type -Path '{source_path}' "
            f"-OutputType ConsoleApplication -OutputAssembly '{path}'"
        )
        result = subprocess.run(
            [
                str(POWERSHELL),
                "-NoProfile",
                "-Command",
                command,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr)
        return path


if __name__ == "__main__":
    unittest.main()
