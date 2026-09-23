#!/usr/bin/env python3
"""Regression smoke for ``service_interface::state_service::write_state`` registration.

Background: on 2026-06-07 the platform exhibited a P0 failure where every
``vault_service::store*`` action returned ``vault.encryption_failed``. The
masked error was actually an ``action_factory`` lookup failure for
``service_interface::state_service::write_state`` — the verb existed as a
plain Python method on ``StateService`` and as a ``@platform_process``
verb on ``postgres_state_management_plugin`` (``write_state_action``), but
the **service-interface registration** had no counterpart: no
``@service_interface_process`` decorator on ``StateManagementAPI.write_state``
and no ``write_state.json`` process definition file.

Any consumer that submitted ``service_interface::state_service::write_state``
through the action queue hit ``ProcessRegistryError`` and surfaced as a
masked downstream failure (vault → ``vault.encryption_failed``; agent
messaging bridge open → ``process_call submit failed``).

This smoke asserts the registration is back so the regression cannot
recur silently:

  1. Every abstract method on ``StateManagementAPI`` carries the
     ``_service_interface_metadata`` attribute attached by the
     ``@service_interface_process`` decorator. Adding a new method
     without the decorator fails this gate.
  2. The KB process JSON files under ``ananta/knowledge_base/processes/state_service/``
     cover every abstract method one-to-one. Adding a method without the
     companion JSON fails this gate. Adding orphan JSON also fails (the
     declared ``process_key`` must resolve to a real abstract method).

Project policy: no pytest. Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.services.state_service.interfaces.public import StateManagementAPI  # noqa: E402

_PROCESSES_DIR = REPO_ROOT / "ananta" / "knowledge_base" / "processes" / "state_service"
_EXPECTED_PROVIDER = "state_service"
_EXPECTED_PROCESS_KEY_PREFIX = "service_interface::state_service::"

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _abstract_method_names() -> list[str]:
    names: list[str] = []
    for name, member in inspect.getmembers(StateManagementAPI):
        if name.startswith("_"):
            continue
        if getattr(member, "__isabstractmethod__", False):
            names.append(name)
    return sorted(names)


def _case_decorator_attached_to_every_method() -> set[str]:
    print("\nCase 1: every abstract method carries @service_interface_process metadata")
    method_names = _abstract_method_names()
    _check(
        "write_state" in method_names,
        "StateManagementAPI declares an abstract `write_state` method",
    )
    decorated: set[str] = set()
    for name in method_names:
        method = getattr(StateManagementAPI, name)
        metadata = getattr(method, "_service_interface_metadata", None)
        _check(
            metadata is not None,
            f"{name}: has _service_interface_metadata attribute",
        )
        if metadata is None:
            continue
        _check(
            getattr(metadata, "provider", None) == _EXPECTED_PROVIDER,
            f"{name}: metadata.provider == 'state_service'",
        )
        _check(
            getattr(metadata, "name", None) == name,
            f"{name}: metadata.name matches method name",
        )
        decorated.add(name)
    return decorated


def _case_kb_json_one_to_one(decorated: set[str]) -> None:
    print("\nCase 2: KB process JSON files cover the decorated set one-to-one")
    json_paths = sorted(_PROCESSES_DIR.glob("*.json"))
    _check(
        len(json_paths) > 0,
        f"At least one JSON file in {_PROCESSES_DIR.relative_to(REPO_ROOT)}",
    )

    json_names: set[str] = set()
    for path in json_paths:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        process_key = payload.get("process_key", "")
        _check(
            isinstance(process_key, str) and process_key.startswith(_EXPECTED_PROCESS_KEY_PREFIX),
            f"{path.name}: process_key starts with '{_EXPECTED_PROCESS_KEY_PREFIX}'",
        )
        method_name = process_key.removeprefix(_EXPECTED_PROCESS_KEY_PREFIX)
        _check(
            path.stem == method_name,
            f"{path.name}: filename matches process_key suffix ('{method_name}')",
        )
        json_names.add(method_name)

    missing_json = decorated - json_names
    _check(
        not missing_json,
        f"every decorated method has a JSON file (missing: {sorted(missing_json) or 'none'})",
    )
    orphan_json = json_names - decorated
    _check(
        not orphan_json,
        f"no orphan JSON files without a decorated method (orphan: {sorted(orphan_json) or 'none'})",
    )


def _case_write_state_specific_invariants() -> None:
    print("\nCase 3: write_state-specific invariants (regression of the 2026-06-07 P0)")
    method = getattr(StateManagementAPI, "write_state", None)
    _check(method is not None, "write_state method exists on StateManagementAPI")
    if method is None:
        return
    metadata = getattr(method, "_service_interface_metadata", None)
    _check(metadata is not None, "write_state carries _service_interface_metadata")
    if metadata is None:
        return
    params = getattr(metadata, "parameters", {}) or {}
    _check("namespace" in params, "write_state declares 'namespace' parameter")
    _check("data" in params, "write_state declares 'data' parameter")

    json_path = _PROCESSES_DIR / "write_state.json"
    _check(json_path.exists(), f"{json_path.relative_to(REPO_ROOT)} exists")
    if not json_path.exists():
        return
    with json_path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    _check(
        payload.get("process_key") == "service_interface::state_service::write_state",
        "write_state.json process_key matches canonical service-interface key",
    )


def main() -> int:
    print("StateService write_state registration smoke")
    print("===========================================")
    decorated = _case_decorator_attached_to_every_method()
    _case_kb_json_one_to_one(decorated)
    _case_write_state_specific_invariants()

    print("\n-------------------------------------------")
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    if _failed:
        print("\nFailures:")
        for label in _failed:
            print(f"  - {label}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
