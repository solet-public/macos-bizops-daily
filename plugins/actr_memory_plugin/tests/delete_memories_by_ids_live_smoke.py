#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for memory_service delete_memories_by_ids.

Pins ``ACTRMemoryBackend.delete_memories_by_ids`` — the id-list counterpart of
``delete_memories_by_tag`` (GAP-5), for callers that already hold the exact ids
(knowledge-base per-file + sweep deletes). Asserts the three behaviors that
matter:
  1. BY-IDS: the named memories AND their embeddings are hard-deleted; untouched
     memories/vectors survive; ``deleted_count == len(ids)``.
  2. EMPTY-LIST: a no-op that mutates nothing and reports ``deleted_count`` 0.
  3. CASCADE VECTOR-FIRST + FAIL-LOUD: if the embedding delete fails, the verb
     RAISES and the memory records are PRESERVED — proving embeddings are deleted
     BEFORE records, so a crash mid-cascade leaves an orphan-MEMORY (reconcilable
     via reindex), never an orphan-VECTOR.

WIRING mirrors ``actr_vector_rebuild_live_smoke``: the MEMORY side (the hard
delete is the SUT) runs against a real ``PostgresProvider``; the vector side is a
faithful in-memory model of ``delete_by_external_ids`` (the only vector verb the
SUT calls — it checks ``action_status`` only).

Env-gated behind ``ACTR_DELETE_BY_IDS_LIVE_SMOKE=1`` (needs the live DB). Run::

    ACTR_DELETE_BY_IDS_LIVE_SMOKE=1 \\
      .venv/bin/python3 plugins/actr_memory_plugin/tests/delete_memories_by_ids_live_smoke.py
"""

from __future__ import annotations

import importlib
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, LiteralString, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)
sys.path.insert(0, str(REPO_ROOT / "plugins" / "actr_memory_plugin" / "src"))

importlib.import_module("ananta.core.config.config_manager")
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
    """``delete_records`` over a real provider — serves ``delete_memory_records``,
    the SUT's record-delete path (hard delete via soft_delete=False)."""

    def __init__(self, provider: PostgresProvider) -> None:
        self._provider = provider

    def delete_records(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        deleted = self._provider.delete(
            namespace=namespace,
            table=str(query["table"]),
            conditions=cast("dict[str, Any]", query.get("filters") or {}),
            soft_delete=bool(query.get("soft_delete", True)),
        )
        return {"action_status": "completed", "data": {"result": {"deleted": deleted}}, "error": None}


class _InMemVectorService:
    """Faithful model of ``vector_service.delete_by_external_ids`` (the only vector
    verb the SUT calls). Tracks active external_ids; ``fail`` flips it to an error
    envelope to exercise the fail-loud path. The completed envelope mirrors the
    real ``PGVectorPlugin`` ``data.result`` nesting (the verb only inspects
    ``action_status``, but the shape stays honest)."""

    def __init__(self) -> None:
        self._ids: set[str] = set()
        self.fail = False

    def seed(self, ids: list[str]) -> None:
        self._ids.update(ids)

    def has(self, external_id: str) -> bool:
        return external_id in self._ids

    def delete_by_external_ids(self, namespace: str, external_ids: list[str]) -> dict[str, Any]:
        assert namespace == VECTOR_NAMESPACE
        if self.fail:
            return {"action_status": "error", "data": None, "error": "vector delete failed (injected)"}
        removed = [i for i in external_ids if i in self._ids]
        for i in removed:
            self._ids.discard(i)
        return {
            "action_status": "completed",
            "data": {"result": {"deleted_count": len(removed)}},
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


def _seed_memory(provider: PostgresProvider, schema: str, mid: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'INSERT INTO "{schema}"."{_MEMORY_TABLE}" '
                "(id, namespace, content, status, is_deleted) VALUES (%s, %s, %s, %s, 0)",
            ),
            (mid, "actr_memory_plugin", f"content {mid}", "active"),
        )


def _row_exists(provider: PostgresProvider, schema: str, mid: str) -> bool:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(LiteralString, f'SELECT 1 FROM "{schema}"."{_MEMORY_TABLE}" WHERE id = %s'),
            (mid,),
        )
        return cur.fetchone() is not None


