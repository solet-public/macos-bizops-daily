#!/usr/bin/env python3
"""Prove a migrated verified PostgreSQL operation is re-evaluated."""

from __future__ import annotations

import shutil
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.adapter_protocol import OperationResult  # noqa: E402
from solet_manager.contracts import (  # noqa: E402
    ContractBundle,
    load_contract_reconciliations,
)
from solet_manager.flow import initial_stage_probe_statuses  # noqa: E402
from solet_manager.models import CheckpointStatus, JsonValue  # noqa: E402
from solet_manager.operation_records import attempt_record  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.stage_activation import reconcile_contract_stage_probe_state  # noqa: E402
from solet_manager.transaction import Transaction, canonical_sha256  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
_CONTRACTS = _ROOT / "plugins/github_midwife_plugin/knowledge_base"
_HISTORICAL_FIXTURES = (
    _ROOT / "solet_cli/tests/fixtures/reconciliation_operation_status_reset"
)
_MIGRATION_ID = "macos-repository-setup-pgvector-package-postcondition-v1"
_SOURCE_DIGEST = "sha256:c2a0386e86e0378e694f1793732fac223ec70ff3358e856b54ccf588e51024a4"
_DESTINATION_DIGEST = "sha256:ce20096b3746db9363af96eb0416dd65d2bee96dac615e06c1d6f4d5e5bfd148"
_ACTIVE_DESTINATION_MIGRATION_IDS = frozenset(
    {
        "macos-repository-setup-pgvector-package-postcondition-v1",
        "macos-repository-setup-pre-ram-preflight-pgvector-package-postcondition-v1",
        "macos-lm-studio-provisioning-to-pgvector-package-from-92634d2b-v1",
        "macos-lm-studio-provisioning-to-pgvector-package-from-2b9eb957-v1",
        "macos-lm-studio-provisioning-to-pgvector-package-from-73af1c9d-v1",
        # r64: one bridge per published stable seed commit that records 19350a8d.
        "macos-repository-setup-lm-studio-not-recommended-from-1dedebb6-v1",
        "macos-repository-setup-lm-studio-not-recommended-from-d81e014a-v1",
        "macos-repository-setup-lm-studio-not-recommended-from-8b3fe23d-v1",
        "macos-repository-setup-lm-studio-not-recommended-from-85f2994b-v1",
        "macos-repository-setup-lm-studio-not-recommended-from-6b292e91-v1",
        "macos-repository-setup-lm-studio-not-recommended-from-ee00d702-v1",
        "macos-repository-setup-lm-studio-not-recommended-from-1e167174-v1",
        "macos-repository-setup-lm-studio-not-recommended-from-a5732c9f-v1",
    }
)
_SOURCE_BUNDLE_COMMITS = {
    "sha256:19350a8d3b0b29139afe25b6f11648c0f3f2e1fdb8ca91f81d5bfa1280fc7492": (
        "dfe109303"
    ),
    "sha256:515ab65fcf6d82756b3ffc6bf42f782ddc33b69907b5d32a372b019bea6a8e72": (
        "8f12bbd0f"
    ),
    "sha256:5652d22cdc1c0a7e99820e0cee5d39751c65c066302ef6aba59e9863a17fa27c": (
        "13e82cb5c"
    ),
    "sha256:c2a0386e86e0378e694f1793732fac223ec70ff3358e856b54ccf588e51024a4": (
        "32fabad2c"
    ),
    "sha256:ce6d9d88fd77b2feeab964e2cd2f75e4d0ca6149634b0bcfac520cd047d720bb": (
        "cc478b616"
    ),
}
_CHECKS = 0


