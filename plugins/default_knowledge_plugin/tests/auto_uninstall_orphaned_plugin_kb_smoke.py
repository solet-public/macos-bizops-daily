#!/usr/bin/env python3
"""Standalone smoke for the manifest-aware auto-uninstall pass (no pytest).

Validates the new ``manifest_plugin_set`` thread through
``kb_lifecycle.auto_install_knowledge_bases``:

* Owning-plugin resolution from a record's ``resolved_path`` (string
  form) correctly identifies ``plugins/<name>/knowledge_base/...`` and
  rejects unrelated paths.
* Conservative match: even if a path lives under ``plugins/<name>/``,
  the record is only treated as plugin-owned when its KB name equals
  ``<name>``.
* Real-filesystem ``_resolve_owning_plugin`` correctly traverses the
  ``knowledge_bases/<name> -> ../plugins/<name>/knowledge_base``
  symlink convention used by the platform.
* ``_uninstall_orphaned_plugin_kbs`` deletes the records (and forgets
  the memories) for plugin-owned KBs whose plugin is NOT in the
  manifest set, while leaving in-manifest plugin KBs AND non-plugin
  KBs (e.g., ``ananta_platform``) untouched.

Uses lightweight fake services so the smoke runs without Postgres or
the live actr_memory plugin. The fakes capture every state mutation
the production path makes, then the assertions read directly off
that capture.

Run:
    .venv/bin/python3 plugins/default_knowledge_plugin/tests/auto_uninstall_orphaned_plugin_kb_smoke.py
"""

from __future__ import annotations

