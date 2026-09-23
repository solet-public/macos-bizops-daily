#!/usr/bin/env python3
"""Smoke — `search` `name` + `process_key` scoping is reachable through the
service-interface surface (Phase-2 fix: schema parity for two pre-existing
plugin-only params).

`name` scopes search to one knowledge base (what makes a default-search-excluded
KB like workbench reachable via the W5 bypass); `process_key` is the tier-1
exact process-key match. Both existed only on the plugin layer / method signature;
the decorated service-interface API's `parameters` dict — the real schema source —
never published either, so `process_schema` omitted them and MCP silently dropped
the arguments. This smoke pins, for each param, the three published surfaces: the
decorated process metadata declares it, the KnowledgeService wrapper forwards it to
the backend, and the process JSON surfaces it for discovery.

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run:
    .venv/bin/python3 plugins/default_knowledge_plugin/tests/search_name_scope_smoke.py
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.services.knowledge_service.interfaces.search import (  # noqa: E402
    KnowledgeSearchAPI,
)
from ananta.services.knowledge_service.knowledge_service_file_ops import (  # noqa: E402
    KnowledgeFileOpsWrapper,
)
from ananta.services.knowledge_service.knowledge_service_search import (  # noqa: E402
    KnowledgeSearchWrapper,
)

_SEARCH_JSON = (
    REPO_ROOT / "ananta" / "knowledge_base" / "processes" / "knowledge_service" / "search.json"
)

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


class _FakeBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def search(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"status": "success", "data": {"results": [], "count": 0}}


class _WrapperUnderTest(KnowledgeSearchWrapper):
    def __init__(self, backend: _FakeBackend) -> None:
        self._backend = backend

    def _get_backend(self) -> Any:
        return self._backend


def test_decorated_api_declares_name() -> None:
    meta = getattr(KnowledgeSearchAPI.search, "_service_interface_metadata", None)
    _check(meta is not None, "decorated search carries service-interface metadata")
    params = getattr(meta, "parameters", {}) if meta is not None else {}
    _check("name" in params, "decorated search parameters DECLARE 'name' (published in process_schema)")
    name_desc = getattr(params.get("name"), "description", "") if "name" in params else ""
    _check(
        "workbench" in name_desc and "scope" in name_desc.lower(),
        "'name' parameter description explains scoping + reaching excluded KBs",
    )
    _check(
        getattr(params.get("name"), "required", True) is False,
        "'name' is optional (required=False)",
    )


def test_wrapper_forwards_name() -> None:
    sig = inspect.signature(KnowledgeSearchWrapper.search)
    _check("name" in sig.parameters, "KnowledgeSearchWrapper.search signature has 'name'")
    backend = _FakeBackend()
    wrapper = _WrapperUnderTest(backend)
    wrapper.search("q", name="workbench")
    _check(len(backend.calls) == 1, "wrapper.search delegates to the backend")
    _check(
        backend.calls and backend.calls[0].get("name") == "workbench",
        "wrapper forwards name='workbench' to the backend (not dropped)",
    )


def test_search_json_surfaces_name() -> None:
    data = json.loads(_SEARCH_JSON.read_text(encoding="utf-8"))
    blob = f"{data.get('description', '')} {data.get('embedding_description', '')}".lower()
    _check("name" in blob and "scope" in blob, "search.json surfaces scoping by name")
    _check("workbench" in blob, "search.json names workbench as a reachable excluded KB")


def test_process_key_published_and_forwarded() -> None:
    # Same defect class as `name`: present on ABC/wrapper/plugin, was absent from
    # the decorator params (so tier-1 process-key search was MCP-unreachable).
    meta = getattr(KnowledgeSearchAPI.search, "_service_interface_metadata", None)
    params = getattr(meta, "parameters", {}) if meta is not None else {}
    _check("process_key" in params, "decorated search parameters DECLARE 'process_key'")
    pk_desc = getattr(params.get("process_key"), "description", "") if "process_key" in params else ""
    _check("process" in pk_desc.lower(), "'process_key' description explains tier-1 process-key match")
    backend = _FakeBackend()
    wrapper = _WrapperUnderTest(backend)
    wrapper.search("q", process_key="plugin::x::y")  # wint:negative-fixture
    _check(
        backend.calls and backend.calls[0].get("process_key") == "plugin::x::y",  # wint:negative-fixture
        "wrapper forwards process_key to the backend (not dropped)",
    )
    data = json.loads(_SEARCH_JSON.read_text(encoding="utf-8"))
    blob = f"{data.get('description', '')} {data.get('embedding_description', '')}".lower()
    _check("process_key" in blob or "process key" in blob, "search.json surfaces process_key")


def test_file_ops_wrapper_edit_still_threads_hash() -> None:
    # Guard: the sibling W12 five-surface change stays intact alongside this fix.
    sig = inspect.signature(KnowledgeFileOpsWrapper.edit_file)
    _check("expected_content_hash" in sig.parameters, "wrapper edit_file still threads expected_content_hash")
    _check(hasattr(KnowledgeFileOpsWrapper, "archive_file"), "wrapper still exposes archive_file")


def main() -> int:
    print("search name-scoping five-surface parity smoke")
    print("=============================================")
    test_decorated_api_declares_name()
    test_wrapper_forwards_name()
    test_search_json_surfaces_name()
    test_process_key_published_and_forwarded()
    test_file_ops_wrapper_edit_still_threads_hash()
    print(f"\nPASSED: {_passed}\nFAILED: {len(_failed)}")
    if _failed:
        for label in _failed:
            print(f"  - {label}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