def _check(condition: bool, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _answers() -> dict[str, JsonValue]:
    return {
        "decisions": {
            "setup_profile": "custom",
            "embeddings_implementation": "lm_studio",
            "embedding_model": "nomic",
            "inference_implementation": "lm_studio",
            "inference_model": "qwen",
            "coding_agents": ["codex"],
            "execution_topology": "solo",
            "git_mutation_control": "single_session",
            "connector_configuration_timing": "configure_later",
            "autostart": "enabled",
            "session_sources": [],
        },
        "consents": {"system_change_consent": True},
    }


def _seed() -> SeedLock:
    return SeedLock(
        "https://github.com/solet-public/macos-bizops.git",
        "release-2026-09-11",
        "a" * 40,
        "b" * 40,
        "c" * 64,
        "macos-bizops",
    )


def _install_stage(bundle: ContractBundle) -> str:
    matches = [
        stage_id
        for stage_id, stage in bundle.stages.items()
        if "install_postgresql" in stage.get("operation_refs", [])
    ]
    if len(matches) != 1:
        raise AssertionError("fixture requires exactly one install_postgresql stage")
    return matches[0]


def _verified_result() -> OperationResult:
    return OperationResult(
        request_id=str(uuid.uuid4()),
        operation_id="install_postgresql",
        phase="apply",
        probe_purpose=None,
        checkpoint_status=CheckpointStatus.VERIFIED,
        error_kind=None,
        retry_safe=True,
        exit_code=0,
        timed_out=False,
        duration_ms=1,
        stdout="",
        stderr="",
        planned_actions=(),
        discovered_candidates=(),
        evidence=(),
        repair=None,
    )


def _historical_contract(source_revision: str, digest: str) -> ContractBundle:
    """Load a digest-pinned source bundle from a checked-in fixture."""

    fixture = _HISTORICAL_FIXTURES / digest.removeprefix("sha256:")
    return ContractBundle.load(
        source_revision=source_revision,
        directory=fixture,
        expected_digest=digest,
    )


def _postcondition_probe_refs(bundle: ContractBundle, operation_id: str) -> set[str]:
    operation = bundle.operations[operation_id]
    idempotency = operation["idempotency"]
    return set(idempotency["postcondition_probe_refs"])


def _assert_grown_postconditions_reset(destination: ContractBundle) -> None:
    """Require every bridge to the active contract to invalidate grown operations."""

    active_bridges = [
        item
        for item in load_contract_reconciliations()
        if item.destination_digest == destination.contract_digest
    ]
    _check(active_bridges, "the active contract has declared reconciliation bridges")
    _check(
        {migration.migration_id for migration in active_bridges}
        == _ACTIVE_DESTINATION_MIGRATION_IDS,
        "every historical reconciliation bridge pins the active candidate destination",
    )
    for migration in active_bridges:
        if migration.source_digest not in _SOURCE_BUNDLE_COMMITS:
            raise AssertionError(f"missing historical bundle commit for {migration.source_digest}")
        source = _historical_contract(migration.source_revision, migration.source_digest)
        for operation_id in sorted(set(source.operations) & set(destination.operations)):
            source_probes = _postcondition_probe_refs(source, operation_id)
            destination_probes = _postcondition_probe_refs(destination, operation_id)
            if destination_probes > source_probes:
                _check(
                    operation_id in migration.operation_statuses_to_reset,
                    f"{migration.migration_id} resets {operation_id} after postconditions grow",
                )


def main() -> int:
    active_destination = ContractBundle.load(
        source_revision="candidate",
        directory=_CONTRACTS,
        expected_digest=_DESTINATION_DIGEST,
    )
    _check(
        active_destination.contract_digest == _DESTINATION_DIGEST,
        "destination pin is computed from unmodified active contract bundle bytes",
    )
    migration = next(
        item for item in load_contract_reconciliations() if item.migration_id == _MIGRATION_ID
    )
    _check(
        migration.source_digest == _SOURCE_DIGEST
        and migration.destination_digest == _DESTINATION_DIGEST
        and migration.operation_statuses_to_reset == ("install_postgresql",),
        "the released pgvector migration pins its exact reset authority",
    )
    with tempfile.TemporaryDirectory(prefix="reconciliation-operation-reset-") as raw:
        root = Path(raw)
        source = _historical_contract(migration.source_revision, _SOURCE_DIGEST)
        destination_path = root / "destination"
        shutil.copytree(_CONTRACTS, destination_path)
        destination = ContractBundle.load(
            source_revision=migration.source_revision,
            directory=destination_path,
            expected_digest=_DESTINATION_DIGEST,
        )
        _assert_grown_postconditions_reset(destination)
        answers = _answers()
        stage_id = _install_stage(source)
        transaction = Transaction.create(
            name="pgvector-reset",
            target=root / "target",
            input_fingerprint=canonical_sha256({"name": "pgvector-reset"}),
            answers=answers,
            seed=_seed(),
            flow_id=source.flow_id,
            flow_source_revision=source.source_revision,
            flow_contract_digest=source.contract_digest,
            stage_ids=tuple(source.stages),
            completion_probe_ids=source.completion_probe_ids,
            stage_probe_statuses=initial_stage_probe_statuses(source, answers),
        ).bind_operations({"install_postgresql": stage_id})
        verified = transaction.with_operation_status(
            "install_postgresql",
            CheckpointStatus.VERIFIED,
            attempt=attempt_record(
                _verified_result(),
                stage_id=stage_id,
                phase="apply",
                attempt=1,
                owner_operation_id="install_postgresql",
            ),
        )
        reconciled = reconcile_contract_stage_probe_state(destination, verified, migration)
        _check(
            reconciled.operation_statuses["install_postgresql"] is CheckpointStatus.PENDING,
            "a formerly verified PostgreSQL operation is pending under its new postconditions",
        )
        _check(
            reconciled.operation_attempts == verified.operation_attempts,
            "the reset retains the verified operation attempt as history",
        )
        _check(
            Transaction.from_dict(reconciled.to_dict()).operation_statuses["install_postgresql"]
            is CheckpointStatus.PENDING,
            "the explicit reconciliation reset transition survives journal validation",
        )
    print(f"reconciliation_operation_status_reset_smoke: {_CHECKS}/{_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
