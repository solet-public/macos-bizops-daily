"""Step-6 update journal v4 (design section 4.7): graph, ``abandoned``, ``promoted``, ``recovers``, ``source_mode``,
``retirement``, per-row ``operation_type``, and the v3 in-memory upgrade with one-shot classification."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]
from solet_manager.errors import StateConflictError, StateError  # noqa: E402
from solet_manager.existing_install_bundle import SYNTHESISED_OPERATION_TYPES  # noqa: E402
from solet_manager.models import OperationType  # noqa: E402
from solet_manager.update_journal import (  # noqa: E402
    ABANDONABLE_STATUSES,
    DOCTOR_STATUSES,
    GUARDED_STATUSES,
    RUNTIME_STATUSES,
    TERMINAL_UPDATE_STATUSES,
    UPDATE_JOURNAL_SCHEMA_VERSION,
    V3_UNCLASSIFIED,
    advance_update_journal,
    classify_v3_rows,
    create_update_journal,
    is_retired,
    parse_update_journal_bytes,
    read_update_journal,
    record_runtime_approval,
    retire_update_journal,
    write_update_journal,
)

_CHECKS = 0


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _expect(kind: type[Exception], action: Any, label: str) -> Exception:
    try:
        action()
    except kind as exc:
        return exc
    raise AssertionError(label)


def _journal(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "operation_id": "opr_" + "a" * 32,
        "instance_id": "ins_" + "b" * 32,
        "fingerprint": "sha256:" + "c" * 64,
        "baseline_commit": "1" * 40,
        "baseline_tree": "2" * 40,
        "branch": "main",
        "candidate_descriptor_digest": "sha256:" + "d" * 64,
        "candidate_commit": "e" * 40,
        "candidate_tree": "f" * 40,
        "candidate_tag": "r2",
        "candidate_contract_digest": "sha256:" + "9" * 64,
        "receipt_digest": "sha256:" + "0" * 64,
        "planned_actions": ("target.fetch_exact_candidate", "target.fast_forward_exact_candidate"),
        "timestamp": "2026-09-18T00:00:00Z",
    }
    fields.update(overrides)
    return cast(dict[str, Any], create_update_journal(**fields))


def _row(operation_id: str, ref: str, operation_type: str) -> dict[str, Any]:
    return {"operation_id": operation_id, "operation_ref": ref, "stage": "dependencies", "status": "pending", "idempotency_key": "sha256:" + "1" * 64, "mutation_class": "venv", "rollback_class": "reversible", "operation_type": operation_type}


def _to_runtime_advanced(value: dict[str, Any]) -> dict[str, Any]:
    for status in ("operation_published", "target_fetched", "source_applying", "source_advanced"):
        value = cast(dict[str, Any], advance_update_journal(value, status=status, stage_id=status, note="ok"))
    value = cast(dict[str, Any], record_runtime_approval(value, fingerprint="sha256:" + "e" * 64, planned_actions=("a",), strategy="single_color_restart", forward_only_boundary=None, operations=(_row("dependencies_reconcile", "existing::dependencies.reconcile", "closure_repair"),), note="approved"))
    for status in RUNTIME_STATUSES[1:]:
        value = cast(dict[str, Any], advance_update_journal(value, status=status, stage_id=status, note="ok"))
    return value


def _assert_shape_and_keys() -> None:
    value = _journal()
    _check(value["schema_version"] == UPDATE_JOURNAL_SCHEMA_VERSION == 5 and value["recovers"] is None and value["source_mode"] == "advance" and value["retirement"] is None, "v4 keys present with defaults (persisted at v5 since Step 7)")
    _check(parse_update_journal_bytes(json.dumps(value).encode()) == value, "round trip")
    _expect(StateError, lambda: parse_update_journal_bytes(json.dumps({**value, "source_mode": "verify"}).encode()), "verify mode with baseline != candidate accepted")
    _expect(StateError, lambda: parse_update_journal_bytes(json.dumps({**value, "recovers": value["operation_id"]}).encode()), "self-recovery accepted")
    _expect(StateError, lambda: parse_update_journal_bytes(json.dumps({**value, "retirement": {"released_at": "t", "head_observed": "1" * 40, "reason": "x"}}).encode()), "retirement on a nonterminal accepted")
    _expect(StateError, lambda: parse_update_journal_bytes(json.dumps({**value, "status": "doctor_verifying"}).encode()), "doctor status without a runtime approval accepted")
    verify = _journal(baseline_commit="e" * 40, baseline_tree="f" * 40, source_mode="verify", recovers="opr_" + "b" * 32)
    _check(verify["source_mode"] == "verify" and verify["recovers"] == "opr_" + "b" * 32, "verify-mode successor journal")
    _expect(StateError, lambda: _journal(baseline_commit="e" * 40, baseline_tree="f" * 40), "advance mode at the candidate accepted")
    _check(TERMINAL_UPDATE_STATUSES == {"blocked", "failed", "abandoned", "promoted"} and set(DOCTOR_STATUSES) == {"doctor_verifying", "doctor_verified", "doctor_incomplete", "promoting"}, "terminal and doctor status sets")
    _check(DOCTOR_STATUSES[0] in GUARDED_STATUSES and "source_advanced" in GUARDED_STATUSES and "hydration_applying" not in GUARDED_STATUSES, "guarded statuses are the doctor phases, frontiers and terminals")


def _assert_graph() -> None:
    value = _journal()
    for status in sorted(ABANDONABLE_STATUSES):
        prepared = _journal()
        current: dict[str, Any] = prepared
        for step in ("operation_published", "target_fetched", "source_applying"):
            if step == status or current["status"] == status:
                break
            current = cast(dict[str, Any], advance_update_journal(current, status=step, stage_id=step, note="ok"))
        abandoned = advance_update_journal(current, status="abandoned", stage_id="abandoned", note="op", result={"kind": "abandoned", "reason_code": "operator_abandon", "repair": "No target byte changed."})
        _check(abandoned["status"] == "abandoned", f"abandon legal from {current['status']}")
    advanced = _to_runtime_advanced(value)
    _check(advanced["status"] == "runtime_advanced", "reached runtime_advanced")
    _expect(StateConflictError, lambda: advance_update_journal(advanced, status="abandoned", stage_id="abandoned", note="x", result={"kind": "abandoned", "reason_code": "operator_abandon", "repair": "n"}), "abandon past the fast-forward accepted")
    _expect(StateConflictError, lambda: advance_update_journal(advanced, status="blocked", stage_id="blocked", note="x", result={"kind": "blocked", "reason_code": "x", "repair": "y"}), "runtime_advanced terminalised")
    verifying = advance_update_journal(advanced, status="doctor_verifying", stage_id="doctor_verifying", note="doctor")
    _expect(StateConflictError, lambda: advance_update_journal(verifying, status="blocked", stage_id="blocked", note="x", result={"kind": "blocked", "reason_code": "x", "repair": "y"}), "doctor_verifying has no edge to blocked")
    incomplete = advance_update_journal(verifying, status="doctor_incomplete", stage_id="doctor_incomplete", note="x")
    rerun = advance_update_journal(incomplete, status="doctor_verifying", stage_id="doctor_verifying", note="again")
    verified = advance_update_journal(rerun, status="doctor_verified", stage_id="doctor_verified", note="ok")
    _expect(StateConflictError, lambda: advance_update_journal(verified, status="promoted", stage_id="promoted", note="skip", result={"kind": "promoted", "reason_code": "verified", "repair": "n"}), "promoting skipped")
    promoting = advance_update_journal(verified, status="promoting", stage_id="promoting", note="cas")
    _expect(StateError, lambda: advance_update_journal(promoting, status="promoted", stage_id="promoted", note="no result"), "promoted without a result accepted")
    promoted = advance_update_journal(promoting, status="promoted", stage_id="promoted", note="done", result={"kind": "promoted", "reason_code": "verified", "repair": "No operator action is required."})
    _check(promoted["status"] == "promoted" and promoted["result"] is not None, "promoted terminal with a result")
    _expect(StateConflictError, lambda: advance_update_journal(promoted, status="doctor_verifying", stage_id="x", note="x"), "promoted is terminal")


def _assert_retirement() -> None:
    prepared = _journal()
    published = advance_update_journal(prepared, status="operation_published", stage_id="operation_published", note="p")
    blocked = advance_update_journal(published, status="blocked", stage_id="blocked", note="fetch failed", result={"kind": "blocked", "reason_code": "history_diverged", "repair": "reconcile"})
    _expect(StateConflictError, lambda: retire_update_journal(published, head_observed="1" * 40, reason="operator_abandon"), "nonterminal retired")
    retired = retire_update_journal(blocked, head_observed="1" * 40, reason="operator_abandon")
    _check(is_retired(retired) and retired["status"] == "blocked" and retired["result"] == blocked["result"] and retired["attempts"] == blocked["attempts"], "retirement leaves status, result and attempts untouched")
    _expect(StateConflictError, lambda: retire_update_journal(retired, head_observed="1" * 40, reason="again"), "double retirement accepted")
    with TemporaryDirectory() as temporary:
        path = Path(temporary) / "s" / "o" / "i" / "opr.json"
        write_update_journal(path, None, prepared)
        write_update_journal(path, prepared, published)
        write_update_journal(path, published, blocked)
        write_update_journal(path, blocked, retired)
        _check(read_update_journal(path) == retired, "retirement persisted with a same-status write")
        forged = {**retired, "retirement": {**cast(dict[str, Any], retired["retirement"]), "reason": "forged"}}
        _expect(StateConflictError, lambda: write_update_journal(path, retired, cast(dict[str, Any], forged)), "retirement rewrite accepted")


def _assert_operation_types() -> None:
    value = _journal()
    for status in ("operation_published", "target_fetched", "source_applying", "source_advanced"):
        value = cast(dict[str, Any], advance_update_journal(value, status=status, stage_id=status, note="ok"))
    bad = _row("x", "existing::dependencies.reconcile", "not_a_type")
    _expect(StateError, lambda: record_runtime_approval(value, fingerprint="sha256:" + "e" * 64, planned_actions=("a",), strategy="single_color_restart", forward_only_boundary=None, operations=(bad,), note="x"), "unknown operation type accepted")
    rows = (_row("dependencies_reconcile", "existing::dependencies.reconcile", V3_UNCLASSIFIED), _row("lifecycle_restart_single_color", "existing::lifecycle.restart_single_color", "process_lifecycle"))
    planned = cast(dict[str, Any], record_runtime_approval(value, fingerprint="sha256:" + "e" * 64, planned_actions=("a",), strategy="single_color_restart", forward_only_boundary=None, operations=rows, note="x"))
    classified = cast(dict[str, Any], classify_v3_rows(planned, {"dependencies_reconcile": OperationType.CLOSURE_REPAIR.value}))
    types = {row["operation_id"]: row["operation_type"] for row in classified["runtime_operations"]}
    _check(types == {"dependencies_reconcile": "closure_repair", "lifecycle_restart_single_color": "process_lifecycle"}, "one-shot classification of the v3 placeholder")
    _check(classify_v3_rows(classified, {}) is classified, "nothing to classify returns the same document")
    _expect(StateError, lambda: classify_v3_rows(planned, {}), "unclassifiable v3 row accepted")
    with TemporaryDirectory() as temporary:
        path = Path(temporary) / "s" / "o" / "i" / "opr.json"
        write_update_journal(path, None, _journal())
        current = _journal()
        for status in ("operation_published", "target_fetched", "source_applying", "source_advanced"):
            nxt = advance_update_journal(current, status=status, stage_id=status, note="ok")
            write_update_journal(path, current, nxt)
            current = nxt
        planned = cast(dict[str, Any], record_runtime_approval(current, fingerprint="sha256:" + "e" * 64, planned_actions=("a",), strategy="single_color_restart", forward_only_boundary=None, operations=rows, note="x"))
        write_update_journal(path, current, planned)
        classified = cast(dict[str, Any], classify_v3_rows(planned, {"dependencies_reconcile": OperationType.CLOSURE_REPAIR.value}))
        write_update_journal(path, planned, classified)
        retyped = {**classified, "runtime_operations": [{**classified["runtime_operations"][0], "operation_type": "plugin_cache_refresh"}, classified["runtime_operations"][1]]}
        _expect(StateConflictError, lambda: write_update_journal(path, classified, cast(dict[str, Any], retyped)), "operation type change after classification accepted")


_V4_ONLY = frozenset({"recovers", "source_mode", "retirement"})
_V5_ONLY = frozenset({"local_state"})


def _v3_document() -> dict[str, Any]:
    advanced = _to_runtime_advanced(_journal())
    v3 = {key: value for key, value in advanced.items() if key not in _V4_ONLY | _V5_ONLY}
    v3["schema_version"] = 3
    rows = [{key: value for key, value in row.items() if key != "operation_type"} for row in v3["runtime_operations"]]
    rows.append({"operation_id": "runtime_readiness", "operation_ref": "existing::runtime.readiness", "stage": "lifecycle", "status": "verified", "idempotency_key": "sha256:" + "2" * 64, "mutation_class": "process_lifecycle", "rollback_class": "reversible", "attempts": []})
    v3["runtime_operations"] = rows
    return v3


def _without(document: dict[str, Any], keys: frozenset[str]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key not in keys}


def _assert_v3_upgrade() -> None:
    v3 = _v3_document()
    upgraded = parse_update_journal_bytes(json.dumps(v3).encode())
    _check((upgraded["schema_version"], upgraded["recovers"], upgraded["source_mode"], upgraded["retirement"]) == (5, None, "advance", None), "v3 -> v4 keys added (then v4 -> v5)")
    types = {cast(dict[str, Any], row)["operation_id"]: cast(dict[str, Any], row)["operation_type"] for row in cast(list[Any], upgraded["runtime_operations"])}
    _check(types == {"dependencies_reconcile": V3_UNCLASSIFIED, "runtime_readiness": SYNTHESISED_OPERATION_TYPES["existing::runtime.readiness"].value}, f"declared rows wait for the bundle, synthesised rows are typed by ref: {types}")
    _check(_without(cast(dict[str, Any], upgraded), _V4_ONLY | _V5_ONLY | {"schema_version", "runtime_operations"}) == _without(v3, frozenset({"schema_version", "runtime_operations"})), "every other v3 field is untouched")
    v2 = _without(_journal(), _V4_ONLY | _V5_ONLY | {"runtime_approval", "runtime_operations"})
    v2["schema_version"] = 2
    _check(parse_update_journal_bytes(json.dumps(v2).encode())["schema_version"] == 5, "v2 -> v5 still upgrades")
    _assert_v4_upgrade()


def _assert_v4_upgrade() -> None:
    """Step 7 section 6.7 (D2): the v4 -> v5 sibling of the v3 -> v4 fixture -- ``local_state`` with empty snapshots."""
    advanced = _to_runtime_advanced(_journal())
    v4 = {key: value for key, value in advanced.items() if key not in _V5_ONLY}
    v4["schema_version"] = 4
    upgraded = parse_update_journal_bytes(json.dumps(v4).encode())
    empty = {"preserved_tracked_paths": [], "committed_inventory": [], "local_state_commitment": None, "preserved_surface": []}
    _check(upgraded["schema_version"] == 5 and upgraded["local_state"] == {"baseline": empty, "current": empty, "revisions": []}, "v4 -> v5 adds local_state with empty snapshots (the clean-tree frontier's meaning)")
    _check(_without(cast(dict[str, Any], upgraded), _V5_ONLY | {"schema_version"}) == _without(v4, frozenset({"schema_version"})), "every other v4 field is untouched")
    _expect(StateError, lambda: parse_update_journal_bytes(json.dumps({**v4, "schema_version": 5}).encode()), "a v5 document without local_state accepted")
    tampered = {**advanced, "local_state": {**advanced["local_state"], "current": {**empty, "local_state_commitment": None, "committed_inventory": [{"path": "x", "kind": "file", "mode": "0644", "size": 1}]}}}
    _expect(StateError, lambda: parse_update_journal_bytes(json.dumps(tampered).encode()), "a null commitment with a non-empty committed inventory accepted")
    landed = [(Path(__file__).resolve().parent / f"{name}.py").exists() for name in ("update_execution_smoke", "update_runtime_execution_smoke")]
    _check(all(landed), "the landed Step-4/5 smokes still exist and read v4 (run separately)")


def main() -> int:
    _assert_shape_and_keys()
    _assert_graph()
    _assert_retirement()
    _assert_operation_types()
    _assert_v3_upgrade()
    print(f"update_journal_v4_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
