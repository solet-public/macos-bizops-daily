"""Focused smoke for atomic state, registry, locking, and roll-up."""

from __future__ import annotations

import json
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.errors import (  # noqa: E402
    InstanceUnmanagedError,
    StateConflictError,
    StateError,
)
from solet_manager.journal_rollup import derive_stage_statuses  # noqa: E402
from solet_manager.models import (  # noqa: E402
    CheckpointStatus,
    InstanceRecord,
    JsonValue,
    TransactionStatus,
)
from solet_manager.registry import InstanceRegistry  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.state_io import atomic_write_json, instance_lock, load_json_object  # noqa: E402
from solet_manager.transaction import (  # noqa: E402
    Transaction,
    assert_resume_identity,
    load_transaction,
    roll_up_transaction,
    write_transaction,
)

_CHECKS = 0


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _raises(error: type[BaseException], callback: object, label: str) -> None:
    try:
        callback()  # type: ignore[operator]
    except error:
        _check(True, label)
    else:
        _check(False, label)


def _operation_attempt(
    base: dict[str, JsonValue],
    *,
    phase: str,
    number: int,
    status: str,
    error_kind: str | None,
    retry_safe: bool,
) -> dict[str, JsonValue]:
    return {
        **base,
        "phase": phase,
        "attempt": number,
        "checkpoint_status": status,
        "error_kind": error_kind,
        "retry_safe": retry_safe,
    }


def _assert_failed_attempt_round_trip(
    root: Path,
    bound: Transaction,
    first_attempt: dict[str, JsonValue],
) -> None:
    failed_attempt = {
        **first_attempt,
        "checkpoint_status": "failed",
        "error_kind": "fixture_command_failed",
        "reason": {
            "outcome_class": "launch_error",
            "exit_code": None,
            "duration_ms": 17,
            "timed_out": False,
            "stdout_bytes": 0,
            "stderr_bytes": 23,
            "stdout_truncated": False,
            "stderr_truncated": False,
        },
    }
    failed = bound.with_operation_status(
        "first", CheckpointStatus.FAILED, attempt=failed_attempt
    )
    failed_path = root / "state" / "transactions" / "failed.json"
    write_transaction(failed_path, failed)
    reloaded_failed = load_transaction(failed_path)
    _check(
        reloaded_failed is not None
        and reloaded_failed.operation_attempts[0]["exit_code"] is None
        and reloaded_failed.operation_attempts[0]["duration_ms"] == 17
        and reloaded_failed.operation_attempts[0]["reason"] == failed_attempt["reason"],
        "failed journal round trip preserves bounded command failure diagnostics",
    )


def _set_rolled_up_operation_status(
    transaction: dict[str, JsonValue],
    operation_id: str,
    status: str,
) -> None:
    transaction["operation_statuses"][operation_id] = status  # type: ignore[index]
    statuses = {
        item_id: CheckpointStatus(value)
        for item_id, value in transaction["operation_statuses"].items()  # type: ignore[index,union-attr]
    }
    stages = derive_stage_statuses(
        {
            stage_id: CheckpointStatus(value)
            for stage_id, value in transaction["stages"].items()  # type: ignore[index,union-attr]
        },
        {
            stage_id: {
                boundary: {
                    probe_id: CheckpointStatus(value)
                    for probe_id, value in probes.items()
                }
                for boundary, probes in boundaries.items()
            }
            for stage_id, boundaries in transaction["stage_probe_statuses"].items()  # type: ignore[index,union-attr]
        },
        transaction["operation_stages"],  # type: ignore[arg-type,index]
        statuses,
    )
    transaction["stages"] = {
        stage_id: value.value for stage_id, value in stages.items()
    }
    transaction["status"] = roll_up_transaction(
        stages,
        {
            probe_id: CheckpointStatus(value)
            for probe_id, value in transaction["completion"].items()  # type: ignore[index,union-attr]
        },
    ).value


