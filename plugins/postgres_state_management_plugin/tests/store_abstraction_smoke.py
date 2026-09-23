#!/usr/bin/env python3
"""Smoke verification of the :class:`Store` abstraction.

Opens an :class:`InMemoryStore` and a :class:`PostgresStore` against the
SAME :class:`TableSchema` declaration, runs the same 15-step CRUD
sequence against both, and asserts identical observable behavior.

Also verifies the storage-lifetime contract:

* in-memory: a freshly-constructed store has zero rows (process-local,
  no persistence)
* postgres: after destructing the store and opening a new one against
  the same table, prior rows are still present

Postgres connection settings come from
``profile/config/plugins/postgres_state_management_plugin.json``.  The
script creates a one-off ``store_smoke_thing`` table, runs the sequence,
and drops the table on the way out (success or failure).

Sandboxed via a one-off table; cleanup drops it in a ``finally``.
Env-gated behind ``STORE_ABSTRACTION_SMOKE=1``.

Standalone — not pytest.  Run with::

    STORE_ABSTRACTION_SMOKE=1 \\
      .venv/bin/python3 \\
      plugins/postgres_state_management_plugin/tests/store_abstraction_smoke.py
"""

from __future__ import annotations

# ruff: noqa: E402
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ananta.services.store import (
    EmptyUpdateError,
    InMemoryStore,
    Store,
    UniqueViolationError,
    open_store,
)
from ananta.types.column_types import ColumnType
from ananta.types.schema_types import ColumnDefinition, TableSchema

# Side-effect import: registers the "postgres" backend with the factory.
from postgres_state_management_plugin.postgres_backend import store_factory as _pg_store_module
from postgres_state_management_plugin.postgres_backend.config import PostgresConfig
from postgres_state_management_plugin.postgres_backend.provider import PostgresProvider
from postgres_state_management_plugin.postgres_backend.utils import build_table_name
from psycopg import sql

# Quiet "unused import" lints: the import above is for its side effect
# (backend registration), but pyright/ruff want a reference.
assert _pg_store_module.PostgresStore is not None


SMOKE_NAMESPACE = "store_smoke"
SMOKE_TABLE = "thing"
PROFILE_CONFIG_PATH = (
    Path(__file__).resolve().parents[3]
    / "profile"
    / "config"
    / "plugins"
    / "postgres_state_management_plugin.json"
)


def build_schema() -> TableSchema:
    """The shared schema declaration both backends operate over.

    A unique business column (``thing_key``) plus a free-text column
    (``label``) is enough to exercise auto-id, unique constraints, soft
    delete, touch, and upsert.
    """
    return TableSchema(
        table_name=SMOKE_TABLE,
        description="Smoke-test thing",
        id_prefix="thg",
        columns={
            "thing_key": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                unique=True,
                description="Business identifier",
            ),
            "label": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Human-readable label",
            ),
        },
    )


def load_postgres_config() -> PostgresConfig:
    """Read the profile's postgres config (matches the live platform DB)."""
    raw = json.loads(PROFILE_CONFIG_PATH.read_text(encoding="utf-8"))
    return PostgresConfig(**raw)


def make_postgres_provider() -> PostgresProvider:
    """Initialize a provider against the configured DB.

    Also creates the smoke table via the legacy ``create_table`` path so
    the BEFORE UPDATE trigger fires (the modern DDL-renderer path would
    work too but requires plugin_schema_service plumbing).
    """
    cfg = load_postgres_config()
    provider = PostgresProvider(cfg)
    provider.initialize()
    return provider


def install_smoke_table(provider: PostgresProvider) -> None:
    """Create ``{schema}.{namespace}__{table}`` with all standard columns."""
    columns: dict[str, ColumnType | str | ColumnDefinition] = {
        "id": ColumnDefinition(type=ColumnType.TEXT, primary_key=True),
        "external_id": ColumnDefinition(type=ColumnType.TEXT),
        "namespace": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
        "created_at": ColumnType.DATETIME,
        "updated_at": ColumnType.DATETIME,
        "created_by": ColumnDefinition(type=ColumnType.TEXT),
        "updated_by": ColumnDefinition(type=ColumnType.TEXT),
        "name": ColumnDefinition(type=ColumnType.TEXT),
        "is_deleted": ColumnDefinition(type=ColumnType.INTEGER, default=0),
        "thing_key": ColumnDefinition(
            type=ColumnType.TEXT, not_null=True, unique=True,
        ),
        "label": ColumnDefinition(type=ColumnType.TEXT),
    }
    provider.create_table(
        namespace=SMOKE_NAMESPACE,
        table=SMOKE_TABLE,
        columns=columns,
        table_prefix="thg",
    )


def drop_smoke_table(provider: PostgresProvider) -> None:
    """Hard-drop the smoke table.

    Best-effort cleanup — the script tolerates a missing table so a
    failed prior run doesn't leave a stale table that blocks re-run.
    """
    full_name = build_table_name(SMOKE_NAMESPACE, SMOKE_TABLE)
    with provider.get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(
                sql.Identifier(provider.config.schema_name, full_name),
            ),
        )


