#!/usr/bin/env python3
"""R-A freeze and digest-keyed reconciliation controls for Lane I1."""

from __future__ import annotations

import inspect
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_SOLET_CLI_ROOT = Path(__file__).resolve().parents[1]
_ROOT = _SOLET_CLI_ROOT.parents[0]
sys.path.insert(0, str(_SOLET_CLI_ROOT / "src"))

from solet_manager import contract_reconciliation as reconciliation_module  # noqa: E402
from solet_manager import contracts as contracts_module  # noqa: E402
from solet_manager.contracts import (  # noqa: E402
    ContractReconciliation,
    contract_digest,
    load_contract_reconciliations,
)
from solet_manager.errors import StateConflictError  # noqa: E402
from solet_manager.models import CheckpointStatus  # noqa: E402
from solet_manager.registry import InstanceRegistry  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.transaction import Transaction  # noqa: E402

_CHECKS = 0
_LEGACY_DIGEST = "sha256:67c903ea332287f2c73f036ff055b793f50072ed3af0784d68a505801c040540"
_LEGACY_FLOW = (
    _SOLET_CLI_ROOT
    / "tests/fixtures/contracts/reconcile_contract_legacy_4ff38b3d/macos_setup_flow.json"
)
_CONTRACTS = _ROOT / "plugins/github_midwife_plugin/knowledge_base"
_DIGEST_TOOL = _SOLET_CLI_ROOT / "tools/compute_contract_digest.py"
_MANIFEST = (
    _SOLET_CLI_ROOT / "src/solet_manager/released_metadata/contract_reconciliation_manifest.json"
)


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _migration_at(migrations: object, index: int) -> object:
    if not isinstance(migrations, list):
        raise AssertionError("released reconciliation manifest must contain a migrations list")
    return migrations[index] if len(migrations) > index else None


def _transaction() -> Transaction:
    return Transaction.create(
        name="fixture",
        target=Path("/tmp/fixture"),
        input_fingerprint="sha256:" + "a" * 64,
        answers={},
        seed=SeedLock(
            "https://example.invalid/seed.git", "release", "b" * 40, "c" * 40, None, "fixture"
        ),
        flow_id="macos.repository_setup",
        flow_source_revision="d" * 40,
        flow_contract_digest="sha256:" + "e" * 64,
        stage_ids=("preflight",),
        completion_probe_ids=(),
    )


def _check_freeze() -> None:
    original = _transaction()
    reconciled = original.reconciled_contract(
        flow_contract_digest="sha256:" + "f" * 64,
        stages={"preflight": CheckpointStatus.PENDING},
        stage_probe_statuses={},
        probe_activations={},
        stage_probe_attempts=(),
        completion_probe_ids=(),
    )
    _check(
        reconciled.flow_source_revision == original.flow_source_revision, "journal revision freezes"
    )
    _check(
        "flow_source_revision"
        not in inspect.signature(InstanceRegistry.reconcile_contract).parameters,
        "registry CAS no longer accepts a replacement revision",
    )


def _check_legacy_digest_gate() -> None:
    flow = json.loads(_LEGACY_FLOW.read_text(encoding="utf-8"))
    normalized = contracts_module._normalize_legacy_resume_flow_v1(_LEGACY_DIGEST, flow)
    _check(normalized != flow, "known legacy digest enables normalizer")
    _check(
        contracts_module._normalize_legacy_resume_flow_v1("sha256:" + "0" * 64, flow) == flow,
        "same shape with another digest is not normalized",
    )
    _check(
        flow == json.loads(_LEGACY_FLOW.read_text(encoding="utf-8")), "normalizer preserves input"
    )


def _check_flow_binding() -> None:
    transaction = _transaction()
    candidate = ContractReconciliation(
        migration_id="flow-binding",
        flow_id=transaction.flow_id,
        source_revision=transaction.flow_source_revision,
        source_digest=transaction.flow_contract_digest,
        destination_digest="sha256:" + "f" * 64,
        stage_probe_mappings=(),
    )
    source = SimpleNamespace(flow_id=transaction.flow_id)
    wrong_destination = SimpleNamespace(
        flow_id="other.flow", contract_digest=candidate.destination_digest
    )
    with patch.object(
        reconciliation_module, "load_contract_reconciliations", return_value=(candidate,)
    ):
        try:
            reconciliation_module._select_reconciliation(
                transaction, source, wrong_destination, None
            )
        except StateConflictError as exc:
            _check("destination flow_id" in str(exc), "destination flow binding refuses mismatch")
        else:
            _check(False, "destination flow binding must refuse mismatch")


