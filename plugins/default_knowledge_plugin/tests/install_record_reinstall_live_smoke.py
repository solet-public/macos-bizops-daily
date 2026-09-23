#!/usr/bin/env python3
"""Live-PG coverage for the KB install-record re-install (delete-then-reinsert)
cycle, against the REAL standardized DDL.

Permanent live coverage for the K0/D4a migration of ``install_kb``'s own-record
writes onto the typed ``StateTransaction`` primitives. The re-install path, when
an install record already exists for ``name``:

    txn1: txn.delete_records(soft_delete=False, filters={"name": name})  # HARD
    (outside txn) index_files
    txn2: txn.write_state(record={id: NEW, name: SAME, is_active: 1, ...})

exercises ``_PostgresStateTransaction.delete_records(soft_delete=False)`` followed
by ``write_state`` against the REAL install-record schema — UNIQUE(name),
CHECK(is_active IN (0,1)), the standardizer-defaulted audit columns
(created_at/updated_at/is_deleted, all omitted by the record), JSON columns
(manifest_tags/process_keys/memory_ids), and RETURNING id. The offline w5p /
auto_uninstall smokes are stub-only; this cycle runs against the real DDL.

The table is built via the EXACT production render path:
``SchemaStandardizer().standardize_schema(get_knowledge_schema())`` →
``emit_create_table_ops`` (the same ops ``PluginSchemaLifecycle._install_fresh``
emits). The transaction is the real ``_PostgresStateTransaction`` constructed
exactly as ``PostgresStatePlugin.transactional()`` does.

Negative controls make the positive assertions non-vacuous:
* re-insert SAME name WITHOUT delete must RAISE (proves UNIQUE(name) is real, so
  the post-delete re-insert succeeding is meaningful).
* is_active=2 must RAISE (proves CHECK(is_active IN (0,1)) is real, so is_active=1
  being accepted is meaningful).

Run (needs the live solet DB)::
    KB_REINSTALL_LIVE_SMOKE=1 .venv/bin/python3 \\
      plugins/default_knowledge_plugin/tests/install_record_reinstall_live_smoke.py
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, LiteralString, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_knowledge_plugin" / "src"))

from ananta.types.schema_standardizer import SchemaStandardizer  # noqa: E402
from default_knowledge_plugin.constants import (  # noqa: E402
    PLUGIN_NAME,
    TABLE_KNOWLEDGE_INSTALL,
)
from default_knowledge_plugin.schema import get_knowledge_schema  # noqa: E402
from postgres_state_management_plugin.plugin import (  # noqa: E402
    _PostgresStateTransaction,
)
from postgres_state_management_plugin.postgres_backend.config import (  # noqa: E402
    PostgresConfig,
)
from postgres_state_management_plugin.postgres_backend.ddl_renderer import (  # noqa: E402
    emit_create_table_ops,
)
from postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    PostgresProvider,
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


_PROFILE_PG_CONFIG = (
    REPO_ROOT / "profile" / "config" / "plugins"
    / "postgres_state_management_plugin.json"
)


def _load_pg_config(schema_name: str) -> PostgresConfig:
    config = PostgresConfig(**json.loads(_PROFILE_PG_CONFIG.read_text(encoding="utf-8")))
    config.pg_schema = schema_name
    return config


def _build_real_install_table(provider: PostgresProvider, schema_name: str) -> None:
    """Render + execute the REAL install-record DDL via the production path."""
    standardized = SchemaStandardizer().standardize_schema(get_knowledge_schema())
    table = standardized.tables[TABLE_KNOWLEDGE_INSTALL]
    ops = emit_create_table_ops(PLUGIN_NAME, table, schema_name)
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        for op in ops:
            cur.execute(op)


def _record(*, rid: str, name: str, is_active: int = 1) -> dict[str, Any]:
    """A record dict matching install_kb's write_state shape."""
    return {
        "id": rid,
        "namespace": PLUGIN_NAME,
        "name": name,
        "source": None,
        "source_type": "local",
        "resolved_path": f"/tmp/kb/{name}",
        "manifest_name": f"{name}-manifest",
        "manifest_tags": ["alpha", "beta"],
        "process_keys": ["p.one", "p.two"],
        "chunk_count": 3,
        "memory_ids": ["mem-1", "mem-2", "mem-3"],
        "branch": None,
        "last_indexed_commit": None,
        "is_active": is_active,
        "indexed_at": "2026-06-22T00:00:00",
    }


def _write_in_txn(provider: PostgresProvider, record: dict[str, Any]) -> str:
    """Exactly mirrors PostgresStatePlugin.transactional() + txn.write_state."""
    with provider.get_transactional_connection() as conn:
        txn = _PostgresStateTransaction(conn, provider)
        return txn.write_state(
            namespace=PLUGIN_NAME,
            data={"table": TABLE_KNOWLEDGE_INSTALL, "record": record},
        )


def _delete_in_txn(provider: PostgresProvider, name: str) -> int:
    with provider.get_transactional_connection() as conn:
        txn = _PostgresStateTransaction(conn, provider)
        return txn.delete_records(
            namespace=PLUGIN_NAME,
            query={
                "table": TABLE_KNOWLEDGE_INSTALL,
                "filters": {"name": name},
                "soft_delete": False,
            },
        )


