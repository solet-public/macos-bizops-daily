"""Regression proof for activation reconciliation during resume preview."""

from __future__ import annotations

import sys
import tempfile
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import solet_manager.preview_engine as preview_engine  # noqa: E402
from solet_manager.config import CreateConfig  # noqa: E402
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.errors import StateError  # noqa: E402
from solet_manager.flow import initial_probe_activations, initial_stage_probe_statuses  # noqa: E402
from solet_manager.journal_migrations import activation_site_key  # noqa: E402
from solet_manager.models import CheckpointStatus, CommandResult, ExitCode, JsonValue  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.registry import InstanceRegistry  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.transaction import Transaction, canonical_sha256, write_transaction  # noqa: E402

_CONTRACTS = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "github_midwife_plugin"
    / "knowledge_base"
)
_DEACTIVATED_PROBES = (
    "postgres_ready",
    "postgres_role_policy_valid",
    "pgvector_ready",
    "genesis_artifacts_valid",
    "fresh_shell_path_valid",
    "fresh_shell_python_valid",
    "launchagent_running",
)
_DEACTIVATED_PROBE_LOCATIONS = (
    ("system_dependencies", "exit", "postgres_ready"),
    ("system_dependencies", "exit", "postgres_role_policy_valid"),
    ("system_dependencies", "exit", "pgvector_ready"),
    ("genesis", "exit", "genesis_artifacts_valid"),
    ("genesis", "exit", "fresh_shell_path_valid"),
    ("genesis", "exit", "fresh_shell_python_valid"),
    ("models", "exit", "launchagent_running"),
)


def _business_profile(bundle: ContractBundle) -> str:
    option_source = cast(dict[str, JsonValue], bundle.decisions["setup_profile"]["option_source"])
    options = cast(dict[str, dict[str, JsonValue]], option_source["options"])
    return next(
        profile
        for profile, definition in options.items()
        if definition["label"] == "Business operations"
    )


def _answers(bundle: ContractBundle, target: Path) -> dict[str, JsonValue]:
    return {
        "schema_version": 1,
        "flow_id": "macos.repository_setup",
        "flow_source_revision": "a" * 40,
        "name": "activation-resume",
        "target": str(target),
        "public_inputs": {},
        "decisions": {
            "setup_profile": _business_profile(bundle),
            "autostart": "enabled",
            "embeddings_implementation": "lm_studio",
            "embedding_model": "fixture-embedding",
            "inference_implementation": "lm_studio",
            "inference_model": "fixture-inference",
            "coding_agents": ["codex", "claude_code"],
            "execution_topology": "fleet",
            "connector_configuration_timing": "first_use",
        },
        "consents": {},
        "resolution_evidence": [],
    }


def _seed(profile: str) -> SeedLock:
    return SeedLock(
        f"https://github.com/solet-public/{profile}.git",
        "release-1",
        "a" * 40,
        "b" * 40,
        "c" * 64,
        profile,
    )


def _transaction(
    bundle: ContractBundle,
    *,
    target: Path,
) -> Transaction:
    answers = _answers(bundle, target)
    return Transaction.create(
        name="activation-resume",
        target=target,
        input_fingerprint="sha256:" + "1" * 64,
        answers=answers,
        seed=_seed(_business_profile(bundle)),
        flow_id=bundle.flow_id,
        flow_source_revision=bundle.source_revision,
        flow_contract_digest=bundle.contract_digest,
        stage_ids=tuple(bundle.stages),
        stage_probe_statuses=initial_stage_probe_statuses(
            bundle,
            answers,
        ),
        probe_activations=initial_probe_activations(bundle, answers),
        completion_probe_ids=bundle.completion_probe_ids,
    )


def _write_resume_transaction(
    transaction: Transaction,
    root: Path,
) -> tuple[ManagerPaths, CreateConfig]:
    paths = ManagerPaths.resolve(explicit_home=root / "manager", home=root)
    config = CreateConfig(
        name=transaction.name,
        target=Path(transaction.target),
        autostart=True,
    )
    write_transaction(paths.transaction_path(config.name), transaction)
    return paths, config


def _status_delta(
    before: Transaction,
    after: Transaction,
) -> set[tuple[str, str, str, CheckpointStatus, CheckpointStatus]]:
    return {
        (stage_id, boundary, probe_id, before_status, after_status)
        for stage_id, boundaries in before.stage_probe_statuses.items()
        for boundary, probes in boundaries.items()
        for probe_id, before_status in probes.items()
        if (
            after_status := after.stage_probe_statuses[stage_id][boundary][probe_id]
        ) is not before_status
    }


def _upgraded_bundle(bundle: ContractBundle) -> ContractBundle:
    upgraded = deepcopy(bundle)
    for probe_id in _DEACTIVATED_PROBES:
        upgraded.probes[probe_id]["required_when"] = {
            "decision_ref": "setup_profile",
            "operator": "equals",
            "value": "free",
        }
    return upgraded


