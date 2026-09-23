#!/usr/bin/env python3
"""Unit smoke: the new ``tags`` param reaches the live service-interface schema.

Unified-memory-passthrough Slice 1(e). Slice 2 drains and hydrates through
``process_call`` on ``service_interface::memory_service::upsert_memory_by_tag`` and
``export_memories``, so backend-only support for ``tags`` would be a silent wire
no-op: the param must appear in the ``@service_interface_process`` decorator
metadata, which is exactly what ``process_schema`` / the registry build from
(``to_process_dict()['parameters']`` -> invocation_schema). This smoke asserts the
param is exposed on the callable surface — hermetically, by introspecting the
decorated interface class (no live solet, no restart needed).

Run::

    .venv/bin/python3 plugins/actr_memory_plugin/tests/actr_memory_tags_param_schema_parity_smoke.py
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "actr_memory_plugin" / "src"))

importlib.import_module("ananta.core.config.config_manager")
from ananta.core.actions.action_metadata import ParameterType  # noqa: E402
from ananta.services.memory_service.interfaces.public import MemoryServiceAPI  # noqa: E402

_passed = 0
_failed: list[str] = []

_PROCESS_JSON_DIR = REPO_ROOT / "ananta" / "knowledge_base" / "processes" / "memory_service"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _assert_tags_exposed(method_name: str) -> None:
    method = getattr(MemoryServiceAPI, method_name)
    metadata = getattr(method, "_service_interface_metadata", None)
    _check(metadata is not None, f"{method_name}: carries @service_interface_process metadata")
    if metadata is None:
        return

    _check("tags" in metadata.parameters, f"{method_name}: 'tags' is a declared decorator parameter")
    # to_process_dict() is what the registry -> process_schema surface is built from.
    wire_params = metadata.to_process_dict()["parameters"]
    _check("tags" in wire_params, f"{method_name}: 'tags' present in to_process_dict() (the wire schema)")

    tags_meta = metadata.parameters.get("tags")
    if tags_meta is not None:
        _check(tags_meta.type == ParameterType.LIST, f"{method_name}: 'tags' typed as LIST")
        _check(tags_meta.required is False, f"{method_name}: 'tags' is optional (required=False)")


def test_tags_param_exposed_on_both_verbs() -> None:
    _assert_tags_exposed("upsert_memory_by_tag")
    _assert_tags_exposed("export_memories")


def test_process_json_discovery_text_present() -> None:
    """The discovery-text side of both verbs stays complete (non-empty
    embedding_description) so retrieval surfaces the tag capability."""
    for name in ("upsert_memory_by_tag", "export_memories"):
        path = _PROCESS_JSON_DIR / f"{name}.json"
        _check(path.exists(), f"{name}.json exists")
        data = json.loads(path.read_text(encoding="utf-8"))
        _check(
            bool(data.get("embedding_description", "").strip()),
            f"{name}.json has a non-empty embedding_description",
        )


def main() -> int:
    print("=== actr_memory_tags_param_schema_parity_smoke ===")
    test_tags_param_exposed_on_both_verbs()
    test_process_json_discovery_text_present()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
