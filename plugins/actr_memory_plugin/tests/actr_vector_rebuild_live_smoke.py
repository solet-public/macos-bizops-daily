#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for the actr cleanup_orphaned_vectors REBUILD.

Pins ``ACTRMemoryBackend.cleanup_orphaned_vectors`` after its restructure
(D1/GAP-5, Architect RECIPROCAL ruling): the raw foreign-SQL orphan sweep is now
a one-shot operator-gated namespace REBUILD — clear EVERY vector in
``VECTOR_NAMESPACE`` (delete_all_in_namespace), then reindex every live memory
through the SQL-free reindex path.

CRITICAL HAZARD (Architect): ``VECTOR_NAMESPACE`` is shared by actr memories AND
knowledge-base chunks (kb chunks are memories written via ``memory_service.remember``).
So clear-all + reindex MUST regenerate kb-chunk vectors too — if the reindex
enumeration were actr-scoped, the rebuild would WIPE kb chunk vectors with no
restore. This smoke PROVES it does not: it seeds an actr memory AND a kb-chunk
memory (a ``knowledge:official``-tagged row), runs the rebuild, and asserts BOTH
have a vector again afterward.

WHY THIS WIRING: the data-loss risk surface is the REINDEX ENUMERATION
(``get_all_memories(status='active')`` — does it return kb chunks?), so the
MEMORY side runs against a REAL ``PostgresProvider`` end-to-end (a real table
holding both row kinds). The vector side is a FAITHFUL in-memory model of the
``vector_service`` contract — store/clear/stats/find_missing — whose return
shapes were verified by reading the real ``PGVectorProvider``
(delete_all_in_namespace → ``{deleted_count}``; get_namespace_stats →
``{vector_count}`` raising on empty; find_missing_external_ids →
``data.result.missing``); the REAL find_missing extraction is additionally
pinned end-to-end in ``actr_orphan_reconcile_migration_live_smoke.py``. The SUT
here is the BACKEND's rebuild orchestration (does it reindex kb chunks), not
pgvector's storage, which is covered by pgvector's own smokes.

ENVELOPE FIDELITY (load-bearing): the real ``PGVectorPlugin`` wraps EVERY
provider payload one level under ``data.result`` (``_create_success_result`` →
``data={"result": data}``). So the backend reads ``data.result.deleted_count`` /
``data.result.vector_count`` / ``data.result.missing`` — the stub mirrors that
nesting exactly. A flat ``data.deleted_count`` stub would silently pass a
backend that forgot ``.result`` (extracting 0), so the nesting is what makes the
``cleared`` count discriminating, not decorative.

Env-gated behind ``ACTR_VECTOR_REBUILD_LIVE_SMOKE=1`` (needs the live DB). Run::

    ACTR_VECTOR_REBUILD_LIVE_SMOKE=1 \\
      .venv/bin/python3 plugins/actr_memory_plugin/tests/actr_vector_rebuild_live_smoke.py
