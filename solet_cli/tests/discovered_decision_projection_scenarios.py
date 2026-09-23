"""Operation-input projection scenarios for discovered-decision lifecycle tests."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import cast

from discovered_decision_support import _CONTRACTS, _check, _raises
from solet_manager.config import CreateConfig
from solet_manager.contracts import ContractBundle, validate_normalized_answers
from solet_manager.errors import ContractError
from solet_manager.flow import SetupPlan, build_setup_plan
from solet_manager.release_lock import SeedLock
from solet_manager.transaction import canonical_sha256

MODEL_BASE_URL = "http://localhost:1234/v1"
MODEL_SELECTIONS = {
    "embedding_model": "embedding_model.recommended",
    "inference_model": "inference_model.recommended",
}
MODEL_OPERATION_INPUTS = {
    "configure_lm_studio_embeddings": {
        "lm_studio_base_url": MODEL_BASE_URL,
        "model": MODEL_SELECTIONS["embedding_model"],
    },
    "configure_lm_studio_inference": {
        "lm_studio_base_url": MODEL_BASE_URL,
        "model": MODEL_SELECTIONS["inference_model"],
    },
}


def _planned_model_operation_inputs(plan: SetupPlan) -> dict[str, object]:
    return {
        operation.operation_id: operation.public_inputs
        for operation in plan.operations
        if operation.operation_id in MODEL_OPERATION_INPUTS
    }


def _build_projection_plan(
    bundle: ContractBundle,
    root: Path,
    *,
    operation_stage_ids: set[str] | None,
    profile: str = "macos-bizops",
    selections: dict[str, str] | None = None,
) -> SetupPlan:
    selected = {
        "inference_implementation": "lm_studio",
        **MODEL_SELECTIONS,
    }
    if selections is not None:
        selected = selections
    config = CreateConfig(
        name=f"projection-{profile}",
        target=(root / f"projection-{profile}").resolve(),
        autostart=True,
        decisions={},
        decision_sources={},
    )
    seed = SeedLock(
        "https://github.com/solet-public/macos-bizops.git",
        "release-2026-08-20",
        "a" * 40,
        "b" * 40,
        "c" * 64,
        profile,
    )
    return build_setup_plan(
        bundle=bundle,
        config=config,
        seed=seed,
        journal_path=root / f"projection-{profile}.journal.json",
        decision_selections=selected,
        decision_sources=dict.fromkeys(selected, "flag"),
        operation_stage_ids=operation_stage_ids,
    )


def _flow_default_projection_scenario(
    bundle: ContractBundle,
    root: Path,
) -> None:
    model_plan = _build_projection_plan(
        bundle,
        root,
        operation_stage_ids={"models"},
    )
    public_inputs = cast(dict[str, object], model_plan.answers["public_inputs"])
    evidence = cast(list[object], model_plan.answers["resolution_evidence"])
    _check(
        public_inputs.get("lm_studio_base_url") == MODEL_BASE_URL
        and {
            "id": "lm_studio_base_url",
            "source": "flow_default",
            "summary": MODEL_BASE_URL,
        }
        in evidence,
        "reviewed flow default enters normalized answers with flow-default evidence",
    )

    changed_contracts = root / "projection-changed-default"
    shutil.copytree(_CONTRACTS, changed_contracts)
    changed_flow_path = changed_contracts / "macos_setup_flow.json"
    changed_flow = json.loads(changed_flow_path.read_text(encoding="utf-8"))
    changed_url = "http://localhost:2234/v1"
    changed_flow["inputs"]["lm_studio_base_url"]["default"] = changed_url
    changed_flow_path.write_text(json.dumps(changed_flow, indent=2), encoding="utf-8")
    changed_bundle = ContractBundle.load(
        source_revision="a" * 40,
        directory=changed_contracts,
    )
    changed_plan = _build_projection_plan(
        changed_bundle,
        root,
        operation_stage_ids={"models"},
    )
    changed_inputs = _planned_model_operation_inputs(changed_plan)
    _check(
        canonical_sha256(model_plan.answers) != canonical_sha256(changed_plan.answers)
        and set(changed_inputs) == set(MODEL_OPERATION_INPUTS)
        and all(
            isinstance(value, dict)
            and value.get("lm_studio_base_url") == changed_url
            for value in changed_inputs.values()
        ),
        "changed reviewed default changes normalized fingerprint and operation requests",
    )


def _unreferenced_default_projection_scenario(root: Path) -> None:
    unreferenced_contracts = root / "projection-unreferenced-default"
    shutil.copytree(_CONTRACTS, unreferenced_contracts)
    unreferenced_flow_path = unreferenced_contracts / "macos_setup_flow.json"
    unreferenced_flow = json.loads(
        unreferenced_flow_path.read_text(encoding="utf-8")
    )
    unreferenced_flow["inputs"]["future_public_default"] = {
        "label": "Future public default",
        "description": "Fixture default not referenced by a selected operation.",
        "value_type": "string",
        "sensitive": False,
        "prompt_timing": "before_stage",
        "default": "UNREFERENCED-PUBLIC-MARKER",
        "storage": "generated_config",
    }
    unreferenced_flow_path.write_text(
        json.dumps(unreferenced_flow, indent=2),
        encoding="utf-8",
    )
    unreferenced_bundle = ContractBundle.load(
        source_revision="a" * 40,
        directory=unreferenced_contracts,
    )
    unreferenced_plan = _build_projection_plan(
        unreferenced_bundle,
        root,
        operation_stage_ids={"models"},
    )
    _check(
        "UNREFERENCED-PUBLIC-MARKER"
        not in json.dumps(unreferenced_plan.to_dict(), sort_keys=True),
        "unreferenced public default stays out of frontier answers and requests",
    )


def _secret_default_projection_scenario(
    bundle: ContractBundle,
    root: Path,
) -> None:
    secret_marker = "SECRET-PROJECTION-MARKER"
    secret_flow = json.loads(json.dumps(bundle.flow))
    secret_flow["inputs"]["jira_api_token"]["default"] = secret_marker
    secret_flow["operations"]["configure_lm_studio_embeddings"]["input_refs"].append(
        "jira_api_token"
    )
    secret_bundle = replace(bundle, flow=secret_flow)
    secret_plan = _build_projection_plan(
        secret_bundle,
        root,
        operation_stage_ids={"models"},
    )
    secret_surfaces = (
        secret_plan.answers,
        [operation.public_inputs for operation in secret_plan.operations],
        secret_plan.to_dict(),
        secret_plan.answers["resolution_evidence"],
    )
    _check(
        all(
            secret_marker not in json.dumps(surface, sort_keys=True)
            for surface in secret_surfaces
        ),
        "schema-impossible secret default stays out of answers, requests, preview, and evidence",
    )
    leaked_answers = json.loads(json.dumps(secret_plan.answers))
    leaked_answers["public_inputs"]["jira_api_token"] = secret_marker
    _raises(
        ContractError,
        lambda: validate_normalized_answers(secret_bundle, leaked_answers),
        "normalized-answer validation independently rejects a secret carrier",
    )


def _collision_projection_scenario(root: Path) -> None:
    collision_contracts = root / "projection-collision"
    shutil.copytree(_CONTRACTS, collision_contracts)
    collision_flow_path = collision_contracts / "macos_setup_flow.json"
    collision_flow = json.loads(collision_flow_path.read_text(encoding="utf-8"))
    collision_flow["inputs"]["model"] = {
        "label": "Colliding model input",
        "description": "Fixture input colliding with an operation parameter.",
        "value_type": "string",
        "sensitive": False,
        "prompt_timing": "before_stage",
        "storage": "generated_config",
    }
    collision_flow["operations"]["configure_lm_studio_embeddings"]["input_refs"].append(
        "model"
    )
    collision_flow_path.write_text(
        json.dumps(collision_flow, indent=2),
        encoding="utf-8",
    )
    collision_bundle = ContractBundle.load(
        source_revision="a" * 40,
        directory=collision_contracts,
    )
    try:
        _build_projection_plan(
            collision_bundle,
            root,
            operation_stage_ids={"models"},
        )
    except ContractError as exc:
        _check(
            "configure_lm_studio_embeddings" in str(exc) and "model" in str(exc),
            "parameter-input collision names the operation and key",
        )
    else:
        _check(False, "parameter-input collision must fail closed")


def _check_genesis_profile_projection(
    bundle: ContractBundle,
    root: Path,
) -> None:
    genesis_plan = _build_projection_plan(
        bundle,
        root,
        operation_stage_ids={"genesis"},
    )
    genesis_operation = next(
        operation
        for operation in genesis_plan.operations
        if operation.operation_id == "run_genesis"
    )
    _check(
        genesis_operation.public_inputs
        == {
            "solet_name": "projection-macos-bizops",
            "clone_directory": str((root / "projection-macos-bizops").resolve()),
            "setup_profile": "macos-bizops",
            "autostart": "enabled",
        },
        "genesis carries its resolved profile and independent autostart decisions",
    )


def _frontier_projection_scenario(
    bundle: ContractBundle,
    root: Path,
) -> None:
    _check_genesis_profile_projection(bundle, root)
    prospective = _build_projection_plan(
        bundle,
        root,
        operation_stage_ids=None,
    )
    empty_frontier = _build_projection_plan(
        bundle,
        root,
        operation_stage_ids=set(),
    )
    _check(
        cast(dict[str, object], prospective.answers["public_inputs"]).get(
            "lm_studio_base_url"
        )
        == MODEL_BASE_URL
        and any(
            isinstance(item, dict)
            and item.get("id") == "lm_studio_base_url"
            and item.get("source") == "flow_default"
            for item in cast(list[object], prospective.answers["resolution_evidence"])
        )
        and "lm_studio_base_url"
        not in cast(dict[str, object], empty_frontier.answers["public_inputs"])
        and not empty_frontier.operations,
        "absent operation frontier projects prospective defaults while explicit empty projects none",
    )

    free_plan = _build_projection_plan(
        bundle,
        root,
        profile="free",
        operation_stage_ids={"models"},
        selections={"embedding_model": MODEL_SELECTIONS["embedding_model"]},
    )
    free_decisions = cast(dict[str, object], free_plan.answers["decisions"])
    _check(
        "inference_model" not in free_decisions
        and "configure_lm_studio_inference"
        not in _planned_model_operation_inputs(free_plan)
        and MODEL_SELECTIONS["inference_model"]
        not in json.dumps(free_plan.to_dict(), sort_keys=True),
        "inactive inference-model decision is neither persisted nor projected",
    )


def run_projection_scenarios(root: Path) -> None:
    bundle = ContractBundle.load(source_revision="a" * 40, directory=_CONTRACTS)
    _flow_default_projection_scenario(bundle, root)
    _unreferenced_default_projection_scenario(root)
    _secret_default_projection_scenario(bundle, root)
    _collision_projection_scenario(root)
    _frontier_projection_scenario(bundle, root)
