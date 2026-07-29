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
        spaced_root = cls.root / "path with spaces"
        spaced_root.mkdir()
        cls.large_stderr = cls._compile_source(
            spaced_root,
            "large_stderr",
            """
using System;
public static class Program {
    public static int Main(string[] args) {
        bool version = args.Length == 1 && args[0] == "--version";
        if (version) {
            Console.Out.WriteLine("Python 3.12.4");
            Console.Error.Write(new string('E', 1048576));
            return 0;
        }
        Console.Out.WriteLine("STDOUT_MARKER");
        Console.Error.WriteLine("STDERR_MARKER");
        Console.Error.Write(new string('E', 1048576));
        return 0;
    }
}
""",
        )
        cls.large_both_failure = cls._compile_source(
            spaced_root,
            "large_both_failure",
            """
using System;
public static class Program {
    public static int Main(string[] args) {
        bool version = args.Length == 1 && args[0] == "--version";
        if (version) {
            Console.Out.WriteLine("Python 3.12.4");
            return 0;
        }
        Console.Out.WriteLine("STDOUT_MARKER");
        Console.Out.Write(new string('O', 1048576));
        Console.Error.WriteLine("STDERR_MARKER");
        Console.Error.Write(new string('E', 1048576));
        return 7;
    }
}
""",
        )
        cls.command_recorder = cls._compile_source(
            spaced_root,
            "command_recorder",
            """
using System;
public static class Program {
    public static int Main(string[] args) {
        bool version = args.Length == 1 && args[0] == "--version";
        if (version) {
            Console.Out.WriteLine("Python 3.12.4");
            return 0;
        }
        Console.Out.WriteLine("ARGS:" + string.Join("|", args));
        return 0;
    }
}
""",
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

    def test_large_stderr_is_drained_while_stdout_is_captured(self):
        result = self._run(self.large_stderr, mode="Run", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(result.stdout.count("STDOUT_MARKER"), 5)
        self.assertEqual(result.stderr.count("STDERR_MARKER"), 5)
        self.assertGreaterEqual(result.stderr.count("E"), 6 * 1048576)

    def test_large_stdout_and_stderr_preserve_exit_and_both_streams(self):
        result = self._run(
            self.large_both_failure,
            mode="Run",
            timeout=15,
        )
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout.count("STDOUT_MARKER"), 1)
        self.assertEqual(result.stderr.count("STDERR_MARKER"), 1)
        self.assertGreaterEqual(result.stdout.count("O"), 1048576)
        self.assertGreaterEqual(result.stderr.count("E"), 1048576)

    def test_offline_mode_runs_all_gates_without_acceptance_runner(self):
        result = self._run(
            self.command_recorder,
            mode="Offline",
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "ARGS:-m|unittest|tests.test_process_runtime_integration|-v",
            result.stdout,
        )
        self.assertIn(
            "ARGS:-m|unittest|discover|-s|tests|-v",
            result.stdout,
        )
        self.assertIn(
            "ARGS:-m|compileall|-q|src|tests|tools|run_backend.py",
            result.stdout,
        )
        self.assertIn("ARGS:-m|pip|check", result.stdout)
        self.assertNotIn("simulate_downtime.py", result.stdout)

    def _run(
        self,
        executable: Path,
        *,
        mode: str = "Preflight",
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(POWERSHELL),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(LAUNCHER),
                "-Mode",
                mode,
                "-PythonPath",
                str(executable),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
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

    @classmethod
    def _compile_source(
        cls,
        root: Path,
        name: str,
        source: str,
    ) -> Path:
        path = root / f"{name}.exe"
        source_path = root / f"{name}.cs"
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
