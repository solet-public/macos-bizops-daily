"""Closed maintenance-journal schema and transition regression battery."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]

from solet_manager.errors import StateConflictError, StateError  # noqa: E402
from solet_manager.transaction import (  # noqa: E402
    append_maintenance_attempt,
    create_import_maintenance_operation,
    parse_maintenance_operation_bytes,
    read_maintenance_operation,
    transition_maintenance_operation,
    write_maintenance_operation,
)  # noqa: E402

from solet_setup_contracts import canonical_sha256  # noqa: E402

_TIME = "2026-09-18T00:00:00Z"


def _operation(suffix: str = "1") -> dict[str, object]:
    return create_import_maintenance_operation(
        operation_id="opr_" + suffix * 32,
        instance_id="ins_" + suffix * 32,
        idempotency_key="sha256:" + "a" * 64,
        name="fixture",
        canonical_target="/tmp/fixture",
        target_device=1,
        target_inode=2,
        channel_id="stable",
        provenance_seed_id="seed",
        head_commit="b" * 40,
        head_tree="c" * 40,
        inspection_bundle_digest="sha256:" + "d" * 64,
        diagnostic_contract_digest="sha256:" + "e" * 64,
        approval_fingerprint="sha256:" + "f" * 64,
        manager_write_paths=("/manager/cache", "/manager/inventory", "/manager/journal"),
        non_touch_surfaces=("credentials", "documents"),
        timestamp=_TIME,
    )


def _evidence(operation: dict[str, object], attempt: int, code: str) -> dict[str, str]:
    value = "sha256:" + "0" * 64
    source = "manager:existing_install_inspection"
    evidence_id = "evd_" + canonical_sha256(
        [operation["operation_id"], attempt, "digest", code, value, source]
    ).removeprefix("sha256:")[:32]
    return {
        "evidence_id": evidence_id,
        "kind": "digest",
        "code": code,
        "value": value,
        "source": source,
    }


def _append(operation: dict[str, object], stage_id: str, code: str) -> dict[str, object]:
    attempt = len(operation["attempts"])
    return append_maintenance_attempt(
        operation,
        stage_id=stage_id,
        status="verified",
        evidence=(_evidence(operation, attempt, code),),
        timestamp=_TIME,
    )


def _raises(error: type[BaseException], callback: object) -> None:
    try:
        callback()  # type: ignore[operator]
    except error:
        return
    raise AssertionError(f"expected {error.__name__}")


def _verified() -> dict[str, object]:
    operation = _operation()
    operation = _append(operation, "inspection_bundle_cached", "bundle_cached")
    operation = transition_maintenance_operation(operation, status="bundle_cached", timestamp=_TIME)
    operation = _append(operation, "inventory_published", "inventory_published")
    operation = transition_maintenance_operation(operation, status="inventory_published", timestamp=_TIME)
    operation = _append(operation, "enrollment_verified", "enrollment_verified")
    return transition_maintenance_operation(
        operation,
        status="verified",
        result={"kind": "imported", "inventory_instance_id": operation["instance_id"]},
        timestamp=_TIME,
    )


def _bundle_cached() -> dict[str, object]:
    operation = _operation()
    operation = _append(operation, "inspection_bundle_cached", "bundle_cached")
    return transition_maintenance_operation(operation, status="bundle_cached", timestamp=_TIME)


def _inventory_published() -> dict[str, object]:
    operation = _bundle_cached()
    operation = _append(operation, "inventory_published", "inventory_published")
    return transition_maintenance_operation(operation, status="inventory_published", timestamp=_TIME)


def _assert_closed_schema(prepared: dict[str, object]) -> None:
    assert parse_maintenance_operation_bytes(json.dumps(prepared, sort_keys=True).encode()) == prepared
    verified = _verified()
    assert verified["status"] == "verified"
    assert len(verified["attempts"]) == len(verified["evidence"]) == 4


def _assert_required_fields(prepared: dict[str, object]) -> None:
    for key in prepared:
        malformed = copy.deepcopy(prepared)
        malformed.pop(key)
        _raises(StateError, lambda malformed=malformed: parse_maintenance_operation_bytes(json.dumps(malformed).encode()))
    for section in ("input", "current_identity", "contract_digests", "approval", "stage_statuses"):
        for key in prepared[section]:
            malformed = copy.deepcopy(prepared)
            malformed[section][key] = []
            _raises(StateError, lambda malformed=malformed: parse_maintenance_operation_bytes(json.dumps(malformed).encode()))


def _assert_terminal_transitions() -> None:
    for operation in (_operation("2"), _bundle_cached(), _inventory_published()):
        for terminal in ("blocked", "failed", "abandoned"):
            result = {"kind": terminal, "reason_code": "fixture", "repair": "repair"}
            assert transition_maintenance_operation(operation, status=terminal, result=result, timestamp=_TIME)["status"] == terminal


def _assert_retry_and_invalid_transitions(prepared: dict[str, object]) -> None:
    retrying = append_maintenance_attempt(_operation("3"), stage_id="inspection_bundle_cached", status="blocked", evidence=(_evidence(_operation("3"), 1, "blocked"),), error_kind="fixture_blocked", timestamp=_TIME)
    retrying = append_maintenance_attempt(retrying, stage_id="inspection_bundle_cached", status="applying", evidence=(), timestamp=_TIME)
    assert _append(retrying, "inspection_bundle_cached", "retried")["stage_statuses"]["inspection_bundle_cached"] == "verified"
    _raises(StateConflictError, lambda: transition_maintenance_operation(prepared, status="inventory_published"))


def main() -> int:
    prepared = _operation()
    _assert_closed_schema(prepared)
    _assert_required_fields(prepared)
    _assert_terminal_transitions()
    _assert_retry_and_invalid_transitions(prepared)
    _raises(
        StateConflictError,
        lambda: append_maintenance_attempt(
            prepared,
            stage_id="inspection_revalidated",
            status="verified",
            evidence=(),
            timestamp=_TIME,
        ),
    )

    for mutate in (
        lambda value: value.pop("approval"),
        lambda value: value.update({"unexpected": None}),
        lambda value: value["input"]["target_filesystem_identity"].update({"device": True}),
        lambda value: value.update({"idempotency_key": "sha256:not-a-digest"}),
        lambda value: value.update({"status": "unknown"}),
        lambda value: value.update({"created_at": "not-a-timestamp"}),
        lambda value: value["attempts"][0].update({"stage_key": "sha256:" + "0" * 64}),
        lambda value: value["evidence"][0].update({"value": "secret-token"}),
    ):
        malformed = copy.deepcopy(prepared)
        mutate(malformed)
        _raises(StateError, lambda malformed=malformed: parse_maintenance_operation_bytes(json.dumps(malformed).encode()))

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        state = root / "state"
        state.mkdir(mode=0o700)
        path = state / "operation.json"
        write_maintenance_operation(path, None, prepared)
        assert read_maintenance_operation(path) == prepared
        next_value = _append(prepared, "inspection_bundle_cached", "bundle_cached")
        write_maintenance_operation(path, prepared, next_value)
        forged = copy.deepcopy(next_value)
        forged["attempts"] = copy.deepcopy(prepared["attempts"])
        forged["evidence"] = copy.deepcopy(prepared["evidence"])
        forged["stage_statuses"] = copy.deepcopy(prepared["stage_statuses"])
        _raises(StateConflictError, lambda: write_maintenance_operation(path, next_value, forged))
        changed = copy.deepcopy(next_value)
        changed["created_at"] = "2026-09-18T00:00:01Z"
        _raises(StateConflictError, lambda: write_maintenance_operation(path, next_value, changed))
    print("maintenance_operation_closed_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