import contextlib
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_knowledge_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from default_knowledge_plugin.constants import (  # noqa: E402
    TABLE_KNOWLEDGE_INSTALL,
    kb_id_tag,
)
from default_knowledge_plugin.kb_lifecycle import (  # noqa: E402
    _is_orphaned_plugin_candidate,
    _owning_plugin_from_path,
    _resolve_owning_plugin,
    _uninstall_orphaned_plugin_kbs,
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


class _FakeTxn:
    """Minimal txn capture (mirrors w5p_orphan_invariant_smoke.py)."""

    def __init__(self, records_ref: list[dict[str, Any]]) -> None:
        self.executed_sql: list[tuple[str, list[Any] | None]] = []
        self._records_ref = records_ref

    def execute(self, sql: str, params: list[Any] | None = None) -> None:
        self.executed_sql.append((sql, params))
        # Mimic the live behavior of DELETE FROM default_knowledge_plugin__knowledge_install
        # so a downstream read_state returns the post-delete state.
        if "DELETE FROM" in sql and "knowledge_install" in sql and params:
            name = params[0]
            self._records_ref[:] = [r for r in self._records_ref if r.get("name") != name]

    # Typed in-txn primitives (mirror StateTransaction). The K0 migration routes
    # the own-namespace install-record DELETE through `delete_records`; it
    # records the call AND replicates the same record-removal side-effect the
    # raw DELETE path above mimics, so downstream read_state sees post-delete.
    def delete_records(self, namespace: str, query: dict[str, Any]) -> int:
        table = query.get("table")
        filters = query.get("filters", {})
        vals = list(filters.values()) if isinstance(filters, dict) else None
        self.executed_sql.append((f"delete_records {namespace}__{table}", vals))
        if isinstance(filters, dict) and filters:
            before = len(self._records_ref)
            self._records_ref[:] = [
                r for r in self._records_ref
                if not all(r.get(k) == v for k, v in filters.items())
            ]
            return before - len(self._records_ref)
        return 0

    def write_state(self, namespace: str, data: dict[str, Any]) -> str:
        table = data.get("table")
        record = data.get("record")
        self.executed_sql.append((f"write_state {namespace}__{table}", None))
        if isinstance(record, dict):
            return str(record.get("id", f"{namespace}-fake"))
        return f"{namespace}-fake"

    def update_state(
        self, namespace: str, query: dict[str, Any], updates: dict[str, Any],
    ) -> int:
        table = query.get("table")
        self.executed_sql.append(
            (f"update_state {namespace}__{table}", list(updates.keys())),
        )
        return 1


class _FakeStateService:
    """Captures install records + the DELETE side-effect from uninstall_kb."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = list(records)
        self.executed_sql: list[tuple[str, list[Any] | None]] = []
        self.txns: list[_FakeTxn] = []

    @contextlib.contextmanager
    def transactional(self) -> Any:
        txn = _FakeTxn(records_ref=self._records)
        self.txns.append(txn)
        try:
            yield txn
        finally:
            self.executed_sql.extend(txn.executed_sql)

    def read_state(self, *, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace
        table = query["table"]
        filters = query.get("filters", {})
        matched = [
            r for r in self._records
            if all(r.get(k) == v for k, v in filters.items())
        ]
        return {
            "data": {
                "records": [dict(r) for r in matched],
                "table": table,
            }
        }

    def execute_sql(self, sql: str, params: list[Any] | None = None) -> dict[str, Any]:
        self.executed_sql.append((sql, params))
        if "DELETE FROM" in sql and params:
            name = params[0]
            self._records = [r for r in self._records if r.get("name") != name]
        return {"data": {"records": []}}


class _FakeMemoryService:
    """Captures the owning-service chunk-delete calls.

    Per D4 (operator-locked 2026-06-21): KB lifecycle deletes chunks via
    ``memory_service.delete_memories_by_tag('knowledge:<kb>')`` (which deletes
    the chunk-memories AND their embeddings, vector-first) best-effort outside
    the install-record transaction. This fake records the tags so the smoke can
    assert which KBs' chunks the production path deleted.
    """

    def __init__(self) -> None:
        self.deleted_tags: list[str] = []

    def delete_memories_by_tag(self, tag: str) -> dict[str, Any]:
        self.deleted_tags.append(tag)
        return {"action_status": "completed", "data": {"deleted_count": 0}}


def test_owning_plugin_from_path_string_extraction() -> None:
    _check(
        _owning_plugin_from_path(
            "/Users/x/Workspace/example/plugins/voice_embeddings_plugin/knowledge_base",
            "voice_embeddings_plugin",
        ) == "voice_embeddings_plugin",
        "plugin-owned path with matching kb_name resolves the plugin",
    )
    _check(
        _owning_plugin_from_path(
            "/Users/x/Workspace/example/plugins/voice_embeddings_plugin/knowledge_base",
            "ananta_platform",
        ) is None,
        "conservative match: mismatched kb_name returns None",
    )
    _check(
        _owning_plugin_from_path(
            "/Users/x/Workspace/example/ananta/knowledge_bases/ananta_platform",
            "ananta_platform",
        ) is None,
        "non-plugin path (no 'plugins/' component) returns None",
    )
    _check(
        _owning_plugin_from_path("", "anything") is None,
        "empty resolved_path returns None",
    )


def test_resolve_owning_plugin_through_symlink() -> None:
    with tempfile.TemporaryDirectory(prefix="kb_orphan_smoke_") as tmpdir:
        root = Path(tmpdir)
        plugins_dir = root / "plugins"
        plugin_kb = plugins_dir / "foo_plugin" / "knowledge_base"
        plugin_kb.mkdir(parents=True)
        kb_root = root / "knowledge_bases"
        kb_root.mkdir()
        symlink = kb_root / "foo_plugin"
        symlink.symlink_to(plugin_kb)

        _check(
            _resolve_owning_plugin(symlink, "foo_plugin") == "foo_plugin",
            "symlinked plugin KB resolves through ./plugins/foo_plugin/knowledge_base",
        )

        unrelated = kb_root / "ananta_platform"
        unrelated.mkdir()
        _check(
            _resolve_owning_plugin(unrelated, "ananta_platform") is None,
            "non-plugin KB directory does not resolve to an owning plugin",
        )


def test_is_orphaned_plugin_candidate_gates_install() -> None:
    with tempfile.TemporaryDirectory(prefix="kb_orphan_smoke_") as tmpdir:
        root = Path(tmpdir)
        plugin_kb = root / "plugins" / "bar_plugin" / "knowledge_base"
        plugin_kb.mkdir(parents=True)
        kb_root = root / "knowledge_bases"
        kb_root.mkdir()
        symlink = kb_root / "bar_plugin"
        symlink.symlink_to(plugin_kb)

        _check(
            _is_orphaned_plugin_candidate(symlink, "bar_plugin", None) is False,
            "manifest_plugin_set=None disables the gate (backwards compat)",
        )
        _check(
            _is_orphaned_plugin_candidate(symlink, "bar_plugin", {"bar_plugin"}) is False,
            "plugin in manifest set → candidate is NOT orphan",
        )
        _check(
            _is_orphaned_plugin_candidate(symlink, "bar_plugin", {"other_plugin"}) is True,
            "plugin NOT in manifest set → candidate IS orphan",
        )
        _check(
            _is_orphaned_plugin_candidate(symlink, "bar_plugin", set()) is True,
            "empty manifest set treats every plugin-owned KB as orphan",
        )


def test_uninstall_pass_removes_only_orphan_plugin_records() -> None:
    records = [
        {
            "id": "kin-1",
            "namespace": "default_knowledge_plugin",
            "name": "voice_embeddings_plugin",
            "resolved_path": "/repo/plugins/voice_embeddings_plugin/knowledge_base",
            "memory_ids": '["mem-vox-1", "mem-vox-2"]',
            "is_active": 1,
        },
        {
            "id": "kin-2",
            "namespace": "default_knowledge_plugin",
            "name": "audio_processing_plugin",
            "resolved_path": "/repo/plugins/audio_processing_plugin/knowledge_base",
            "memory_ids": '["mem-aud-1"]',
            "is_active": 1,
        },
        {
            "id": "kin-3",
            "namespace": "default_knowledge_plugin",
            "name": "ananta_platform",
            "resolved_path": "/repo/ananta/knowledge_bases/ananta_platform",
            "memory_ids": '["mem-platform-1"]',
            "is_active": 1,
        },
    ]
    state = _FakeStateService(records)
    memory = _FakeMemoryService()

    # Manifest contains audio_processing_plugin but NOT voice_embeddings_plugin.
    manifest = {"audio_processing_plugin"}
    count = _uninstall_orphaned_plugin_kbs(manifest, state, memory)

    _check(count == 1, f"uninstall pass returns count=1 (got {count})")
    _check(
        kb_id_tag("voice_embeddings_plugin") in memory.deleted_tags,
        "voice_embeddings_plugin chunks deleted by tag (orphan, uninstalled)",
    )
    _check(
        kb_id_tag("audio_processing_plugin") not in memory.deleted_tags,
        "audio_processing_plugin chunks NOT deleted (in manifest)",
    )
    _check(
        kb_id_tag("ananta_platform") not in memory.deleted_tags,
        "ananta_platform chunks NOT deleted (not plugin-owned)",
    )
    delete_calls = [
        sql for sql, _ in state.executed_sql
        if "DELETE FROM" in sql or sql.startswith("delete_records ")
    ]
    # Per D4 (operator-locked 2026-06-21) uninstall_kb shape: the chunk delete now
    # routes through memory_service.delete_memories_by_tag (tracked on the memory
    # stub, NOT state.executed_sql); only the typed install-record delete_records
    # (K0) reaches state.executed_sql. One orphan KB → one install-record delete.
    _check(
        len(delete_calls) == 1,
        f"one install-record delete_records issued for the orphan KB (got {len(delete_calls)})",
    )


def test_uninstall_pass_empty_manifest_purges_every_plugin_kb() -> None:
    records = [
        {
            "id": "kin-4",
            "namespace": "default_knowledge_plugin",
            "name": "alpha_plugin",
            "resolved_path": "/repo/plugins/alpha_plugin/knowledge_base",
            "memory_ids": '["mem-a"]',
            "is_active": 1,
        },
        {
            "id": "kin-5",
            "namespace": "default_knowledge_plugin",
            "name": "beta_plugin",
            "resolved_path": "/repo/plugins/beta_plugin/knowledge_base",
            "memory_ids": '["mem-b"]',
            "is_active": 1,
        },
        {
            "id": "kin-6",
            "namespace": "default_knowledge_plugin",
            "name": "ananta_service",
            "resolved_path": "/repo/ananta/knowledge_base",
            "memory_ids": '["mem-svc"]',
            "is_active": 1,
        },
    ]
    state = _FakeStateService(records)
    memory = _FakeMemoryService()

    count = _uninstall_orphaned_plugin_kbs(set(), state, memory)
    _check(count == 2, f"empty manifest purges both plugin KBs (count={count})")
    _check(
        kb_id_tag("ananta_service") not in memory.deleted_tags,
        "ananta_service (non-plugin) is preserved even on empty manifest",
    )


def test_uninstall_pass_no_op_when_no_orphans() -> None:
    records = [
        {
            "id": "kin-7",
            "namespace": "default_knowledge_plugin",
            "name": "gamma_plugin",
            "resolved_path": "/repo/plugins/gamma_plugin/knowledge_base",
            "memory_ids": '["mem-g"]',
            "is_active": 1,
        },
    ]
    state = _FakeStateService(records)
    memory = _FakeMemoryService()
    count = _uninstall_orphaned_plugin_kbs({"gamma_plugin"}, state, memory)
    _check(count == 0, "no-op when manifest covers every plugin-owned KB")
    _check(not memory.deleted_tags, "no chunks deleted in no-op pass")


def main() -> int:
    print("=== auto_uninstall_orphaned_plugin_kb_smoke ===")
    print(f"[smoke] using table_name suffix: {TABLE_KNOWLEDGE_INSTALL}")
    test_owning_plugin_from_path_string_extraction()
    test_resolve_owning_plugin_through_symlink()
    test_is_orphaned_plugin_candidate_gates_install()
    test_uninstall_pass_removes_only_orphan_plugin_records()
    test_uninstall_pass_empty_manifest_purges_every_plugin_kb()
    test_uninstall_pass_no_op_when_no_orphans()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
