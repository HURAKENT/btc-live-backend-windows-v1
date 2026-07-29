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
MANUAL_RUNNER = PROJECT_ROOT / "scripts" / "RUN_TASK14_MANUAL.ps1"
POWERSHELL = (
    Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    if sys.platform == "win32"
    else None
)


class ManualRunnerExistenceTests(unittest.TestCase):
    def test_manual_runner_exists(self):
        self.assertTrue(MANUAL_RUNNER.is_file())


@unittest.skipUnless(
    sys.platform == "win32" and MANUAL_RUNNER.is_file(),
    "Windows manual runner contract",
)
class Task14ManualRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.root = Path(cls.temp.name) / "manual runner project with spaces"
        cls.scripts = cls.root / "scripts"
        cls.scripts.mkdir(parents=True)
        shutil.copy2(MANUAL_RUNNER, cls.scripts / MANUAL_RUNNER.name)
        (cls.scripts / "RUN_C1_ACCEPTANCE_SAFE.ps1").write_text(
            "param([string]$Mode = 'Run')\n"
            "Write-Output ('LAUNCHER_MODE=' + $Mode)\n"
            "if ($Mode -ne 'Preflight') { exit 91 }\n"
            "exit 0\n",
            encoding="utf-8",
        )
        cls.python = cls._compile_python_stub()
        cls.report = cls.root / "reports" / "C1_FINAL_ACCEPTANCE.json"
        cls.pack = cls.root / "artifacts" / "C1_ACCEPTANCE_PACK.zip"
        cls.report.parent.mkdir()
        cls.pack.parent.mkdir()
        cls.report.write_text('{"status":"HISTORICAL"}\n', encoding="utf-8")
        cls.pack.write_bytes(b"historical-pack")
        cls.before_hashes = cls._canonical_hashes()
        cls._initialize_clean_repository()
        cls.before_files = cls._relative_files()
        preflight_env = os.environ.copy()
        windows_git = Path("C:/Program Files/Git/cmd")
        preflight_env["PATH"] = (
            str(windows_git)
            + os.pathsep
            + preflight_env.get("PATH", "")
        )
        cls.preflight = subprocess.run(
            [
                str(POWERSHELL),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(cls.scripts / MANUAL_RUNNER.name),
                "-Mode",
                "Preflight",
                "-NoPause",
            ],
            cwd=cls.root,
            env=preflight_env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        cls.after_files = cls._relative_files()
        cls.after_hashes = cls._canonical_hashes()
        cls.source = MANUAL_RUNNER.read_text(encoding="utf-8-sig")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_powershell_parser_accepts_script(self):
        command = (
            "$Errors = $null; $Tokens = $null; "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            f"'{MANUAL_RUNNER}', [ref]$Tokens, [ref]$Errors) | Out-Null; "
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

    def test_preflight_calls_only_launcher_preflight(self):
        output = self.preflight.stdout + self.preflight.stderr
        self.assertIn("LAUNCHER_MODE=Preflight", output)
        self.assertNotIn("LAUNCHER_MODE=Run", output)
        self.assertNotIn("LAUNCHER_MODE=Offline", output)

    def test_preflight_does_not_call_acceptance_tool(self):
        output = self.preflight.stdout + self.preflight.stderr
        self.assertNotIn("simulate_downtime.py", output)

    def test_preflight_creates_no_acceptance_database(self):
        self.assertEqual(self.before_files, self.after_files)
        self.assertFalse(any(path.endswith(".sqlite3") for path in self.after_files))

    def test_preflight_preserves_canonical_reports_and_pack(self):
        self.assertEqual(self.before_hashes, self.after_hashes)

    def test_transcript_path_is_outside_project_root(self):
        self.assertIn(
            "$CodexRoot = Split-Path -Parent $ProjectRoot",
            self.source,
        )
        self.assertNotIn("reports\\manual_task14_", self.source)

    def test_transcript_path_uses_task14_manual_logs(self):
        self.assertIn(
            "Join-Path $CodexRoot 'task14_manual_logs'",
            self.source,
        )

    def test_run_mode_has_complete_try_catch_finally(self):
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

    def test_read_host_is_in_script(self):
        self.assertIn(
            "Read-Host 'Нажмите Enter, чтобы закрыть это окно'",
            self.source,
        )

    def test_no_pause_controls_only_final_pause(self):
        matches = re.findall(r"\$NoPause", self.source)
        self.assertEqual(len(matches), 2)
        self.assertRegex(
            self.source,
            re.compile(
                r"if\s*\(-not \$NoPause\)\s*\{\s*"
                r"Read-Host",
                re.S,
            ),
        )

    def test_launcher_exit_code_is_propagated(self):
        self.assertIn("$LauncherExitCode = $LASTEXITCODE", self.source)
        self.assertIn("exit $LauncherExitCode", self.source)

    def test_tests_never_invoke_default_run(self):
        output = self.preflight.stdout + self.preflight.stderr
        self.assertNotIn("LAUNCHER_MODE=Run", output)

    @classmethod
    def _compile_python_stub(cls) -> Path:
        scripts = cls.root / ".venv" / "Scripts"
        scripts.mkdir(parents=True)
        source = scripts / "python.cs"
        executable = scripts / "python.exe"
        source.write_text(
            "using System; public static class Program { "
            "public static int Main(string[] args) { "
            'Console.Out.WriteLine("Python 3.12.4"); return 0; } }',
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
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Task14 Test",
            "GIT_AUTHOR_EMAIL": "task14@example.invalid",
            "GIT_COMMITTER_NAME": "Task14 Test",
            "GIT_COMMITTER_EMAIL": "task14@example.invalid",
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
                env=env,
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
    def _canonical_hashes(cls) -> tuple[str, str]:
        return (
            hashlib.sha256(cls.report.read_bytes()).hexdigest(),
            hashlib.sha256(cls.pack.read_bytes()).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