def _backend(provider: PostgresProvider, vectors: _InMemVectorService) -> ACTRMemoryBackend:
    backend = object.__new__(ACTRMemoryBackend)
    backend.state_service = cast("Any", _LiveStateAdapter(provider))
    backend.vector_service = cast("Any", vectors)
    backend.embedding_service = cast("Any", object())  # unused by delete_memories_by_ids
    backend.actr_enabled = True
    return backend


def test_delete_by_ids(provider: PostgresProvider, schema: str) -> None:
    ids = ["bid_1", "bid_2", "bid_3"]
    for mid in ids:
        _seed_memory(provider, schema, mid)
    vectors = _InMemVectorService()
    vectors.seed(ids)

    result = _backend(provider, vectors).delete_memories_by_ids(["bid_1", "bid_2"])

    _check(
        result.get("action_status") == "completed" and result["data"]["deleted_count"] == 2,
        f"delete_memories_by_ids([bid_1, bid_2]) → completed, deleted_count 2; got {result}",
    )
    _check(
        not _row_exists(provider, schema, "bid_1") and not _row_exists(provider, schema, "bid_2"),
        "the two named memory RECORDS are hard-deleted",
    )
    _check(_row_exists(provider, schema, "bid_3"), "the un-named memory RECORD survives")
    _check(
        not vectors.has("bid_1") and not vectors.has("bid_2"),
        "the two named EMBEDDINGS are deleted",
    )
    _check(vectors.has("bid_3"), "the un-named EMBEDDING survives")


def test_empty_ids_is_noop(provider: PostgresProvider, schema: str) -> None:
    ids = ["bid_e1", "bid_e2"]
    for mid in ids:
        _seed_memory(provider, schema, mid)
    vectors = _InMemVectorService()
    vectors.seed(ids)

    result = _backend(provider, vectors).delete_memories_by_ids([])

    _check(
        result.get("action_status") == "completed" and result["data"]["deleted_count"] == 0,
        f"delete_memories_by_ids([]) → completed, deleted_count 0; got {result}",
    )
    _check(
        _row_exists(provider, schema, "bid_e1") and _row_exists(provider, schema, "bid_e2"),
        "empty-list mutated NO memory records",
    )
    _check(
        vectors.has("bid_e1") and vectors.has("bid_e2"),
        "empty-list mutated NO embeddings",
    )


def test_vector_failure_is_fail_loud_and_records_preserved(
    provider: PostgresProvider, schema: str
) -> None:
    ids = ["bid_f1", "bid_f2"]
    for mid in ids:
        _seed_memory(provider, schema, mid)
    vectors = _InMemVectorService()
    vectors.seed(ids)
    vectors.fail = True

    raised = False
    try:
        _backend(provider, vectors).delete_memories_by_ids(["bid_f1", "bid_f2"])
    except Exception as exc:  # FrameworkError(memory.embedding_delete_failed)
        raised = "embedding" in str(exc).lower()

    _check(raised, "an embedding-delete failure RAISES (fail-loud, no swallow)")
    _check(
        _row_exists(provider, schema, "bid_f1") and _row_exists(provider, schema, "bid_f2"),
        "CASCADE VECTOR-FIRST: the embedding delete failed BEFORE any record delete → "
        "records PRESERVED (orphan-MEMORY never orphan-VECTOR)",
    )


def main() -> int:
    if os.environ.get("ACTR_DELETE_BY_IDS_LIVE_SMOKE") != "1":
        print("=== delete_memories_by_ids_live_smoke ===")
        print("  SKIP  set ACTR_DELETE_BY_IDS_LIVE_SMOKE=1 to run; needs the live solet DB.")
        return 0
    print("=== delete_memories_by_ids_live_smoke ===")
    schema_name = f"example_test_delbyids_{secrets.token_hex(3)}"
    provider = PostgresProvider(_load_pg_config(schema_name))
    provider.initialize()
    try:
        _create_tables(provider, schema_name)
        test_delete_by_ids(provider, schema_name)
        test_empty_ids_is_noop(provider, schema_name)
        test_vector_failure_is_fail_loud_and_records_preserved(provider, schema_name)
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
