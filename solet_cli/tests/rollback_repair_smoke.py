#!/usr/bin/env python3
"""Disposable positive/refusal controls for the rollback repair executor."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
_PYTHON = Path(sys.executable)
_PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_PACKAGE_ROOT))

from solet_manager import rollback_repair as rollback_module  # noqa: E402
from solet_manager.models import ExitCode, JsonValue  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.rollback_repair import (  # noqa: E402
    RepairPlan,
    RollbackRepairExecutor,
)
from solet_manager.rollback_repair_contract import artifact_identity  # noqa: E402
from solet_manager.state_io import instance_lock  # noqa: E402

_CHECKS = 0


def _check(condition: object, label: str, *, observed: object = None) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"{label}; observed={observed!r}")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _write_json(path: Path, value: object, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(mode)


@contextmanager
def _identity_environment(
    *,
    name: str | None,
    app_home: Path | None,
) -> Iterator[None]:
    previous_name = os.environ.get("SOLET_NAME")
    previous_app_home = os.environ.get("APP_HOME")
    if name is None:
        os.environ.pop("SOLET_NAME", None)
    else:
        os.environ["SOLET_NAME"] = name
    if app_home is None:
        os.environ.pop("APP_HOME", None)
    else:
        os.environ["APP_HOME"] = str(app_home)
    try:
        yield
    finally:
        if previous_name is None:
            os.environ.pop("SOLET_NAME", None)
        else:
            os.environ["SOLET_NAME"] = previous_name
        if previous_app_home is None:
            os.environ.pop("APP_HOME", None)
        else:
            os.environ["APP_HOME"] = previous_app_home


def _write_running_identity(root: Path, name: str) -> Path:
    app_home = root / "profile"
    app_home.mkdir(parents=True, exist_ok=True)
    (root / "root_manifest.yaml").write_text(
        f"schema_version: 1\nsolet_name: {name}\n",
        encoding="utf-8",
    )
    return app_home


def _failure_record(
    root: Path,
    *,
    name: str,
    target: Path,
    manager_home: Path | None,
    disposition: str = "disposable_fixture",
    prepare_target: bool = True,
) -> tuple[Path, Path, Path, bytes, bytes]:
    genesis = target / ".solet/genesis.json"
    artifact = target / "profile/config/service_bindings.json"
    fault_bytes = b'{"embedding":'
    if prepare_target:
        target.mkdir(parents=True, exist_ok=True)
        (target / "root_manifest.yaml").write_text(
            f"schema_version: 1\nsolet_name: {name}\n",
            encoding="utf-8",
        )
        _write_json(
            genesis,
            {
                "schema_version": 1,
                "solet_name": name,
                "completed_at": "2026-08-25T05:21:24+00:00",
            },
            mode=0o644,
        )
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(fault_bytes)
        artifact.chmod(0o644)
    genesis_digest = _sha256(genesis) if prepare_target else "0" * 64
    # Stat the artifact actually on disk rather than assuming the calling
    # process's own uid/gid — a new file's group is inherited from its
    # PARENT DIRECTORY on macOS/APFS, not from the creating process, so a
    # fixture rooted somewhere whose group differs from os.getgid() (e.g.
    # /tmp itself, group wheel) would otherwise record a gid that never
    # matches what verify_metadata later observes.
    artifact_now = artifact_identity(artifact)
    failure_id = "failure-rollback-service-bindings-v1"
    operation_id = f"operation-{name}-failed"
    manager_transaction = (
        ManagerPaths.resolve(explicit_home=manager_home).transaction_path(name)
        if manager_home is not None
        else root / "missing-manager-transaction.json"
    )
    if manager_home is not None:
        _write_json(
            manager_transaction,
            {
                "schema_version": 1,
                "operation_id": operation_id,
                "name": name,
                "target": str(target),
                "status": "failed",
                "repair_failure": {
                    "failure_id": failure_id,
                    "reason_code": "service_bindings_json_corrupt",
                    "recipe_id": "restore_generated_service_bindings_json_v1",
                    "artifact_relative_path": "profile/config/service_bindings.json",
                },
            },
        )
    manager_transaction_digest = (
        _sha256(manager_transaction) if manager_transaction.is_file() else "0" * 64
    )
    before_bytes = b'{"embedding":"openai_embeddings_plugin"}\n'
    before_image = root / "trusted/fault_original.bin"
    before_image.parent.mkdir(parents=True)
    before_image.write_bytes(before_bytes)
    before_image.chmod(0o600)
    record = root / f"{name}.failure.json"
    _write_json(
        record,
        {
            "schema_version": 1,
            "failure_id": failure_id,
            "recipe_id": "restore_generated_service_bindings_json_v1",
            "reason_code": "service_bindings_json_corrupt",
            "instance_name": name,
            "target": str(target),
            "transaction_state": "failed",
            "evidence_disposition": disposition,
            "genesis_transaction": {
                "relative_path": ".solet/genesis.json",
                "sha256": genesis_digest,
            },
            "manager_transaction": {
                "path": str(manager_transaction),
                "sha256": manager_transaction_digest,
                "operation_id": operation_id,
            },
            "artifact": {
                "relative_path": "profile/config/service_bindings.json",
                "artifact_class": "generated_service_bindings",
                "fault_sha256": _sha256_bytes(fault_bytes),
                "expected_sha256": _sha256_bytes(before_bytes),
                "mode": artifact_now.mode,
                "uid": artifact_now.uid,
                "gid": artifact_now.gid,
            },
            "before_image": {
                "path": str(before_image),
                "sha256": _sha256_bytes(before_bytes),
                "source_kind": "same_transaction_before_image",
                "transaction_sha256": manager_transaction_digest,
            },
            "diagnostic": {
                "check_id": "repair::service_bindings_json_v1",
                "healthy_control_status": "verified",
                "failed_subject_status": "failed",
                "healthy_control_sha256": _sha256_bytes(before_bytes),
                "failed_subject_sha256": _sha256_bytes(fault_bytes),
            },
            "postcondition": {
                "kind": "service_bindings_json_v1",
                "expected_sha256": _sha256_bytes(before_bytes),
            },
        },
    )
    return record, artifact, before_image, fault_bytes, before_bytes


def _invoke(manager_home: Path, record: Path) -> tuple[int, dict[str, Any] | None, str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(_PACKAGE_ROOT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            str(_PYTHON),
            "-m",
            "solet_manager",
            "--json",
            "--home",
            str(manager_home),
            "repair",
            "--failure-record",
            str(record),
            "--failure-record-sha256",
            f"sha256:{_sha256(record)}",
        ],
        cwd=_REPO,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=20,
    )
    try:
        payload = json.loads(completed.stdout) if completed.stdout else None
    except json.JSONDecodeError:
        payload = None
    return completed.returncode, payload, completed.stderr


def _tree_snapshot(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): ("directory" if path.is_dir() else _sha256(path))
        for path in sorted(root.rglob("*"))
        if not path.is_symlink()
    }


def _journal_trace(path: Path) -> list[tuple[object, object]]:
    return [
        (event.get("phase"), event.get("status"))
        for event in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    ]


def _direct_run(
    manager_home: Path,
    record: Path,
    *,
    mutator: Any = None,
    act_time_hook: Any = None,
) -> Any:
    return RollbackRepairExecutor(
        ManagerPaths.resolve(explicit_home=manager_home),
        mutator=mutator,
        act_time_hook=act_time_hook,
    ).run(record, f"sha256:{_sha256(record)}")


def _exercise_adversarial_controls(root: Path) -> None:
    _control_shared_instance_lock_namespace(root)
    _control_manager_transaction_preconditions(root)
    _control_success_without_mutation(root)
    _control_collateral_mutation(root)
    _control_act_time_refusal(root)
    _control_interrupted_reentry(root)
    _control_running_identity_floor(root)


def _control_shared_instance_lock_namespace(root: Path) -> None:
    name = "shared-lock-fixture"
    manager_home = root / "shared-lock-manager"
    record, _, _, _, _ = _failure_record(
        root / "shared-lock-record",
        name=name,
        target=root / "shared-lock-target",
        manager_home=manager_home,
    )
    paths = ManagerPaths.resolve(explicit_home=manager_home)
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(_PACKAGE_ROOT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [
        str(_PYTHON),
        "-m",
        "solet_manager",
        "--json",
        "--home",
        str(manager_home),
        "repair",
        "--failure-record",
        str(record),
        "--failure-record-sha256",
        f"sha256:{_sha256(record)}",
    ]
    with instance_lock(paths.lock_path(name), create=True):
        repair = subprocess.Popen(
            command,
            cwd=_REPO,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        try:
            repair.wait(timeout=0.3)
        except subprocess.TimeoutExpired:
            contended = True
        else:
            contended = False
    stdout, stderr = repair.communicate(timeout=20)
    _check(
        contended and repair.returncode == 0 and json.loads(stdout)["status"] == "repaired",
        "repair contends on the same per-instance lock namespace as doctor/create/start",
        observed={"contended": contended, "returncode": repair.returncode, "stderr": stderr},
    )


def _control_manager_transaction_preconditions(root: Path) -> None:
    missing_home = root / "missing-manager"
    missing_record, missing_artifact, _, missing_fault, _ = _failure_record(
        root / "missing-record",
        name="missing-manager-fixture",
        target=root / "missing-manager-target",
        manager_home=missing_home,
    )
    missing_transaction = ManagerPaths.resolve(
        explicit_home=missing_home
    ).transaction_path("missing-manager-fixture")
    missing_transaction.unlink()
    missing = _direct_run(missing_home, missing_record)
    _check(
        missing.status == "refused"
        and missing.exit_code == ExitCode.HUMAN_ACTION
        and missing.data["precondition"]["guard"] == "manager_transaction"
        and missing.data["mutation_count"] == 0
        and missing_artifact.read_bytes() == missing_fault,
        "missing canonical manager transaction refuses with zero mutation",
        observed=missing.to_dict(),
    )

    mismatch_home = root / "mismatch-manager"
    mismatch_record, mismatch_artifact, _, mismatch_fault, _ = _failure_record(
        root / "mismatch-record",
        name="mismatch-manager-fixture",
        target=root / "mismatch-manager-target",
        manager_home=mismatch_home,
    )
    mismatch_transaction = ManagerPaths.resolve(
        explicit_home=mismatch_home
    ).transaction_path("mismatch-manager-fixture")
    transaction_payload = json.loads(mismatch_transaction.read_text(encoding="utf-8"))
    transaction_payload["repair_failure"]["recipe_id"] = "different_recipe_v1"
    _write_json(mismatch_transaction, transaction_payload)
    failure_payload = json.loads(mismatch_record.read_text(encoding="utf-8"))
    failure_payload["manager_transaction"]["sha256"] = _sha256(mismatch_transaction)
    failure_payload["before_image"]["transaction_sha256"] = _sha256(
        mismatch_transaction
    )
    _write_json(mismatch_record, failure_payload)
    mismatch = _direct_run(mismatch_home, mismatch_record)
    _check(
        mismatch.status == "refused"
        and mismatch.exit_code == ExitCode.HUMAN_ACTION
        and mismatch.data["precondition"]["guard"]
        == "manager_transaction_identity"
        and mismatch.data["mutation_count"] == 0
        and mismatch_artifact.read_bytes() == mismatch_fault,
        "canonical manager transaction must bind the exact failure and recipe",
        observed=mismatch.to_dict(),
    )


def _control_success_without_mutation(root: Path) -> None:
    no_op_record, no_op_artifact, _, fault_bytes, _ = _failure_record(
        root / "no-op",
        name="no-op-fixture",
        target=root / "no-op-target",
        manager_home=root / "no-op-manager",
    )

    def apparent_success(plan: RepairPlan, stage: Path, recovery: Path) -> dict[str, JsonValue]:
        del plan, stage, recovery
        return {
            "inverse_operation": "sentinel_without_mutation",
            "mutation_count": 0,
        }

    no_op_result = _direct_run(
        root / "no-op-manager",
        no_op_record,
        mutator=apparent_success,
    )
    _check(
        no_op_result.exit_code == ExitCode.FAILED
        and no_op_result.status == "postcondition_failed"
        and no_op_result.data["repair_worked"] is False,
        "action-returned-success without mutation fails independent postconditions",
        observed=no_op_result.to_dict(),
    )
    _check(
        no_op_artifact.read_bytes() == fault_bytes,
        "success sentinel did not become repair evidence",
    )


def _control_collateral_mutation(root: Path) -> None:
    collateral_record, _, _, _, _ = _failure_record(
        root / "collateral",
        name="collateral-fixture",
        target=root / "collateral-target",
        manager_home=root / "collateral-manager",
    )

    def mutate_with_collateral(
        plan: RepairPlan, stage: Path, recovery: Path
    ) -> dict[str, JsonValue]:
        receipt = rollback_module._atomic_restore(plan, stage, recovery)
        (plan.target / "unexpected-collateral.txt").write_text("changed\n", encoding="utf-8")
        return receipt

    collateral_result = _direct_run(
        root / "collateral-manager",
        collateral_record,
        mutator=mutate_with_collateral,
    )
    _check(
        collateral_result.exit_code == ExitCode.FAILED
        and collateral_result.status == "postcondition_failed",
        "intended restore plus collateral mutation fails the full census",
        observed=collateral_result.to_dict(),
    )


def _control_act_time_refusal(root: Path) -> None:
    act_record, act_artifact, _, act_fault, _ = _failure_record(
        root / "act-time",
        name="act-time-fixture",
        target=root / "act-time-target",
        manager_home=root / "act-time-manager",
    )
    mutator_calls: list[str] = []

    def recorded_mutator(plan: RepairPlan, stage: Path, recovery: Path) -> dict[str, JsonValue]:
        mutator_calls.append("called")
        return rollback_module._atomic_restore(plan, stage, recovery)

    def protect_at_action(plan: RepairPlan) -> None:
        os.environ["SOLET_NAME"] = plan.instance_name
        os.environ["APP_HOME"] = str(plan.target / "profile")

    previous_name = os.environ["SOLET_NAME"]
    previous_app_home = os.environ["APP_HOME"]
    try:
        act_result = _direct_run(
            root / "act-time-manager",
            act_record,
            mutator=recorded_mutator,
            act_time_hook=protect_at_action,
        )
    finally:
        os.environ["SOLET_NAME"] = previous_name
        os.environ["APP_HOME"] = previous_app_home
    _check(
        act_result.exit_code == ExitCode.HUMAN_ACTION
        and act_result.status == "refused"
        and act_result.error_kind == "repair_refused_protected_subject",
        "identity becoming protected under the lock is refused",
        observed=act_result.to_dict(),
    )
    _check(
        mutator_calls == [] and act_artifact.read_bytes() == act_fault,
        "act-time refusal has an empty target mutator trace",
        observed=mutator_calls,
    )


def _control_interrupted_reentry(root: Path) -> None:
    recovery_record, recovery_artifact, _, _, recovery_bytes = _failure_record(
        root / "reentry",
        name="reentry-fixture",
        target=root / "reentry-target",
        manager_home=root / "reentry-manager",
    )

    def crash_after_inverse(plan: RepairPlan, stage: Path, recovery: Path) -> dict[str, JsonValue]:
        rollback_module._atomic_restore(plan, stage, recovery)
        raise OSError("injected interruption after inverse")

    interrupted = _direct_run(
        root / "reentry-manager",
        recovery_record,
        mutator=crash_after_inverse,
    )
    interrupted_journal = (
        root
        / "reentry-manager/state/repairs/failure-rollback-service-bindings-v1.jsonl"
    )
    first_trace = _journal_trace(interrupted_journal)
    recovered = _direct_run(root / "reentry-manager", recovery_record)
    recovered_trace = _journal_trace(interrupted_journal)
    _check(
        interrupted.status == "action_failed"
        and interrupted.exit_code == ExitCode.FAILED
        and interrupted.data["mutation_count"] == 1
        and interrupted.data["action"]["receipt"] is not None
        and first_trace
        == [
            ("precondition", "preconditions_verified"),
            ("precondition", "recovery_generation_durable"),
            ("action", "mutation_started"),
            ("action", "action_receipt_recorded"),
            ("action", "action_failed"),
        ],
        "an interrupted post-inverse action reports the mutation and journals its receipt",
        observed=interrupted.to_dict(),
    )
    _check(
        recovered.exit_code == ExitCode.OK
        and recovered.status == "already_repaired"
        and recovered.data["mutation_count"] == 0
        and recovered.data["action"]["receipt"] is not None
        and recovery_artifact.read_bytes() == recovery_bytes,
        "re-entry independently verifies a no-op with the authenticated action receipt",
        observed=recovered.to_dict(),
    )
    _check(
        recovered_trace
        == [
            *first_trace,
            ("postcondition", "postconditions_started"),
            ("postcondition", "repair_worked"),
        ],
        "postconditions never start before the interrupted action receipt is durable",
        observed=recovered_trace,
    )


def _identity_case_app_home(
    root: Path,
    target: Path,
    label: str,
    subject_name: str,
    environment_name: str | None,
    home_kind: str,
) -> Path | None:
    if home_kind == "subject":
        if environment_name != subject_name:
            _write_running_identity(target, str(environment_name))
        return target / "profile"
    if home_kind == "separate":
        return _write_running_identity(root / f"{label}-running", str(environment_name))
    if home_kind == "baseline":
        return Path(os.environ["APP_HOME"])
    if home_kind == "missing":
        return None
    raise AssertionError(home_kind)


def _run_identity_case(
    root: Path,
    case: tuple[str, str, str | None, str, str],
) -> dict[str, object]:
    label, subject_name, environment_name, home_kind, expected_guard = case
    target = root / f"{label}-target"
    manager = root / f"{label}-manager"
    record, artifact, _, fault, _ = _failure_record(
        root / f"{label}-record",
        name=subject_name,
        target=target,
        manager_home=manager,
    )
    app_home = _identity_case_app_home(
        root,
        target,
        label,
        subject_name,
        environment_name,
        home_kind,
    )
    before = _tree_snapshot(manager)
    with _identity_environment(name=environment_name, app_home=app_home):
        result = _direct_run(manager, record)
    return {
        "label": label,
        "status": result.status,
        "exit_code": int(result.exit_code),
        "error_kind": result.error_kind,
        "guard": result.data["precondition"]["guard"],
        "mutation_count": result.data["mutation_count"],
        "artifact_unchanged": artifact.read_bytes() == fault,
        "manager_unchanged": _tree_snapshot(manager) == before,
        "expected_guard": expected_guard,
    }


def _control_running_identity_floor(root: Path) -> None:
    baseline_name = os.environ["SOLET_NAME"]
    cases = (
        ("exact", "exact-running", "exact-running", "subject", "protected_running_identity"),
        ("name", "shared-running", "shared-running", "separate", "protected_running_identity"),
        ("target", "target-subject", "target-running", "subject", "protected_running_identity"),
        ("missing-name", "missing-name-subject", None, "baseline", "running_identity"),
        ("missing-home", "missing-home-subject", baseline_name, "missing", "running_identity"),
        ("ambiguous", "ambiguous-subject", "different-running", "baseline", "running_identity"),
    )
    observations = [_run_identity_case(root, case) for case in cases]
    _check(
        all(
            row["status"] == "refused"
            and row["exit_code"] == int(ExitCode.HUMAN_ACTION)
            and row["guard"] == row["expected_guard"]
            and row["mutation_count"] == 0
            and row["artifact_unchanged"] is True
            and row["manager_unchanged"] is True
            for row in observations
        ),
        "running identity, missing derivation, and ambiguous identity refuse with zero action",
        observed=observations,
    )


def _assert_repairable(
    first: tuple[int, dict[str, Any] | None, str],
    second: tuple[int, dict[str, Any] | None, str],
    *,
    first_artifact: bytes,
    before_bytes: bytes,
    generation_after_first: bytes | None,
    fault_bytes: bytes,
    journal_exists: bool,
    manager_transaction_unchanged: bool,
) -> None:
    _check(
        first[0] == 0
        and first[1] is not None
        and first[1].get("kind") == "rollback_repair"
        and first[1].get("status") == "repaired",
        "repairable arm reports a performed rollback",
        observed=first,
    )
    _check(first_artifact == before_bytes, "action atomically restores the trusted bytes")
    _check(
        generation_after_first == fault_bytes,
        "displaced fault bytes remain as the prior rollback generation",
        observed=generation_after_first,
    )
    _check(journal_exists, "repair writes its durable append-only journal")
    _check(
        manager_transaction_unchanged,
        "repair preserves the exact canonical manager transaction bytes",
    )
    _check(
        second[0] == 0 and second[1] is not None and second[1].get("status") == "already_repaired",
        "second invocation is a verified no-op success",
        observed=second,
    )


def _assert_refusals(
    refused: tuple[int, dict[str, Any] | None, str],
    evidence_refused: tuple[int, dict[str, Any] | None, str],
    *,
    protected_unchanged: bool,
    evidence_unchanged: bool,
) -> None:
    _check(
        refused[0] == 3
        and refused[1] is not None
        and refused[1].get("status") == "refused"
        and refused[1].get("error_kind") == "repair_refused_protected_subject",
        "origin identity is explicitly refused before mutation",
        observed=refused,
    )
    _check(
        protected_unchanged,
        "hermetic origin-identity refusal changes neither manager state nor target bytes",
    )
    _check(
        evidence_refused[0] == 3
        and evidence_refused[1] is not None
        and evidence_refused[1].get("status") == "refused",
        "evidence-destroying repair is explicitly refused",
        observed=evidence_refused,
    )
    _check(
        evidence_unchanged,
        "evidence refusal occurs before target or manager mutation",
    )


def _assert_journal_order(journal_trace: list[tuple[object, object]]) -> None:
    expected_order = [
        ("precondition", "preconditions_verified"),
        ("precondition", "recovery_generation_durable"),
        ("action", "mutation_started"),
        ("action", "action_receipt_recorded"),
        ("postcondition", "postconditions_started"),
        ("postcondition", "repair_worked"),
        ("postcondition", "postconditions_started"),
        ("postcondition", "repair_worked"),
    ]
    _check(
        journal_trace == expected_order,
        "journal preserves three-phase ordering and explicit replay verification",
        observed=journal_trace,
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        running_name = "smoke-running-control"
        os.environ["SOLET_NAME"] = running_name
        os.environ["APP_HOME"] = str(
            _write_running_identity(root / "running-control", running_name)
        )
        manager_home = root / "manager"
        record, artifact, _, fault_bytes, before_bytes = _failure_record(
            root,
            name="repairable-fixture",
            target=root / "repairable-target",
            manager_home=manager_home,
        )
        transaction_pinned = json.loads(record.read_text(encoding="utf-8"))[
            "manager_transaction"
        ]["sha256"]
        first = _invoke(manager_home, record)
        manager_transaction = ManagerPaths.resolve(
            explicit_home=manager_home
        ).transaction_path("repairable-fixture")
        transaction_after_first = _sha256(manager_transaction)
        first_artifact = artifact.read_bytes()
        generation = (
            manager_home / "state/repair_generations/failure-rollback-service-bindings-v1.before"
        )
        journal = manager_home / "state/repairs/failure-rollback-service-bindings-v1.jsonl"
        generation_after_first = generation.read_bytes() if generation.is_file() else None
        second = _invoke(manager_home, record)
        transaction_after_second = _sha256(manager_transaction)
        journal_exists = journal.is_file()

        protected_home = root / "protected-manager"
        protected_name = "protected-running-fixture"
        protected_target = root / "protected-target"
        protected_record, protected_artifact, _, protected_fault, _ = _failure_record(
            root / "protected-record",
            name=protected_name,
            target=protected_target,
            manager_home=None,
        )
        protected_before = _tree_snapshot(protected_home)
        with _identity_environment(
            name=protected_name,
            app_home=protected_target / "profile",
        ):
            refused = _invoke(protected_home, protected_record)
        protected_after = _tree_snapshot(protected_home)
        protected_artifact_after = protected_artifact.read_bytes()

        evidence_home = root / "evidence-manager"
        evidence_record, _, _, _, _ = _failure_record(
            root / "evidence-record",
            name="evidence-fixture",
            target=root / "evidence-target",
            manager_home=None,
            disposition="preserved_evidence",
        )
        evidence_target_before = _tree_snapshot(root / "evidence-target")
        evidence_refused = _invoke(evidence_home, evidence_record)
        evidence_target_after = _tree_snapshot(root / "evidence-target")
        evidence_home_exists = evidence_home.exists()
        # A legitimate repair refusal never writes the journal; read it only
        # when it exists so an unexpected refusal reports through the
        # existing _check() assertions below instead of an unhandled
        # FileNotFoundError.
        journal_trace = _journal_trace(journal) if journal_exists else []

        _exercise_adversarial_controls(root / "controls")

    _assert_repairable(
        first,
        second,
        first_artifact=first_artifact,
        before_bytes=before_bytes,
        generation_after_first=generation_after_first,
        fault_bytes=fault_bytes,
        journal_exists=journal_exists,
        manager_transaction_unchanged=(
            transaction_after_first == transaction_after_second
            == transaction_pinned
        ),
    )
    _assert_refusals(
        refused,
        evidence_refused,
        protected_unchanged=(
            protected_before == protected_after and protected_artifact_after == protected_fault
        ),
        evidence_unchanged=(
            evidence_target_before == evidence_target_after and not evidence_home_exists
        ),
    )
    _assert_journal_order(journal_trace)
    print(f"rollback_repair_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
