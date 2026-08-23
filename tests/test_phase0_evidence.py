from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock


def _git_prefix(project_root: Path) -> tuple[str, ...]:
    pointer_file = project_root / ".git"
    if os.name != "nt" or not pointer_file.is_file():
        return ("git", "-C", str(project_root))
    pointer = pointer_file.read_text(encoding="utf-8").strip()
    if not pointer.startswith("gitdir: "):
        return ("git", "-C", str(project_root))
    git_dir = pointer.removeprefix("gitdir: ")
    if (
        git_dir.startswith("/mnt/")
        and len(git_dir) > 7
        and git_dir[5].isalpha()
        and git_dir[6] == "/"
    ):
        git_dir = git_dir[5].upper() + ":\\" + git_dir[7:].replace("/", "\\")
    return (
        "git",
        f"--git-dir={git_dir}",
        f"--work-tree={project_root}",
    )


class Phase0EvidenceTests(unittest.TestCase):
    def test_canonical_phase0_c1_c2_c3_evidence_has_crlf_checkout_contract(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        git_prefix = _git_prefix(project_root)
        canonical_reports = (
            "reports/PHASE0_ACCEPTED_BASELINE.json",
            "reports/PHASE0_COMMAND_RECEIPT.json",
            "reports/C1_DOWNTIME_ACCEPTANCE.json",
            "reports/C1_FINAL_ACCEPTANCE.json",
            "reports/C2_RECOVERY_ACCEPTANCE.json",
            "reports/C3_MARKET_ROLLOVER_ACCEPTANCE.json",
            "reports/C2_C3_FINAL_ACCEPTANCE.json",
        )

        for relative_path in canonical_reports:
            result = subprocess.run(
                (*git_prefix,
                    "check-attr",
                    "text",
                    "eol",
                    "--",
                    relative_path,
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            attributes = result.stdout.splitlines()
            self.assertIn(
                f"{relative_path}: text: set",
                attributes,
                msg=relative_path,
            )
            self.assertIn(
                f"{relative_path}: eol: crlf",
                attributes,
                msg=relative_path,
            )

        for relative_path in (
            "reports/C4_STRATEGY_47_ACCEPTANCE.json",
            "reports/C5_STRATEGY_47_ACTIVATION_ACCEPTANCE.json",
            "reports/STRATEGY_47_STATUS_MATRIX.json",
        ):
            result = subprocess.run(
                (*git_prefix,
                    "check-attr",
                    "text",
                    "eol",
                    "--",
                    relative_path,
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            attributes = result.stdout.splitlines()
            self.assertIn(
                f"{relative_path}: text: unset",
                attributes,
                msg=relative_path,
            )
            self.assertIn(
                f"{relative_path}: eol: unspecified",
                attributes,
                msg=relative_path,
            )

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

    @staticmethod
    def canonical_bytes(value) -> bytes:
        return (
            json.dumps(value, sort_keys=True, separators=(",", ":"))
            + "\r\n"
        ).encode("utf-8")

    def generate_fixture(self):
        _, generate, _ = self.api()
        return generate(
            root=self.root,
            source_commit=self.commit,
            harness_commit=self.commit,
            command_specs=self.command_specs(),
            historical_paths=("seed.txt",),
            test_only=True,
        )

    def resign_receipt(self, result, receipt, report) -> None:
        receipt_bytes = self.canonical_bytes(receipt)
        result.receipt_path.write_bytes(receipt_bytes)
        report["receipt_sha256"] = hashlib.sha256(receipt_bytes).hexdigest()
        result.report_path.write_bytes(self.canonical_bytes(report))

    def rewrite_pack(self, result, mutate) -> None:
        with zipfile.ZipFile(result.pack_path) as archive:
            entries = [(name, archive.read(name)) for name in archive.namelist()]
        entries = mutate(entries)
        with zipfile.ZipFile(result.pack_path, "w") as archive:
            for name, payload in entries:
                archive.writestr(name, payload)

    def test_generator_captures_outputs_and_links_receipt_hash(self) -> None:
        _, generate, _ = self.api()

        result = generate(
            root=self.root,
            source_commit=self.commit,
            harness_commit=self.commit,
            command_specs=self.command_specs(),
            historical_paths=("seed.txt",),
            test_only=True,
        )

        receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))
        report = json.loads(result.report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "PASS")
        self.assertFalse(report["trading_approval"])
        self.assertEqual(report["public_provider_requests"], "NOT_OBSERVED")
        self.assertEqual(
            report["provider_network_observation"],
            "NOT_PERFORMED",
        )
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
                    "MANIFEST.json",
                    "SHA256SUMS",
                },
            )

    def test_command_outputs_are_sanitized_and_bind_raw_and_stored_hashes(self) -> None:
        from tools.phase0_evidence import _sanitize_output

        windows_root = Path(
            "/mnt/c/Users/gegos/Documents/Codex/btc_live_backend_windows_v1"
        )
        variants = (
            b"C:\\Users\\gegos\\Documents\\Codex\\btc_live_backend_windows_v1\\tests\n"
            b"/mnt/c/Users/gegos/Documents/Codex/btc_live_backend_windows_v1/src\n"
        )
        sanitized_variants, variant_count = _sanitize_output(
            variants,
            root=windows_root,
        )
        self.assertEqual(variant_count, 2)
        self.assertEqual(sanitized_variants.count(b"<PROJECT_ROOT>"), 2)

        _, generate, verify = self.api()
        (self.root / "sitecustomize.py").write_text(
            (
                "from pathlib import Path\n"
                "import sys\n"
                "print('root=' + str(Path.cwd()))\n"
                "print('stderr-root=' + str(Path.cwd()), file=sys.stderr)\n"
            ),
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(self.root), "add", "sitecustomize.py"],
            check=True,
        )
        self._commit("test output hook")
        self.commit = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        with mock.patch.dict(
            os.environ,
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(self.root),
            },
        ):
            result = generate(
                root=self.root,
                source_commit=self.commit,
                harness_commit=self.commit,
                command_specs=self.command_specs(),
                historical_paths=("seed.txt",),
                test_only=True,
            )

        receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))
        report = json.loads(result.report_path.read_text(encoding="utf-8"))
        first = receipt["commands"][0]
        stdout_bytes = (self.root / first["stdout_path"]).read_bytes()
        stderr_bytes = (self.root / first["stderr_path"]).read_bytes()
        stdout = stdout_bytes.decode("utf-8")
        stderr = stderr_bytes.decode("utf-8")
        self.assertNotIn(str(self.root.resolve()), stdout + stderr)
        self.assertEqual((stdout + stderr).count("<PROJECT_ROOT>"), 2)
        self.assertEqual(first["stdout_sanitization_replacements"], 1)
        self.assertEqual(first["stderr_sanitization_replacements"], 1)
        self.assertEqual(
            first["stdout_sha256"],
            hashlib.sha256(stdout_bytes).hexdigest(),
        )
        self.assertEqual(
            first["stderr_sha256"],
            hashlib.sha256(stderr_bytes).hexdigest(),
        )
        self.assertNotEqual(first["raw_stdout_sha256"], first["stdout_sha256"])
        self.assertNotEqual(first["raw_stderr_sha256"], first["stderr_sha256"])
        self.assertEqual(
            receipt["output_sanitization_policy"],
            "PROJECT_ROOT_AND_USER_HOME_REPLACEMENT_V2",
        )
        self.assertEqual(receipt["output_sanitization_replacement_count"], 10)
        self.assertEqual(
            report["output_sanitization_policy"],
            receipt["output_sanitization_policy"],
        )
        self.assertEqual(report["output_sanitization_replacement_count"], 10)
        self.assertEqual(
            report["raw_output_commitment_sha256"],
            receipt["raw_output_commitment_sha256"],
        )
        self.assertEqual(
            report["stored_output_commitment_sha256"],
            receipt["stored_output_commitment_sha256"],
        )
        with zipfile.ZipFile(result.pack_path) as archive:
            packed = b"\n".join(archive.read(name) for name in archive.namelist())
        self.assertNotIn(str(self.root.resolve()).encode("utf-8"), packed)
        self.assertIn(b"<PROJECT_ROOT>", packed)
        verify(result.report_path, root=self.root, verify_repository=False)

    def test_command_output_sanitizer_redacts_user_home_outside_project(self) -> None:
        from tools.phase0_evidence import _sanitize_output

        payload = (
            b"C:\\Users\\gegos\\AppData\\Local\\Programs\\Python\\Python312\\Lib\\asyncio.py\n"
            b"/mnt/c/Users/gegos/AppData/Local/Temp/phase0.txt\n"
        )
        sanitized, replacement_count = _sanitize_output(
            payload,
            root=Path(
                "/mnt/c/Users/gegos/Documents/Codex/btc_live_backend_windows_v1"
            ),
        )

        self.assertEqual(replacement_count, 2)
        self.assertEqual(sanitized.count(b"<USER_HOME>"), 2)
        self.assertNotIn(b"gegos", sanitized)

    def test_verifier_rejects_residual_windows_or_wsl_user_paths(self) -> None:
        _, _, verify = self.api()
        result = self.generate_fixture()
        original_receipt_bytes = result.receipt_path.read_bytes()
        original_report_bytes = result.report_path.read_bytes()
        original_outputs = {
            path: (self.root / path).read_bytes()
            for path in result.command_output_paths
        }
        residual_paths = (
            r"C:\Users\gegos\Documents\Codex\btc_live_backend_windows_v1\tests",
            "/mnt/c/Users/gegos/Documents/Codex/btc_live_backend_windows_v1/tests",
        )
        for residual in residual_paths:
            with self.subTest(residual=residual):
                result.receipt_path.write_bytes(original_receipt_bytes)
                result.report_path.write_bytes(original_report_bytes)
                for path, payload in original_outputs.items():
                    (self.root / path).write_bytes(payload)
                receipt = json.loads(original_receipt_bytes.decode("utf-8"))
                report = json.loads(original_report_bytes.decode("utf-8"))
                command = receipt["commands"][0]
                output = (residual + "\nRan 7 tests\n").encode("utf-8")
                (self.root / command["stdout_path"]).write_bytes(output)
                command["stdout_sha256"] = hashlib.sha256(output).hexdigest()
                self.resign_receipt(result, receipt, report)
                with self.assertRaisesRegex(
                    ValueError,
                    "PHASE0_UNSANITIZED_USER_PATH",
                ):
                    verify(
                        result.report_path,
                        root=self.root,
                        verify_repository=False,
                    )

    def test_verifier_rejects_tampered_output_bindings_and_report_summary(self) -> None:
        _, _, verify = self.api()
        result = self.generate_fixture()
        original_receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))
        original_report = json.loads(result.report_path.read_text(encoding="utf-8"))

        receipt = copy.deepcopy(original_receipt)
        report = copy.deepcopy(original_report)
        receipt["commands"][0]["raw_stdout_sha256"] = "0" * 64
        self.resign_receipt(result, receipt, report)
        with self.assertRaisesRegex(
            ValueError,
            "PHASE0_RAW_OUTPUT_COMMITMENT_MISMATCH",
        ):
            verify(result.report_path, root=self.root, verify_repository=False)

        receipt = copy.deepcopy(original_receipt)
        report = copy.deepcopy(original_report)
        receipt["commands"][0]["stdout_sha256"] = "1" * 64
        self.resign_receipt(result, receipt, report)
        with self.assertRaisesRegex(ValueError, "COMMAND_OUTPUT_SHA256_MISMATCH"):
            verify(result.report_path, root=self.root, verify_repository=False)

        result.receipt_path.write_bytes(self.canonical_bytes(original_receipt))
        report = copy.deepcopy(original_report)
        report["output_sanitization_replacement_count"] += 1
        report["receipt_sha256"] = hashlib.sha256(
            result.receipt_path.read_bytes()
        ).hexdigest()
        result.report_path.write_bytes(self.canonical_bytes(report))
        with self.assertRaisesRegex(ValueError, "PHASE0_REPORT_RECEIPT_MISMATCH"):
            verify(result.report_path, root=self.root, verify_repository=False)

    def test_hand_edited_pass_or_output_cannot_verify(self) -> None:
        _, generate, verify = self.api()
        result = generate(
            root=self.root,
            source_commit=self.commit,
            harness_commit=self.commit,
            command_specs=self.command_specs(),
            historical_paths=("seed.txt",),
            test_only=True,
        )
        original_report = result.report_path.read_bytes()
        report = json.loads(original_report.decode("utf-8"))
        report["receipt_sha256"] = "0" * 64
        result.report_path.write_bytes(self.canonical_bytes(report))
        with self.assertRaisesRegex(ValueError, "RECEIPT_SHA256_MISMATCH"):
            verify(result.report_path, root=self.root, verify_repository=False)

        result.report_path.write_bytes(original_report)
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
                test_only=True,
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
                test_only=True,
                test_only_allow_custom_commands=True,
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
            test_only=True,
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

    def test_report_fields_must_match_receipt_contract(self) -> None:
        _, _, verify = self.api()
        result = self.generate_fixture()
        original = json.loads(result.report_path.read_text(encoding="utf-8"))
        substitutions = {
            "branch": "other-branch",
            "source_commit": "1" * 40,
            "harness_commit": "2" * 40,
            "command_count": 4,
            "focused_test_count": 8,
            "full_offline_test_count": 12,
            "historical_sha256": {},
            "network_mode": "UNVERIFIED",
            "public_provider_requests": 1,
            "registry_executions": 1,
            "task14_runs": 1,
            "trading_approval": True,
            "gate": "HAND_EDITED_PASS",
        }
        for field, substituted in substitutions.items():
            with self.subTest(field=field):
                report = copy.deepcopy(original)
                report[field] = substituted
                result.report_path.write_bytes(self.canonical_bytes(report))
                with self.assertRaises(ValueError):
                    verify(
                        result.report_path,
                        root=self.root,
                        verify_repository=False,
                    )

    def test_receipt_cannot_weaken_historical_or_command_contract(self) -> None:
        _, _, verify = self.api()
        result = self.generate_fixture()
        original_receipt = json.loads(
            result.receipt_path.read_text(encoding="utf-8")
        )
        original_report = json.loads(
            result.report_path.read_text(encoding="utf-8")
        )

        receipt = copy.deepcopy(original_receipt)
        report = copy.deepcopy(original_report)
        receipt["historical_sha256"] = {}
        report["historical_sha256"] = {}
        self.resign_receipt(result, receipt, report)
        with self.assertRaises(ValueError):
            verify(result.report_path, root=self.root, verify_repository=False)

        mutations = ("argv", "command", "expected_test_count")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                receipt = copy.deepcopy(original_receipt)
                report = copy.deepcopy(original_report)
                command = receipt["commands"][0]
                if mutation == "argv":
                    command["argv"][-1] = "print('Ran 8 tests')"
                    command["command"] = subprocess.list2cmdline(command["argv"])
                elif mutation == "command":
                    command["command"] = "hand-edited command"
                else:
                    command["expected_test_count"] = 8
                self.resign_receipt(result, receipt, report)
                with self.assertRaises(ValueError):
                    verify(
                        result.report_path,
                        root=self.root,
                        verify_repository=False,
                    )

    def test_successful_command_that_dirties_repository_is_rejected(self) -> None:
        command_spec, generate, _ = self.api()
        commands = list(self.command_specs())
        commands[-1] = command_spec(
            name="scope_audit",
            argv=(
                sys.executable,
                "-c",
                "from pathlib import Path; Path('intruder.txt').write_text('x'); print('scope audit ok')",
            ),
        )
        with self.assertRaisesRegex(ValueError, "PHASE0_COMMAND_DIRTIED_REPOSITORY"):
            generate(
                root=self.root,
                source_commit=self.commit,
                harness_commit=self.commit,
                command_specs=tuple(commands),
                historical_paths=("seed.txt",),
                test_only=True,
                test_only_allow_custom_commands=True,
            )

    def test_production_generator_rejects_caller_weakened_history_set(self) -> None:
        _, generate, _ = self.api()
        with self.assertRaisesRegex(
            ValueError,
            "PHASE0_HISTORICAL_CONTRACT_WEAKENED",
        ):
            generate(
                root=self.root,
                source_commit=self.commit,
                harness_commit=self.commit,
                command_specs=self.command_specs(),
                historical_paths=("seed.txt",),
            )

    def test_pack_missing_corrupt_or_structurally_unsafe_is_rejected(self) -> None:
        _, _, verify = self.api()
        result = self.generate_fixture()
        original_pack = result.pack_path.read_bytes()

        result.pack_path.unlink()
        with self.assertRaises(ValueError):
            verify(result.report_path, root=self.root, verify_repository=False)

        result.pack_path.write_bytes(b"not a zip")
        with self.assertRaises(ValueError):
            verify(result.report_path, root=self.root, verify_repository=False)

        result.pack_path.write_bytes(original_pack)
        self.rewrite_pack(result, lambda entries: entries[1:])
        with self.assertRaises(ValueError):
            verify(result.report_path, root=self.root, verify_repository=False)

        result.pack_path.write_bytes(original_pack)
        self.rewrite_pack(
            result,
            lambda entries: [
                (name, b"0" * len(payload) if name == "SHA256SUMS" else payload)
                for name, payload in entries
            ],
        )
        with self.assertRaises(ValueError):
            verify(result.report_path, root=self.root, verify_repository=False)

        result.pack_path.write_bytes(original_pack)
        self.rewrite_pack(
            result,
            lambda entries: [*entries, ("../escape.txt", b"escape")],
        )
        with self.assertRaises(ValueError):
            verify(result.report_path, root=self.root, verify_repository=False)

        result.pack_path.write_bytes(original_pack)
        duplicate_name = result.command_output_paths[0]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            self.rewrite_pack(
                result,
                lambda entries: [*entries, (duplicate_name, b"duplicate")],
            )
        with self.assertRaises(ValueError):
            verify(result.report_path, root=self.root, verify_repository=False)

        result.pack_path.write_bytes(original_pack)
        self.rewrite_pack(
            result,
            lambda entries: [
                (name, b"mutated" if name == result.command_output_paths[0] else payload)
                for name, payload in entries
            ],
        )
        with self.assertRaises(ValueError):
            verify(result.report_path, root=self.root, verify_repository=False)

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
