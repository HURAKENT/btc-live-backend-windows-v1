from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER = (
    PROJECT_ROOT
    / "scripts"
    / "RUN_POLYMARKET_WS_WIRE_PROBE_MANUAL.ps1"
)
TOOL = PROJECT_ROOT / "tools" / "polymarket_ws_wire_shape_probe.py"
POWERSHELL = (
    Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    if sys.platform == "win32"
    else None
)


class ManualRunnerExistenceTests(unittest.TestCase):
    def test_script_exists(self):
        self.assertTrue(RUNNER.is_file())


@unittest.skipUnless(
    sys.platform == "win32" and RUNNER.is_file(),
    "Windows manual probe runner contract",
)
class ManualRunnerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.root = Path(cls.temp.name) / "probe project with spaces"
        cls.scripts = cls.root / "scripts"
        cls.tools = cls.root / "tools"
        cls.scripts.mkdir(parents=True)
        cls.tools.mkdir()
        shutil.copy2(RUNNER, cls.scripts / RUNNER.name)
        shutil.copy2(TOOL, cls.tools / TOOL.name)
        cls.python = cls._compile_python_stub()
        cls._initialize_clean_repository()
        cls.before_files = cls._relative_files()
        cls.before_hashes = cls._file_hashes()
        environment = os.environ.copy()
        windows_git = Path("C:/Program Files/Git/cmd")
        environment["PATH"] = (
            str(windows_git)
            + os.pathsep
            + environment.get("PATH", "")
        )
        cls.preflight = subprocess.run(
            [
                str(POWERSHELL),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(cls.scripts / RUNNER.name),
                "-Mode",
                "Preflight",
                "-NoPause",
            ],
            cwd=cls.root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        cls.after_files = cls._relative_files()
        cls.after_hashes = cls._file_hashes()
        cls.source = RUNNER.read_text(encoding="utf-8-sig")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_powershell_51_parser_accepts_script(self):
        command = (
            "$Errors = $null; $Tokens = $null; "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            f"'{RUNNER}', [ref]$Tokens, [ref]$Errors) | Out-Null; "
            "if ($Errors.Count -ne 0) { "
            "$Errors | ForEach-Object { Write-Error $_ }; exit 1 }"
        )
        result = subprocess.run(
            [
                str(POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_default_mode_is_run(self):
        self.assertRegex(
            self.source,
            r"\[string\]\$Mode\s*=\s*['\"]Run['\"]",
        )

    def test_preflight_exits_zero(self):
        self.assertEqual(
            self.preflight.returncode,
            0,
            self.preflight.stdout + self.preflight.stderr,
        )

    def test_preflight_reports_python_3124(self):
        self.assertIn("Python 3.12.4", self.preflight.stdout)

    def test_preflight_reports_aiohttp_3143(self):
        self.assertIn("aiohttp 3.14.3", self.preflight.stdout)

    def test_preflight_calls_no_network(self):
        output = self.preflight.stdout + self.preflight.stderr
        self.assertNotIn("gamma-api.polymarket.com", output)
        self.assertNotIn("ws-subscriptions-clob.polymarket.com", output)
        self.assertIn("REAL_EXTERNAL_NETWORK_REQUESTS=0", output)

    def test_preflight_does_not_invoke_probe_run(self):
        output = self.preflight.stdout + self.preflight.stderr
        self.assertIn("POLYMARKET_WS_WIRE_PROBE_PREFLIGHT_PASS", output)
        self.assertNotIn("POLYMARKET_WS_WIRE_SHAPE_ESTABLISHED", output)

    def test_preflight_creates_no_external_report(self):
        self.assertEqual(self.before_files, self.after_files)
        self.assertFalse(
            any(
                name.endswith("polymarket_ws_wire_shape_report.json")
                for name in self.after_files
            )
        )

    def test_preflight_changes_no_repository_files(self):
        self.assertEqual(self.before_hashes, self.after_hashes)

    def test_output_path_is_outside_project_root(self):
        self.assertIn(
            "$CodexRoot = Split-Path -Parent $ProjectRoot",
            self.source,
        )
        self.assertNotIn("reports\\", self.source)

    def test_exact_external_base_path_is_used(self):
        self.assertIn(
            "Join-Path $CodexRoot 'polymarket_ws_wire_shape_probes'",
            self.source,
        )

    def test_run_contains_one_probe_invocation(self):
        invocations = re.findall(
            r"polymarket_ws_wire_shape_probe\.py",
            self.source,
            flags=re.IGNORECASE,
        )
        self.assertEqual(len(invocations), 1)

    def test_run_has_no_retry_loop(self):
        self.assertNotRegex(self.source, r"\bwhile\s*\(")
        self.assertNotRegex(self.source, r"\bfor\s*\(")
        self.assertIn("AUTOMATIC_RETRIES=0", self.source)

    def test_max_frames_is_exactly_ten(self):
        self.assertIn("--max-frames 10", self.source)

    def test_timeout_is_exactly_thirty(self):
        self.assertIn("--timeout-seconds 30", self.source)

    def test_try_catch_finally_is_complete(self):
        self.assertRegex(
            self.source,
            re.compile(r"try\s*\{.*\}\s*catch\s*\{.*\}\s*finally\s*\{", re.S),
        )

    def test_stop_transcript_is_guarded(self):
        self.assertRegex(
            self.source,
            re.compile(
                r"if\s*\(\$TranscriptStarted\)\s*\{"
                r".*Stop-Transcript",
                re.S,
            ),
        )

    def test_read_host_is_present(self):
        self.assertIn("Read-Host", self.source)

    def test_no_pause_controls_only_final_pause(self):
        self.assertEqual(len(re.findall(r"\$NoPause", self.source)), 2)
        self.assertRegex(
            self.source,
            re.compile(r"if\s*\(-not \$NoPause\)\s*\{\s*Read-Host", re.S),
        )

    def test_exact_child_exit_code_is_propagated(self):
        self.assertIn("$ProbeExitCode = $ProbeResult.ExitCode", self.source)
        self.assertIn("exit $ProbeExitCode", self.source)

    def test_tests_never_invoke_mode_run(self):
        output = self.preflight.stdout + self.preflight.stderr
        self.assertNotIn("PROBE_MODE=Run", output)

    @classmethod
    def _compile_python_stub(cls) -> Path:
        scripts = cls.root / ".venv" / "Scripts"
        scripts.mkdir(parents=True)
        source = scripts / "python.cs"
        executable = scripts / "python.exe"
        source.write_text(
            "using System; public static class Program { "
            "public static int Main(string[] args) { "
            "if (args.Length == 1 && args[0] == \"--version\") { "
            "Console.Out.WriteLine(\"Python 3.12.4\"); return 0; } "
            "if (args.Length >= 2 && args[0] == \"-c\") { "
            "Console.Out.WriteLine(\"aiohttp 3.14.3\"); return 0; } "
            "if (Array.IndexOf(args, \"--help\") >= 0) { "
            "Console.Out.WriteLine(\"offline probe help\"); return 0; } "
            "Console.Error.WriteLine(\"unexpected python invocation\"); "
            "return 91; } }",
            encoding="utf-8",
        )
        command = (
            f"Add-Type -Path '{source}' -OutputType ConsoleApplication "
            f"-OutputAssembly '{executable}'"
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
        return executable

    @classmethod
    def _initialize_clean_repository(cls):
        environment = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Probe Test",
            "GIT_AUTHOR_EMAIL": "probe@example.invalid",
            "GIT_COMMITTER_NAME": "Probe Test",
            "GIT_COMMITTER_EMAIL": "probe@example.invalid",
        }
        commands = (
            ["git", "init", "-b", "codex/c0-c1"],
            ["git", "add", "."],
            ["git", "commit", "-m", "fixture"],
            [
                "git",
                "update-ref",
                "refs/remotes/origin/codex/c0-c1",
                "HEAD",
            ],
        )
        for command in commands:
            result = subprocess.run(
                command,
                cwd=cls.root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr)

    @classmethod
    def _relative_files(cls) -> tuple[str, ...]:
        return tuple(
            sorted(
                path.relative_to(cls.root).as_posix()
                for path in cls.root.rglob("*")
                if path.is_file() and ".git" not in path.parts
            )
        )

    @classmethod
    def _file_hashes(cls) -> dict[str, str]:
        return {
            relative: hashlib.sha256(
                (cls.root / relative).read_bytes()
            ).hexdigest()
            for relative in cls._relative_files()
        }


if __name__ == "__main__":
    unittest.main()
