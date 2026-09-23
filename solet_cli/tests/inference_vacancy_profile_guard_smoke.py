"""Fail-closed inference vacancy across prompts, selections, resume, and plans."""

from __future__ import annotations

import copy
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "plugins/github_midwife_plugin/tests"))

from setup_flow_schema_smoke import _projection_transaction  # noqa: E402
from solet_manager.answer_validation import (  # noqa: E402
    static_option_available,
    validate_normalized_answers,
)
from solet_manager.config import CreateConfig  # noqa: E402
from solet_manager.contracts import ContractBundle, target_contract_directory  # noqa: E402
from solet_manager.decision_resolution import static_decision_prompts  # noqa: E402
from solet_manager.decision_state import declined_answer  # noqa: E402
from solet_manager.errors import ContractError, StateConflictError  # noqa: E402
from solet_manager.models import JsonValue  # noqa: E402
from solet_manager.plan_builder import (  # noqa: E402
    SetupPlan,
    build_setup_plan,
    selected_operation_ids,
)
from solet_manager.probe_input_projection import probe_public_inputs  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402

_CONTRACTS = Path(__file__).resolve().parents[2] / "plugins/github_midwife_plugin/knowledge_base"


class InferenceVacancyProfileGuard(unittest.TestCase):
    def setUp(self) -> None:
        self.bundle = ContractBundle.load(source_revision="a" * 40, directory=_CONTRACTS)

    def _plan(
        self,
        profile: str,
        selections: dict[str, JsonValue] | None = None,
        recorded: dict[str, JsonValue] | None = None,
        source: str = "flag",
    ) -> SetupPlan:
        seed = SeedLock("example/seed", "v1", "a" * 40, "b" * 40, "c" * 64, profile)
        return build_setup_plan(
            bundle=self.bundle,
            config=CreateConfig(name="vacancy-guard", target=Path("/tmp/vacancy-guard"), autostart=True),
            seed=seed,
            journal_path=Path("/tmp/vacancy-guard.json"),
            decision_selections=selections,
            recorded_answers=recorded,
            decision_source=source,
        )

    def test_nonfree_candidates_exclude_vacancy(self) -> None:
        for profile in ("macos-bizops", "custom"):
            with self.subTest(profile=profile):
                prompts = static_decision_prompts(self.bundle, self._plan(profile))
                prompt = next(
                    item for item in prompts
                    if isinstance(item, dict) and item["id"] == "inference_implementation"
                )
                candidates = cast(list[dict[str, JsonValue]], prompt["candidates"])
                values = {item["value"] for item in candidates}
                self.assertNotIn("none", values)
                self.assertNotIn("alternative_plugin", values)
                self.assertIn("lm_studio", values)

    def test_explicit_vacancy_rejected_before_operation_planning(self) -> None:
        for profile in ("macos-bizops", "custom"):
            for source in ("flag", "config", "interactive"):
                with self.subTest(profile=profile, source=source), patch(
                    "solet_manager.plan_builder._plan_operations"
                ) as operations:
                    with self.assertRaisesRegex(ContractError, "inference_implementation.*none"):
                        self._plan(profile, {"inference_implementation": "none"}, source=source)
                    operations.assert_not_called()

    def test_recorded_vacancy_rejected(self) -> None:
        for profile in ("macos-bizops", "custom"):
            with self.subTest(profile=profile):
                answers = copy.deepcopy(self._plan(profile).answers)
                decisions = cast(dict[str, JsonValue], answers["decisions"])
                decisions["inference_implementation"] = "none"
                with self.assertRaisesRegex(ContractError, "inference_implementation.*none"):
                    validate_normalized_answers(self.bundle, answers)
                with patch("solet_manager.plan_builder._plan_operations") as operations:
                    with self.assertRaisesRegex(ContractError, "inference_implementation.*none"):
                        self._plan(profile, recorded=answers)
                    operations.assert_not_called()

    def test_direct_operation_selection_and_projection_reject_vacancy(self) -> None:
        for profile in ("macos-bizops", "custom"):
            with self.subTest(profile=profile):
                with self.assertRaisesRegex(ContractError, "inference_implementation.*none"):
                    selected_operation_ids(
                        self.bundle, {"setup_profile": profile, "inference_implementation": "none"}
                    )
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "projection-target"
            directory = target_contract_directory(target)
            shutil.copytree(_CONTRACTS, directory)
            contract = ContractBundle.load(source_revision="a" * 40, directory=directory)
            transaction = _projection_transaction(contract.flow)
            for profile in ("macos-bizops", "custom"):
                with self.subTest(profile=profile):
                    answers = copy.deepcopy(
                        self._plan(
                            profile,
                            {"inference_implementation": "lm_studio"},
                        ).answers
                    )
                    answers["target"] = str(target)
                    validate_normalized_answers(contract, answers)
                    decisions = cast(dict[str, JsonValue], answers["decisions"])
                    decisions["inference_implementation"] = "none"
                    changed = replace(
                        transaction,
                        target=str(target),
                        flow_source_revision=contract.source_revision,
                        flow_contract_digest=contract.contract_digest,
                        answers=answers,
                    )
                    with self.assertRaises((ContractError, StateConflictError)):
                        probe_public_inputs(changed, "setup::lm_studio.server_ready")
            mismatched_answers = copy.deepcopy(
                self._plan(
                    "macos-bizops",
                    {"inference_implementation": "lm_studio"},
                ).answers
            )
            mismatched_answers["target"] = str(target)
            mismatched_decisions = cast(dict[str, JsonValue], mismatched_answers["decisions"])
            mismatched_decisions["inference_implementation"] = "none"
            seed_free = replace(transaction.seed, profile="free")
            with self.assertRaises(StateConflictError):
                probe_public_inputs(
                    replace(
                        transaction,
                        target=str(target),
                        seed=seed_free,
                        flow_source_revision=contract.source_revision,
                        flow_contract_digest=contract.contract_digest,
                        answers=mismatched_answers,
                    ),
                    "setup::lm_studio.server_ready",
                )
            free_answers = copy.deepcopy(self._plan("free").answers)
            free_answers["target"] = str(target)
            free_decisions = cast(dict[str, JsonValue], free_answers["decisions"])
            free_decisions["inference_implementation"] = "none"
            projected = probe_public_inputs(
                replace(
                    transaction,
                    target=str(target),
                    flow_source_revision=contract.source_revision,
                    flow_contract_digest=contract.contract_digest,
                    answers=free_answers,
                ),
                "setup::lm_studio.server_ready",
            )
            self.assertEqual(projected["inference_implementation"], "none")

    def test_planned_options_rejected_across_selection_paths(self) -> None:
        for decision_id, option_id in (
            ("inference_implementation", "alternative_plugin"),
            ("embeddings_implementation", "ollama_plugin"),
        ):
            for profile in ("macos-bizops", "custom"):
                with self.subTest(profile=profile, decision=decision_id):
                    for source in ("flag", "config", "interactive"):
                        with patch("solet_manager.plan_builder._plan_operations") as operations:
                            with self.assertRaisesRegex(ContractError, decision_id):
                                self._plan(profile, {decision_id: option_id}, source=source)
                            operations.assert_not_called()
                    answers = copy.deepcopy(self._plan(profile).answers)
                    decisions = cast(dict[str, JsonValue], answers["decisions"])
                    decisions[decision_id] = option_id
                    with self.assertRaisesRegex(ContractError, decision_id):
                        validate_normalized_answers(self.bundle, answers)
                    with patch("solet_manager.plan_builder._plan_operations") as operations:
                        with self.assertRaisesRegex(ContractError, decision_id):
                            self._plan(profile, recorded=answers)
                        operations.assert_not_called()
                    with self.assertRaisesRegex(ContractError, decision_id):
                        selected_operation_ids(self.bundle, decisions)

    def test_declared_status_is_generic_and_preserves_metadata(self) -> None:
        source = cast(dict[str, JsonValue], self.bundle.decisions["inference_implementation"]["option_source"])
        options = cast(dict[str, JsonValue], source["options"])
        planned = copy.deepcopy(options["alternative_plugin"])
        self._plan("macos-bizops")
        self.assertEqual(options["alternative_plugin"], planned)
        for status in ("planned", "unavailable", None, False):
            with self.subTest(status=status):
                self.assertFalse(static_option_available({"availability": status}, {}))
        self.assertTrue(static_option_available({"availability": "supported"}, {}))
        self.assertTrue(static_option_available({}, {}))
        self.bundle.decisions["inference_implementation"]["recommended_option_refs"] = ["alternative_plugin"]
        plan = self._plan("macos-bizops")
        self.assertIn("inference_implementation", plan.unresolved_decisions)

    def test_real_inference_retains_model_and_readiness_plan(self) -> None:
        for profile in ("macos-bizops", "custom"):
            with self.subTest(profile=profile):
                plan = self._plan(profile, {
                    "inference_implementation": "lm_studio", "inference_model": "qwen3-14b"
                })
                operations = {item.operation_id: item for item in plan.operations}
                for operation_id in (
                    "configure_lm_studio_inference", "pull_lm_studio_inference_model",
                    "load_lm_studio_inference_model",
                ):
                    self.assertIn(operation_id, operations)
                genesis = next(item for item in plan.operations if item.operation_ref == "genesis::solet.run")
                self.assertTrue(genesis.postcondition_probe_ids)
                self.assertEqual(
                    operations["configure_lm_studio_inference"].postcondition_probe_ids,
                    (),
                )

    def test_free_retains_inactive_declared_vacancy(self) -> None:
        plan = self._plan("free")
        decisions = cast(dict[str, JsonValue], plan.answers["decisions"])
        self.assertNotIn("inference_implementation", decisions)
        self.assertNotIn("inference_implementation", plan.unresolved_decisions)
        self.assertFalse(any("inference" in item.operation_id for item in plan.operations))
        self.assertFalse(any(
            isinstance(item, dict) and item["id"] == "inference_implementation"
            for item in static_decision_prompts(self.bundle, plan)
        ))

    def _vacancy_option(self) -> dict[str, JsonValue]:
        source = cast(dict[str, JsonValue], self.bundle.decisions["inference_implementation"]["option_source"])
        options = cast(dict[str, JsonValue], source["options"])
        return cast(dict[str, JsonValue], options["none"])

    def test_condition_inputs_must_be_resolved_even_under_negation(self) -> None:
        option: JsonValue = {"available_when": {"not": {
            "decision_ref": "setup_profile", "operator": "equals", "value": "free"
        }}}
        self.assertFalse(static_option_available(option, {}))
        self.assertFalse(static_option_available(option, {"setup_profile": "free"}))
        self.assertTrue(static_option_available(option, {"setup_profile": "custom"}))
        self._vacancy_option()["available_when"] = {
            "not": {
                "decision_ref": "session_sources",
                "operator": "equals",
                "value": "codex",
            }
        }
        self.bundle.validate()
        answers = copy.deepcopy(
            self._plan("macos-bizops", {"inference_implementation": "lm_studio"}).answers
        )
        decisions = cast(dict[str, JsonValue], answers["decisions"])
        decisions["session_sources"] = declined_answer(
            decided_at="2026-09-13T00:00:00+00:00",
            decided_by="inference-vacancy-smoke",
        )
        decisions["inference_implementation"] = "none"
        with self.assertRaisesRegex(ContractError, "inference_implementation.*none"):
            validate_normalized_answers(self.bundle, answers)

    def test_recursive_conditions_use_normalized_selections(self) -> None:
        self._vacancy_option()["available_when"] = {"all": [
            {"decision_ref": "autostart", "operator": "equals", "value": "enabled"},
            {"any": [
                {"decision_ref": "setup_profile", "operator": "equals", "value": "free"},
                {"not": {"decision_ref": "setup_profile", "operator": "equals", "value": "custom"}},
            ]},
        ]}
        self.bundle.validate()
        self.assertTrue(static_option_available(self._vacancy_option(), {
            "setup_profile": "macos-bizops", "autostart": "enabled"
        }))
        self.assertFalse(static_option_available(self._vacancy_option(), {
            "setup_profile": "custom", "autostart": "enabled"
        }))

    def test_contract_rejects_malformed_conditions(self) -> None:
        leaf: dict[str, JsonValue] = {
            "decision_ref": "setup_profile", "operator": "equals", "value": "free"
        }
        for condition in (
            None, True, {}, {"all": []}, {"not": leaf, "any": [leaf]},
            {**leaf, "operator": "matches"}, {**leaf, "value": []},
            {**leaf, "unexpected": True},
            {"fact_ref": "host_memory_mb", "operator": "equals", "value": 24576},
        ):
            with self.subTest(condition=condition):
                self._vacancy_option()["available_when"] = condition
                with self.assertRaisesRegex(ContractError, "available_when"):
                    self.bundle.validate()

    def test_contract_rejects_unknown_later_self_and_same_stage_cycles(self) -> None:
        for reference in ("not_a_decision", "inference_model", "inference_implementation"):
            with self.subTest(reference=reference):
                self._vacancy_option()["available_when"] = {
                    "not": {"decision_ref": reference, "operator": "equals", "value": "none"}
                }
                with self.assertRaisesRegex(ContractError, "available_when"):
                    self.bundle.validate()
        cycle_bundle = ContractBundle.load(source_revision="a" * 40, directory=_CONTRACTS)
        for decision_id, prerequisite in (
            ("inference_implementation", "embeddings_implementation"),
            ("embeddings_implementation", "inference_implementation"),
        ):
            source = cast(dict[str, JsonValue], cycle_bundle.decisions[decision_id]["option_source"])
            options = cast(dict[str, JsonValue], source["options"])
            options["lm_studio"]["available_when"] = {
                "decision_ref": prerequisite,
                "operator": "equals",
                "value": "lm_studio",
            }
        with self.assertRaisesRegex(ContractError, "available_when decision dependency cycle"):
            cycle_bundle.validate()

    def test_unavailable_recommendation_is_not_applied_as_default(self) -> None:
        self.bundle.decisions["inference_implementation"]["recommended_option_refs"] = ["none"]
        plan = self._plan("macos-bizops")
        decisions = cast(dict[str, JsonValue], plan.answers["decisions"])
        self.assertNotIn("inference_implementation", decisions)
        self.assertIn("inference_implementation", plan.unresolved_decisions)

    def test_declared_condition_controls_both_rendering_and_validation(self) -> None:
        self._vacancy_option()["available_when"] = {
            "decision_ref": "setup_profile", "operator": "equals", "value": "custom"
        }
        self.bundle.validate()
        plan = self._plan("custom", {"inference_implementation": "none"})
        validate_normalized_answers(self.bundle, plan.answers)
        prompts = static_decision_prompts(self.bundle, self._plan("custom"))
        prompt = next(item for item in prompts if isinstance(item, dict) and item["id"] == "inference_implementation")
        self.assertIn("none", {item["value"] for item in cast(list[dict[str, JsonValue]], prompt["candidates"])})
        with self.assertRaisesRegex(ContractError, "inference_implementation.*none"):
            self._plan("macos-bizops", {"inference_implementation": "none"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
