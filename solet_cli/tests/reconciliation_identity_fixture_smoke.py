#!/usr/bin/env python3
"""Integrity controls for Lane I's captured reconciliation-identity fixtures."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

_CHECKS = 0
_FIXTURES = Path(__file__).parent / "fixtures" / "reconciliation_identity"
_CAVEAT = "tests must not assume pre-1.40 convergence behavior in fixture-3 material."
_REQUIRED_FILES = {
    "registry_instances.json",
    "transaction_bizopsb15.json",
    "install_state.json",
    "terminal_contract_reconciliation_receipt.json",
    "contract_bundle/macos_setup_flow.json",
    "contract_bundle/setup_adapter_envelope.schema.json",
    "contract_bundle/setup_answers.schema.json",
    "contract_bundle/setup_flow.schema.json",
    "contract_bundle/setup_journal.schema.json",
}
_SNAPSHOTS = {
    "bizopsb15_pre_adapter_refresh": "betty-vm-snapshot-bizopsb15-r20-reconciled-stranded-adapter-session-roots-enoent-20260902T200929Z",
    "bizopsb15_adapter_refreshed_contract_stale": "betty-vm-snapshot-bizopsb15-r20-adapter-refreshed-v2-journal-platform-stale-20260902T203709Z",
    "bizopsb15_postcutover_convergefail": "betty-vm-b15-postcutover-convergefail-20260902T2125Z",
}


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _load(path: Path) -> dict[str, object]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise AssertionError(f"{path} must contain a JSON object")
    return loaded


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_snapshot_fixture(name: str, snapshot_name: str) -> None:
    root = _FIXTURES / name
    manifest = _load(root / "provenance.json")
    _check(manifest.get("snapshot_name") == snapshot_name, f"{name}: snapshot provenance")
    _check(manifest.get("realism_caveat") == _CAVEAT, f"{name}: required 1.40 caveat")
    files = manifest.get("files")
    _check(isinstance(files, list), f"{name}: provenance file list")
    entries = {entry.get("fixture_path"): entry for entry in files if isinstance(entry, dict)}
    _check(set(entries) == _REQUIRED_FILES, f"{name}: complete captured file set")
    for fixture_path, entry in entries.items():
        _check(isinstance(fixture_path, str), f"{name}: fixture path type")
        source_path = entry.get("in_vm_path")
        _check(
            isinstance(source_path, str) and source_path.startswith("/"),
            f"{name}: in-VM source path",
        )
        _check(entry.get("sha256") == _hash(root / fixture_path), f"{name}: hash {fixture_path}")

    transaction = _load(root / "transaction_bizopsb15.json")
    install_state = _load(root / "install_state.json")
    registry = _load(root / "registry_instances.json")
    instances = registry.get("instances")
    _check(isinstance(instances, dict), f"{name}: registry instances")
    record = instances.get("bizopsb15")
    _check(isinstance(record, dict), f"{name}: b15 registry record")
    for field in ("flow_id", "flow_source_revision"):
        _check(
            transaction.get(field) == install_state.get(field) == record.get(field),
            f"{name}: three-store {field}",
        )
    _check(
        transaction.get("seed_commit") == record.get("seed_commit"),
        f"{name}: seed commit agreement",
    )
    _check(
        transaction.get("flow_contract_digest") == record.get("flow_contract_digest"),
        f"{name}: digest agreement",
    )


def _assert_synthetic_cases() -> None:
    cases_document = _load(_FIXTURES / "synthetic_identity_cases.json")
    cases = cases_document.get("cases")
    _check(isinstance(cases, list), "synthetic cases list")
    indexed = {case.get("case_id"): case for case in cases if isinstance(case, dict)}
    expected_ids = {
        "selector_zero_destination",
        "selector_many_destination",
        "destination_answer_tightening",
        "verified_resolution_stage_reopening",
        "three_store_crash_boundary",
        "managed_tree_drift",
    }
    _check(set(indexed) == expected_ids, "complete synthetic identity case set")
    red = indexed["verified_resolution_stage_reopening"]
    expected = red.get("expected")
    _check(isinstance(expected, dict), "reopening red contract expectation")
    _check(
        expected.get("accepted_red_contract") is True,
        "reopening remains an explicit accepted red contract",
    )
    _check(expected.get("disposition") == "pending_newly_introduced", "reopening disposition")
    _check(expected.get("frontier_contains") == ["models"], "reopening frontier")


def main() -> None:
    for name, snapshot_name in _SNAPSHOTS.items():
        _assert_snapshot_fixture(name, snapshot_name)
    _assert_synthetic_cases()
    print(f"reconciliation identity fixture smoke: {_CHECKS} checks passed")


if __name__ == "__main__":
    main()
