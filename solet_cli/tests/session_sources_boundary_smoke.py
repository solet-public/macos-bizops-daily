"""Regression coverage for explicit session-source boundary selection."""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager import stage_boundaries  # noqa: E402
from solet_manager.adapter_protocol import OperationResult  # noqa: E402
from solet_manager.answer_validation import validate_decision_selection  # noqa: E402
from solet_manager.cli_commands import parse_decisions  # noqa: E402
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.decision_resolution import unresolved_required_decisions  # noqa: E402
from solet_manager.errors import ContractError, StateConflictError  # noqa: E402
from solet_manager.flow import (  # noqa: E402
    initial_probe_activations,
    initial_stage_probe_statuses,
)
from solet_manager.models import CheckpointStatus  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.transaction import (  # noqa: E402
    Transaction,
    canonical_sha256,
    load_transaction,
    write_transaction,
)

_CONTRACTS = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "github_midwife_plugin"
    / "knowledge_base"
)


def _decisions(*, session_sources: list[str] | None) -> dict[str, object]:
    decisions: dict[str, object] = {
        "setup_profile": "macos-bizops",
        "autostart": "enabled",
        "embeddings_implementation": "lm_studio",
        "embedding_model": "fixture-embedding",
        "inference_implementation": "none",
        "coding_agents": ["codex"],
        "execution_topology": "solo",
        "connector_configuration_timing": "first_use",
    }
    if session_sources is not None:
        decisions["session_sources"] = session_sources
    return decisions


def _assert_explicit_answer_required(bundle: ContractBundle) -> None:
    definition = bundle.decisions["session_sources"]
    if definition.get("required") is not True:
        raise AssertionError("red: permit session-source omission to force-fire roots")
    omitted = _decisions(session_sources=None)
    unresolved = unresolved_required_decisions(
        bundle, omitted, resolution_stage_ids={"decision_review"}
    )
    if "session_sources" not in unresolved:
        raise AssertionError("red: advance without an explicit session-source choice")
    if parse_decisions(["session_sources="]) != {"session_sources": []}:
        raise AssertionError("red: CLI cannot submit an explicit empty session-source answer")
    required_empty = parse_decisions(["coding_agents="])
    try:
        validate_decision_selection(
            "coding_agents",
            required_empty["coding_agents"],
            bundle.decisions["coding_agents"],
            bundle.decisions,
        )
    except ContractError as exc:
        if "fewer than 1 selections" not in str(exc):
            raise AssertionError("red: required selections reject with an unrelated error") from exc
    else:
        raise AssertionError("red: CLI empty answer bypasses a minimum-one decision")


def _assert_probe_boundaries(bundle: ContractBundle) -> None:
    declined = initial_stage_probe_statuses(
        bundle, {"decisions": _decisions(session_sources=[])}
    )["session_sources"]["entry"]
    if not all(status is CheckpointStatus.NOT_APPLICABLE for status in declined.values()):
        raise AssertionError("red: probe roots after an explicit empty selection")
    selected = initial_stage_probe_statuses(
        bundle, {"decisions": _decisions(session_sources=["codex_local"])}
    )["session_sources"]["entry"]
    if (
        selected["codex_session_roots_readable"] is not CheckpointStatus.PENDING
        or selected["claude_session_roots_readable"]
        is not CheckpointStatus.NOT_APPLICABLE
    ):
        raise AssertionError("red: probe unselected session-source roots")


def _assert_boundary_uses_journal_answers_fingerprint() -> None:
    journal_answers: dict[str, object] = {
        "decisions": {"session_sources": []},
        "resolution_evidence": [{"source": "journal"}],
    }
    transient_answers: dict[str, object] = {
        "decisions": {"session_sources": []},
        "resolution_evidence": [{"source": "preview"}],
    }
    journal_fingerprint = canonical_sha256(journal_answers)
    transient_fingerprint = canonical_sha256(transient_answers)
    if journal_fingerprint == transient_fingerprint:
        raise AssertionError("fixture must differ only in transient resolution evidence")
    transaction = SimpleNamespace(
        name="session-source-fixture",
        target="/tmp/session-source-fixture",
        answers_fingerprint=journal_fingerprint,
    )
    bundle = SimpleNamespace(
        flow_id="macos.repository_setup",
        source_revision="a" * 40,
        probes={
            "session_sources_retrievable": {
                "runner": "service_interface",
                "probe_ref": "selected_session_sources",
            }
        }
    )
    with (
        patch.object(stage_boundaries, "invoke_adapter", return_value=object()) as invoke,
        patch.object(
            stage_boundaries,
            "startup_readiness_budget",
            return_value=SimpleNamespace(consumer_probe_refs=frozenset()),
        ),
    ):
        stage_boundaries._invoke_boundary_probe(
            bundle=bundle,
            transaction=transaction,
            registry=object(),
            stage_id="session_sources",
            boundary="exit",
            probe_id="session_sources_retrievable",
            answers=transient_answers,
            attempt=1,
        )
    request = invoke.call_args.kwargs["request"]
    if request.answers_fingerprint != journal_fingerprint:
        raise AssertionError("red: boundary hashes transient resolution evidence")
    if request.answers_fingerprint == transient_fingerprint:
        raise AssertionError("red: boundary request carries the transient fingerprint")
    tampered_journal_answers = {
        "decisions": {"session_sources": ["codex_local"]},
        "resolution_evidence": [{"source": "journal"}],
    }
    if canonical_sha256(tampered_journal_answers) == journal_fingerprint:
        raise AssertionError("red: altered journal answers preserve their fingerprint")


