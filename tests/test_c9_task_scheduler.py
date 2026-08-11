from __future__ import annotations

import json
import os
import subprocess
import unittest
import uuid
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASK_SCRIPT = PROJECT_ROOT / "scripts" / "C9_TASK_SCHEDULER.ps1"
PYTHON_PATH = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"


@unittest.skipUnless(os.name == "nt", "native Windows Task Scheduler required")
class C9TaskSchedulerAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.task_name = f"BTC_C9_ACCEPTANCE_{uuid.uuid4().hex}"

    def tearDown(self):
        self._run("Unregister", check=False)

    def test_registration_is_user_level_deterministic_and_idempotent(self):
        first = self._run("Register")
        second = self._run("Register")
        verified = self._run("Verify")

        self.assertEqual(first["status"], "PASS")
        self.assertEqual(second["status"], "PASS")
        self.assertEqual(verified["status"], "PASS")
        self.assertEqual(
            first["definition_fingerprint"],
            second["definition_fingerprint"],
        )
        self.assertEqual(
            second["definition_fingerprint"],
            verified["definition_fingerprint"],
        )
        self.assertEqual(verified["task_count"], 1)
        self.assertEqual(verified["run_level"], "Limited")
        self.assertEqual(verified["logon_type"], "Interactive")
        self.assertEqual(
            Path(verified["project_root"]),
            PROJECT_ROOT,
        )
        self.assertNotIn("-ExecutionPolicy", self._registered_action_arguments())

        removed = self._run("Unregister")
        self.assertEqual(removed["status"], "PASS")
        absent = self._run("Verify", check=False)
        self.assertEqual(absent.returncode, 41)
        self.assertIn("TASK_NOT_FOUND", absent.stderr)

    def _run(self, operation: str, *, check: bool = True):
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(TASK_SCRIPT),
                "-Operation",
                operation,
                "-TaskName",
                self.task_name,
                "-ProjectRoot",
                str(PROJECT_ROOT),
                "-PythonPath",
                str(PYTHON_PATH),
            ],
            cwd=PROJECT_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if not check:
            return result
        if result.returncode != 0:
            self.fail(
                f"Task Scheduler operation {operation} failed "
                f"with {result.returncode}: {result.stderr}"
            )
        return json.loads(result.stdout.strip().splitlines()[-1])

    def _registered_action_arguments(self) -> str:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    "(Get-ScheduledTask -TaskName '"
                    + self.task_name
                    + "' -TaskPath '\\').Actions[0].Arguments"
                ),
            ],
            cwd=PROJECT_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            self.fail(
                "Task Scheduler action lookup failed with "
                f"{result.returncode}: {result.stderr}"
            )
        return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