# ---------------------------------------------------------------------------
# Shared CRUD sequence — each step asserts the observable outcome
# ---------------------------------------------------------------------------


def _step_insert_and_read_alpha(store: Store) -> tuple[str, dict[str, Any]]:
    """Steps 1-2: insert alpha and verify the standard fields read back."""
    id_a = store.insert({"thing_key": "alpha", "label": "first"})
    print(f"  1. insert alpha -> {id_a}")
    assert id_a.startswith("thg_"), f"id should carry schema id_prefix, got {id_a!r}"

    row_a = store.read_one({"thing_key": "alpha"})
    assert row_a is not None, "alpha should be readable"
    assert row_a["label"] == "first"
    assert row_a["namespace"] == SMOKE_NAMESPACE
    assert row_a["is_deleted"] in (0, False)
    assert row_a["created_at"] == row_a["updated_at"], (
        "fresh insert: created_at == updated_at"
    )
    print("  2. read alpha — standard fields present")
    return id_a, row_a


def _step_insert_bravo_and_read_all(store: Store) -> str:
    """Steps 3-5: insert bravo, read all, and filter-read bravo."""
    id_b = store.insert({"thing_key": "bravo", "label": "second"})
    rows = store.read()
    assert len(rows) == 2, f"expected 2 rows, got {len(rows)}"
    print(f"  3-4. insert bravo + read all -> {len(rows)} rows")

    bravo_rows = store.read({"thing_key": "bravo"})
    assert len(bravo_rows) == 1
    assert bravo_rows[0]["id"] == id_b
    return id_b


def _step_duplicate_insert_raises(store: Store) -> None:
    """Step 6: duplicate insert on the unique column raises UniqueViolationError."""
    raised = False
    try:
        store.insert({"thing_key": "alpha", "label": "dup"})
    except UniqueViolationError as exc:
        raised = True
        assert exc.column == "thing_key", f"expected thing_key, got {exc.column!r}"
    assert raised, "expected UniqueViolationError"
    print("  6. duplicate insert -> UniqueViolationError")


def _step_update_alpha(store: Store) -> dict[str, Any]:
    """Steps 7-8: update alpha's label and verify updated_at advanced."""
    n_updated = store.update({"thing_key": "alpha"}, {"label": "first-updated"})
    assert n_updated == 1, f"expected 1 update, got {n_updated}"

    row_a_v2 = store.read_one({"thing_key": "alpha"})
    assert row_a_v2 is not None
    assert row_a_v2["label"] == "first-updated"
    assert row_a_v2["updated_at"] > row_a_v2["created_at"], (
        "update should bump updated_at"
    )
    print("  7-8. update alpha -> updated_at advanced")
    return row_a_v2


def _step_touch_alpha(store: Store, prev_row: dict[str, Any]) -> None:
    """Step 9: touch alpha — updated_at advances, label unchanged."""
    n_touched = store.touch({"thing_key": "alpha"})
    assert n_touched == 1
    row_a_v3 = store.read_one({"thing_key": "alpha"})
    assert row_a_v3 is not None
    assert row_a_v3["label"] == "first-updated", "touch must not change other fields"
    assert row_a_v3["updated_at"] > prev_row["updated_at"], (
        "touch should advance updated_at"
    )
    print("  9. touch alpha -> updated_at advanced, label unchanged")


def _step_upsert_charlie_and_bravo(store: Store, id_b: str) -> str:
    """Steps 10-11: upsert new (charlie) + upsert existing (bravo) keeps id."""
    id_c = store.upsert(
        {"thing_key": "charlie", "label": "third"},
        conflict_columns=["thing_key"],
    )
    assert id_c.startswith("thg_")

    id_b2 = store.upsert(
        {"thing_key": "bravo", "label": "second-upserted"},
        conflict_columns=["thing_key"],
    )
    assert id_b2 == id_b, "upsert on existing must return existing id"
    bravo_v2 = store.read_one({"thing_key": "bravo"})
    assert bravo_v2 is not None
    assert bravo_v2["label"] == "second-upserted"
    print(f"  10-11. upsert charlie ({id_c}) + upsert bravo -> updated")
    return id_c


def _step_soft_delete_alpha(store: Store) -> None:
    """Steps 12-14: soft-delete alpha and verify default vs. include_deleted reads."""
    n_soft = store.delete({"thing_key": "alpha"}, soft_delete=True)
    assert n_soft == 1

    alpha_default = store.read({"thing_key": "alpha"})
    assert alpha_default == [], "soft-deleted row should be absent by default"

    alpha_with_deleted = store.read({"thing_key": "alpha"}, include_deleted=True)
    assert len(alpha_with_deleted) == 1
    assert alpha_with_deleted[0]["is_deleted"] in (1, True)
    print("  12-14. soft delete alpha + visibility flips")