def _select_by_name(provider: PostgresProvider, name: str) -> list[dict[str, Any]]:
    return provider.select(
        namespace=PLUGIN_NAME,
        table=TABLE_KNOWLEDGE_INSTALL,
        conditions={"name": name},
    )


def _scalar(provider: PostgresProvider, schema: str, col: str, rid: str) -> object:
    full = f"{PLUGIN_NAME}__{TABLE_KNOWLEDGE_INSTALL}"
    rows = provider.execute_query(
        f'SELECT "{col}" FROM "{schema}"."{full}" WHERE id = %s', (rid,)
    )
    return rows[0][0] if rows else "<<absent>>"


def _raised(fn: Any) -> bool:
    try:
        fn()
    except Exception:  # noqa: BLE001 — any constraint violation counts
        return True
    return False


def _as_list(value: object) -> object:
    return json.loads(value) if isinstance(value, str) else value


def test_fresh_install(provider: PostgresProvider, schema: str) -> None:
    """write_state against the real DDL: RETURNING id, JSON round-trip, audit defaults."""
    rid = "kin-fresh-aaa"
    returned = _write_in_txn(provider, _record(rid=rid, name="kb-reinstall"))
    _check(returned == rid, f"write_state RETURNING id echoes caller id; got {returned!r}")
    rows = _select_by_name(provider, "kb-reinstall")
    _check(len(rows) == 1 and rows[0]["id"] == rid,
           f"exactly one row after fresh install; got {len(rows)}")
    _check(rows and rows[0]["is_active"] == 1, "is_active stored as 1")
    _check(_as_list(rows[0]["manifest_tags"]) == ["alpha", "beta"],
           f"manifest_tags JSON round-trips; got {rows[0].get('manifest_tags')!r}")
    _check(_as_list(rows[0]["memory_ids"]) == ["mem-1", "mem-2", "mem-3"],
           "memory_ids JSON round-trips")
    # Audit columns the record OMITS must be standardizer-defaulted (non-null).
    _check(_scalar(provider, schema, "created_at", rid) is not None,
           "omitted created_at defaulted (standardizer NOT-NULL default)")
    _check(_scalar(provider, schema, "updated_at", rid) is not None,
           "omitted updated_at defaulted")
    _check(_scalar(provider, schema, "is_deleted", rid) == 0,
           "omitted is_deleted defaulted to 0")


def test_unique_is_real(provider: PostgresProvider) -> None:
    """Negative control: re-insert SAME name WITHOUT delete must RAISE."""
    _check(
        _raised(lambda: _write_in_txn(provider, _record(rid="kin-dup-bbb", name="kb-reinstall"))),
        "re-insert SAME name without delete RAISES (UNIQUE(name) is enforced)",
    )


def test_reinstall_cycle(provider: PostgresProvider) -> None:
    """LOAD-BEARING: hard-delete then re-insert SAME name with a NEW id succeeds."""
    affected = _delete_in_txn(provider, "kb-reinstall")
    _check(affected == 1, f"delete_records(soft_delete=False) removed 1 row; got {affected}")
    _check(len(_select_by_name(provider, "kb-reinstall")) == 0,
           "row physically gone after hard-delete (not soft-flagged)")
    rid2 = "kin-reinstall-ccc"
    returned = _write_in_txn(provider, _record(rid=rid2, name="kb-reinstall"))
    _check(returned == rid2,
           f"RE-INSERT same name + new id succeeds, no UNIQUE collision; got {returned!r}")
    rows = _select_by_name(provider, "kb-reinstall")
    _check(len(rows) == 1 and rows[0]["id"] == rid2,
           f"exactly one row, now the new id, after re-install; got {[r['id'] for r in rows]}")
    _check(rows and rows[0]["is_active"] == 1, "re-installed row is_active=1")


def test_check_is_real(provider: PostgresProvider) -> None:
    """Negative control: is_active=2 must RAISE (CHECK is real → is_active=1 accept is meaningful)."""
    _check(
        _raised(lambda: _write_in_txn(provider, _record(rid="kin-chk-ddd", name="kb-check", is_active=2))),
        "is_active=2 RAISES (CHECK(is_active IN (0,1)) is enforced)",
    )
    rid = "kin-chk-ok"
    returned = _write_in_txn(provider, _record(rid=rid, name="kb-check-ok", is_active=0))
    _check(returned == rid, "is_active=0 accepted (boundary of the CHECK set)")


def main() -> int:
    if os.environ.get("KB_REINSTALL_LIVE_SMOKE") != "1":
        print("=== install_record_reinstall_live_smoke ===")
        print("  SKIP  set KB_REINSTALL_LIVE_SMOKE=1 to run; needs the live solet DB.")
        return 0
    print("=== install_record_reinstall_live_smoke ===")
    schema_name = f"example_test_kb_reinstall_{secrets.token_hex(3)}"
    provider = PostgresProvider(_load_pg_config(schema_name))
    provider.initialize()
    try:
        _build_real_install_table(provider, schema_name)
        test_fresh_install(provider, schema_name)
        test_unique_is_real(provider)
        test_reinstall_cycle(provider)
        test_check_is_real(provider)
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