def _assert_activation_drift_reconciles(bundle: ContractBundle, root: Path) -> None:
    target = root / "Solets" / "activation-resume"
    target.mkdir(parents=True)
    transaction = _transaction(bundle, target=target)
    paths, config = _write_resume_transaction(transaction, root)

    resumed = preview_engine._load_setup_transaction(  # pyright: ignore[reportPrivateUsage]
        paths,
        config,
        _upgraded_bundle(bundle),
    )

    assert _status_delta(transaction, resumed) == set()
    assert {
        (site, before["state"], resumed.probe_activations[site]["state"])
        for site, before in transaction.probe_activations.items()
        if resumed.probe_activations[site] != before
    } == {
        (activation_site_key(stage_id, boundary, probe_id), "active", "inactive")
        for stage_id, boundary, probe_id in _DEACTIVATED_PROBE_LOCATIONS
    }
    assert resumed.stages == {
        **transaction.stages,
        "genesis": CheckpointStatus.NOT_APPLICABLE,
    }
    assert resumed.operation_statuses == transaction.operation_statuses
    assert resumed.completion == transaction.completion


def _assert_tampered_probe_map_stays_corrupt(
    bundle: ContractBundle,
    root: Path,
) -> None:
    target = root / "Solets" / "corrupt-resume"
    target.mkdir(parents=True)
    transaction = _transaction(bundle, target=target)
    raw = transaction.to_dict()
    del raw["stage_probe_statuses"]["preflight"]["entry"]["git_checkout_valid"]  # type: ignore[index]
    try:
        Transaction.from_dict(raw)
    except StateError as exc:
        assert exc.error_kind == "corrupt_state"
    else:
        raise AssertionError("tampered stage-probe map must fail parse as corrupt_state")


def _resume_transaction_with_matching_identity(
    bundle: ContractBundle,
    *,
    target: Path,
) -> tuple[CreateConfig, Transaction]:
    transaction = _transaction(bundle, target=target)
    config = CreateConfig(
        name=transaction.name,
        target=target,
        autostart=True,
    )
    return config, replace(
        transaction,
        input_fingerprint=canonical_sha256(config.to_identity_dict()),
    )


def _assert_absent_target_resume_renders_permission_preflight(
    bundle: ContractBundle,
    root: Path,
) -> None:
    target = root / "Solets" / "absent-resume"
    config, transaction = _resume_transaction_with_matching_identity(bundle, target=target)
    paths = ManagerPaths.resolve(explicit_home=root / "manager", home=root)
    with (
        patch.object(preview_engine.ContractBundle, "load", return_value=bundle) as load,
        patch("solet_manager.permission_preflight.display_session_attached", return_value=False),
    ):
        preview = preview_engine._resume_preview(  # pyright: ignore[reportPrivateUsage]
            paths=paths,
            registry=InstanceRegistry(paths.registry_path),
            config=config,
            transaction=transaction,
            selections={},
            decision_source="flag",
            sources={},
        )
    preflight = preview.data.get("permission_preflight")
    assert isinstance(preflight, dict) and isinstance(preflight.get("items"), list)
    assert load.call_args is not None
    assert load.call_args.kwargs == {
        "source_revision": transaction.flow_source_revision,
        "expected_digest": transaction.flow_contract_digest,
        "resume_compatibility": True,
    }


def _assert_target_exists_resume_keeps_target_contract_source(
    bundle: ContractBundle,
    root: Path,
) -> None:
    target = root / "Solets" / "target-exists-resume"
    target_contracts = target / "plugins" / "github_midwife_plugin" / "knowledge_base"
    target_contracts.mkdir(parents=True)
    for source in _CONTRACTS.iterdir():
        if source.is_file():
            (target_contracts / source.name).write_bytes(source.read_bytes())
    config, transaction = _resume_transaction_with_matching_identity(bundle, target=target)
    paths = ManagerPaths.resolve(explicit_home=root / "manager", home=root)
    expected = CommandResult(
        kind="fixture",
        status="preview_ready",
        message="fixture",
        exit_code=ExitCode.OK,
    )
    with patch.object(preview_engine, "setup_preview", return_value=expected) as setup:
        preview = preview_engine._resume_preview(  # pyright: ignore[reportPrivateUsage]
            paths=paths,
            registry=InstanceRegistry(paths.registry_path),
            config=config,
            transaction=transaction,
            selections={},
            decision_source="flag",
            sources={},
        )
    assert preview is expected
    assert setup.call_args is not None
    resumed_bundle = setup.call_args.kwargs["bundle"]
    assert isinstance(resumed_bundle, ContractBundle)
    assert resumed_bundle.directory == target_contracts.resolve()


def main() -> int:
    bundle = ContractBundle.load(source_revision="a" * 40, directory=_CONTRACTS)
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _assert_activation_drift_reconciles(bundle, root)
        _assert_tampered_probe_map_stays_corrupt(bundle, root)
        _assert_absent_target_resume_renders_permission_preflight(bundle, root)
        _assert_target_exists_resume_keeps_target_contract_source(bundle, root)
    print("preview_resume_activation_smoke OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
