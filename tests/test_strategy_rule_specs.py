from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from src.strategy_registry import (
    expected_strategy_rule_pack_sha256,
    load_strategy_rule_pack,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FROZEN_ROOT = PROJECT_ROOT / "strategy_sources" / "frozen"
RULE_PACK_PATH = FROZEN_ROOT / "STRATEGY_RULE_MAP_47.json"
LOCK_PATH = PROJECT_ROOT / "contract" / "STRATEGY_REGISTRY_47_LIVE_BACKEND_LOCK.json"
REGISTRY_JSON_PATH = PROJECT_ROOT / "registry" / "STRATEGY_REGISTRY_47.json"
REGISTRY_CSV_PATH = PROJECT_ROOT / "registry" / "STRATEGY_REGISTRY_47.csv"


class StrategyRulePackContractTests(unittest.TestCase):
    def test_rule_pack_is_independently_hash_locked(self) -> None:
        actual = hashlib.sha256(RULE_PACK_PATH.read_bytes()).hexdigest()
        self.assertEqual(actual, expected_strategy_rule_pack_sha256())

    def test_loader_binds_exact_registry_order_and_counts(self) -> None:
        pack = self._load()
        locked_ids = tuple(json.loads(LOCK_PATH.read_text(encoding="utf-8"))["strategy_ids"])

        self.assertEqual(pack.strategy_count, 47)
        self.assertEqual(pack.v1_count, 34)
        self.assertEqual(pack.v2_count, 13)
        self.assertEqual(tuple(rule.strategy_id for rule in pack.rules), locked_ids)
        self.assertFalse(pack.trading_approval)

    def test_all_frozen_artifacts_are_resolvable_and_byte_verified(self) -> None:
        pack = self._load()
        expected_v1_full_decision = {
            "PARITY_V1_CONFIRMATION_BASKET_FULL",
            "PARITY_V1_CONFIRMATION_BASKET_RECEIPT",
            "PARITY_V1_EARLY_CONFIDENCE_FULL",
            "PARITY_V1_EARLY_CONFIDENCE_RECEIPT",
            "PARITY_V1_EARLY_HORIZON_FULL",
            "PARITY_V1_EARLY_HORIZON_RECEIPT",
        }
        self.assertTrue(expected_v1_full_decision.issubset(
            {artifact.artifact_id for artifact in pack.artifacts}
        ))
        for artifact in pack.artifacts:
            with self.subTest(artifact_id=artifact.artifact_id):
                path = FROZEN_ROOT / artifact.relative_path
                self.assertTrue(path.is_file())
                self.assertEqual(path.stat().st_size, artifact.size_bytes)
                self.assertEqual(
                    hashlib.sha256(path.read_bytes()).hexdigest(), artifact.sha256
                )
                self.assertFalse(Path(artifact.relative_path).is_absolute())
                self.assertNotIn("..", Path(artifact.relative_path).parts)

    def test_strict_and_pf1_bind_exact_recovered_executable_sources(self) -> None:
        pack = self._load()
        expected_artifacts = {
            "SRC_STRICT_A_SELECTOR": (
                "cb34c5b4df8cdc13629576bf9d326d4ff7debab55a43de8d822756352b973e1c"
            ),
            "SRC_PF1_CORE": (
                "71fd5851c9dbefae3c2bdc5c3cbc2558728479c13518ad58a9e35ab878983ea4"
            ),
            "SRC_PF1_MODEL": (
                "087e219574833767bfd470b2f3fafe873fdc5c72c11b6f4770de3255c50e6bd4"
            ),
            "CONTRACT_PF1": (
                "a86aec5cbd499bc6459a30eacb89e82fe966e1e6687b639a1d5b59f1a8438ce8"
            ),
            "CONTRACT_PF1_FORWARD": (
                "c6535fe8b2d4327582c1bcbbbf2c7fe67f52e009033721362b8a8e4fb4cc011f"
            ),
        }
        for artifact_id, expected_sha in expected_artifacts.items():
            self.assertEqual(pack.artifact(artifact_id).sha256, expected_sha)

        by_id = {rule.strategy_id: rule for rule in pack.rules}
        strict_ids = {
            "YES_STRICT_A_T60",
            "YES_STRICT_A_T30",
            "YES_STRICT_A_OPERATIONAL",
        }
        pf1_ids = {
            "YES_PF1_OPERATIONAL",
            "YES_PF1_T30",
            "YES_PF1_T60",
            "YES_PF1_T6H",
            "YES_PF1_T8H",
        }
        for strategy_id in strict_ids:
            self.assertEqual(by_id[strategy_id].spec_id, "STRICT_A_COMPOSED_V1")
        for strategy_id in pf1_ids:
            self.assertEqual(by_id[strategy_id].spec_id, "PF1_COMPOSED_V1")

    def test_strict_and_pf1_specs_preserve_historical_and_execution_stages(self) -> None:
        pack = self._load()
        strict = pack.family_spec("STRICT_A_COMPOSED_V1").semantic_contract
        pf1 = pack.family_spec("PF1_COMPOSED_V1").semantic_contract

        self.assertEqual(strict["historical_parity_stage"], "early_horizon_evaluate_strict_a")
        self.assertEqual(strict["execution_stage"], "select_strict_favorite")
        self.assertEqual(strict["effective_cost"], "vwap5_plus_fee")
        self.assertTrue(strict["t30_blocked_after_t60_position"])
        self.assertEqual(pf1["historical_parity_stage"], "early_horizon_or_confidence_pf1")
        self.assertEqual(pf1["execution_stage"], "checkpoint_decision")
        self.assertEqual(pf1["vwap5_cap_micros"], 950_000)
        self.assertEqual(pf1["full_depth_shares"], 5)
        self.assertEqual(pf1["historical_only_checkpoints_minutes"], [480, 360])
        self.assertEqual(pf1["non_execution_checkpoint_policy"], "execution_eligible_false")

    def test_every_identity_binds_spec_evaluator_schema_and_parity_ledger(self) -> None:
        pack = self._load()
        for rule in pack.rules:
            with self.subTest(strategy_id=rule.strategy_id):
                family = pack.family_spec(rule.spec_id)
                parity = pack.artifact(rule.parity_artifact_id)
                self.assertEqual(rule.evaluator_key, family.evaluator_key)
                self.assertEqual(rule.input_schema_version, family.input_schema_version)
                self.assertTrue(family.artifact_ids)
                self.assertEqual(parity.role, "IMMUTABLE_TRADE_LEDGER")

    def test_v1_family_specs_include_executable_primitive_details(self) -> None:
        pack = self._load()
        basket = pack.family_spec("FAVORITE_NEIGHBOR_BASKET_V1").semantic_contract
        no_fade = pack.family_spec("NO_FADE_P1_V1").semantic_contract
        a0 = pack.family_spec("NO_CONFIRMATION_A0_V1").semantic_contract
        c1 = pack.family_spec("NO_CONFIRMATION_C1_V1").semantic_contract

        self.assertEqual(basket["selector"], "favorite_plus_best_immediate_neighbor")
        self.assertEqual(basket["neighbor_tie_tolerance"], "1e-9")
        self.assertEqual(basket["basket_edge_threshold_micros"], 20_000)
        self.assertEqual(
            basket["original_source_sha256"],
            "5993a6a37ea7513c69a32da40d951f4128e07ed959781151558df693713cf839",
        )
        self.assertEqual(no_fade["selector"], "argmin(model_p_minus_market_q_raw)")
        self.assertTrue(no_fade["exclude_unique_favorite"])
        self.assertEqual(a0["score_tie_policy"], "NO_SIGNAL_ON_EXACT_SCORE_TIE")
        self.assertEqual(
            a0["source_conflict_resolution"],
            "FROZEN_CONTRACT_PRIORITY_OVER_EXECUTABLE_SOURCE",
        )
        self.assertEqual(c1["selector_entrypoint"], "choose_c1")
        self.assertTrue(c1["requires_old_and_new_favorite_identity"])

    def test_no_fade_specs_preserve_exact_historical_and_execution_gates(self) -> None:
        pack = self._load()
        p1 = pack.family_spec("NO_FADE_P1_V1").semantic_contract
        p2 = pack.family_spec("NO_FADE_P2_V1").semantic_contract

        self.assertEqual(p1["historical_edge_basis"], "p_no_minus_stressed_proxy_q_no")
        self.assertEqual(p1["historical_edge_threshold_micros"], 20_000)
        self.assertTrue(p1["stressed_cost_strictly_below_one"])
        self.assertEqual(p2["historical_edge_basis"], "p_no_minus_proxy_q_no")
        self.assertEqual(p2["historical_edge_threshold_micros"], 20_000)
        self.assertTrue(p2["stressed_cost_strictly_below_one"])
        for contract in (p1, p2):
            self.assertEqual(contract["execution_edge_basis"], "p_no_minus_actual_no_vwap5")
            self.assertEqual(contract["execution_edge_threshold_micros"], 20_000)
            self.assertEqual(contract["execution_minimum_depth_shares"], 5)
            self.assertEqual(
                contract["missing_fee_policy"], "net_pnl_not_calculable"
            )

    def test_v2_binds_overlay_probability_contract_and_parent(self) -> None:
        pack = self._load()
        overlay = pack.family_spec("VOL_OVERLAY_A_V1")
        self.assertEqual(
            set(overlay.artifact_ids),
            {
                "SRC_VOL_OVERLAY",
                "SRC_VOL_PROBABILITY",
                "CONTRACT_VOL_OVERLAY",
                "PARITY_VOL_OVERLAY_DECISIONS",
                "PARITY_VOL_OVERLAY_RECEIPT",
            },
        )
        self.assertEqual(
            pack.artifact("SRC_VOL_PROBABILITY").sha256,
            "08e1b08150eaa7994a9e583643b9f90b190cbde1eeb56666846adcff009c7172",
        )
        by_id = {rule.strategy_id: rule for rule in pack.rules}
        v2_rules = [rule for rule in pack.rules if rule.version == "V2"]
        self.assertEqual(len(v2_rules), 13)
        self.assertEqual(
            sum(rule.schedule["overlay_mode"] == "VOL_VETO_ONLY" for rule in v2_rules),
            7,
        )
        self.assertEqual(
            sum(
                rule.schedule["overlay_mode"] == "VOL_CONFIRMATION"
                for rule in v2_rules
            ),
            6,
        )
        for rule in v2_rules:
            self.assertEqual(by_id[rule.parent_strategy_id].version, "V1")

    def test_self_consistent_pack_mutations_fail_trusted_digest(self) -> None:
        mutations = []
        payload = self._pack_payload()
        payload["family_specs"]["NO_FADE_P1_V1"]["semantic_contract"]["shares"] = 6
        payload["family_specs"]["NO_FADE_P1_V1"]["spec_sha256"] = self._canonical_sha256(
            payload["family_specs"]["NO_FADE_P1_V1"]["semantic_contract"]
        )
        mutations.append(payload)

        payload = self._pack_payload()
        payload["canonical_sources"]["source_map_34_sha256"] = "0" * 64
        payload["artifacts"][0]["sha256"] = "1" * 64
        mutations.append(payload)

        payload = self._pack_payload()
        v2 = next(row for row in payload["identities"] if row["version"] == "V2")
        v2["parent_strategy_id"] = "YES_FAVORITE_ONLY"
        mutations.append(payload)

        payload = self._pack_payload()
        payload["identities"][0]["input_schema_version"] = (
            "BTC_STRATEGY_VOL_OVERLAY_INPUT_V1"
        )
        mutations.append(payload)

        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=index):
                self._assert_mutated_pack_rejected(mutation)

    def test_frozen_artifact_byte_mutation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied_root = Path(directory) / "frozen"
            shutil.copytree(FROZEN_ROOT, copied_root)
            artifact = copied_root / "contracts" / "A0_RULES_FROZEN.json"
            artifact.write_bytes(artifact.read_bytes() + b"\n")
            with self.assertRaisesRegex(
                ValueError, "STRATEGY_RULE_ARTIFACT_HASH_MISMATCH"
            ):
                self._load(copied_root / RULE_PACK_PATH.name)

    def test_registry_csv_line_ending_is_repository_owned(self) -> None:
        attributes = (PROJECT_ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("registry/STRATEGY_REGISTRY_47.csv text eol=crlf", attributes)
        self.assertEqual(
            hashlib.sha256(REGISTRY_CSV_PATH.read_bytes()).hexdigest(),
            "77fc26814e3c05182b0b13dbb3d162c41536328517a40b4a6713d7d6e9bad8fd",
        )

    def test_frozen_public_pack_contains_no_absolute_user_paths(self) -> None:
        payload = RULE_PACK_PATH.read_text(encoding="utf-8")
        self.assertNotIn("C:\\Users\\", payload)
        self.assertNotIn("/mnt/c/Users/", payload)
        pack = self._load()
        for artifact in pack.artifacts:
            with self.subTest(artifact_id=artifact.artifact_id):
                artifact_payload = (FROZEN_ROOT / artifact.relative_path).read_bytes()
                self.assertNotIn(b"C:\\Users\\", artifact_payload)
                self.assertNotIn(b"/mnt/c/Users/", artifact_payload)
        self.assertFalse(self._pack_payload()["trading_approval"])

    @staticmethod
    def _canonical_sha256(value: object) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _pack_payload() -> dict[str, object]:
        return json.loads(RULE_PACK_PATH.read_text(encoding="utf-8"))

    def _load(self, path: Path = RULE_PACK_PATH):
        return load_strategy_rule_pack(
            path,
            lock_path=LOCK_PATH,
            registry_json_path=REGISTRY_JSON_PATH,
            registry_csv_path=REGISTRY_CSV_PATH,
        )

    def _assert_mutated_pack_rejected(self, payload: dict[str, object]) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / RULE_PACK_PATH.name
            path.write_text(
                json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2)
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError, "STRATEGY_RULE_PACK_HASH_MISMATCH"
            ):
                self._load(path)


if __name__ == "__main__":
    unittest.main()
