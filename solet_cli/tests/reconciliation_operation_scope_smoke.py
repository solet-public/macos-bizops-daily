"""Reconciliation must not carry retired operations forward (rollup-cluster 1.42).

``reconcile_contract_stage_probe_state`` passed ``transaction.operation_stages``
into ``derive_stage_statuses`` verbatim, so an operation the SOURCE revision
declared but the DESTINATION revision does not kept feeding the destination
stage roll-up.  A retired operation left in a non-final state therefore pinned
its destination stage open forever, and no amount of work on the destination
contract could clear it -- the operation no longer exists to be run.

The re-scope drops those bindings and recomputes stages and status in the same
write.  Retained history is untouched: ``operation_attempts`` still records what
ran, which is legal to keep only because 1.56 stopped treating a retained
attempt for an unbound operation as corruption.  This fixture therefore also
pins that dependency -- it cannot pass without 1.56.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.adapter_protocol import OperationResult  # noqa: E402
from solet_manager.contracts import ContractBundle, ContractReconciliation  # noqa: E402
from solet_manager.models import CheckpointStatus, JsonValue  # noqa: E402
from solet_manager.operation_records import attempt_record  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.stage_activation import (  # noqa: E402
    initial_stage_probe_statuses,
    reconcile_contract_stage_probe_state,
)
from solet_manager.transaction import Transaction, canonical_sha256  # noqa: E402

_CONTRACTS = (
    Path(__file__).resolve().parents[2] / "plugins" / "github_midwife_plugin" / "knowledge_base"
)

# Declared by the source revision, absent from the destination contract.
_RETIRED = "__retired_by_destination__"
_KEPT = "install_tmux"
_STAGE = "preflight"
_CHECKS = 0


def _check(condition: bool, message: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {message}")


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


def _result(operation_id: str, status: CheckpointStatus) -> OperationResult:
    return OperationResult(
        request_id=str(uuid.uuid4()),
        operation_id=operation_id,
        phase="apply",
        probe_purpose=None,
        checkpoint_status=status,
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


def _applied(transaction: Transaction, operation_id: str, status: CheckpointStatus) -> Transaction:
    return transaction.with_operation_status(
        operation_id,
        status,
        attempt=attempt_record(
            _result(operation_id, status),
            stage_id=_STAGE,
            phase="apply",
            attempt=1,
            owner_operation_id=operation_id,
        ),
    )


def _source_transaction(bundle: ContractBundle) -> Transaction:
    """A journal carrying one kept operation and one the destination retired."""

    answers = _answers()
    created = Transaction.create(
        name="reconcile",
        target=Path("/tmp/reconcile"),
        input_fingerprint=canonical_sha256({"name": "reconcile"}),
        answers=answers,
        seed=SeedLock(
            "https://github.com/solet-public/macos-bizops.git",
            "release-2026-08-20",
            "a" * 40,
            "b" * 40,
            "c" * 64,
            "macos-bizops",
        ),
        flow_id=bundle.flow_id,
        flow_source_revision=bundle.source_revision,
        flow_contract_digest=bundle.contract_digest,
        stage_ids=tuple(bundle.stages),
        completion_probe_ids=(),
        stage_probe_statuses=initial_stage_probe_statuses(bundle, answers),
    )
    bound = created.bind_operations({_KEPT: _STAGE, _RETIRED: _STAGE})
    verified = _applied(bound, _KEPT, CheckpointStatus.VERIFIED)
    return _applied(verified, _RETIRED, CheckpointStatus.AWAITING_USER)


def _reconciliation(bundle: ContractBundle, transaction: Transaction) -> ContractReconciliation:
    return ContractReconciliation(
        migration_id="rollup-1-42-fixture",
        flow_id=bundle.flow_id,
        source_revision=transaction.flow_source_revision,
        source_digest=transaction.flow_contract_digest,
        destination_digest=bundle.contract_digest,
        stage_probe_mappings=(),
    )


def main() -> int:
    bundle = ContractBundle.load(source_revision="a" * 40, directory=_CONTRACTS)
    _check(
        _RETIRED not in bundle.operations and _KEPT in bundle.operations,
        "fixture premise is wrong: the destination contract's operation set does "
        "not distinguish the retired operation from the kept one",
    )
    source = _source_transaction(bundle)
    _check(
        source.stages[_STAGE] is CheckpointStatus.AWAITING_USER,
        "the retired operation does not pin the source stage, so there is "
        "nothing for this fixture to discriminate",
    )

    reconciled = reconcile_contract_stage_probe_state(
        bundle,
        source,
        _reconciliation(bundle, source),
    )

    _check(
        _RETIRED not in reconciled.operation_stages,
        f"{_RETIRED} survived reconciliation into the destination bindings",
    )
    _check(
        reconciled.stages[_STAGE] is not CheckpointStatus.AWAITING_USER,
        f"{_RETIRED} still pins destination stage {_STAGE} at awaiting_user -- "
        "a retired operation blocks a stage that can never clear it",
    )
    _check(
        reconciled.operation_statuses.get(_KEPT) is CheckpointStatus.VERIFIED,
        "reconciliation lost the kept operation's verified status",
    )
    retired_attempts = [
        attempt
        for attempt in reconciled.operation_attempts
        if attempt.get("operation_id") == _RETIRED
    ]
    _check(
        len(retired_attempts) == 1,
        "reconciliation discarded the retired operation's history; the prune "
        "must drop the binding, never the attempt record",
    )
    # Round-tripping is the shared repair constraint, and it only holds with
    # 1.56 in place -- without it the retained attempt above reads as corruption.
    reloaded = Transaction.from_dict(reconciled.to_dict())
    _check(
        reloaded.stages == reconciled.stages,
        "the reconciled journal failed its own derived-state validation",
    )
    print(f"reconciliation_operation_scope_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
