"""Retained attempts of unbound operations are history, not corruption (1.56).

``_expected_attempt_stage`` resolved an attempt's expected stage by looking its
operation up in the CURRENT ``completion`` / ``operation_stages`` maps, so a
retained attempt whose operation had legitimately left the plan raised
``StateError: transaction attempt stage mismatch``.

That enforced an invariant the storage model does not hold.  ``operation_attempts``
is retained history; ``operation_stages`` is the derived current binding; and
``bind_operations`` already drops operations as a normal, correct act.  A
validator that raises on legitimate state is a bug, and it is the shared root
under the rollup cluster: it blocks 1.40's plan-purity prune and it forces
1.41's completion-set prune to retain probes it should be able to drop.

The tolerance is deliberately narrow, and both branches are pinned below:

* operation ABSENT from the bindings  -> tolerated as history
* operation PRESENT but stage MISMATCHED -> still ``StateError``

Coverage honesty: admitting the absent case loses no real corruption coverage.
The old check never verified binding completeness -- a never-run operation has
no attempt, so it was never checked at all.  It was only ever a two-record
consistency check between an attempt and a binding that both exist.
"""

from __future__ import annotations

import sys
import uuid
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.adapter_protocol import OperationResult  # noqa: E402
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.errors import StateError  # noqa: E402
from solet_manager.journal_rollup import derive_stage_statuses  # noqa: E402
from solet_manager.models import CheckpointStatus, JsonValue  # noqa: E402
from solet_manager.operation_records import attempt_record  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.stage_activation import initial_stage_probe_statuses  # noqa: E402
from solet_manager.transaction import Transaction, canonical_sha256  # noqa: E402

_CONTRACTS = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "github_midwife_plugin"
    / "knowledge_base"
)

_OPERATION = "install_tmux"
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


def _attempted(bundle: ContractBundle, status: CheckpointStatus) -> Transaction:
    """A transaction with one bound operation that has recorded an attempt."""

    answers = _answers()
    created = Transaction.create(
        name="retained",
        target=Path("/tmp/retained"),
        input_fingerprint=canonical_sha256({"name": "retained"}),
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
    bound = created.bind_operations({_OPERATION: _STAGE})
    return bound.with_operation_status(
        _OPERATION,
        status,
        attempt=attempt_record(
            _result(_OPERATION, status),
            stage_id=_STAGE,
            phase="apply",
            attempt=1,
            owner_operation_id=_OPERATION,
        ),
    )


def _assert_absent_operation_is_tolerated(bundle: ContractBundle) -> Transaction:
    """The unblocking branch: a pruned operation's attempt is history."""

    attempted = _attempted(bundle, CheckpointStatus.AWAITING_USER)
    _check(
        len(attempted.operation_attempts) == 1,
        "fixture did not record the attempt it needs to retain",
    )
    pruned = attempted.bind_operations({})
    _check(
        _OPERATION not in pruned.operation_stages
        and len(pruned.operation_attempts) == 1,
        "prune did not leave a retained attempt for an unbound operation",
    )
    try:
        reloaded = Transaction.from_dict(pruned.to_dict())
    except StateError as error:
        raise AssertionError(
            f"red: a pruned operation's retained attempt was rejected as "
            f"corruption -- {error}"
        ) from error
    _check(
        len(reloaded.operation_attempts) == 1,
        "the retained attempt did not survive the round trip",
    )
    return pruned


def _assert_bound_mismatch_still_raises(bundle: ContractBundle) -> None:
    """The branch that must NOT widen: a bound operation filed under a wrong stage."""

    attempted = _attempted(bundle, CheckpointStatus.AWAITING_USER)
    other_stage = next(
        stage_id for stage_id in attempted.stages if stage_id != _STAGE
    )
    corrupted = replace(
        attempted,
        operation_stages={_OPERATION: other_stage},
    )
    try:
        Transaction.from_dict(corrupted.to_dict())
    except StateError as error:
        # Pin the SPECIFIC check, not merely "something raised".  Several other
        # validators also reject this document, so a bare StateError assertion
        # passes even with the mismatch check deleted outright.
        _check(
            "attempt stage mismatch" in str(error),
            "a bound-operation stage mismatch was caught by some other "
            f"validator, not by the attempt-stage check -- got: {error}",
        )
        return
    raise AssertionError(
        "red: an attempt filed under a stage its BOUND operation does not "
        "belong to was accepted -- the tolerance widened past absent operations"
    )


def _assert_retained_attempts_do_not_feed_stage_status(pruned: Transaction) -> None:
    """The leak regression: history must not resurrect through stage derivation.

    ``derive_stage_statuses`` takes only the binding and status maps, never
    ``operation_attempts``.  Pinning it here means a future reader that starts
    consulting attempts for stage status trips this instead of silently
    reviving a pruned operation's blocking status.
    """

    derived = derive_stage_statuses(
        dict(pruned.stages),
        pruned.stage_probe_statuses,
        pruned.operation_stages,
        pruned.operation_statuses,
        pruned.probe_activations,
    )
    _check(
        derived[_STAGE] is not CheckpointStatus.AWAITING_USER,
        "a pruned operation's retained attempt still pins its stage at "
        "awaiting_user -- retained history is feeding stage-status derivation",
    )
    _check(
        derived == pruned.stages,
        "the pruned journal's persisted stages disagree with their own "
        "re-derivation",
    )


def main() -> int:
    bundle = ContractBundle.load(source_revision="a" * 40, directory=_CONTRACTS)
    pruned = _assert_absent_operation_is_tolerated(bundle)
    _assert_bound_mismatch_still_raises(bundle)
    _assert_retained_attempts_do_not_feed_stage_status(pruned)
    print(f"retained_attempt_validation_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
