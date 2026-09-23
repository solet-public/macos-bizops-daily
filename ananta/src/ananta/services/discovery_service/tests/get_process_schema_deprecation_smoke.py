#!/usr/bin/env python3
"""Phase 6 §4.6 — get_process_schema surfaces the deprecation tombstone (no pytest).

The Tier-2 overlay (``apply_deprecation``) sets a ``deprecation`` block on a
registry entry, and ``get_process_by_key`` returns it wholesale — but the
agent-facing ``get_process_schema`` verb projected a FIXED field set that
dropped it, so a migrating agent inspecting a schema could not see the
replacement key. This smoke proves the one-line projection fix:

* a deprecated process's schema now carries its ``deprecation`` block
  (replacement_key / superseded_date / migration_note / active_retrieval);
* a non-deprecated process's schema carries ``deprecation: None``;
* the pre-existing projected fields are unchanged;
* an invalid process_key format still errors (no regression).

Offline: a stub state service (the usage-stats table setup is try-wrapped) +
an in-memory ``processes`` map; the REAL ``get_process_schema`` runs.

Run:
    .venv/bin/python3 ananta/src/ananta/services/discovery_service/tests/get_process_schema_deprecation_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.services.discovery_service.service import DiscoveryService  # noqa: E402

# Deliberately-unregistered fixture keys (this smoke exercises projection over
# in-memory entries, not the live registry). The line-scoped negative-fixture
# marker keeps the whole-tree integration gate's C3.1 detector from flagging
# them as registry drift.
_DEP_KEY = "service_interface::thinking_service::old_verb"  # wint:negative-fixture
_LIVE_KEY = "service_interface::thinking_service::live_verb"  # wint:negative-fixture
_DEPRECATION = {
    "replacement_key": "service_interface::thinking_service::new_verb",  # wint:negative-fixture
    "superseded_date": "2026-07-02",
    "migration_note": "call the replacement with the same arguments",
    "active_retrieval": False,
}


class _StubState:
    """Minimal StateServiceProtocol stub (usage-stats setup is try-wrapped)."""

    def read_state(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"data": {"result": {"records": []}}}

    def write_state(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"action_status": "completed", "data": {}}


class Checker:
    def __init__(self, title: str) -> None:
        self.title = title
        self.passed = 0
        self.failed: list[str] = []

    def check(self, condition: object, label: str) -> None:
        if condition:
            self.passed += 1
        else:
            self.failed.append(label)

    def report(self) -> bool:
        total = self.passed + len(self.failed)
        print(f"\n=== {self.title} ===")
        print(f"passed {self.passed}/{total}")
        for f in self.failed:
            print(f"  FAIL: {f}")
        return not self.failed


def _build_service() -> DiscoveryService:
    svc = DiscoveryService(
        app_home=str(_REPO_ROOT),
        state_service=_StubState(),  # type: ignore[arg-type]
        plugin_manager=None,
        process_registry=None,
        embedding_service=None,
        vector_service=None,
    )
    svc.processes = {
        _DEP_KEY: {
            "description": "the old, deprecated verb",
            "invocation_schema": {"type": "object", "properties": {}},
            "is_long_running": False,
            "deprecation": _DEPRECATION,
        },
        _LIVE_KEY: {
            "description": "a live verb",
            "invocation_schema": {"type": "object", "properties": {}},
            "is_long_running": False,
        },
    }
    return svc


def _data(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data")
    return data if isinstance(data, dict) else {}


def main() -> int:
    c = Checker("Phase 6 §4.6 get_process_schema deprecation projection")
    svc = _build_service()

    dep = _data(svc.get_process_schema(_DEP_KEY))
    c.check(dep.get("deprecation") == _DEPRECATION, "deprecated schema surfaces the tombstone block")
    c.check(dep.get("process_key") == _DEP_KEY, "process_key still projected")
    c.check(dep.get("description") == "the old, deprecated verb", "description still projected")
    c.check("invocation_schema" in dep, "invocation_schema still projected")
    c.check(dep.get("is_long_running") is False, "is_long_running still projected")

    live = _data(svc.get_process_schema(_LIVE_KEY))
    c.check("deprecation" in live, "non-deprecated schema carries the deprecation key")
    c.check(live.get("deprecation") is None, "non-deprecated deprecation is None")

    bad = svc.get_process_schema("not_a_valid_key")
    c.check(bad.get("action_status") == "error", "invalid process_key format still errors")

    return 0 if c.report() else 1


if __name__ == "__main__":
    raise SystemExit(main())