def _check_operation_attempt_coverage(
    bound: Transaction,
    after_first: Transaction,
) -> None:
    first_attempt = after_first.operation_attempts[-1]
    missing_operation_attempt = json.loads(json.dumps(after_first.to_dict()))
    missing_operation_attempt["operation_attempts"] = []
    _raises(
        StateError,
        lambda: Transaction.from_dict(missing_operation_attempt),
        "recorded operation status without an attempt is rejected",
    )
    stale_operation_status = json.loads(json.dumps(after_first.to_dict()))
    _set_rolled_up_operation_status(stale_operation_status, "first", "blocked")
    _raises(
        StateError,
        lambda: Transaction.from_dict(stale_operation_status),
        "rollup-consistent operation status disagreeing with latest attempt is rejected",
    )
    pre_probe_verified = bound.with_operation_status(
        "first",
        CheckpointStatus.VERIFIED,
        attempt=_operation_attempt(
            first_attempt,
            phase="pre_probe",
            number=1,
            status="verified",
            error_kind=None,
            retry_safe=True,
        ),
    )
    probe_drift_refused = pre_probe_verified.with_operation_status(
        "first", CheckpointStatus.AWAITING_USER
    ).with_result_kind("probe_drift")
    _check(
        Transaction.from_dict(probe_drift_refused.to_dict()).operation_statuses["first"]
        is CheckpointStatus.AWAITING_USER,
        "probe-drift refusal preserves its verified pre-probe attempt",
    )
    counterfeit_probe_drift = json.loads(json.dumps(probe_drift_refused.to_dict()))
    _set_rolled_up_operation_status(counterfeit_probe_drift, "first", "blocked")
    _raises(
        StateError,
        lambda: Transaction.from_dict(counterfeit_probe_drift),
        "probe-drift result_kind cannot conceal another status mismatch",
    )
    pre_apply_ready = bound.with_operation_status(
        "first",
        CheckpointStatus.AWAITING_USER,
        attempt=_operation_attempt(
            first_attempt,
            phase="pre_probe",
            number=1,
            status="awaiting_user",
            error_kind=None,
            retry_safe=True,
        ),
    )
    in_flight_apply = pre_apply_ready.with_operation_status(
        "first", CheckpointStatus.APPLYING
    )
    _check(
        Transaction.from_dict(in_flight_apply.to_dict()).operation_statuses["first"]
        is CheckpointStatus.APPLYING,
        "in-flight apply preserves its pre-apply approval attempt",
    )
    invalid_in_flight_apply = pre_probe_verified.with_operation_status(
        "first", CheckpointStatus.APPLYING
    )
    _raises(
        StateError,
        lambda: Transaction.from_dict(invalid_in_flight_apply.to_dict()),
        "in-flight apply requires an awaiting-user pre-apply attempt",
    )
    retry_blocked = after_first.with_operation_status(
        "first",
        CheckpointStatus.BLOCKED,
        attempt=_operation_attempt(
            first_attempt,
            phase="pre_probe",
            number=2,
            status="blocked",
            error_kind="probe_not_verified",
            retry_safe=False,
        ),
    )
    resumed = retry_blocked.with_operation_status(
        "first",
        CheckpointStatus.VERIFIED,
        attempt=_operation_attempt(
            first_attempt,
            phase="post_probe",
            number=3,
            status="verified",
            error_kind=None,
            retry_safe=True,
        ),
    )
    _check(
        Transaction.from_dict(resumed.to_dict()).operation_statuses["first"]
        is CheckpointStatus.VERIFIED,
        "latest operation attempt permits a blocked retry followed by resume",
    )
    repeated_pre_apply = bound.with_operation_status(
        "first",
        CheckpointStatus.AWAITING_USER,
        attempt=_operation_attempt(
            first_attempt,
            phase="pre_probe",
            number=1,
            status="awaiting_user",
            error_kind="manual_resolution_required",
            retry_safe=False,
        ),
    ).with_operation_status(
        "first",
        CheckpointStatus.AWAITING_USER,
        attempt=_operation_attempt(
            first_attempt,
            phase="pre_probe",
            number=2,
            status="awaiting_user",
            error_kind="manual_resolution_required",
            retry_safe=False,
        ),
    )
    _check(
        Transaction.from_dict(repeated_pre_apply.to_dict()).operation_statuses["first"]
        is CheckpointStatus.AWAITING_USER,
        "repeated pre-apply attempts retain their latest recorded status",
    )
    pgvector_post_probe_block = bound.with_operation_status(
        "first",
        CheckpointStatus.BLOCKED,
        attempt=_operation_attempt(
            first_attempt,
            phase="pre_probe",
            number=1,
            status="blocked",
            error_kind="postgres_probe_not_verified",
            retry_safe=False,
        ),
    ).with_operation_status(
        "first",
        CheckpointStatus.APPLIED,
        attempt=_operation_attempt(
            first_attempt,
            phase="apply",
            number=1,
            status="applied",
            error_kind=None,
            retry_safe=True,
        ),
    ).with_operation_status(
        "first",
        CheckpointStatus.BLOCKED,
        attempt=_operation_attempt(
            first_attempt,
            phase="post_probe",
            number=1,
            status="blocked",
            error_kind="postgres_probe_not_verified",
            retry_safe=False,
        ),
    )
    _check(
        Transaction.from_dict(pgvector_post_probe_block.to_dict()).operation_statuses[
            "first"
        ]
        is CheckpointStatus.BLOCKED,
        "post-probe block may overwrite applied when its latest attempt agrees",
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        state = root / "state.json"
        atomic_write_json(state, {"schema_version": 1, "value": "ok"})
        _check(stat.S_IMODE(state.stat().st_mode) == 0o600, "atomic state is 0600")
        _check(load_json_object(state) == {"schema_version": 1, "value": "ok"}, "atomic round trip")
        state.write_text('{"truncated":', encoding="utf-8")
        state.chmod(0o600)
        _raises(StateError, lambda: load_json_object(state), "truncated state refused")
        state.write_text("{}", encoding="utf-8")
        state.chmod(0o644)
        _raises(StateError, lambda: load_json_object(state), "broad mode refused")

        registry = InstanceRegistry(root / "config" / "instances.json")
        record = InstanceRecord(
            name="bizops",
            target=str(root / "Solets" / "bizops"),
            launcher=str(root / "bin" / "bizops"),
            seed_repository="https://github.com/solet-public/macos-bizops.git",
            seed_tag="release-1",
            seed_commit="a" * 40,
            seed_tree_hash="b" * 40,
            profile="macos-bizops",
            flow_id="macos.repository_setup",
            flow_source_revision="a" * 40,
            flow_contract_digest="sha256:" + "d" * 64,
            created_at="2026-08-21T03:30:00Z",
            updated_at="2026-08-21T03:30:00Z",
        )
        registry.add(record)
        _check(registry.get("bizops") == record, "registry round trip")
        _check([item.name for item in registry.list()] == ["bizops"], "registry list stable")
        tagless_record = InstanceRecord(
            **{**record.__dict__, "name": "tagless", "seed_tag": None}
        )
        registry.add(tagless_record)
        _check(registry.get("tagless") == tagless_record, "tagless registry round trip")
        bad_record = InstanceRecord(**{**record.__dict__, "launcher": "/opt/homebrew/Cellar/solet/1/bin/solet"})
        _raises(StateError, lambda: registry.add(bad_record), "formula-keg path rejected")
        unmanaged_target = root / "unmanaged-target"
        unmanaged_target.mkdir()
        try:
            registry.require("unmanaged", candidate_target=unmanaged_target)
        except InstanceUnmanagedError as exc:
            _check(
                exc.repair == (
                    "Resume the same reviewed transaction with 'solet create unmanaged'. "
                    "Do not edit the registry by hand or adopt an arbitrary target."
                ),
                "unmanaged target names only same-fingerprint create continuation",
            )
        else:
            _check(False, "unmanaged target refuses arbitrary directory authority")
        provisional = InstanceRecord(
            **{
                **record.__dict__,
                "name": "incomplete",
                "lifecycle_state": "setup_incomplete",
                "input_fingerprint": "sha256:" + "e" * 64,
                "expected_router_name": "incomplete",
                "expected_router_socket": str(root / "runtime" / "incomplete.router.sock"),
                "expected_router_port_range": "8800-8999",
            }
        )
        registry.add(provisional)
        _check(
            registry.get("incomplete") == provisional,
            "setup-incomplete transaction materialization is retained in the closed registry",
        )
        upgraded = InstanceRecord(
            **{**provisional.__dict__, "lifecycle_state": "verified"}
        )
        registry.add(upgraded)
        _check(
            registry.get("incomplete") == upgraded,
            "same-fingerprint completion atomically upgrades the provisional record",
        )
        pending_mismatch = InstanceRecord(
            **{
                **provisional.__dict__,
                "name": "fingerprint-mismatch",
            }
        )
        registry.add(pending_mismatch)
        fingerprint_mismatch = InstanceRecord(
            **{
                **pending_mismatch.__dict__,
                "lifecycle_state": "verified",
                "input_fingerprint": "sha256:" + "f" * 64,
            }
        )
        _raises(
            StateConflictError,
            lambda: registry.add(fingerprint_mismatch),
            "different transaction fingerprint cannot upgrade provisional authority",
        )

        verified = {"stage": CheckpointStatus.VERIFIED}
        complete = {"probe": CheckpointStatus.VERIFIED}
        _check(
            roll_up_transaction(verified, complete) is TransactionStatus.VERIFIED,
            "roll-up verified",
        )
        _check(
            roll_up_transaction({"s": CheckpointStatus.FAILED}, complete) is TransactionStatus.FAILED,
            "roll-up failed precedence",
        )
        _check(
            roll_up_transaction({"s": CheckpointStatus.BLOCKED}, {"p": CheckpointStatus.AWAITING_USER}) is TransactionStatus.BLOCKED,
            "roll-up blocked precedence",
        )
        _check(
            roll_up_transaction({"s": CheckpointStatus.AWAITING_USER}, complete) is TransactionStatus.AWAITING_USER,
            "roll-up awaiting user",
        )
        _check(
            roll_up_transaction({"s": CheckpointStatus.APPLYING}, complete) is TransactionStatus.APPLYING,
            "roll-up applying",
        )
        _check(
            roll_up_transaction({"s": CheckpointStatus.APPLIED}, complete) is TransactionStatus.PENDING,
            "applied is not verified",
        )
        _check(
            roll_up_transaction({"s": CheckpointStatus.DECLINED}, complete) is TransactionStatus.VERIFIED,
            "optional decline permits verified roll-up",
        )

        seed = SeedLock(
            "https://github.com/solet-public/macos-bizops.git",
            "release-1",
            "a" * 40,
            "b" * 40,
            "c" * 64,
            "bizops",
        )
        transaction = Transaction.create(
            name="bizops",
            target=root / "Solets" / "bizops",
            input_fingerprint="sha256:" + "1" * 64,
            answers={
                "schema_version": 1,
                "flow_id": "macos.repository_setup",
                "flow_source_revision": "a" * 40,
                "name": "bizops",
                "target": str(root / "Solets" / "bizops"),
                "public_inputs": {},
                "decisions": {},
                "consents": {},
                "resolution_evidence": [],
            },
            seed=seed,
            flow_id="macos.repository_setup",
            flow_source_revision="a" * 40,
            flow_contract_digest="sha256:" + "d" * 64,
            stage_ids=("preflight",),
            completion_probe_ids=("doctor",),
        )
        transaction_path = root / "state" / "transactions" / "bizops.json"
        projection_dir = Path(transaction.target) / ".solet"
        projection_dir.mkdir(parents=True, mode=0o700)
        write_transaction(transaction_path, transaction)
        assert_resume_identity(
            transaction,
            name=transaction.name,
            target=Path(transaction.target),
            input_fingerprint=transaction.input_fingerprint,
        )
        _check(True, "same transaction fingerprint resumes")
        _raises(
            StateConflictError,
            lambda: assert_resume_identity(
                transaction,
                name=transaction.name,
                target=Path(transaction.target),
                input_fingerprint="sha256:" + "0" * 64,
            ),
            "changed transaction fingerprint refuses resume",
        )
        loaded = load_transaction(transaction_path)
        _check(
            loaded is not None and loaded.seed.release_tag == "release-1",
            "journal retains seed lock",
        )
        _check(loaded is not None and loaded == transaction, "journal round trip")
        projection_path = projection_dir / "install-state.json"
        projection = json.loads(projection_path.read_text(encoding="utf-8"))
        _check(
            projection
            == {
                "name": transaction.name,
                "target": transaction.target,
                "flow_id": transaction.flow_id,
                "flow_source_revision": transaction.flow_source_revision,
                "answers_fingerprint": transaction.answers_fingerprint,
            },
            "manager journal writes the exact non-secret target-side projection",
        )
        tagless_seed = SeedLock(
            "https://github.com/solet-public/tagless-seed.git",
            None,
            "a" * 40,
            "b" * 40,
            None,
            "tagless",
        )
        tagless_transaction = Transaction.create(
            name="tagless",
            target=root / "Solets" / "tagless",
            input_fingerprint="sha256:" + "2" * 64,
            answers=transaction.answers,
            seed=tagless_seed,
            flow_id=transaction.flow_id,
            flow_source_revision=transaction.flow_source_revision,
            flow_contract_digest=transaction.flow_contract_digest,
            stage_ids=("preflight",),
            completion_probe_ids=("doctor",),
        )
        tagless_transaction_path = root / "state" / "transactions" / "tagless.json"
        write_transaction(tagless_transaction_path, tagless_transaction)
        loaded_tagless = load_transaction(tagless_transaction_path)
        _check(
            loaded_tagless is not None and loaded_tagless.seed.release_tag is None,
            "tagless journal round trip",
        )

        bound = transaction.bind_operations({"first": "preflight", "second": "preflight"})
        first_attempt: dict[str, JsonValue] = {
            "operation_id": "first",
            "stage_id": "preflight",
            "phase": "post_probe",
            "attempt": 1,
            "request_id": "8f2f3ed3-03fc-4f58-915e-eb400a172a67",
            "checkpoint_status": "verified",
            "error_kind": None,
            "retry_safe": True,
            "exit_code": None,
            "timed_out": False,
            "duration_ms": 17,
            "planned_actions": [],
            "evidence": [],
            "reason": None,
            "repair": None,
            "recorded_at": "2026-08-21T03:30:00Z",
        }

        _assert_failed_attempt_round_trip(root, bound, first_attempt)

        after_first = bound.with_operation_status(
            "first",
            CheckpointStatus.VERIFIED,
            attempt=first_attempt,
        )
        invalid_request_id = json.loads(json.dumps(after_first.to_dict()))
        invalid_request_id["operation_attempts"][0][  # type: ignore[index]
            "request_id"
        ] = "not-a-uuid"
        _raises(
            StateError,
            lambda: Transaction.from_dict(invalid_request_id),
            "corrupt operation attempt request id refused",
        )
        _check_operation_attempt_coverage(bound, after_first)
        _check(
            after_first.stages["preflight"] is CheckpointStatus.PENDING,
            "first verified operation cannot verify multi-operation stage",
        )
        after_second = after_first.with_operation_status("second", CheckpointStatus.VERIFIED)
        _check(
            after_second.stages["preflight"] is CheckpointStatus.VERIFIED,
            "stage verifies only after every selected operation",
        )
        write_transaction(transaction_path, after_first)
        refreshed_projection = json.loads(projection_path.read_text(encoding="utf-8"))
        _check(
            refreshed_projection["answers_fingerprint"] == after_first.answers_fingerprint,
            "subsequent manager journal writes keep the target projection synchronized",
        )
        interrupted = load_transaction(transaction_path)
        _check(
            interrupted is not None and interrupted.operation_attempts[0]["operation_id"] == "first" and interrupted.stages["preflight"] == CheckpointStatus.PENDING,
            "interrupted attempt preserves truthful operation and stage state",
        )

        lock_path = root / "locks" / "bizops.lock"
        entered: list[str] = []

        def first() -> None:
            with instance_lock(lock_path):
                entered.append("first")
                time.sleep(0.15)
                entered.append("first_done")

        def second() -> None:
            time.sleep(0.03)
            with instance_lock(lock_path):
                entered.append("second")

        one = threading.Thread(target=first)
        two = threading.Thread(target=second)
        one.start()
        two.start()
        one.join()
        two.join()
        _check(entered == ["first", "first_done", "second"], "same-name mutation serializes")
        absent_lock = root / "locks" / "dry.lock"
        with instance_lock(absent_lock, create=False) as handle:
            _check(handle is None, "dry lock has no handle when absent")
        _check(not absent_lock.exists(), "dry lock creates no file")

        parsed = json.loads(transaction_path.read_text(encoding="utf-8"))
        parsed["status"] = "verified"
        atomic_write_json(transaction_path, parsed)
        _raises(StateError, lambda: load_transaction(transaction_path), "inconsistent roll-up refused")

    print(f"state_transaction_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
