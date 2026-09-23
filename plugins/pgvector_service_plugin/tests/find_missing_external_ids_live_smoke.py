#!/usr/bin/env python3
"""Live-Postgres twin-parity smoke for ``find_missing_external_ids`` (GAP-5 STUB-1).

Pins the new read verb across BOTH pgvector twins
(``pgvector_service_plugin`` + ``rds_pgvector_service_plugin``) against a REAL
``PostgresProvider``. The verb is a thin orphan-reconcile read:

    find_missing_external_ids(namespace, candidate_external_ids) -> {"missing": [...]}

A single ``external_id = ANY(candidates) AND is_deleted = 0`` lookup over the
namespace's ``{ns}__embeddings`` table (via the ``read_state`` primitive — no
raw SQL), then a Python set-difference: candidates − present.

WHY a real ``PostgresProvider`` (not a stub state service): the dimension under
test is the Postgres ``= ANY`` translation of a LIST-valued ``read_state``
filter (``provider.select`` -> ``build_select_sql`` -> ``_build_filter_clauses``
-> ``col = ANY(%s)``). A hand-built stub that reimplements ``=ANY`` could not
discriminate "the real grammar translates the list" from "my stub happens to
match" — so the ``_LiveStateAdapter`` here does envelope-shaping ONLY and
delegates the actual filter compilation to the genuine ``provider.select``.

The verb is driven through the PLUGIN (``object.__new__`` + the single
``_provider`` attribute it touches), so the test also pins the nested envelope
the deferred actr orphan-reconcile consumer depends on:
``ActionResult["data"]["result"]["missing"]``.

Both twins are exercised against the SAME seeded table with the SAME inputs and
their ``missing`` lists are asserted equal — a one-sided edit fails loudly.

Env-gated behind ``PGVECTOR_FIND_MISSING_LIVE_SMOKE=1`` (needs the live DB up;
own throwaway schema, dropped in ``finally``).

Run::

    PGVECTOR_FIND_MISSING_LIVE_SMOKE=1 \\
      .venv/bin/python3 plugins/pgvector_service_plugin/tests/find_missing_external_ids_live_smoke.py
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
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "pgvector_service_plugin" / "src"),
)
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "rds_pgvector_service_plugin" / "src"),
)

# Pre-load config_manager (via importlib so the import sorter can't reorder it
# after the plugin imports) to cache the deep plugin_contracts chain before
# ``ananta.utils`` initializes — avoids the utils<->config circular import when
# the plugins are imported standalone.
importlib.import_module("ananta.core.config.config_manager")
from pgvector_service_plugin.plugin import (  # noqa: E402
    PGVectorServicePlugin,
)
from pgvector_service_plugin.postgres_backend.vector.provider import (  # noqa: E402
    PGVectorProvider as LocalPGVectorProvider,
)
from postgres_state_management_plugin.postgres_backend.config import (  # noqa: E402
    PostgresConfig,
)
from postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    PostgresProvider,
)
from rds_pgvector_service_plugin.plugin import (  # noqa: E402
    RDSPGVectorServicePlugin,
)
from rds_pgvector_service_plugin.postgres_backend.vector.provider import (  # noqa: E402
    PGVectorProvider as RdsPGVectorProvider,
)

_passed = 0
_failed: list[str] = []

# The two twins, parametrized: (label, provider_cls, plugin_cls). Both provider
# classes are byte-identical ``PGVectorProvider`` in different packages; both
# plugin verbs delegate to ``self._provider.find_missing_external_ids``.
_TWINS: tuple[tuple[str, type, type], ...] = (
    ("local", LocalPGVectorProvider, PGVectorServicePlugin),
    ("rds", RdsPGVectorProvider, RDSPGVectorServicePlugin),
)

_NAMESPACE = "memory"
_SEED_AT = "2026-06-01T00:00:00"
# Representative slice of the embeddings table — exactly the columns the read
# verb touches (external_id, is_deleted) plus the standardized fields. The
# ``embedding`` vector column is intentionally omitted: find_missing never reads
# it, and omitting it keeps the smoke free of the pgvector extension.
_DDL = (
    "id text PRIMARY KEY, external_id text, "
    "is_deleted integer NOT NULL DEFAULT 0, "
    "created_at timestamp NOT NULL, updated_at timestamp NOT NULL"
)
_TABLE = f"{_NAMESPACE}__embeddings"


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
    """Envelope-shaping ONLY: delegates ``read_state`` to the genuine
    ``PostgresProvider.select`` so the real ``_build_filter_clauses`` ``= ANY``
    grammar compiles the list-valued ``external_id`` filter. No filter logic
    lives here — that is the whole point of using the real provider."""

    def __init__(self, provider: PostgresProvider) -> None:
        self._provider = provider

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        table = query.get("table")
        if not isinstance(table, str):
            return {"action_status": "error", "data": None, "error": "bad table"}
        filters = cast("dict[str, Any]", query.get("filters") or {})
        limit = query.get("limit")
        try:
            rows = self._provider.select(
                namespace=namespace,
                table=table,
                conditions=filters,
                limit=cast("int | None", limit),
            )
        except (psycopg.Error, OSError, RuntimeError, ValueError) as exc:
            return {"action_status": "error", "data": None, "error": str(exc)}
        return {
            "action_status": "completed",
            "data": {"namespace": namespace, "records": rows},
            "error": None,
            "timestamp": "",
        }


def _make_plugin(provider_cls: type, plugin_cls: type, adapter: _LiveStateAdapter) -> Any:
    """Partial-construct provider + plugin with only the attributes the read
    verb touches (``object.__new__`` bypasses the keyword-only pool_builder/
    state_service ctor — the verb never needs a pool)."""
    provider = object.__new__(provider_cls)
    provider._state_service = cast("Any", adapter)
    plugin = object.__new__(plugin_cls)
    plugin._provider = provider
    return plugin


def _create_table(provider: PostgresProvider, schema: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(cast(LiteralString, f'CREATE TABLE "{schema}"."{_TABLE}" ({_DDL})'))


def _seed(
    provider: PostgresProvider, schema: str, *, eid: str, external_id: str, is_deleted: int,
) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'INSERT INTO "{schema}"."{_TABLE}" '
                "(id, external_id, is_deleted, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s)",
            ),
            (eid, external_id, is_deleted, _SEED_AT, _SEED_AT),
        )


def _missing(plugin: Any, candidates: list[str]) -> Any:
    """Drive the plugin verb and extract data.result.missing (the exact nested
    envelope the actr orphan-reconcile consumer will read)."""
    result = plugin.find_missing_external_ids(
        namespace=_NAMESPACE, candidate_external_ids=candidates,
    )
    if result.get("action_status") != "completed":
        return result  # surfaced as a non-list -> the asserting test fails loudly
    return result["data"]["result"]["missing"]


def _run_for_twin(label: str, plugin: Any) -> None:
    # Seeded rows (shared across cases): e1/e2/e3 ACTIVE, e9 SOFT-DELETED.
    # 1. Basic set-diff: present-active excluded, absent reported.
    _check(
        _missing(plugin, ["e1", "e2", "e4", "e5"]) == ["e4", "e5"],
        f"[{label}] set-diff: absent ids (e4,e5) missing; active ids (e1,e2) present",
    )
    # 2. Soft-deleted counts as MISSING (is_deleted=0 active filter discriminates).
    _check(
        _missing(plugin, ["e1", "e9"]) == ["e9"],
        f"[{label}] soft-deleted e9 reported MISSING (excluded by is_deleted=0)",
    )
    # 3. Empty candidates -> empty (short-circuit, no DB hit needed).
    _check(_missing(plugin, []) == [], f"[{label}] empty candidates -> []")
    # 4. All present-active -> none missing.
    _check(_missing(plugin, ["e1", "e2", "e3"]) == [], f"[{label}] all-present -> []")
    # 5. All absent -> all missing (order preserved).
    _check(
        _missing(plugin, ["z1", "z2"]) == ["z1", "z2"],
        f"[{label}] all-absent -> candidates verbatim",
    )
    # 6. Dedup + first-seen order: duplicate absent id collapses to one.
    _check(
        _missing(plugin, ["e4", "e4", "e1"]) == ["e4"],
        f"[{label}] dedup: duplicate absent e4 -> one entry; active e1 excluded",
    )


def test_twins_and_parity(provider: PostgresProvider, schema: str) -> None:
    _seed(provider, schema, eid="r1", external_id="e1", is_deleted=0)
    _seed(provider, schema, eid="r2", external_id="e2", is_deleted=0)
    _seed(provider, schema, eid="r3", external_id="e3", is_deleted=0)
    _seed(provider, schema, eid="r9", external_id="e9", is_deleted=1)

    adapter = _LiveStateAdapter(provider)
    plugins = {
        label: _make_plugin(provider_cls, plugin_cls, adapter)
        for label, provider_cls, plugin_cls in _TWINS
    }
    for label, plugin in plugins.items():
        _run_for_twin(label, plugin)

    # Twin parity: both twins return identical missing for a discriminating mix.
    mixed = ["e1", "e9", "e4", "e4", "z1"]
    local_missing = _missing(plugins["local"], mixed)
    rds_missing = _missing(plugins["rds"], mixed)
    _check(
        local_missing == rds_missing == ["e9", "e4", "z1"],
        f"TWIN PARITY: local == rds == ['e9','e4','z1'] (got {local_missing!r} / {rds_missing!r})",
    )


def main() -> int:
    if os.environ.get("PGVECTOR_FIND_MISSING_LIVE_SMOKE") != "1":
        print("=== find_missing_external_ids_live_smoke ===")
        print("  SKIP  set PGVECTOR_FIND_MISSING_LIVE_SMOKE=1 to run; needs the live solet DB.")
        return 0
    print("=== find_missing_external_ids_live_smoke ===")
    schema_name = f"example_test_findmissing_{secrets.token_hex(3)}"
    provider = PostgresProvider(_load_pg_config(schema_name))
    provider.initialize()
    try:
        _create_table(provider, schema_name)
        test_twins_and_parity(provider, schema_name)
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