"""

from __future__ import annotations

import importlib
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, LiteralString, cast

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)
sys.path.insert(0, str(REPO_ROOT / "plugins" / "actr_memory_plugin" / "src"))

importlib.import_module("ananta.core.config.config_manager")
import actr_memory_plugin.backend as _backend_mod  # noqa: E402
from actr_memory_plugin.backend import ACTRMemoryBackend  # noqa: E402
from ananta.services.memory_service.actr.constants import VECTOR_NAMESPACE  # noqa: E402
from postgres_state_management_plugin.postgres_backend.config import (  # noqa: E402
    PostgresConfig,
)
from postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    PostgresProvider,
)

_passed = 0
_failed: list[str] = []

_MEMORY_TABLE = "actr_memory_plugin__memory"
_M_ACTR = "m_actr"
_M_KB = "m_kb"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


_PROFILE_PG_CONFIG = (
    REPO_ROOT / "profile" / "config" / "plugins"
    / "postgres_state_management_plugin.json"
)


def _load_pg_config(schema_name: str) -> PostgresConfig:
    config = PostgresConfig(**json.loads(_PROFILE_PG_CONFIG.read_text(encoding="utf-8")))
    config.pg_schema = schema_name
    return config


class _LiveStateAdapter:
    """``read_state`` over a real provider (serves get_all_memories, the reindex
    enumeration the data-loss hazard hinges on)."""

    def __init__(self, provider: PostgresProvider) -> None:
        self._provider = provider

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        table = query.get("table")
        if not isinstance(table, str):
            return {"action_status": "error", "data": None, "error": "bad table"}
        filters = cast("dict[str, Any]", query.get("filters") or {})
        try:
            rows = self._provider.select(
                namespace=namespace,
                table=table,
                conditions=filters,
                limit=cast("int | None", query.get("limit")),
            )
        except (psycopg.Error, OSError, RuntimeError, ValueError) as exc:
            return {"action_status": "error", "data": None, "error": str(exc)}
        return {
            "action_status": "completed",
            "data": {"namespace": namespace, "records": rows},
            "error": None,
        }


class _InMemVectorService:
    """Faithful in-memory model of the vector_service verbs the rebuild touches.

    Return shapes mirror the real ``PGVectorPlugin`` (verified by reading
    ``plugin._create_success_result`` → ``data={"result": data}``): the plugin
    nests every provider payload one level under ``data.result``. So
    delete_all_in_namespace → ``data.result.deleted_count``; get_namespace_stats
    → ``data.result.vector_count`` (error envelope when empty, matching the
    provider's raise-on-empty that the plugin surfaces as an error result);
    find_missing_external_ids → ``data.result.missing``; store_vectors →
    completed envelope. The ``data.result`` nesting is load-bearing: a flat
    ``data.deleted_count`` stub would silently pass a backend that forgot
    ``.result`` (extracting 0). Tracks active external_ids in a set."""

    def __init__(self) -> None:
        self._ids: set[str] = set()

    def store_vectors(self, _namespace: str, vectors: list[dict[str, Any]]) -> dict[str, Any]:
        for vec in vectors:
            self._ids.add(str(vec["external_id"]))
        return {"action_status": "completed", "data": {"result": {"count": len(vectors)}}, "error": None}

    def delete_all_in_namespace(self, _namespace: str) -> dict[str, Any]:
        count = len(self._ids)
        self._ids.clear()
        return {"action_status": "completed", "data": {"result": {"deleted_count": count}}, "error": None}

    def get_namespace_stats(self, _namespace: str) -> dict[str, Any]:
        if not self._ids:  # the real provider raises "Namespace is empty"
            return {"action_status": "error", "data": None, "error": "Namespace is empty"}
        return {
            "action_status": "completed",
            "data": {"result": {"vector_count": len(self._ids)}},
            "error": None,
        }

    def find_missing_external_ids(
        self, namespace: str, candidate_external_ids: list[str]
    ) -> dict[str, Any]:
        # backend calls this by KEYWORD (namespace=...), so the param name is
        # load-bearing and can't be underscore-prefixed; assert it to use it.
        assert namespace == VECTOR_NAMESPACE
        missing = [cid for cid in candidate_external_ids if cid not in self._ids]
        return {
            "action_status": "completed",
            "data": {"result": {"missing": missing}},
            "error": None,
        }


class _StubEmbedding:
    """Deterministic embedding (dim 4) — the rebuild regenerates vectors, but the
    smoke asserts presence, not similarity, so a fixed vector suffices."""

    def generate_embeddings(self, inputs: list[str]) -> dict[str, Any]:
        return {
            "action_status": "completed",
            "data": {"result": {"embeddings": [[0.1, 0.2, 0.3, 0.4] for _ in inputs]}},
            "error": None,
        }


_MEMORY_DDL = (
    "id text PRIMARY KEY, namespace text NOT NULL, content text NOT NULL, "
    "status text NOT NULL, tags text, retrieval_times text, source_memory_ids text, "
    "is_deleted integer NOT NULL DEFAULT 0, "
    "created_at timestamp NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'), "
    "updated_at timestamp NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC')"
)


def _create_tables(provider: PostgresProvider, schema: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(cast(LiteralString, f'CREATE TABLE "{schema}"."{_MEMORY_TABLE}" ({_MEMORY_DDL})'))


def _seed_memory(provider: PostgresProvider, schema: str, *, mid: str, content: str, tags: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'INSERT INTO "{schema}"."{_MEMORY_TABLE}" '
                "(id, namespace, content, status, tags, is_deleted) "
                "VALUES (%s, %s, %s, %s, %s, 0)",
            ),
            (mid, "actr_memory_plugin", content, "active", tags),
        )


def _backend(provider: PostgresProvider, vectors: _InMemVectorService) -> ACTRMemoryBackend:
    backend = object.__new__(ACTRMemoryBackend)
    backend.state_service = cast("Any", _LiveStateAdapter(provider))
    backend.vector_service = cast("Any", vectors)
    backend.embedding_service = cast("Any", _StubEmbedding())
    backend.actr_enabled = True
    return backend


def _seed_memories(provider: PostgresProvider, schema: str) -> None:
    # An actr memory AND a kb-chunk memory (knowledge:official-tagged) — both live
    # rows in the SAME memory table. Seeded ONCE; the rebuild never mutates memory
    # rows, so the three tests share them. Each test gets its own fresh vector set.
    _seed_memory(provider, schema, mid=_M_ACTR, content="actr fact", tags='["episodic"]')
    _seed_memory(provider, schema, mid=_M_KB, content="kb chunk text", tags='["knowledge:official"]')


def _fresh_vectors() -> _InMemVectorService:
    vectors = _InMemVectorService()
    vectors.store_vectors(VECTOR_NAMESPACE, [{"external_id": _M_ACTR}, {"external_id": _M_KB}])
    return vectors


def test_dry_run_reports_without_mutating(provider: PostgresProvider) -> None:
    vectors = _fresh_vectors()
    result = _backend(provider, vectors).cleanup_orphaned_vectors(dry_run=True)
    _check(
        result == {"dry_run": True, "cleared": 2, "reindexed": 2},
        f"dry_run reports the would-be {{cleared:2, reindexed:2}} counts; got {result}",
    )
    _check(
        vectors.find_missing_external_ids(VECTOR_NAMESPACE, [_M_ACTR, _M_KB])["data"]["result"]["missing"] == [],
        "dry_run mutated NOTHING — both vectors still present",
    )


def test_destructive_run_requires_confirm(provider: PostgresProvider) -> None:
    vectors = _fresh_vectors()
    raised = False
    try:
        _backend(provider, vectors).cleanup_orphaned_vectors(dry_run=False, confirm=False)
    except Exception as exc:  # FrameworkError(memory.rebuild_not_confirmed)
        raised = "confirm" in str(exc).lower()
    _check(raised, "non-dry-run without confirm=True is rejected (operator-gated)")
    _check(
        vectors.find_missing_external_ids(VECTOR_NAMESPACE, [_M_ACTR, _M_KB])["data"]["result"]["missing"] == [],
        "the rejected run mutated NOTHING — both vectors still present",
    )


def test_rebuild_regenerates_actr_and_kb(provider: PostgresProvider) -> None:
    vectors = _fresh_vectors()
    result = _backend(provider, vectors).cleanup_orphaned_vectors(dry_run=False, confirm=True)
    _check(
        result == {"dry_run": False, "cleared": 2, "reindexed": 2},
        f"rebuild cleared 2 + reindexed 2; got {result}",
    )
    # THE HAZARD PROOF: clear-all wiped both vectors, then reindex regenerated
    # BOTH — including the kb chunk. If the reindex were actr-scoped, m_kb would
    # be missing here (wiped with no restore).
    missing = vectors.find_missing_external_ids(VECTOR_NAMESPACE, [_M_ACTR, _M_KB])["data"]["result"]["missing"]
    _check(missing == [], f"after rebuild BOTH actr + kb memories have a vector (no kb wipe); missing={missing}")
    _check(_M_KB not in missing, "the kb-chunk memory's vector was REGENERATED (not actr-scoped)")


def test_rebuild_drains_across_multiple_batches(
    provider: PostgresProvider, drain_ids: list[str]
) -> None:
    """The rebuild must DRAIN over many capped passes, not just terminate on one.

    ``_rebuild_all_vectors`` loops ``_find_orphaned_memories`` (capped at
    ``_REINDEX_BATCH_LIMIT`` = 1000 per pass) until no orphan remains. The other
    tests seed a single sub-cap batch, so they pin loop TERMINATION but NOT the
    multi-pass DRAIN. Production reindexes ~18,737 memories in ~19 passes; a
    future single-batch regression (drop the ``while`` loop) would pass the
    single-batch tests yet silently WIPE every vector beyond the first batch with
    NO restore. Here we shrink the cap below the live population and assert the
    loop reindexes EVERY memory across multiple capped, strictly-shrinking passes
    (the read_events ``_MESSAGE_PAGE_LIMIT=2`` precedent).
    """
    cap = 2
    all_ids = [_M_ACTR, _M_KB, *drain_ids]
    total = len(all_ids)
    assert total > cap, "drain test needs more memories than the cap to force multiple passes"

    vectors = _InMemVectorService()
    vectors.store_vectors(VECTOR_NAMESPACE, [{"external_id": mid} for mid in all_ids])
    backend = _backend(provider, vectors)

    # Spy on the capped batch fetch to observe the per-pass drain. Instance-attr
    # shadow over the bound method (instance __dict__ wins on lookup); cast to Any
    # so the assignment doesn't trip pyright's method-assign rule.
    pass_sizes: list[int] = []
    orig_find = backend._find_orphaned_memories

    def _recording_find() -> list[dict[str, Any]]:
        rows = orig_find()
        pass_sizes.append(len(rows))
        return rows

    cast("Any", backend)._find_orphaned_memories = _recording_find

    saved_cap = _backend_mod._REINDEX_BATCH_LIMIT
    setattr(_backend_mod, "_REINDEX_BATCH_LIMIT", cap)  # noqa: B010 (Final — set via setattr)
    try:
        result = backend.cleanup_orphaned_vectors(dry_run=False, confirm=True)
    finally:
        setattr(_backend_mod, "_REINDEX_BATCH_LIMIT", saved_cap)  # noqa: B010

    nonempty = [s for s in pass_sizes if s > 0]
    _check(
        result.get("reindexed") == total,
        f"rebuild reindexed ALL {total} memories across batches (a single-batch "
        f"regression would stop at cap={cap}); got {result.get('reindexed')}",
    )
    _check(
        len(nonempty) >= 2,
        f"the reindex DRAINED over multiple capped passes, not one batch; pass sizes={pass_sizes}",
    )
    _check(
        bool(nonempty) and max(nonempty) <= cap and pass_sizes[-1] == 0,
        f"every pass respected cap={cap} and the loop terminated on a drained (empty) pass; "
        f"pass sizes={pass_sizes}",
    )
    _check(
        sum(pass_sizes) == total and all(s >= 1 for s in pass_sizes[:-1]),
        f"every orphan processed exactly once with strict per-pass shrinkage; pass sizes={pass_sizes}",
    )
    missing = vectors.find_missing_external_ids(VECTOR_NAMESPACE, all_ids)["data"]["result"]["missing"]
    _check(
        missing == [],
        f"after the multi-batch rebuild EVERY memory has a vector (no beyond-first-batch wipe); missing={missing}",
    )


def main() -> int:
    if os.environ.get("ACTR_VECTOR_REBUILD_LIVE_SMOKE") != "1":
        print("=== actr_vector_rebuild_live_smoke ===")
        print("  SKIP  set ACTR_VECTOR_REBUILD_LIVE_SMOKE=1 to run; needs the live solet DB.")
        return 0
    print("=== actr_vector_rebuild_live_smoke ===")
    schema_name = f"example_test_vecrebuild_{secrets.token_hex(3)}"
    provider = PostgresProvider(_load_pg_config(schema_name))
    provider.initialize()
    try:
        _create_tables(provider, schema_name)
        _seed_memories(provider, schema_name)
        test_dry_run_reports_without_mutating(provider)
        test_destructive_run_requires_confirm(provider)
        test_rebuild_regenerates_actr_and_kb(provider)
        # Drain test runs LAST: it seeds extra rows (would perturb the 2-row
        # single-batch tests above), then exercises the multi-pass drain.
        drain_ids = [f"m_drain_{i}" for i in range(5)]
        for mid in drain_ids:
            _seed_memory(provider, schema_name, mid=mid, content=f"drain memory {mid}", tags='["episodic"]')
        test_rebuild_drains_across_multiple_batches(provider, drain_ids)
    finally:
        with provider.get_transactional_connection() as conn, conn.cursor() as cur:
            cur.execute(cast(LiteralString, f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
