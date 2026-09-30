#!/usr/bin/env python3
"""A solet recorded at the r55-r63 setup contract reconciles to r64's (chg_04232fcc, iss_f510b5c9).

r64 stops recommending LM Studio for inference and rewords three LM Studio descriptions
in ``macos_setup_flow.json``.  That file is a digested contract file, so the setup
contract's release identity moves from ``19350a8d`` to ``ce20096b``.  Every stable seed
published from r55 through r63 records ``19350a8d`` with its own seed commit as the
flow source revision, and reconciliation matches that pair exactly.  This smoke proves:

* the active bundle is the pinned r64 identity, and the ``19350a8d`` fixture is the
  released bytes (it matches the published seeds' contract files);
* the two contracts differ only at the four r64 JSON paths, and they declare the same
  stages, stage-probe identities, operations, probes and completion probes, so the
  declared bridge needs no stage-probe mapping, reset or answer rewrite;
* each published stable seed revision at ``19350a8d`` resolves in one declared hop;
* a setup-incomplete transaction with verified progress reconciles with every
  stage-probe, operation, stage and completion status unchanged, including a solet
  that chose LM Studio for inference;
* the Manager's full reconcile-contract preview and apply succeed for the live
  stable revision (r63) and record the new digest without re-running a stage;
* an unknown revision at ``19350a8d`` is still refused (control);
* every historical source that reached ``19350a8d`` reaches ``ce20096b``.

Offline, reads only shipped files.  Run from the repository root with
``.venv/bin/python3 solet_cli/tests/contract_reconciliation_lm_studio_not_recommended_smoke.py``.
"""

from __future__ import annotations

# ruff: noqa: E402
import json
import sys
import tempfile
import uuid
from pathlib import Path
from typing import cast

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "solet_cli" / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "solet_cli" / "src"))

from solet_manager.adapter_protocol import OperationResult
from solet_manager.completion_verifier import resolved_completion_probe_ids
from solet_manager.contract_reconciliation import ContractReconciliationManager
from solet_manager.contract_reconciliation_chain import resolve_reconciliation_chain
from solet_manager.contracts import (
    ContractBundle,
    ContractReconciliation,
    contract_digested_filenames,
    load_contract_reconciliations,
)
from solet_manager.errors import StateConflictError
from solet_manager.flow import initial_stage_probe_statuses
from solet_manager.models import CheckpointStatus, InstanceRecord, JsonValue
from solet_manager.operation_records import attempt_record
from solet_manager.paths import ManagerPaths
from solet_manager.registry import InstanceRegistry
from solet_manager.release_lock import SeedLock
from solet_manager.stage_activation import initial_probe_activations, reconcile_contract_stage_probe_state
from solet_manager.transaction import (
    Transaction,
    load_transaction,
    target_install_state_projection,
    write_transaction,
)

