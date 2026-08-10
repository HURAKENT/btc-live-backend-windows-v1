from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "tools/run_data_completion_source_import.py"


class DataCompletionSourceImportRunnerTests(unittest.TestCase):
    def test_runner_exists_and_direct_help_has_no_side_effects(self) -> None:
        self.assertTrue(SCRIPT.is_file())
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_runner_has_one_bounded_offline_import_path(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("verify_authoritative_source_packs", source)
        self.assertIn("import_source_pack", source)
        self.assertIn("OFFLINE_LOCAL_SOURCE_IMPORT", source)
        self.assertNotIn("aiohttp", source)
        self.assertNotIn("requests", source)
        self.assertNotIn("http://", source)
        self.assertNotIn("https://", source)
        self.assertNotIn("while True", source)

    def test_copy_is_hash_verified_idempotent_and_conflict_safe(self) -> None:
        from tools.run_data_completion_source_import import _copy_verified
        import hashlib

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.zip"
            destination = root / "data/source.zip"
            destination.parent.mkdir()
            source.write_bytes(b"verified")
            digest = hashlib.sha256(b"verified").hexdigest()
            _copy_verified(source, destination, digest)
            _copy_verified(source, destination, digest)
            self.assertEqual(destination.read_bytes(), b"verified")
            destination.write_bytes(b"conflict")
            with self.assertRaisesRegex(RuntimeError, "SOURCE_PACK_ARCHIVE_CONFLICT"):
                _copy_verified(source, destination, digest)

    def test_external_path_and_actual_head_guards(self) -> None:
        from tools.run_data_completion_source_import import _validate_paths, _verify_source_commit

        head = "a" * 40
        clean_results = (
            subprocess.CompletedProcess([], 0, head + "\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        )
        with mock.patch("tools.run_data_completion_source_import.subprocess.run", side_effect=clean_results):
            _verify_source_commit(PROJECT_ROOT, head)
        with mock.patch(
            "tools.run_data_completion_source_import.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "b" * 40 + "\n", ""),
        ):
            with self.assertRaisesRegex(RuntimeError, "SOURCE_COMMIT_HEAD_MISMATCH"):
                _verify_source_commit(PROJECT_ROOT, head)
        dirty_results = (
            subprocess.CompletedProcess([], 0, head + "\n", ""),
            subprocess.CompletedProcess([], 0, "?? unexpected\n", ""),
        )
        with mock.patch("tools.run_data_completion_source_import.subprocess.run", side_effect=dirty_results):
            with self.assertRaisesRegex(RuntimeError, "SOURCE_WORKTREE_NOT_CLEAN"):
                _verify_source_commit(PROJECT_ROOT, head)
        with tempfile.TemporaryDirectory() as directory:
            external = Path(directory)
            _validate_paths(PROJECT_ROOT, external, external / "receipt.json")
            with self.assertRaisesRegex(RuntimeError, "DATA_COMPLETION_ROOT_MUST_BE_OUTSIDE_REPOSITORY"):
                _validate_paths(PROJECT_ROOT, PROJECT_ROOT / "runtime", external / "receipt.json")
            with self.assertRaisesRegex(RuntimeError, "RECEIPT_OUTPUT_MUST_BE_OUTSIDE_REPOSITORY"):
                _validate_paths(PROJECT_ROOT, external, PROJECT_ROOT / "receipt.json")
            with self.assertRaisesRegex(RuntimeError, "RECEIPT_OUTPUT_MUST_BE_OUTSIDE_REPOSITORY"):
                _validate_paths(PROJECT_ROOT, PROJECT_ROOT.parent, PROJECT_ROOT / "receipt.json")

    def test_existing_receipt_rerun_is_exact_noop_after_local_reverification(self) -> None:
        from src.data_completion_source_receipt import build_source_pack_receipt, write_source_pack_receipt
        from tests.test_data_completion_source_receipt import DataCompletionSourceReceiptTests
        from tools.run_data_completion_source_import import _load_existing_receipt

        fixture = DataCompletionSourceReceiptTests
        receipt = build_source_pack_receipt(
            project_root=PROJECT_ROOT,
            source_commit="a" * 40,
            generated_at_utc="2026-08-10T20:00:00Z",
            elapsed_seconds=12.5,
            packs=fixture._packs(),
            database=fixture._database(),
            historical_artifact_observations=fixture._historical_artifacts(),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source_packs").mkdir()
            (root / "historical_acceptance").mkdir()
            for item in receipt["packs"]:
                (root / "source_packs" / item["file_name"]).write_bytes(b"pack")
            database = root / "historical_source.sqlite3"
            database.write_bytes(b"x" * receipt["database"]["file_size_bytes"])
            for item in receipt["historical_acceptance"]["artifacts"]:
                path = root / "historical_acceptance" / Path(item["source_path"]).name
                path.write_bytes(b"x" * item["size_bytes"])
            output = root / "receipt.json"
            write_source_pack_receipt(receipt, output)

            def observed_sha(path: Path) -> str:
                if path == database:
                    return receipt["database"]["database_sha256"]
                for item in receipt["packs"]:
                    if path.name == item["file_name"]:
                        return item["outer_sha256"]
                for item in receipt["historical_acceptance"]["artifacts"]:
                    if path.name == Path(item["source_path"]).name:
                        return item["sha256"]
                raise AssertionError(path)

            before = output.read_bytes()
            with mock.patch(
                "tools.run_data_completion_source_import._sha256_file",
                side_effect=observed_sha,
            ):
                replay = _load_existing_receipt(root, output, "a" * 40)
            self.assertEqual(replay, receipt)
            self.assertEqual(output.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