def _assert_rebound_answers_persist_before_boundary_probe(bundle: ContractBundle) -> None:
    old_answers: dict[str, object] = {
        "decisions": _decisions(session_sources=[]),
        "consents": {"codex_session_files_permission": "denied"},
    }
    rebound_answers: dict[str, object] = {
        "decisions": _decisions(session_sources=[]),
        "consents": {"codex_session_files_permission": "granted"},
    }
    if canonical_sha256(old_answers) == canonical_sha256(rebound_answers):
        raise AssertionError("fixture must rebind a changed consent fingerprint")
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        journal_path = root / "rebound.json"
        old_transaction = Transaction.create(
            name="session-source-rebound",
            target=root / "target",
            input_fingerprint="sha256:" + "1" * 64,
            answers=old_answers,  # type: ignore[arg-type]
            seed=SeedLock(
                "https://example.invalid/seed.git",
                "release-1",
                "a" * 40,
                "b" * 40,
                "c" * 64,
                "macos-bizops",
            ),
            flow_id=bundle.flow_id,
            flow_source_revision=bundle.source_revision,
            flow_contract_digest=bundle.contract_digest,
            stage_ids=tuple(bundle.stages),
            stage_probe_statuses=initial_stage_probe_statuses(
                bundle, rebound_answers  # type: ignore[arg-type]
            ),
            probe_activations=initial_probe_activations(
                bundle, rebound_answers  # type: ignore[arg-type]
            ),
            completion_probe_ids=bundle.completion_probe_ids,
        )
        write_transaction(journal_path, old_transaction)
        rebound = old_transaction.with_answers(rebound_answers)  # type: ignore[arg-type]
        entry_activations = (
            rebound.probe_activations[
                stage_boundaries.activation_site_key("session_sources", "entry", probe_id)
            ]
            for probe_id in rebound.stage_probe_statuses["session_sources"]["entry"]
        )
        if not all(activation["state"] == "inactive" for activation in entry_activations):
            raise AssertionError("fixture must cross inactive session-source entry probes")

        def adapter_reads_journal(*_args: object, **kwargs: object) -> OperationResult:
            request = kwargs["request"]
            journal = load_transaction(journal_path)
            if journal is None:
                raise AssertionError("boundary invocation must retain a journal")
            if journal.answers_fingerprint != request.answers_fingerprint:  # type: ignore[union-attr]
                raise StateConflictError("answers_fingerprint_mismatch")
            return OperationResult(
                request_id=request.request_id,  # type: ignore[union-attr]
                operation_id=request.operation_id,  # type: ignore[union-attr]
                phase="probe",
                probe_purpose="stage_exit",
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

        with patch.object(stage_boundaries, "invoke_adapter", side_effect=adapter_reads_journal):
            entered, _observations, entry_failures = stage_boundaries.run_stage_boundaries(
                bundle=bundle,
                transaction=rebound,
                registry=object(),
                stage_ids=("session_sources",),
                boundary="entry",
                answers=rebound_answers,  # type: ignore[arg-type]
                persist_path=journal_path,
            )
            if entry_failures:
                raise AssertionError("inactive session-source entry must not fail")
            stage_boundaries.run_stage_boundaries(
                bundle=bundle,
                transaction=entered,
                registry=object(),
                stage_ids=("session_sources",),
                boundary="exit",
                answers=rebound_answers,  # type: ignore[arg-type]
                persist_path=journal_path,
            )

        persisted = load_transaction(journal_path)
        if persisted is None or persisted.answers_fingerprint != rebound.answers_fingerprint:
            raise AssertionError("rebound fingerprint must persist before boundary invocation")
        _assert_tampered_journal_rejected(
            bundle=bundle,
            transaction=rebound,
            answers=rebound_answers,
            journal_path=journal_path,
            old_transaction=old_transaction,
            adapter_reads_journal=adapter_reads_journal,
        )


def _assert_tampered_journal_rejected(
    *,
    bundle: ContractBundle,
    transaction: Transaction,
    answers: dict[str, object],
    journal_path: Path,
    old_transaction: Transaction,
    adapter_reads_journal: Callable[..., OperationResult],
) -> None:
    write_transaction(journal_path, old_transaction)
    try:
        with patch.object(
            stage_boundaries,
            "invoke_adapter",
            side_effect=adapter_reads_journal,
        ):
            stage_boundaries._invoke_boundary_probe(
                bundle=bundle,
                transaction=transaction,
                registry=object(),
                stage_id="session_sources",
                boundary="exit",
                probe_id="session_sources_retrievable",
                answers=answers,  # type: ignore[arg-type]
                attempt=2,
            )
    except StateConflictError as exc:
        if str(exc) != "answers_fingerprint_mismatch":
            raise AssertionError("tampered journal must retain its mismatch signal") from exc
    else:
        raise AssertionError("red: tampered journal bypasses fingerprint validation")


def main() -> int:
    bundle = ContractBundle.load(source_revision="a" * 40, directory=_CONTRACTS)
    _assert_explicit_answer_required(bundle)
    _assert_probe_boundaries(bundle)
    _assert_boundary_uses_journal_answers_fingerprint()
    _assert_rebound_answers_persist_before_boundary_probe(bundle)
    print("session_sources_boundary_smoke OK: 12 checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