_CONTRACTS = _ROOT / "plugins/github_midwife_plugin/knowledge_base"
_SOURCE_DIGEST = "sha256:19350a8d3b0b29139afe25b6f11648c0f3f2e1fdb8ca91f81d5bfa1280fc7492"
_DESTINATION_DIGEST = "sha256:ce20096b3746db9363af96eb0416dd65d2bee96dac615e06c1d6f4d5e5bfd148"
_SOURCE_FIXTURE = (
    _ROOT
    / "solet_cli/tests/fixtures/reconciliation_operation_status_reset"
    / _SOURCE_DIGEST.removeprefix("sha256:")
)
# Every solet-public/macos-bizops commit whose five digested contract files hash to
# 19350a8d: r55 and r56 (RELEASE_NOTES r57), the stable cuts after them through r61
# (1e167174), and r63 (a5732c9f).
_STABLE_SEED_REVISIONS = (
    "1dedebb6622ee6559d549eac4a2465591c130d80",
    "d81e014adce8ed4b4258db35a4f46339431ea158",
    "8b3fe23d447e384541bd5a18a7d7090fa41ae5a6",
    "85f2994b3a76aa86e33b6cd49724731280161f42",
    "6b292e918e83520afbb4eecd79da67766a35d538",
    "ee00d7025187733edcc79466f71c708d427c086f",
    "1e167174a086615d15abf7108ec9e4bd6111af94",
    "a5732c9f3d58c756c085bc76a81841c19626d156",
)
_LIVE_STABLE_REVISION = "a5732c9f3d58c756c085bc76a81841c19626d156"
# The source identities that resolved to 19350a8d before r64.
_EARLIER_SOURCES = (
    ("10d83525cef65b06996029110be5268ff3f39995", "sha256:5652d22cdc1c0a7e99820e0cee5d39751c65c066302ef6aba59e9863a17fa27c"),
    ("10d83525cef65b06996029110be5268ff3f39995", "sha256:c2a0386e86e0378e694f1793732fac223ec70ff3358e856b54ccf588e51024a4"),
    ("92634d2b505becf51d34313ca8d27ee65151de23", "sha256:ce6d9d88fd77b2feeab964e2cd2f75e4d0ca6149634b0bcfac520cd047d720bb"),
    ("2b9eb9573ce136362ad566ea4ba38e5ebc55205e", "sha256:ce6d9d88fd77b2feeab964e2cd2f75e4d0ca6149634b0bcfac520cd047d720bb"),
    ("73af1c9de0b3132ce9f59abce20e2de19ced18c9", "sha256:515ab65fcf6d82756b3ffc6bf42f782ddc33b69907b5d32a372b019bea6a8e72"),
)
_R64_FLOW_PATHS = frozenset(
    {
        "decisions.inference_implementation.recommended_option_refs",
        "decisions.embeddings_implementation.option_source.options.lm_studio.description",
        "decisions.inference_implementation.option_source.options.lm_studio.description",
        "plugins.default_inference_plugin.description",
    }
)
_CHECKS: list[str] = []


def _check(label: str, condition: bool, detail: object = "") -> None:
    if not condition:
        raise AssertionError(f"FAIL: {label}: {detail}")
    _CHECKS.append(label)


def _seed(commit: str) -> SeedLock:
    return SeedLock(
        "https://github.com/solet-public/macos-bizops.git",
        None,
        commit,
        "b" * 40,
        None,
        "macos-bizops",
    )


def _answers(target: Path, revision: str) -> dict[str, JsonValue]:
    return {
        "schema_version": 1,
        "flow_id": "macos.repository_setup",
        "flow_source_revision": revision,
        "name": "fixture",
        "target": str(target),
        "public_inputs": {
            "clone_directory": str(target),
            "repository_ref": revision,
            "setup_journal_path": str(target / ".solet" / "transaction.json"),
            "solet_name": "fixture",
        },
        "decisions": {
            "setup_profile": "macos-bizops",
            "autostart": "enabled",
            "embeddings_implementation": "lm_studio",
            "embedding_model": "fixture-embedding",
            "inference_implementation": "lm_studio",
            "inference_model": "fixture-inference",
            "coding_agents": ["codex", "claude_code"],
            "execution_topology": "fleet",
            "git_mutation_control": "designated_controller",
            "connector_configuration_timing": "first_use",
            "session_sources": ["codex_local", "claude_code_local"],
        },
        "consents": {
            "background_service_consent": True,
            "shell_modification_consent": True,
            "system_change_consent": True,
            "codex_session_ingestion_consent": True,
            "claude_session_ingestion_consent": True,
        },
        "resolution_evidence": [],
    }


def _flow_diff(before: JsonValue, after: JsonValue, path: str = "") -> set[str]:
    if isinstance(before, dict) and isinstance(after, dict):
        changed: set[str] = set()
        for key in set(before) | set(after):
            child = f"{path}.{key}" if path else key
            if key not in before or key not in after:
                changed.add(child)
            else:
                changed |= _flow_diff(before[key], after[key], child)
        return changed
    return set() if before == after else {path}


