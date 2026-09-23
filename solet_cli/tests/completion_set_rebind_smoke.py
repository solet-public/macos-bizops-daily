"""Discriminating coverage for the frozen completion set (rollup-cluster 1.41).

Two defects meet here and both fail GREEN, which is why the assertions below
are about what RUNS rather than about set membership alone:

* ``resolved_completion_probe_ids`` evaluated ``required_when`` with
  ``condition_matches``, whose ``_leaf_result`` reads an *absent* decision as
  ``False``.  A completion probe gated on a decision that is reachable but not
  yet answered was therefore dropped at create time, conflating "not resolved
  yet" with "resolved, and not required".
* ``Transaction.with_answers`` replaced the answers without re-scoping
  ``completion``, so the create-time set was frozen for the life of the
  transaction.  Answering the gating decision later could never re-admit the
  probe.

The shipped flow already contains the real instance: ``launchagent_running``
is gated on ``autostart``, and ``autostart`` is only a follow-up of
``setup_profile: custom``.  Under ``custom`` the decision is *active but
unresolved* at create; under ``free`` it is genuinely inactive.  One contract
exercises both directions, so no synthetic flow is needed.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.adapter_protocol import OperationResult  # noqa: E402
from solet_manager.adapters import AdapterRegistry  # noqa: E402
from solet_manager.completion_verifier import (  # noqa: E402
    rebind_completion_probes,
    resolved_completion_probe_ids,
    run_completion_probes,
)
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.models import (  # noqa: E402
    CheckpointStatus,
    JsonValue,
    TransactionStatus,
)
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.stage_activation import initial_stage_probe_statuses  # noqa: E402
from solet_manager.transaction import Transaction, canonical_sha256  # noqa: E402

_CONTRACTS = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "github_midwife_plugin"
    / "knowledge_base"
)

# Gated on `autostart`, which only exists as a follow-up under `custom`.
_GATED_PROBE = "launchagent_running"
# Gated on a *resolved* list decision, so only a rebind can re-admit it.
_REVISED_PROBE = "codex_session_roots_readable"
_CHECKS = 0


def _check(condition: bool, message: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {message}")


def _answers(
    *,
    setup_profile: str,
    autostart: str | None,
    session_sources: list[str] | None = None,
) -> dict[str, JsonValue]:
    """Build normalized answers, omitting `autostart` to leave it unresolved."""

    decisions: dict[str, JsonValue] = {
        "setup_profile": setup_profile,
        "embeddings_implementation": "lm_studio",
        "embedding_model": "nomic",
        "inference_implementation": "lm_studio",
        "inference_model": "qwen",
        "coding_agents": ["codex"],
        "execution_topology": "solo",
        "git_mutation_control": "single_session",
        "connector_configuration_timing": "configure_later",
        "session_sources": list(session_sources or []),
    }
    if autostart is not None:
        decisions["autostart"] = autostart
    return {"decisions": decisions, "consents": {"system_change_consent": True}, "public_inputs": {"lm_studio_base_url": "http://localhost:1234/v1"}}


def _seed() -> SeedLock:
    return SeedLock(
        "https://github.com/solet-public/macos-bizops.git",
        "release-2026-08-20",
        "a" * 40,
        "b" * 40,
        "c" * 64,
        "macos-bizops",
    )


def _transaction(
    bundle: ContractBundle,
    target: Path,
    answers: dict[str, JsonValue],
) -> Transaction:
    return Transaction.create(
        name="rebind",
        target=target,
        input_fingerprint=canonical_sha256({"name": "rebind"}),
        answers=answers,
        seed=_seed(),
        flow_id=bundle.flow_id,
        flow_source_revision=bundle.source_revision,
        flow_contract_digest=bundle.contract_digest,
        stage_ids=tuple(bundle.stages),
        completion_probe_ids=resolved_completion_probe_ids(bundle, answers),
        stage_probe_statuses=initial_stage_probe_statuses(bundle, answers),
    )


def _verified_result(request_id: str, operation_id: str) -> OperationResult:
    return _result_with(request_id, operation_id, CheckpointStatus.VERIFIED)


def _result_with(
    request_id: str,
    operation_id: str,
    status: CheckpointStatus,
) -> OperationResult:
    return OperationResult(
        request_id=request_id,
        operation_id=operation_id,
        phase="probe",
        probe_purpose="completion",
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


def _assert_unresolved_is_retained(bundle: ContractBundle) -> None:
    """An active-but-unanswered gate must not read as 'not required'."""

    unresolved = resolved_completion_probe_ids(
        bundle,
        _answers(setup_profile="custom", autostart=None),
    )
    _check(
        _GATED_PROBE in unresolved,
        f"{_GATED_PROBE} was dropped while its gating decision was merely "
        "unresolved -- absent decision read as False",
    )


def _assert_deselected_is_excluded(bundle: ContractBundle) -> None:
    """The opposite direction: a genuinely unreachable gate must not block."""

    deselected = resolved_completion_probe_ids(
        bundle,
        _answers(setup_profile="free", autostart=None),
    )
    _check(
        _GATED_PROBE not in deselected,
        f"{_GATED_PROBE} was retained under a profile that never offers "
        "autostart -- a de-selected probe would block convergence forever",
    )


def _assert_resolved_directions(bundle: ContractBundle) -> None:
    """A resolved gate still decides membership both ways."""

    disabled = resolved_completion_probe_ids(
        bundle,
        _answers(setup_profile="custom", autostart="disabled"),
    )
    _check(
        _GATED_PROBE not in disabled,
        f"{_GATED_PROBE} survived a resolved, non-matching gate",
    )
    enabled = resolved_completion_probe_ids(
        bundle,
        _answers(setup_profile="custom", autostart="enabled"),
    )
    _check(
        _GATED_PROBE in enabled,
        f"{_GATED_PROBE} is absent under a resolved, matching gate",
    )


def _assert_probe_runs_after_revision(bundle: ContractBundle, root: Path) -> Transaction:
    """The load-bearing one: the re-admitted probe must actually EXECUTE.

    Asserting the bound set alone is not a discriminator here.  The failure
    direction is omission, and an omitted completion probe lets the roll-up
    report VERIFIED, so a membership-only assertion can pass while the target
    converges without ever running the check.

    This uses ``session_sources`` rather than the unresolved-``autostart``
    case on purpose.  At create the answer is ``[]`` -- *resolved*, and
    correctly not required -- so create-time scoping is not what re-admits the
    probe when the user later selects ``codex_local``.  Only the rebind can.
    A fixture built on the unresolved gate would pass with the rebind removed
    entirely, because the condition fix alone already admits that probe at
    create.
    """

    target = root / "target"
    target.mkdir(parents=True, exist_ok=True)
    paths = ManagerPaths.resolve(explicit_home=root / "home", home=root)
    created = _transaction(
        bundle,
        target,
        _answers(setup_profile="custom", autostart="enabled", session_sources=[]),
    )
    _check(
        _REVISED_PROBE not in created.completion,
        f"{_REVISED_PROBE} was bound while its source was deselected",
    )
    revised = _answers(
        setup_profile="custom",
        autostart="enabled",
        session_sources=["codex_local"],
    )
    updated = rebind_completion_probes(bundle, created.with_answers(revised), revised)
    _check(
        updated.completion.get(_REVISED_PROBE) is CheckpointStatus.PENDING,
        f"{_REVISED_PROBE} was not re-admitted as pending when its gate matched",
    )
    registry = AdapterRegistry(target=target, base_python=None)
    with patch(
        "solet_manager.completion_verifier.invoke_adapter",
        side_effect=lambda _registry, *, runner, request: _verified_result(
            request.request_id, request.operation_id
        ),
    ):
        final, checks = run_completion_probes(bundle, updated, registry, paths)
    ran = {str(check["id"]) for check in checks if isinstance(check, dict)}
    _check(
        _REVISED_PROBE in ran,
        f"{_REVISED_PROBE} never RAN after its gating decision was revised -- "
        "the completion set stayed frozen at create",
    )
    _check(
        any(
            attempt.get("operation_id") == _REVISED_PROBE
            and attempt.get("phase") == "completion_probe"
            for attempt in final.operation_attempts
        ),
        f"{_REVISED_PROBE} produced no completion attempt record",
    )
    return final


def _assert_deselection_drops_a_bound_probe(bundle: ContractBundle, root: Path) -> None:
    """The prune direction: a probe deselected after create must not linger.

    Without it the transaction can never converge -- a pending completion
    probe for a source the user has since dropped blocks the roll-up forever.
    """

    target = root / "prune"
    target.mkdir(parents=True, exist_ok=True)
    created = _transaction(
        bundle,
        target,
        _answers(
            setup_profile="custom",
            autostart="enabled",
            session_sources=["codex_local"],
        ),
    )
    _check(
        _REVISED_PROBE in created.completion,
        f"{_REVISED_PROBE} was not bound while its source was selected",
    )
    revised = _answers(
        setup_profile="custom", autostart="enabled", session_sources=[]
    )
    updated = rebind_completion_probes(bundle, created.with_answers(revised), revised)
    _check(
        _REVISED_PROBE not in updated.completion,
        f"{_REVISED_PROBE} survived deselection and would block convergence",
    )


def _assert_deselected_ran_probe_is_pruned(bundle: ContractBundle, root: Path) -> None:
    """The residual closure: a probe that RAN is still dropped once deselected.

    This was the disclosed gap in the first cut of 1.41.  Pruning a probe that
    owned a completion attempt used to make the journal fail its own
    ``validate_transaction_state`` with "transaction attempt stage mismatch",
    so the prune had to retain it -- and a probe that ran, FAILED, and was then
    deselected went on blocking convergence forever on the strength of a result
    nobody had asked for.

    1.56 removed that coupling: a retained attempt for an unbound probe is
    history, not corruption.  The two directions below are what distinguish
    this closure's contribution from 1.56's own -- 1.56 makes the prune LEGAL,
    while dropping the retention guard is what makes it HAPPEN.
    """

    target = root / "ran"
    target.mkdir(parents=True, exist_ok=True)
    paths = ManagerPaths.resolve(explicit_home=root / "ranhome", home=root)
    for label, status in (
        ("verified", CheckpointStatus.VERIFIED),
        ("failed", CheckpointStatus.FAILED),
    ):
        created = _transaction(
            bundle,
            target,
            _answers(
                setup_profile="custom",
                autostart="enabled",
                session_sources=["codex_local"],
            ),
        )
        registry = AdapterRegistry(target=target, base_python=None)
        with patch(
            "solet_manager.completion_verifier.invoke_adapter",
            # Only the probe under test takes the status being exercised; every
            # other bound probe verifies, or a blanket failure would keep the
            # transaction non-convergent for reasons unrelated to the prune.
            # `status` is bound as a default so the closure captures this
            # iteration's value rather than the loop variable.
            side_effect=lambda _registry, *, runner, request, status=status: (
                _result_with(
                    request.request_id,
                    request.operation_id,
                    status
                    if request.operation_id == _REVISED_PROBE
                    else CheckpointStatus.VERIFIED,  # noqa
                )
            ),
        ):
            ran, _checks = run_completion_probes(bundle, created, registry, paths)
        _check(
            ran.completion.get(_REVISED_PROBE) is status,
            f"{_REVISED_PROBE} did not record its {label} result",
        )
        revised = _answers(
            setup_profile="custom", autostart="enabled", session_sources=[]
        )
        pruned = rebind_completion_probes(bundle, ran.with_answers(revised), revised)
        _check(
            _REVISED_PROBE not in pruned.completion,
            f"a deselected {label} probe was retained in the completion set",
        )
        _check(
            any(
                attempt.get("operation_id") == _REVISED_PROBE
                for attempt in pruned.operation_attempts
            ),
            f"pruning the {label} probe discarded its history; the prune must "
            "drop the requirement, never the attempt record",
        )
        reloaded = Transaction.from_dict(pruned.to_dict())
        _check(
            reloaded.completion == pruned.completion,
            f"the journal pruned of a {label} probe failed "
            "validate_transaction_state on reload",
        )
        if status is CheckpointStatus.FAILED:
            _check(
                pruned.status is not TransactionStatus.FAILED,
                "a deselected FAILED probe still blocks convergence -- the "
                "1.41 residual is not closed",
            )


def _assert_rebind_keeps_derived_state_valid(final: Transaction) -> None:
    """The shared repair constraint: a rebind must persist consistent state.

    ``Transaction.from_dict`` runs ``validate_transaction_state``, which is
    where ``_validate_derived_state`` re-derives stages and re-rolls status
    from the retained sets, so the round-trip is the constraint.
    """

    reloaded = Transaction.from_dict(final.to_dict())
    _check(
        reloaded.completion == final.completion
        and reloaded.status is final.status,
        "a rebound journal did not survive its own derived-state validation",
    )


def _assert_prefix_journal_still_loads(bundle: ContractBundle, root: Path) -> None:
    """A journal written before this fix must still load and validate."""

    target = root / "legacy"
    target.mkdir(parents=True, exist_ok=True)
    answers = _answers(setup_profile="free", autostart=None)
    legacy = _transaction(bundle, target, answers)
    reloaded = Transaction.from_dict(legacy.to_dict())
    _check(
        reloaded.completion == legacy.completion,
        "a pre-fix journal did not round-trip its retained completion set",
    )


def main() -> int:
    bundle = ContractBundle.load(source_revision="a" * 40, directory=_CONTRACTS)
    _assert_unresolved_is_retained(bundle)
    _assert_deselected_is_excluded(bundle)
    _assert_resolved_directions(bundle)
    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        final = _assert_probe_runs_after_revision(bundle, root)
        _assert_deselection_drops_a_bound_probe(bundle, root)
        _assert_deselected_ran_probe_is_pruned(bundle, root)
        _assert_rebind_keeps_derived_state_valid(final)
        _assert_prefix_journal_still_loads(bundle, root)
    print(f"completion_set_rebind_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