def _step_empty_update_raises(store: Store) -> None:
    """Step 15a: empty updates dict raises EmptyUpdateError."""
    raised_empty = False
    try:
        store.update({"thing_key": "bravo"}, {})
    except EmptyUpdateError:
        raised_empty = True
    assert raised_empty, "empty updates dict should raise"
    print("  15a. empty update -> EmptyUpdateError")


def _step_hard_delete_bravo(store: Store) -> None:
    """Step 15b: hard-delete bravo — gone even with include_deleted."""
    n_hard = store.delete({"thing_key": "bravo"}, soft_delete=False)
    assert n_hard == 1
    bravo_gone = store.read({"thing_key": "bravo"}, include_deleted=True)
    assert bravo_gone == [], "hard-deleted row must not be visible even with include_deleted"
    print("  15b. hard delete bravo -> row gone entirely")


def run_sequence(store: Store, label: str) -> dict[str, Any]:
    """15-step CRUD walk; returns a few values for cross-backend comparison."""
    print(f"--- {label}: running sequence")

    id_a, row_a = _step_insert_and_read_alpha(store)
    id_b = _step_insert_bravo_and_read_all(store)
    _step_duplicate_insert_raises(store)

    # Small sleep so updated_at strictly advances under coarse clocks.
    time.sleep(0.05)
    row_a_v2 = _step_update_alpha(store)

    time.sleep(0.05)
    _step_touch_alpha(store, row_a_v2)

    id_c = _step_upsert_charlie_and_bravo(store, id_b)
    _step_soft_delete_alpha(store)
    _step_empty_update_raises(store)
    _step_hard_delete_bravo(store)

    summary = {
        "alpha_id": id_a,
        "bravo_id": id_b,
        "charlie_id": id_c,
        "alpha_namespace": row_a["namespace"],
        "id_prefix_ok": id_a.startswith("thg_") and id_c.startswith("thg_"),
    }
    print(f"--- {label}: sequence complete")
    return summary


def verify_lifetime_contract(
    schema: TableSchema, provider: PostgresProvider,
) -> None:
    """Demonstrate the two backends' different lifetime semantics."""
    print("--- lifetime contract")

    # In-memory: a freshly-constructed store is empty even though we
    # already used one in this process.
    fresh_in_memory = InMemoryStore(schema, namespace=SMOKE_NAMESPACE)
    assert fresh_in_memory.read() == [], "new InMemoryStore must start empty"
    print("  in-memory: fresh store has zero rows")

    # Postgres: a freshly-constructed store sees rows from the prior
    # run (charlie survived; alpha was soft-deleted; bravo was hard-deleted).
    fresh_pg = open_store(
        schema, namespace=SMOKE_NAMESPACE, backend="postgres", provider=provider,
    )
    survivors = fresh_pg.read(include_deleted=True)
    survivor_keys = sorted(r["thing_key"] for r in survivors)
    # alpha (soft-deleted, still in table) + charlie (live)
    assert survivor_keys == ["alpha", "charlie"], (
        f"expected ['alpha', 'charlie'], got {survivor_keys}"
    )
    print(f"  postgres: rows survived restart ({survivor_keys})")


def assert_parity(in_memory: dict[str, Any], postgres: dict[str, Any]) -> None:
    """Cross-backend assertions: id_prefix shape + namespace echo line up."""
    print("--- cross-backend parity")
    assert in_memory["id_prefix_ok"] and postgres["id_prefix_ok"], (
        "both backends must auto-id with schema.id_prefix"
    )
    assert in_memory["alpha_namespace"] == postgres["alpha_namespace"] == SMOKE_NAMESPACE
    print(f"  both backends: id_prefix=thg, namespace={SMOKE_NAMESPACE}")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main() -> int:
    if os.environ.get("STORE_ABSTRACTION_SMOKE") != "1":
        print(
            "  SKIP  STORE_ABSTRACTION_SMOKE != 1; creates/drops a sandbox "
            "table in the live DB.",
        )
        return 0

    schema = build_schema()
    provider = make_postgres_provider()
    # Ensure clean slate before run (idempotency across re-runs).
    drop_smoke_table(provider)
    install_smoke_table(provider)
    try:
        in_memory_store = open_store(
            schema, namespace=SMOKE_NAMESPACE, backend="in_memory",
        )
        postgres_store = open_store(
            schema,
            namespace=SMOKE_NAMESPACE,
            backend="postgres",
            provider=provider,
        )

        in_memory_summary = run_sequence(in_memory_store, "in_memory")
        postgres_summary = run_sequence(postgres_store, "postgres")
        assert_parity(in_memory_summary, postgres_summary)
        verify_lifetime_contract(schema, provider)

        print()
        print("STORE ABSTRACTION SMOKE: PASS")
        return 0
    except AssertionError as exc:
        print("STORE ABSTRACTION SMOKE: FAIL")
        traceback.print_exc()
        print(f"\nAssertion failure: {exc}")
        return 1
    except Exception:
        print("STORE ABSTRACTION SMOKE: ERROR")
        traceback.print_exc()
        return 2
    finally:
        drop_smoke_table(provider)
        provider.close()


if __name__ == "__main__":
    sys.exit(main())