def _stage_probe_identities(bundle: ContractBundle, answers: dict[str, JsonValue]) -> set[tuple[str, str, str]]:
    return {
        (stage_id, boundary, probe_id)
        for stage_id, boundaries in initial_stage_probe_statuses(bundle, answers).items()
        for boundary, probes in boundaries.items()
        for probe_id in probes
    }


def _check_contract_delta(source: ContractBundle, destination: ContractBundle) -> None:
    for name in contract_digested_filenames():
        if name == "macos_setup_flow.json":
            continue
        _check(
            f"{name} is byte-identical across the two contracts",
            (source.directory / name).read_bytes() == (destination.directory / name).read_bytes(),
        )
    changed = _flow_diff(source.flow, destination.flow)
    _check("the setup flow changes only at the four r64 paths", changed == _R64_FLOW_PATHS, sorted(changed))
    _check("both contracts declare the same stages in order", list(source.stages) == list(destination.stages))
    _check("both contracts declare the same operations", set(source.operations) == set(destination.operations))
    _check("both contracts declare the same probes", set(source.probes) == set(destination.probes))
    _check(
        "both contracts declare the same completion probes",
        source.completion_probe_ids == destination.completion_probe_ids,
    )
    answers = _answers(Path("/tmp/fixture"), _LIVE_STABLE_REVISION)
    _check(
        "every stage-probe identity exists in both contracts, so the bridge mapping is identity",
        _stage_probe_identities(source, answers) == _stage_probe_identities(destination, answers),
    )
    inference = cast(dict[str, JsonValue], destination.decisions["inference_implementation"])
    options = cast(dict[str, dict[str, JsonValue]], cast(dict[str, JsonValue], inference["option_source"])["options"])
    _check("lm_studio is no longer a recommended inference option", "lm_studio" not in cast(list[str], inference["recommended_option_refs"]))
    _check("lm_studio stays a supported inference option", options["lm_studio"]["availability"] == "supported")


def _transaction(source: ContractBundle, target: Path, revision: str) -> Transaction:
    """A journal built the way ``create_execution`` builds one, then progressed mid-setup."""

    answers = _answers(target, revision)
    transaction = Transaction.create(
        name="fixture",
        target=target,
        input_fingerprint="sha256:" + "3" * 64,
        answers=answers,
        seed=_seed(revision),
        flow_id=source.flow_id,
        flow_source_revision=revision,
        flow_contract_digest=source.contract_digest,
        stage_ids=tuple(source.stages),
        completion_probe_ids=resolved_completion_probe_ids(source, answers),
        stage_probe_statuses=initial_stage_probe_statuses(source, answers),
        probe_activations=initial_probe_activations(source, answers),
    ).approve("sha256:" + "4" * 64)
    return _with_progress(source, transaction)


