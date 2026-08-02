from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


class Phase0EvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "seed.txt").write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "seed.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "user.name=Phase0 Test",
                "-c",
                "user.email=phase0@example.invalid",
                "commit",
                "-q",
                "-m",
                "seed",
            ],
            check=True,
        )
        self.commit = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def api(self):
        from tools.phase0_evidence import (
            CommandSpec,
            generate_phase0_evidence,
            verify_phase0_report,
        )

        return CommandSpec, generate_phase0_evidence, verify_phase0_report

    def command_specs(self):
        command_spec, _, _ = self.api()
        return (
            command_spec(
                name="focused_tests",
                argv=(sys.executable, "-c", "print('Ran 7 tests')"),
                expected_test_count=7,
            ),
            command_spec(
                name="full_offline_tests",
                argv=(sys.executable, "-c", "print('Ran 11 tests')"),
                expected_test_count=11,
            ),
            command_spec(
                name="compileall",
                argv=(sys.executable, "-c", "print('compile ok')"),
            ),
            command_spec(
                name="pip_check",
                argv=(sys.executable, "-c", "print('pip check ok')"),
            ),
            command_spec(
                name="scope_audit",
                argv=(sys.executable, "-c", "print('scope audit ok')"),
            ),
        )

    def test_generator_captures_outputs_and_links_receipt_hash(self) -> None:
        _, generate, _ = self.api()

        result = generate(
            root=self.root,
            source_commit=self.commit,
            harness_commit=self.commit,
            command_specs=self.command_specs(),
            historical_paths=("seed.txt",),
        )

        receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))
        report = json.loads(result.report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "PASS")
        self.assertFalse(report["trading_approval"])
        self.assertEqual(report["receipt_sha256"], result.receipt_sha256)
        self.assertEqual(len(receipt["commands"]), 5)
        self.assertTrue(all(item["exit_code"] == 0 for item in receipt["commands"]))
        self.assertEqual(receipt["commands"][0]["test_count"], 7)
        self.assertEqual(receipt["commands"][1]["test_count"], 11)
        self.assertTrue(all(item["stdout_sha256"] for item in receipt["commands"]))
        self.assertTrue(all(item["stderr_sha256"] for item in receipt["commands"]))
        with zipfile.ZipFile(result.pack_path) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(
                set(archive.namelist()),
                {
                    result.receipt_path.relative_to(self.root).as_posix(),
                    result.report_path.relative_to(self.root).as_posix(),
                    *result.command_output_paths,
                },
            )

    def test_hand_edited_pass_or_output_cannot_verify(self) -> None:
        _, generate, verify = self.api()
        result = generate(
            root=self.root,
            source_commit=self.commit,
            harness_commit=self.commit,
            command_specs=self.command_specs(),
            historical_paths=("seed.txt",),
        )
        original_report = result.report_path.read_text(encoding="utf-8")
        report = json.loads(original_report)
        report["receipt_sha256"] = "0" * 64
        result.report_path.write_text(
            json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "RECEIPT_SHA256_MISMATCH"):
            verify(result.report_path, root=self.root, verify_repository=False)

        result.report_path.write_text(original_report, encoding="utf-8")
        output_path = self.root / result.command_output_paths[0]
        output_path.write_text("hand edited\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "COMMAND_OUTPUT_SHA256_MISMATCH"):
            verify(result.report_path, root=self.root, verify_repository=False)

    def test_dirty_failed_and_stale_evidence_are_rejected(self) -> None:
        command_spec, generate, verify = self.api()
        (self.root / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "PHASE0_EVIDENCE_DIRTY_START"):
            generate(
                root=self.root,
                source_commit=self.commit,
                harness_commit=self.commit,
                command_specs=self.command_specs(),
                historical_paths=("seed.txt",),
            )
        (self.root / "dirty.txt").unlink()

        failed = list(self.command_specs())
        failed[2] = command_spec(
            name="compileall",
            argv=(sys.executable, "-c", "raise SystemExit(9)"),
        )
        with self.assertRaisesRegex(ValueError, "PHASE0_COMMAND_FAILED:compileall:9"):
            generate(
                root=self.root,
                source_commit=self.commit,
                harness_commit=self.commit,
                command_specs=tuple(failed),
                historical_paths=("seed.txt",),
            )
        subprocess.run(
            ["git", "-C", str(self.root), "clean", "-fd"],
            check=True,
            capture_output=True,
        )

        result = generate(
            root=self.root,
            source_commit=self.commit,
            harness_commit=self.commit,
            command_specs=self.command_specs(),
            historical_paths=("seed.txt",),
        )
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        self._commit("evidence")
        verify(result.report_path, root=self.root)
        (self.root / "unrelated.txt").write_text("stale\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.root), "add", "unrelated.txt"], check=True
        )
        self._commit("unrelated")
        with self.assertRaisesRegex(ValueError, "PHASE0_EVIDENCE_STALE"):
            verify(result.report_path, root=self.root)

    def _commit(self, message: str) -> None:
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "user.name=Phase0 Test",
                "-c",
                "user.email=phase0@example.invalid",
                "commit",
                "-q",
                "-m",
                message,
            ],
            check=True,
        )


if __name__ == "__main__":
    unittest.main()
