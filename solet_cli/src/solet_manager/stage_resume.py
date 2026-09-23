"""Named-stage guard for a journal-backed create resume."""

from __future__ import annotations

from .config import CreateConfig
from .contracts import ContractBundle, target_contract_directory
from .errors import StateConflictError
from .flow import current_frontier_stage_ids
from .paths import ManagerPaths
from .transaction import canonical_sha256, load_transaction


def require_named_resume_stage(
    *,
    paths: ManagerPaths,
    config: CreateConfig,
    stage_id: str,
) -> None:
    """Require that a retained journal has exactly ``stage_id`` as its frontier.

    The ordinary ``create`` path remains responsible for preview, approval, and
    mutation.  This guard makes a preserved snapshot's intended boundary
    explicit before that path may run.
    """

    transaction = load_transaction(paths.transaction_path(config.name))
    if transaction is None:
        raise StateConflictError(
            "--resume-stage requires an existing transaction journal",
            repair="Use create without --resume-stage for a new instance.",
        )
    expected_identity = canonical_sha256(config.to_identity_dict())
    if (
        transaction.name != config.name
        or transaction.target != str(config.target)
        or transaction.input_fingerprint != expected_identity
    ):
        raise StateConflictError(
            "--resume-stage inputs do not match the retained transaction identity",
            repair="Use the original name and target from the preserved guest journal.",
        )
    bundle = ContractBundle.load(
        source_revision=transaction.flow_source_revision,
        directory=target_contract_directory(config.target),
        expected_digest=transaction.flow_contract_digest,
        resume_compatibility=True,
    )
    if stage_id not in bundle.stages:
        raise StateConflictError(
            f"--resume-stage names no declared stage: {stage_id!r}",
            repair="Inspect the target-local setup flow and select one declared stage id.",
        )
    frontier = current_frontier_stage_ids(bundle, transaction, transaction.answers)
    if frontier != (stage_id,):
        raise StateConflictError(
            "--resume-stage does not match the journal's sole executable frontier: "
            f"requested={stage_id!r}, current={list(frontier)!r}",
            repair=(
                "Resume the exact current frontier, or preserve a snapshot whose journal "
                "stops immediately before the requested stage."
            ),
        )
