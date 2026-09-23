"""Closed update journal (v2 legs kept, v3 runtime legs added, v4 keys carried): shape, status graph, immutable prefixes, read-back."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]
from solet_manager.errors import StateConflictError, StateError  # noqa: E402
from solet_manager.update_journal import (  # noqa: E402
    advance_update_journal,
    create_update_journal,
    parse_update_journal_bytes,
    read_update_journal,
    write_update_journal,
)


def _journal() -> dict[str, object]:
    return create_update_journal(
        operation_id="opr_" + "a" * 32,
        instance_id="ins_" + "b" * 32,
        fingerprint="sha256:" + "c" * 64,
        baseline_commit="1" * 40,
        baseline_tree="2" * 40,
        branch="main",
        candidate_descriptor_digest="sha256:" + "d" * 64,
        candidate_commit="e" * 40,
        candidate_tree="f" * 40,
        candidate_tag="r2",
        candidate_contract_digest="sha256:" + "9" * 64,
        receipt_digest="sha256:" + "0" * 64,
        planned_actions=("target.fetch_exact_candidate", "target.fast_forward_exact_candidate"),
        timestamp="2026-09-18T00:00:00Z",
    )


def _expect_error(kind: type[Exception], action: object, label: str) -> None:
    try:
        action()  # type: ignore[operator]
    except kind:
        return
    raise AssertionError(label)


def _assert_shape() -> None:
    value = _journal()
    assert parse_update_journal_bytes(json.dumps(value).encode()) == value
    assert value["status"] == "prepared" and value["attempts"] == [] and value["result"] is None
    invalid = (
        b"{}",
        b"[]",
        json.dumps({**value, "kind": "import"}).encode(),
        json.dumps({**value, "planned_actions": [1]}).encode(),
        json.dumps({**value, "status": "verified"}).encode(),
        json.dumps({**value, "rollback_class": "manager_state_only"}).encode(),
        json.dumps({**value, "result": {"kind": "blocked", "reason_code": "x", "repair": "y"}}).encode(),
        json.dumps({**value, "candidate": {**value["candidate"], "contract_digest": "sha256:" + "d" * 64}}).encode(),  # type: ignore[dict-item]
    )
    for raw in invalid:
        _expect_error(StateError, lambda raw=raw: parse_update_journal_bytes(raw), "invalid update journal accepted")


def _assert_status_graph() -> None:
    value = _journal()
    for status in ("operation_published", "target_fetched", "source_applying", "source_advanced"):
        value = advance_update_journal(value, status=status, stage_id=status, note="ok", timestamp="2026-09-18T00:00:01Z")
        assert value["status"] == status
    attempts = value["attempts"]
    assert isinstance(attempts, list) and [item["attempt"] for item in attempts] == [0, 1, 2, 3]  # type: ignore[index]
    _expect_error(StateConflictError, lambda: advance_update_journal(value, status="blocked", stage_id="blocked", note="", result={"kind": "blocked", "reason_code": "x", "repair": "y"}), "source_advanced advanced further")
    prepared = _journal()
    _expect_error(StateConflictError, lambda: advance_update_journal(prepared, status="source_applying", stage_id="skip", note=""), "stage skipped")
    _expect_error(StateError, lambda: advance_update_journal(prepared, status="blocked", stage_id="blocked", note=""), "blocked without result accepted")
    blocked = advance_update_journal(prepared, status="blocked", stage_id="blocked", note="drift", result={"kind": "blocked", "reason_code": "probe_drift", "repair": "preview again"})
    assert blocked["status"] == "blocked" and blocked["result"] == {"kind": "blocked", "reason_code": "probe_drift", "repair": "preview again"}


def _assert_persistence() -> None:
    with TemporaryDirectory() as temporary:
        path = Path(temporary) / "state" / "operations" / "ins" / "opr.json"
        first = _journal()
        write_update_journal(path, None, first)
        assert read_update_journal(path) == first
        second = advance_update_journal(first, status="operation_published", stage_id="operation_published", note="pointer")
        _expect_error(StateConflictError, lambda: write_update_journal(path, None, second), "non-prepared initial write accepted")
        write_update_journal(path, first, second)
        forged = {**second, "attempts": [{**second["attempts"][0], "note": "rewritten"}]}  # type: ignore[index]
        _expect_error(StateConflictError, lambda: write_update_journal(path, second, forged), "attempt history rewrite accepted")
        drifted = {**second, "candidate": {**second["candidate"], "commit": "3" * 40}}  # type: ignore[dict-item]
        _expect_error(StateConflictError, lambda: write_update_journal(path, second, drifted), "immutable identity change accepted")
        assert read_update_journal(path) == second


def _assert_v2_upgrade(advanced: dict[str, object]) -> None:
    later_only = {"runtime_approval", "runtime_operations", "recovers", "source_mode", "retirement", "local_state"}
    v2 = {key: value for key, value in advanced.items() if key not in later_only}
    v2["schema_version"] = 2
    upgraded = parse_update_journal_bytes(json.dumps(v2).encode())
    added = (upgraded["schema_version"], upgraded["runtime_approval"], upgraded["runtime_operations"], upgraded["recovers"], upgraded["source_mode"], upgraded["retirement"])
    assert added == (5, None, [], None, "advance", None)
    empty = {"preserved_tracked_paths": [], "committed_inventory": [], "local_state_commitment": None, "preserved_surface": []}
    assert upgraded["local_state"] == {"baseline": empty, "current": empty, "revisions": []}
    assert {k: v for k, v in upgraded.items() if k not in later_only | {"schema_version"}} == {k: v for k, v in v2.items() if k != "schema_version"}


def _assert_v3_persistence(row: dict[str, object]) -> None:
    from solet_manager.update_journal import record_operation_attempt, record_runtime_approval  # noqa: PLC0415

    with TemporaryDirectory() as temporary:
        path = Path(temporary) / "s" / "o" / "i" / "opr.json"
        write_update_journal(path, None, _journal())
        write_update_journal(path, _journal(), advance_update_journal(_journal(), status="operation_published", stage_id="operation_published", note="p"))
        current = read_update_journal(path)
        for status in ("target_fetched", "source_applying", "source_advanced"):
            nxt = advance_update_journal(current, status=status, stage_id=status, note="ok")
            write_update_journal(path, current, nxt)
            current = nxt
        nxt = record_runtime_approval(current, fingerprint="sha256:" + "e" * 64, planned_actions=("a",), strategy="single_color_restart", forward_only_boundary=None, operations=(row,), note="approved")
        write_update_journal(path, current, nxt)
        current = nxt
        forged = {**current, "runtime_approval": {**current["runtime_approval"], "fingerprint": "sha256:" + "0" * 64}}  # type: ignore[dict-item]
        _expect_error(StateConflictError, lambda: write_update_journal(path, current, forged), "runtime approval rewrite accepted")
        nxt = advance_update_journal(current, status="dependencies_applying", stage_id="dependencies_applying", note="entered")
        write_update_journal(path, current, nxt)
        current = nxt
        nxt = record_operation_attempt(current, "dependencies_reconcile", phase="apply", checkpoint_status="applied", status="applied", evidence={"request_id": "r1"})
        write_update_journal(path, current, nxt)
        dropped = {**nxt, "runtime_operations": [{**nxt["runtime_operations"][0], "attempts": []}]}  # type: ignore[index]
        _expect_error(StateConflictError, lambda: write_update_journal(path, nxt, dropped), "operation attempt history rewrite accepted")
        removed = {**nxt, "runtime_operations": []}
        _expect_error(StateConflictError, lambda: write_update_journal(path, nxt, removed), "operation row removal accepted")
        assert read_update_journal(path) == nxt


def _assert_v3_runtime() -> None:
    """Step-5 additions: v2 upgrade, runtime approval immutability, per-operation attempt prefixes."""
    from solet_manager.update_journal import (  # noqa: PLC0415
        FRONTIER_STATUSES,
        RUNTIME_STATUSES,
        operation_row,
        record_operation_attempt,
        record_runtime_approval,
    )

    advanced = _journal()
    for status in ("operation_published", "target_fetched", "source_applying", "source_advanced"):
        advanced = advance_update_journal(advanced, status=status, stage_id=status, note="ok", timestamp="2026-09-18T00:00:01Z")
    _assert_v2_upgrade(advanced)
    _expect_error(StateError, lambda: parse_update_journal_bytes(json.dumps({**advanced, "status": "runtime_planned"}).encode()), "Step-5 status without a runtime approval accepted")
    row = {"operation_id": "dependencies_reconcile", "operation_ref": "existing::dependencies.reconcile", "stage": "dependencies", "status": "pending", "idempotency_key": "sha256:" + "1" * 64, "mutation_class": "venv", "rollback_class": "reversible", "operation_type": "closure_repair"}
    planned = record_runtime_approval(advanced, fingerprint="sha256:" + "e" * 64, planned_actions=("dependencies_reconcile:pip.install",), strategy="single_color_restart", forward_only_boundary=None, operations=(row,), note="approved", timestamp="2026-09-18T00:00:02Z")
    assert planned["status"] == "runtime_planned" and planned["runtime_approval"]["fingerprint"] == "sha256:" + "e" * 64  # type: ignore[index]
    _expect_error(StateConflictError, lambda: record_runtime_approval(planned, fingerprint="sha256:" + "f" * 64, planned_actions=("x",), strategy="router_cutover", forward_only_boundary=None, operations=(), note="again"), "second runtime approval accepted")
    _expect_error(StateConflictError, lambda: record_runtime_approval(_journal(), fingerprint="sha256:" + "e" * 64, planned_actions=("x",), strategy="router_cutover", forward_only_boundary=None, operations=(), note="early"), "runtime approval before source_advanced accepted")
    applying = advance_update_journal(planned, status="dependencies_applying", stage_id="dependencies_applying", note="entered", timestamp="2026-09-18T00:00:03Z")
    attempted = record_operation_attempt(applying, "dependencies_reconcile", phase="apply", checkpoint_status="applied", status="applied", evidence={"request_id": "r1"}, timestamp="2026-09-18T00:00:04Z")
    op = operation_row(attempted, "dependencies_reconcile")
    assert op is not None and op["status"] == "applied" and op["attempts"][0]["attempt"] == 0 and op["attempts"][0]["evidence_digest"].startswith("sha256:")  # type: ignore[index]
    _expect_error(StateConflictError, lambda: record_operation_attempt(attempted, "missing_op", phase="apply", checkpoint_status="applied", status="applied", evidence={}), "unknown operation attempt accepted")
    _assert_v3_persistence(row)
    terminal = advance_update_journal(applying, status="failed", stage_id="failed", note="x", result={"kind": "failed", "reason_code": "dependency_postcondition_contradiction", "repair": "step 6"})
    assert terminal["status"] == "failed"
    full = planned
    for status in RUNTIME_STATUSES[1:]:
        full = advance_update_journal(full, status=status, stage_id=status, note="ok", timestamp="2026-09-18T00:00:05Z")
    assert full["status"] == "runtime_advanced" and "runtime_advanced" in FRONTIER_STATUSES
    _expect_error(StateConflictError, lambda: advance_update_journal(full, status="blocked", stage_id="blocked", note="x", result={"kind": "blocked", "reason_code": "x", "repair": "y"}), "runtime_advanced terminalised")
    _expect_error(StateConflictError, lambda: advance_update_journal(full, status="abandoned", stage_id="abandoned", note="x", result={"kind": "abandoned", "reason_code": "operator_abandon", "repair": "n"}), "runtime_advanced abandoned")


def main() -> int:
    _assert_shape()
    _assert_status_graph()
    _assert_persistence()
    _assert_v3_runtime()
    print("update_journal_smoke OK (v2 legs + v3 runtime legs + v4 keys)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
