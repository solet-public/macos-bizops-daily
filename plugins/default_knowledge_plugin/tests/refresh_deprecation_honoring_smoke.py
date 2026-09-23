#!/usr/bin/env python3
"""Phase 6 §4.6 — live-refresh honors process deprecation (no pytest).

The Tier-2 deprecation overlay ran only at RESTART (the full overlay merge);
the lighter live-refresh merge (``build_refresh_updates``) dropped a
``deprecation`` block, so a deprecation edit did not take effect until a
restart. This smoke proves the rider that closes that gap:

* ``active_retrieval: false`` folds ``deprecation`` + DERIVES
  ``is_discoverable: false`` into the refresh updates (so the subsequent
  discovery rebuild demotes the process from process_search);
* ``active_retrieval`` defaults true → the block is surfaced but
  ``is_discoverable`` is NOT forced (deprecated-but-still-searchable);
* no block → no deprecation/is_discoverable keys leak into the updates;
* a malformed block fails loud (matches the restart path's fail-fast);
* the base refresh fields (display_name/description/embedding_description)
  still merge alongside a deprecation block.

The orchestrator's ``apply_knowledge_base_updates`` merges every field of the
updates dict onto the live entry (no whitelist) and then triggers a full
discovery rebuild that honors ``is_discoverable`` — verified by reading
``process_registry_manager.apply_knowledge_base_updates`` — so the updates
this smoke asserts are exactly what demotes a refreshed process from search.

Offline: pure ``build_refresh_updates`` over hand-built JSON + registry dicts.

Run:
    .venv/bin/python3 plugins/default_knowledge_plugin/tests/refresh_deprecation_honoring_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_knowledge_plugin" / "src"))

from ananta.error_handling import FrameworkError  # noqa: E402
from default_knowledge_plugin.kb_process_registry import build_refresh_updates  # noqa: E402

# Deliberately-unregistered fixture keys (this smoke exercises the pure refresh
# update-builder, not the live registry). The line-scoped negative-fixture
# marker keeps the whole-tree integration gate's C3.1 detector from flagging
# them as registry drift.
_PK = "service_interface::thinking_service::old_verb"  # wint:negative-fixture
_REPLACEMENT = "service_interface::thinking_service::new_verb"  # wint:negative-fixture


def _registry() -> dict[str, object]:
    return {"processes": {_PK: {"is_discoverable": True, "description": "old"}}}


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

    def raises(self, fn: Any, label: str) -> None:
        try:
            fn()
        except FrameworkError:
            self.passed += 1
        except Exception as exc:  # noqa: BLE001
            self.failed.append(f"{label}: wrong exception {type(exc).__name__}")
        else:
            self.failed.append(f"{label}: expected FrameworkError, none raised")

    def report(self) -> bool:
        total = self.passed + len(self.failed)
        print(f"\n=== {self.title} ===")
        print(f"passed {self.passed}/{total}")
        for f in self.failed:
            print(f"  FAIL: {f}")
        return not self.failed


def main() -> int:
    c = Checker("Phase 6 §4.6 live-refresh deprecation honoring")

    # active_retrieval:false → deprecation surfaced + is_discoverable derived false
    block = {
        "replacement_key": _REPLACEMENT,
        "superseded_date": "2026-07-02",
        "migration_note": "call the replacement with the same args",
        "active_retrieval": False,
    }
    upd = build_refresh_updates({"process_key": _PK, "deprecation": block}, _PK, _registry())
    c.check(upd.get("deprecation") == block, "deprecation block folded into refresh updates")
    c.check(upd.get("is_discoverable") is False, "active_retrieval:false derives is_discoverable:false")

    # active_retrieval default true → surfaced but NOT demoted
    upd2 = build_refresh_updates(
        {"process_key": _PK, "deprecation": {"migration_note": "soon"}}, _PK, _registry()
    )
    c.check("deprecation" in upd2, "deprecated-but-active block still surfaced")
    c.check("is_discoverable" not in upd2, "active_retrieval default true does not force is_discoverable")

    # no block → nothing leaks
    upd3 = build_refresh_updates({"process_key": _PK, "description": "d"}, _PK, _registry())
    c.check("deprecation" not in upd3, "no block => no deprecation key")
    c.check("is_discoverable" not in upd3, "no block => no is_discoverable key")
    c.check(upd3.get("description") == "d", "base refresh fields still merge")

    # base fields merge alongside a deprecation block
    upd4 = build_refresh_updates(
        {"process_key": _PK, "display_name": "Old Verb", "deprecation": block}, _PK, _registry()
    )
    c.check(upd4.get("display_name") == "Old Verb", "display_name merges alongside deprecation")
    c.check(upd4.get("is_discoverable") is False, "deprecation still derived alongside base fields")

    # malformed blocks fail loud (matches the restart path)
    c.raises(
        lambda: build_refresh_updates({"process_key": _PK, "deprecation": "nope"}, _PK, _registry()),
        "non-object deprecation raises",
    )
    c.raises(
        lambda: build_refresh_updates(
            {"process_key": _PK, "deprecation": {"active_retrieval": "no"}}, _PK, _registry()
        ),
        "non-bool active_retrieval raises",
    )

    return 0 if c.report() else 1


if __name__ == "__main__":
    raise SystemExit(main())