def _check_116_destination_selection() -> None:
    """The 1.16 selector remains exact on the observed destination digest."""

    transaction = _transaction()
    source = SimpleNamespace(flow_id=transaction.flow_id)
    destination = SimpleNamespace(flow_id=transaction.flow_id, contract_digest="sha256:" + "f" * 64)
    selected = ContractReconciliation(
        migration_id="one",
        flow_id=transaction.flow_id,
        source_revision=transaction.flow_source_revision,
        source_digest=transaction.flow_contract_digest,
        destination_digest=destination.contract_digest,
        stage_probe_mappings=(),
    )
    wrong_destination = ContractReconciliation(
        migration_id="zero",
        flow_id=transaction.flow_id,
        source_revision=transaction.flow_source_revision,
        source_digest=transaction.flow_contract_digest,
        destination_digest="sha256:" + "0" * 64,
        stage_probe_mappings=(),
    )
    with patch.object(
        reconciliation_module, "load_contract_reconciliations", return_value=(wrong_destination,)
    ):
        try:
            reconciliation_module._select_reconciliation(transaction, source, destination, None)
        except StateConflictError as exc:
            _check("forward-only entry" in str(exc), "1.16 zero destination match refuses")
        else:
            _check(False, "1.16 zero destination match must refuse")
    with patch.object(
        reconciliation_module, "load_contract_reconciliations", return_value=(selected, selected)
    ):
        try:
            reconciliation_module._select_reconciliation(transaction, source, destination, None)
        except StateConflictError as exc:
            _check("multiple entries match" in str(exc), "1.16 many destination matches refuse")
        else:
            _check(False, "1.16 many destination matches must refuse")


def _check_manifest_destination_shape() -> None:
    raw = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    migrations = raw["migrations"]
    fourth = _migration_at(migrations, 3)
    _check(
        fourth
        == {
            "migration_id": "macos-repository-setup-r20-to-postgresql-preprobe-reconciliation-v1",
            "source": {
                "flow_id": "macos.repository_setup",
                "flow_source_revision": "5ae81ea4c453849f971512e5ea7b2eb762f90379",
                "flow_contract_digest": (
                    "sha256:572552bdd63cf06c48b76c79dbcbd0d7eae6e8f15ed85646acf84d36e9e5c054"
                ),
            },
            "destination": {
                "flow_id": "macos.repository_setup",
                "flow_contract_digest": (
                    "sha256:91f7fc9c683f3ae139000a03c2ec747de1a3d0852c2086e2f0a39ac954dead5d"
                ),
            },
            "stage_probe_mappings": [],
            "first_use_inactive_probe_migrations": [],
            "operation_statuses_to_reset": [],
        },
        "fourth migration is the missing install_postgresql pre-probe reconciliation identity",
    )
    fifth = _migration_at(migrations, 4)
    _check(
        fifth
        == {
            "migration_id": (
                "macos-repository-setup-postgresql-preprobe-to-"
                "launchagent-after-models-reconciliation-v1"
            ),
            "source": {
                "flow_id": "macos.repository_setup",
                "flow_source_revision": "5ae81ea4c453849f971512e5ea7b2eb762f90379",
                "flow_contract_digest": (
                    "sha256:91f7fc9c683f3ae139000a03c2ec747de1a3d0852c2086e2f0a39ac954dead5d"
                ),
            },
            "destination": {
                "flow_id": "macos.repository_setup",
                "flow_contract_digest": (
                    "sha256:edefbe8bbb0409a4812b4f0eac1a668ecfdbc72a9fa84d7a877298788e32a5ea"
                ),
            },
            "stage_probe_mappings": [
                {
                    "source": {
                        "stage_id": "genesis",
                        "boundary": "exit",
                        "probe_id": "launchagent_running",
                    },
                    "destination": {
                        "stage_id": "models",
                        "boundary": "exit",
                        "probe_id": "launchagent_running",
                    },
                },
                {
                    "source": {
                        "stage_id": "genesis",
                        "boundary": "exit",
                        "probe_id": "router_ready",
                    },
                    "destination": {
                        "stage_id": "models",
                        "boundary": "exit",
                        "probe_id": "router_ready",
                    },
                },
            ],
            "first_use_inactive_probe_migrations": [],
            "operation_statuses_to_reset": [],
        },
        "fifth migration maps the launch-agent probes after models owns them",
    )
    sixth = _migration_at(migrations, 5)
    _check(
        sixth
        == {
            "migration_id": (
                "macos-repository-setup-launchagent-after-models-to-"
                "precondition-subset-reconciliation-v1"
            ),
            "source": {
                "flow_id": "macos.repository_setup",
                "flow_source_revision": "5ae81ea4c453849f971512e5ea7b2eb762f90379",
                "flow_contract_digest": (
                    "sha256:edefbe8bbb0409a4812b4f0eac1a668ecfdbc72a9fa84d7a877298788e32a5ea"
                ),
            },
            "destination": {
                "flow_id": "macos.repository_setup",
                "flow_contract_digest": (
                    "sha256:82baf4e5c346ebf9a6f1226c36d52b476216b8b579c2a83fc8781dc231a87171"
                ),
            },
            "stage_probe_mappings": [],
            "first_use_inactive_probe_migrations": [],
            "operation_statuses_to_reset": [],
        },
        "sixth migration pins the four precondition-subset sibling repairs",
    )
    _check(
        isinstance(migrations, list) and len(migrations) == 20,
        "twenty shipped migrations including direct LM Studio pgvector reconciliation",
    )
    _check(
        all(
            isinstance(item, dict)
            and isinstance(item.get("destination"), dict)
            and set(item["destination"]) == {"flow_id", "flow_contract_digest"}
            for item in migrations
        ),
        "shipped destination identities are digest-only",
    )
    _check(len(load_contract_reconciliations()) == 20, "digest-only manifest parses")


