#!/usr/bin/env python3
"""W5.P smoke — verifies the 6 acceptance criteria empirically.

Stand-alone (no pytest); uses lightweight fake services so the smoke
runs without Postgres or the live actr_memory plugin. The fakes capture
every SQL statement the production path emits so per-criterion
assertions can introspect them.

Acceptance-criterion coverage (per W5.P design §4):
  #1 — spawn-path KB purge eliminated; `auto_install_knowledge_bases`
       no longer calls `purge_orphaned_chunks`.
  #2 — systematically impossible orphan generation; the per-KB lifecycle
       sites all open `state_service.transactional()` blocks.
  #3 — Gap 6 hard-delete, now via the OWNING memory_service verb (2026-06-21
       SQL-lockdown cohort): `delete_kb_chunks` calls
       `memory_service.delete_memories_by_ids` (hard delete, vector-first
       cascade) — NOT raw SQL on the foreign `actr_memory_plugin__memory`
       namespace, and not a soft archive. `delete_kb_chunks_for_file` FINDs via
       `get_memories_by_tag(doc_tag)` + a `kb_tag` membership filter (cross-KB
       safety), then deletes through the same owner verb.
  #4 — no new transaction infrastructure; the primitive is the existing
       `state_service.transactional()` (provider self-binds psycopg
       autocommit=False); FK CASCADE is PG-native.
  #5 — concurrent same-KB `update_kb` / `reindex_file` operations serialize
       via the single-threaded ActionQueuePoller's one-at-a-time synchronous
       EDGE dispatch (D3/kb GAP-6, 2026-06-21): the redundant per-KB
       `pg_advisory_xact_lock(hashtext(name)::bigint)` was REMOVED; the single
       remaining transactional() block (install-record UPDATE) stays for write
       atomicity (the chunk-delete moved to the owner verb — SQL-lockdown cohort).
  #6 — operator-fired `purge_orphaned_chunks` verb runs dry-run by
       default; `confirm=True` performs batched hard delete. The former
       companion `purge_orphan_vectors` was RETIRED (FORK C, 2026-06-22)
       — superseded by `memory_service::cleanup_orphaned_vectors`; the
       retirement is regression-guarded below.

Plus two cross-cutting checks:
  FK#1 — `ColumnDefinition(foreign_key=..., on_delete=...)` emits the
         expected `REFERENCES <target>(<col>) ON DELETE <action>` SQL.
  FK#2 — `memory_service` schema declares CASCADE FKs on `memorization`
         + `focus_buffer` per W5.P §3.3.

Run:
    .venv/bin/python3 plugins/default_knowledge_plugin/tests/w5p_orphan_invariant_smoke.py
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_knowledge_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.services.memory_service.schema import get_memory_schema  # noqa: E402
from ananta.types.column_types import ColumnType  # noqa: E402
from ananta.types.schema_types import ColumnDefinition  # noqa: E402
from default_knowledge_plugin import kb_indexing, kb_lifecycle, plugin  # noqa: E402
from default_knowledge_plugin.constants import document_tag, kb_id_tag  # noqa: E402

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


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeTxn:
    """Captures every SQL statement issued via `txn.execute` / `txn.fetch_all`."""

    def __init__(self, fetch_responses: list[list[dict[str, Any]]] | None = None) -> None:
        self.executed: list[tuple[str, list[Any] | None]] = []
        self._fetch_responses = list(fetch_responses or [])

    def execute(self, sql: str, params: list[Any] | None = None) -> None:
        self.executed.append((sql, list(params) if params else None))

    def fetch_all(
        self, sql: str, params: list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        self.executed.append((sql, list(params) if params else None))
        if self._fetch_responses:
            return self._fetch_responses.pop(0)
        return []

    # Typed in-txn primitives (mirror StateTransaction) — the K0 migration
    # routes the own-namespace install-record writes through these instead of
    # raw `txn.execute`. Each records a synthetic descriptor so assertions can
    # match the typed call without a raw-SQL string.
    def delete_records(self, namespace: str, query: dict[str, Any]) -> int:
        table = query.get("table")
        filters = query.get("filters", {})
        keys = list(filters.keys()) if isinstance(filters, dict) else None
        self.executed.append((f"delete_records {namespace}__{table}", keys))
        return 1

    def write_state(self, namespace: str, data: dict[str, Any]) -> str:
        table = data.get("table")
        record = data.get("record")
        self.executed.append((f"write_state {namespace}__{table}", None))
        if isinstance(record, dict):
            return str(record.get("id", f"{namespace}-fake"))
        return f"{namespace}-fake"

    def update_state(
        self, namespace: str, query: dict[str, Any], updates: dict[str, Any],
    ) -> int:
        table = query.get("table")
        self.executed.append(
            (f"update_state {namespace}__{table}", list(updates.keys())),
        )
        return 1


class _FakeStateService:
    """Captures execute_sql + provides a real-ish `transactional()` context manager."""

    def __init__(self) -> None:
        self.txns: list[_FakeTxn] = []
        self.executed_sql: list[tuple[str, list[Any] | None]] = []
        self.fetch_responses_per_txn: list[list[list[dict[str, Any]]]] = []
        self.read_state_responses: list[dict[str, Any]] = []
        self.execute_sql_responses: list[dict[str, Any]] = []
        self.raise_in_txn: Exception | None = None

    @contextlib.contextmanager
    def transactional(self) -> Any:
        fetch_q = self.fetch_responses_per_txn.pop(0) if self.fetch_responses_per_txn else None
        txn = _FakeTxn(fetch_responses=fetch_q)
        self.txns.append(txn)
        try:
            yield txn
            if self.raise_in_txn is not None:
                raise self.raise_in_txn
        except Exception:
            # Mark this txn as rolled back so smokes can introspect.
            txn.executed.append(("__ROLLBACK__", None))
            raise

    def execute_sql(
        self, sql_query: str, sql_params: list[Any] | None = None,
    ) -> dict[str, Any]:
        self.executed_sql.append((sql_query, sql_params))
        if self.execute_sql_responses:
            return self.execute_sql_responses.pop(0)
        return {"data": {"records": []}}

    def read_state(self, *, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace, query
        if self.read_state_responses:
            return self.read_state_responses.pop(0)
        return {"data": {"records": []}}

    def generate_id(self, *, prefix: str) -> str:
        return f"{prefix}fake01"


class _FakeMemoryService:
    """Captures owning-service chunk deletes (D4 by-tag + the SQL-lockdown
    by-ids verb) and serves the owning-service tag-FIND read (D1:
    get_memories_by_tag)."""

    def __init__(self) -> None:
        self.deleted_tags: list[str] = []
        self.deleted_id_batches: list[list[str]] = []
        self.tag_queries: list[str] = []
        self.tag_include_archived: list[bool] = []
        self.tag_responses: list[dict[str, Any]] = []

    def delete_memories_by_tag(self, tag: str) -> dict[str, Any]:
        self.deleted_tags.append(tag)
        return {"action_status": "completed", "data": {"deleted_count": 0}}

    def delete_memories_by_ids(self, ids: list[str]) -> dict[str, Any]:
        self.deleted_id_batches.append(list(ids))
        return {"action_status": "completed", "data": {"deleted_count": len(ids)}}

    def get_memories_by_tag(
        self, tag: str, include_archived: bool = False,
    ) -> dict[str, Any]:
        self.tag_queries.append(tag)
        self.tag_include_archived.append(include_archived)
        if self.tag_responses:
            return self.tag_responses.pop(0)
        return {"memories": [], "count": 0}


# ---------------------------------------------------------------------------
# Smoke 1 — Criterion #1: spawn-path purge removed
# ---------------------------------------------------------------------------


def smoke_criterion_1_spawn_purge_removed() -> None:
    print("\n[#1] Spawn-path purge removed from auto_install_knowledge_bases")
    src = (REPO_ROOT / "plugins" / "default_knowledge_plugin" / "src"
           / "default_knowledge_plugin" / "plugin.py").read_text()
    _check(
        "purge_orphaned_chunks(self._state_service, self._memory_service)" not in src,
        "plugin.py no longer auto-calls purge_orphaned_chunks on the spawn path",
    )
    # Confirmation: auto_install_knowledge_bases wrapper still calls _run_auto_install
    # (the install pass) but not the purge.
    _check(
        "_run_auto_install(" in src,
        "auto_install_knowledge_bases still invokes the install pass (_run_auto_install)",
    )
    plugin_method_src = src[src.index("def auto_install_knowledge_bases"):]
    plugin_method_src = plugin_method_src[: plugin_method_src.index("\n    async def ")]
    _check(
        "purge_orphaned_chunks(" not in plugin_method_src,
        "auto_install_knowledge_bases body contains zero purge_orphaned_chunks calls",
    )


# ---------------------------------------------------------------------------
# Smoke 2 — Criterion #2: orphan generation atomic on lifecycle paths
# ---------------------------------------------------------------------------


def smoke_criterion_2_atomic_rollback() -> None:
    print("\n[#2] Install-record delete rolls back on mid-txn failure; chunk delete best-effort (D4)")
    state = _FakeStateService()
    memory = _FakeMemoryService()
    # Seed an install record so uninstall_kb finds it.
    state.read_state_responses.append(
        {"data": {"records": [{
            "name": "kb_x", "memory_ids": '["mem-1","mem-2","mem-3"]',
            "source_type": "local",
        }]}},
    )
    # Force the install-record-delete transaction body to raise so the rollback
    # path triggers. Per D4 (operator-locked 2026-06-21): the chunk delete
    # (memory_service.delete_memories_by_tag) runs best-effort BEFORE the txn and
    # is NOT rolled back; only the typed install-record delete_records is
    # transactional. Relinquished cross-service atomicity — the orphan window is
    # swept by purge_orphaned_chunks (chunks are a regenerable index).
    state.raise_in_txn = RuntimeError("simulated mid-txn failure")
    try:
        kb_lifecycle.uninstall_kb(
            "kb_x", remove_files=False, state_service=state, memory_service=memory,
        )
    except RuntimeError:
        pass
    _check(
        kb_id_tag("kb_x") in memory.deleted_tags,
        "uninstall_kb deletes chunks via memory_service.delete_memories_by_tag (owning service, pre-txn)",
    )
    txn = state.txns[0]
    _check(
        ("__ROLLBACK__", None) in txn.executed,
        "install-record delete transaction rolled back on simulated failure",
    )


# ---------------------------------------------------------------------------
# Smoke 3 — Criterion #3: Gap 6 fix via hard-delete primitive
# ---------------------------------------------------------------------------


def smoke_criterion_3_hard_delete() -> None:
    print("\n[#3] chunk delete routes through the owning memory_service verb (no foreign SQL)")
    # delete_kb_chunks → memory_service.delete_memories_by_ids(ids)
    memory = _FakeMemoryService()
    kb_indexing.delete_kb_chunks(["mem-a", "mem-b"], memory)
    _check(
        memory.deleted_id_batches == [["mem-a", "mem-b"]],
        "delete_kb_chunks calls memory_service.delete_memories_by_ids(ids) "
        "(owner verb; vector-first cascade subsumes the old kb-side vector call)",
    )
    empty = _FakeMemoryService()
    kb_indexing.delete_kb_chunks([], empty)
    _check(
        empty.deleted_id_batches == [],
        "delete_kb_chunks([]) is a no-op (no owner-verb call)",
    )

    # delete_kb_chunks_for_file FINDs via get_memories_by_tag(doc_tag) then
    # AND-filters on kb_tag membership. doc_tag is PATH-ONLY, so a same-path
    # chunk in ANOTHER KB shares it and MUST NOT be deleted — the kb_tag filter
    # is the cross-KB safety guard (replaces the old `tags::text LIKE` SQL).
    per_file = _FakeMemoryService()
    kb_tag = kb_id_tag("kb_x")
    doc_tag = document_tag("a/b.md")
    per_file.tag_responses.append({"memories": [
        {"id": "mine-1", "tags": [kb_tag, doc_tag]},
        {"id": "other-1", "tags": ["kb:other_kb", doc_tag]},
        {"id": "mine-2", "tags": [kb_tag, doc_tag, "knowledge:official"]},
    ], "count": 3})
    deleted = kb_indexing.delete_kb_chunks_for_file("kb_x", "a/b.md", per_file)
    _check(
        per_file.tag_queries == [doc_tag],
        "delete_kb_chunks_for_file FINDs via get_memories_by_tag(doc_tag) (narrower tag)",
    )
    _check(
        per_file.tag_include_archived == [True],
        "per-file FIND passes include_archived=True (status-agnostic; matches old is_deleted=0)",
    )
    _check(
        deleted == ["mine-1", "mine-2"],
        "only THIS KB's chunks selected (kb_tag filter prevents cross-KB delete of the shared doc_tag)",
    )
    _check(
        per_file.deleted_id_batches == [["mine-1", "mine-2"]],
        "per-file delete routes the scoped ids through memory_service.delete_memories_by_ids",
    )

    # Durable source-level tripwire: zero raw foreign-namespace SQL / txn-cursor
    # calls remain in kb_indexing.py (the SQL-lockdown migration this asserts).
    src = (REPO_ROOT / "plugins" / "default_knowledge_plugin" / "src"
           / "default_knowledge_plugin" / "kb_indexing.py").read_text()
    _check(
        "DELETE FROM actr_memory_plugin__memory" not in src
        and "SELECT id FROM actr_memory_plugin__memory" not in src
        and "txn.execute(" not in src
        and "txn.fetch_all(" not in src,
        "kb_indexing.py issues zero raw foreign-namespace SQL / txn-cursor calls (SQL-lockdown)",
    )


# ---------------------------------------------------------------------------
# Smoke 4 — Criterion #4: existing state_service.transactional() primitive
# ---------------------------------------------------------------------------


def smoke_criterion_4_existing_primitive() -> None:
    print("\n[#4] Implementation uses existing state_service.transactional()")
    # Verify the postgres plugin exposes `transactional()` as a context manager,
    # matching the design's "no new infra" claim.
    pg_plugin_src = (REPO_ROOT / "plugins" / "postgres_state_management_plugin"
                     / "src" / "postgres_state_management_plugin" / "plugin.py").read_text()
    _check(
        "def transactional(" in pg_plugin_src and "@contextmanager" in pg_plugin_src,
        "postgres_state_management_plugin already exposes transactional() (no new infra)",
    )
    # And no new transaction wrapper was introduced in default_knowledge_plugin.
    dk_files = list(
        (REPO_ROOT / "plugins" / "default_knowledge_plugin" / "src"
         / "default_knowledge_plugin").rglob("*.py"),
    )
    for path in dk_files:
        src = path.read_text()
        _check(
            "class StateTransaction" not in src,
            f"{path.name} does not define a new StateTransaction class (no new infra)",
        )


# ---------------------------------------------------------------------------
# Smoke 5 — Criterion #5: per-KB serialization via serial EDGE dispatch
# (D3/kb GAP-6, 2026-06-21: the redundant advisory lock was removed)
# ---------------------------------------------------------------------------


def smoke_criterion_5_serial_dispatch_serialization() -> None:
    print("\n[#5] per-KB update/reindex serialize via serial poller dispatch (no advisory lock)")
    src_dir = (REPO_ROOT / "plugins" / "default_knowledge_plugin" / "src"
               / "default_knowledge_plugin")
    lifecycle_src = (src_dir / "kb_lifecycle.py").read_text()
    file_ops_src = (src_dir / "kb_file_ops.py").read_text()

    # The redundant per-KB advisory lock was DELETED. Assert no SQL-emitting
    # acquire call survives in either file (the only remaining mentions are the
    # tripwire docstrings, which never contain the `"SELECT pg_advisory_xact_lock`
    # literal).
    _check(
        '"SELECT pg_advisory_xact_lock' not in lifecycle_src,
        "kb_lifecycle.py no longer issues the advisory-lock acquire SQL",
    )
    _check(
        '"SELECT pg_advisory_xact_lock' not in file_ops_src,
        "kb_file_ops.py no longer issues the advisory-lock acquire SQL",
    )

    # The transactional() blocks remain for write atomicity (chunk-delete txn +
    # install-record UPDATE txn), in BOTH update_kb and reindex_file. Count the
    # `with ... as txn:` code pattern specifically (the tripwire docstrings also
    # mention `state_service.transactional()`, so a bare substring count would
    # over-count).
    txn_block = "with state_service.transactional() as txn:"
    update_kb_src = lifecycle_src[lifecycle_src.index("def update_kb"):]
    update_kb_src = update_kb_src[: update_kb_src.index("\ndef ")]
    _check(
        update_kb_src.count(txn_block) == 1,
        "update_kb retains ONLY the install-record UPDATE transactional() block "
        "(chunk-delete moved to the owner verb — SQL-lockdown cohort)",
    )
    reindex_src = file_ops_src[file_ops_src.index("def reindex_file"):]
    reindex_src = reindex_src[: reindex_src.index("\ndef ")]
    _check(
        reindex_src.count(txn_block) == 1,
        "reindex_file retains ONLY the install-record UPDATE transactional() block "
        "(chunk-delete moved to the owner verb — SQL-lockdown cohort)",
    )

    # The serial-dispatch dependency is documented as the durable tripwire
    # (Dusk: "the tripwire comment IS the durable record").
    _check(
        "ActionQueuePoller" in update_kb_src and "TRIPWIRE" in update_kb_src,
        "update_kb documents the serial-dispatch serialization + tripwire",
    )


# ---------------------------------------------------------------------------
# Smoke 6 — Criterion #6: operator-fired purge verbs
# ---------------------------------------------------------------------------


def smoke_criterion_6_operator_verbs() -> None:
    print("\n[#6] Operator-fired purge verbs registered and dry-run by default")
    plugin_methods = dir(plugin.DefaultKnowledgePlugin)
    _check(
        "purge_orphaned_chunks" in plugin_methods,
        "DefaultKnowledgePlugin.purge_orphaned_chunks exists",
    )
    # FORK C (2026-06-22 kb-cohort forks-ruling): purge_orphan_vectors was RETIRED
    # — its cross-ns pgvector⟕actr anti-join is superseded by
    # memory_service::cleanup_orphaned_vectors. Regression-guard the retirement.
    _check(
        "purge_orphan_vectors" not in plugin_methods,
        "DefaultKnowledgePlugin.purge_orphan_vectors RETIRED (FORK C; use memory_service::cleanup_orphaned_vectors)",
    )
    # Dry-run path on purge_orphaned_chunks. The orphan-FIND read now routes
    # through the OWNING memory service (D1: get_memories_by_tag), NOT raw SQL.
    state = _FakeStateService()
    memory = _FakeMemoryService()
    state.read_state_responses.append(
        {"data": {"records": [{"memory_ids": '["mem-active1","mem-active2"]'}]}},
    )
    memory.tag_responses.append(
        {"memories": [
            {"id": "mem-active1"}, {"id": "mem-orphan1"}, {"id": "mem-orphan2"},
        ], "count": 3},
    )
    result = kb_lifecycle.purge_orphaned_chunks(
        state, memory, confirm=False,
    )
    _check(
        result["status"] == "dry_run",
        "purge_orphaned_chunks default (confirm=False) returns status=dry_run",
    )
    _check(
        result["orphan_count"] == 2,
        f"dry_run reports 2 orphans (got {result.get('orphan_count')})",
    )
    _check(
        memory.tag_queries == ["knowledge:official"],
        "orphan-FIND reads via memory_service.get_memories_by_tag('knowledge:official') (no execute_sql)",
    )
    # Behavior-equivalence regression-guard (Reviewer-A defect, 2026-06-28): the
    # old raw predicate `is_deleted=0` was STATUS-AGNOSTIC (active + archived).
    # The FIND MUST pass include_archived=True so it still drains the accumulated
    # ARCHIVED orphan backlog (229k rows) — active-only would silently miss it.
    _check(
        memory.tag_include_archived == [True],
        "orphan-FIND passes include_archived=True (status-agnostic; drains archived backlog)",
    )
    _check(
        not state.executed_sql,
        "orphan-FIND issues ZERO execute_sql calls (foreign-namespace SQL removed)",
    )
    _check(
        not state.txns,
        "dry_run opens ZERO transactions",
    )
    # Confirm path deletes orphans through the owner verb (no foreign SQL, no txn).
    state = _FakeStateService()
    memory = _FakeMemoryService()
    state.read_state_responses.append(
        {"data": {"records": [{"memory_ids": '["mem-active1"]'}]}},
    )
    memory.tag_responses.append(
        {"memories": [{"id": "mem-active1"}, {"id": "mem-orphan1"}], "count": 2},
    )
    result = kb_lifecycle.purge_orphaned_chunks(
        state, memory, confirm=True, batch_size=10,
    )
    _check(
        result["status"] == "completed" and result["orphan_count"] == 1,
        "confirm=True deletes 1 orphan and returns status=completed",
    )
    _check(
        not state.txns,
        "confirm=True opens ZERO state transactions (chunk-delete is an owner-verb service call)",
    )
    _check(
        memory.deleted_id_batches == [["mem-orphan1"]],
        "confirm batch deletes the orphan via memory_service.delete_memories_by_ids (no foreign-namespace SQL)",
    )


# ---------------------------------------------------------------------------
# Smoke FK#1 — ColumnDefinition FK emits REFERENCES SQL
# ---------------------------------------------------------------------------


def smoke_fk_column_definition_emit() -> None:
    print("\n[FK#1] ColumnDefinition(foreign_key=..., on_delete=...) emits REFERENCES")
    col = ColumnDefinition(
        type=ColumnType.TEXT,
        not_null=True,
        foreign_key=("actr_memory_plugin__memory", "id"),
        on_delete="CASCADE",
    )
    rendered = col.to_sql("memory_id")
    _check(
        "REFERENCES actr_memory_plugin__memory(id)" in rendered,
        f"renders REFERENCES clause (got: {rendered!r})",
    )
    _check(
        "ON DELETE CASCADE" in rendered,
        f"renders ON DELETE CASCADE (got: {rendered!r})",
    )
    _check(
        "ON UPDATE NO ACTION" in rendered,
        f"defaults ON UPDATE NO ACTION (got: {rendered!r})",
    )
    # Regression check: a non-FK column emits no REFERENCES clause.
    plain = ColumnDefinition(type=ColumnType.TEXT, not_null=True)
    plain_sql = plain.to_sql("content")
    _check(
        "REFERENCES" not in plain_sql,
        f"non-FK column emits no REFERENCES clause (got: {plain_sql!r})",
    )


# ---------------------------------------------------------------------------
# Smoke FK#2 — Memory schema declares CASCADE on memorization + focus_buffer
# ---------------------------------------------------------------------------


def smoke_fk_memory_schema_cascade() -> None:
    print("\n[FK#2] memory_service schema declares CASCADE FK on memorization + focus_buffer")
    schema = get_memory_schema()
    memorization = schema.tables["memorization"]
    fk_col = memorization.columns["actr_memory_plugin__memory_id"]
    _check(
        fk_col.foreign_key == ("actr_memory_plugin__memory", "id"),
        "memorization.actr_memory_plugin__memory_id declares FK to memory(id)",
    )
    _check(
        fk_col.on_delete == "CASCADE",
        "memorization FK is ON DELETE CASCADE",
    )
    focus_buffer = schema.tables["focus_buffer"]
    fb_col = focus_buffer.columns["memory_id"]
    _check(
        fb_col.foreign_key == ("actr_memory_plugin__memory", "id"),
        "focus_buffer.memory_id declares FK to memory(id)",
    )
    _check(
        fb_col.on_delete == "CASCADE",
        "focus_buffer FK is ON DELETE CASCADE",
    )


def main() -> int:
    print("=" * 70)
    print("W5.P orphan invariant smoke")
    print("=" * 70)
    smoke_criterion_1_spawn_purge_removed()
    smoke_criterion_2_atomic_rollback()
    smoke_criterion_3_hard_delete()
    smoke_criterion_4_existing_primitive()
    smoke_criterion_5_serial_dispatch_serialization()
    smoke_criterion_6_operator_verbs()
    smoke_fk_column_definition_emit()
    smoke_fk_memory_schema_cascade()
    print("\n" + "=" * 70)
    print(f"  Passed: {_passed}")
    print(f"  Failed: {len(_failed)}")
    for label in _failed:
        print(f"    - {label}")
    print("=" * 70)
    return 0 if not _failed else 1


if __name__ == "__main__":
    sys.exit(main())