def _with_progress(source: ContractBundle, transaction: Transaction) -> Transaction:
    """Record the verified progress a mid-setup solet carries: a stage probe and an operation."""

    stage_id, boundaries = next(
        (stage_id, boundaries)
        for stage_id, boundaries in transaction.stage_probe_statuses.items()
        if any(boundaries.values())
    )
    boundary, probes = next((boundary, probes) for boundary, probes in boundaries.items() if probes)
    probe_id = next(iter(probes))
    progressed = transaction.with_stage_probe_status(
        stage_id,
        boundary,
        probe_id,
        CheckpointStatus.VERIFIED,
        attempt={
            "probe_id": probe_id,
            "stage_id": stage_id,
            "boundary": boundary,
            "attempt": 1,
            "request_id": "5d4b0a8e-6f0e-4d3b-9a57-3c1f2e8d9b10",
            "checkpoint_status": "verified",
            "error_kind": None,
            "retry_safe": True,
            "evidence": [],
            "repair": None,
            "recorded_at": "2026-09-29T00:00:00Z",
        },
    )
    operation_id = "install_postgresql"
    operation_stage = next(
        stage for stage, definition in source.stages.items()
        if operation_id in cast(list[str], definition.get("operation_refs", []))
    )
    result = OperationResult(
        request_id=str(uuid.uuid4()),
        operation_id=operation_id,
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
    return progressed.bind_operations({operation_id: operation_stage}).with_operation_status(
        operation_id,
        CheckpointStatus.VERIFIED,
        attempt=attempt_record(result, stage_id=operation_stage, phase="apply", attempt=1, owner_operation_id=operation_id),
    )


def _resolve(revision: str, digest: str, declarations: tuple[ContractReconciliation, ...]) -> ContractReconciliation:
    transaction = Transaction.create(
        name="probe",
        target=Path("/tmp/probe"),
        input_fingerprint="sha256:" + "a" * 64,
        answers={},
        seed=_seed(revision),
        flow_id="macos.repository_setup",
        flow_source_revision=revision,
        flow_contract_digest=digest,
        stage_ids=(),
        completion_probe_ids=(),
    )
    return resolve_reconciliation_chain(transaction, _DESTINATION_DIGEST, declarations)


def _check_resolution(declarations: tuple[ContractReconciliation, ...]) -> None:
    for revision in _STABLE_SEED_REVISIONS:
        bridge = _resolve(revision, _SOURCE_DIGEST, declarations)
        _check(
            f"stable seed {revision[:8]} at 19350a8d resolves in one declared hop",
            not bridge.migration_id.startswith("chain:") and bridge.source_revision == revision,
            bridge.migration_id,
        )
        _check(
            f"the {revision[:8]} bridge maps, resets and rewrites nothing",
            bridge.stage_probe_mappings == ()
            and bridge.operation_statuses_to_reset == ()
            and bridge.answer_value_migrations == ()
            and bridge.first_use_inactive_probe_migrations == (),
        )
    try:
        _resolve("f" * 40, _SOURCE_DIGEST, declarations)
    except StateConflictError:
        _check("control: an unknown revision at 19350a8d is refused", True)
    else:
        _check("control: an unknown revision at 19350a8d is refused", False)
    for revision, digest in _EARLIER_SOURCES:
        bridge = _resolve(revision, digest, declarations)
        _check(
            f"earlier source {revision[:8]}/{digest[7:15]} still reaches the active contract",
            bridge.destination_digest == _DESTINATION_DIGEST,
            bridge.migration_id,
        )


def _activation_states(transaction: Transaction) -> dict[str, str]:
    """Activation state per probe; reconciliation re-derives the reason text for every bridge."""

    return {probe: value["state"] for probe, value in transaction.probe_activations.items()}


def _check_state_preserved(source: ContractBundle, destination: ContractBundle, declarations: tuple[ContractReconciliation, ...]) -> None:
    for revision in _STABLE_SEED_REVISIONS:
        before = _transaction(source, Path("/tmp/fixture"), revision)
        after = reconcile_contract_stage_probe_state(
            destination, before, _resolve(revision, _SOURCE_DIGEST, declarations)
        )
        _check(
            f"{revision[:8]}: every status survives reconciliation unchanged",
            after.flow_contract_digest == _DESTINATION_DIGEST
            and after.stage_probe_statuses == before.stage_probe_statuses
            and after.stage_probe_attempts == before.stage_probe_attempts
            and after.operation_statuses == before.operation_statuses
            and after.operation_attempts == before.operation_attempts
            and after.stages == before.stages
            and after.completion == before.completion
            and _activation_states(after) == _activation_states(before)
            and after.answers == before.answers,
        )


def _manager(root: Path, source: ContractBundle) -> tuple[ContractReconciliationManager, ManagerPaths, Transaction]:
    target = root / "target"
    transaction = _transaction(source, target, _LIVE_STABLE_REVISION)
    projection = target / ".solet" / "install-state.json"
    projection.parent.mkdir(parents=True, mode=0o700)
    projection.write_text(json.dumps(target_install_state_projection(transaction)) + "\n", encoding="utf-8")
    projection.chmod(0o600)
    contracts = target / "plugins/github_midwife_plugin/knowledge_base"
    contracts.mkdir(parents=True)
    for path in _SOURCE_FIXTURE.iterdir():
        (contracts / path.name).write_bytes(path.read_bytes())
    paths = ManagerPaths.resolve(explicit_home=root / "manager")
    write_transaction(paths.transaction_path("fixture"), transaction)
    InstanceRegistry(paths.registry_path).add(
        InstanceRecord(
            name="fixture",
            target=str(target),
            launcher=str(target / "run.py"),
            seed_repository=transaction.seed.repository,
            seed_tag=transaction.seed.release_tag,
            seed_commit=transaction.seed.commit,
            seed_tree_hash=transaction.seed.tree_hash,
            profile=transaction.seed.profile,
            flow_id=transaction.flow_id,
            flow_source_revision=transaction.flow_source_revision,
            flow_contract_digest=transaction.flow_contract_digest,
            created_at="2026-09-29T00:00:00Z",
            updated_at="2026-09-29T00:00:00Z",
            lifecycle_state="setup_incomplete",
            input_fingerprint=transaction.input_fingerprint,
            expected_router_name="fixture-router",
            expected_router_socket="/tmp/fixture.sock",
            expected_router_port_range="30000-30010",
        )
    )
    return ContractReconciliationManager(paths=paths, contract_directory=_CONTRACTS), paths, transaction


def _check_manager_ceremony(source: ContractBundle) -> None:
    with tempfile.TemporaryDirectory(prefix="r64-reconcile-") as raw:
        manager, paths, before = _manager(Path(raw), source)
        preview = manager.run("fixture", dry_run=True, approved_fingerprint=None)
        _check("the live stable solet previews reconciliation instead of being refused", preview.status == "preview_ready", preview.message)
        applied = manager.run("fixture", dry_run=False, approved_fingerprint=str(preview.data["approval_fingerprint"]))
        _check("the reviewed reconciliation applies", applied.status == "reconciled", applied.message)
        after = load_transaction(paths.transaction_path("fixture"))
        _check("the journal remains readable", after is not None)
        assert after is not None
        _check("the journal records the r64 setup contract", after.flow_contract_digest == _DESTINATION_DIGEST)
        _check("the journal keeps its seed revision", after.flow_source_revision == _LIVE_STABLE_REVISION)
        _check(
            "no stage, probe or operation is reset, so setup resumes where it stopped",
            after.stage_probe_statuses == before.stage_probe_statuses
            and after.operation_statuses == before.operation_statuses
            and after.stages == before.stages
            and after.completion == before.completion,
        )
        record = InstanceRegistry(paths.registry_path).require("fixture")
        _check("the registry records the r64 setup contract", record.flow_contract_digest == _DESTINATION_DIGEST)


def main() -> int:
    source = ContractBundle.load(source_revision=_LIVE_STABLE_REVISION, directory=_SOURCE_FIXTURE, expected_digest=_SOURCE_DIGEST)
    destination = ContractBundle.load(source_revision=_LIVE_STABLE_REVISION, directory=_CONTRACTS)
    _check(
        "the active setup contract is the pinned r64 identity",
        destination.contract_digest == _DESTINATION_DIGEST,
        destination.contract_digest,
    )
    _check_contract_delta(source, destination)
    declarations = load_contract_reconciliations()
    _check_resolution(declarations)
    _check_state_preserved(source, destination, declarations)
    _check_manager_ceremony(source)
    print(f"contract_reconciliation_lm_studio_not_recommended_smoke OK: {len(_CHECKS)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