def _check_digest_tool() -> None:
    """Split per Architect arm-5b37e64a section 3: the SEAM leg ships, the TOOL leg does not.

    The adopter-relevant assertion here was never really about the CLI. The
    property that matters is that the shipped production seam,
    ``solet_manager.contracts.contract_digest``, computes a stable digest over
    the staged contract tree — and that seam ships (``solet_cli/src/`` is in the
    manifest's ``copy:`` allowlist). ``solet_cli/tools/compute_contract_digest.py``
    is a nine-line argparse wrapper around that same function, and ``**/tools/``
    is a categorically pruned operator-tooling class, so the subprocess made this
    smoke 1 of the 14 BLOCKING rows in the r21 born-clone verdict (sealed
    254700866ab0…) with CalledProcessError on a file no seed carries.

    So the seam is asserted directly and unconditionally, and the wrapper's
    agreement with it — a development-tool assertion — runs only where the
    wrapper exists. Deliberately NOT asserting equality to a pinned digest
    value: r21's contract digest is frozen at sha256:6f4a5750… and this lane
    must not author anything that could move or re-pin it.
    """
    expected = contract_digest(_CONTRACTS)
    # Shape asserted against the seam's OWN contract (adapter_validation's
    # DIGEST_PATTERN), not a guessed one: the value is prefixed, `sha256:` plus
    # 64 lowercase hex. Determinism is asserted by recomputing rather than by
    # pinning a literal, so this stays a property assertion and cannot drift into
    # a second, competing statement of r21's frozen digest.
    _check(
        re.fullmatch(r"sha256:[0-9a-f]{64}", expected) is not None
        and expected == contract_digest(_CONTRACTS),
        "shipped contract_digest seam computes a stable sha256-prefixed staged-tree digest",
    )
    if not _DIGEST_TOOL.is_file():
        print("  SKIP  digest CLI wrapper: solet_cli/tools/ is a pruned operator-tooling class")
        return
    completed = subprocess.run(
        [sys.executable, str(_DIGEST_TOOL), str(_CONTRACTS)],
        check=True,
        capture_output=True,
        text=True,
    )
    _check(completed.stdout.strip() == expected, "digest tool computes the staged-tree digest")


def main() -> int:
    _check_freeze()
    _check_legacy_digest_gate()
    _check_flow_binding()
    _check_116_destination_selection()
    _check_manifest_destination_shape()
    _check_digest_tool()
    print(f"reconciliation_identity_i1_smoke: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
