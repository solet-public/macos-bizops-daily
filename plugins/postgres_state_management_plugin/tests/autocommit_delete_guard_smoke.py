#!/usr/bin/env python3
"""Live cross-twin smoke for the autocommit delete_records empty-filter guard.

The autocommit ``delete_records`` facade (postgres ``PostgresStatePlugin`` +
rds ``state_delete``) rejects an empty / non-dict / missing filter UP-FRONT
with an error ``ActionResult`` (``delete.invalid_filters``) instead of letting
it compile to an empty WHERE — the standing fail-fast defense against a
delete-all. Mirrors the typed-txn ``delete_records`` guard. A real filter still
deletes normally and the rejected calls leave the table untouched.

Sandboxed via temporary schemas (one per provider); cleanup drops them in a
``finally``. Env-gated behind ``AUTOCOMMIT_DELETE_GUARD_SMOKE=1``.

Run::

    AUTOCOMMIT_DELETE_GUARD_SMOKE=1 \\
      .venv/bin/python3 \\
      plugins/postgres_state_management_plugin/tests/autocommit_delete_guard_smoke.py
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, LiteralString, cast

from ananta.core.domain.types import ActionResult

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(_REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)
sys.path.insert(
    0,
    str(_REPO_ROOT / "plugins" / "rds_postgres_state_management_plugin" / "src"),
)

from postgres_state_management_plugin.plugin import (  # noqa: E402
    PostgresStatePlugin,
)
from postgres_state_management_plugin.postgres_backend.config import (  # noqa: E402
    PostgresConfig,
)
from postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    PostgresProvider,
)
from rds_postgres_state_management_plugin.postgres_backend.config import (  # noqa: E402
    PostgresConfig as RdsConfig,
)
from rds_postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    PostgresProvider as RdsProvider,
)
from rds_postgres_state_management_plugin.rds_crud import (  # noqa: E402
    state_delete as rds_state_delete,
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
    _REPO_ROOT / "profile" / "config" / "plugins"
    / "postgres_state_management_plugin.json"
)

_NS = "delguard"
_TABLE = "row"
_PHYSICAL = f"{_NS}__{_TABLE}"

AnyProvider = PostgresProvider | RdsProvider
# A facade callable: (namespace, query) -> ActionResult.
DeleteFacade = Callable[[str, dict[str, object]], ActionResult]


def _raw_config() -> dict[str, Any]:
    return json.loads(_PROFILE_PG_CONFIG.read_text(encoding="utf-8"))


def _create_probe_table(provider: AnyProvider, schema: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'CREATE TABLE "{schema}"."{_PHYSICAL}" ('
                "id text PRIMARY KEY, val text, "
                "is_deleted integer NOT NULL DEFAULT 0)",
            )
        )


def _drop_schema(provider: AnyProvider, schema: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(LiteralString, f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        )


def _seed(provider: AnyProvider) -> None:
    for rid in ("r1", "r2", "r3"):
        provider.insert(_NS, _TABLE, {"id": rid, "val": rid, "is_deleted": 0})


def _is_error(result: ActionResult, code: str) -> bool:
    error = result.get("error")
    return (
        result.get("action_status") == "error"
        and isinstance(error, dict)
        and error.get("code") == code
    )


def case_guard(
    provider: AnyProvider, delete: DeleteFacade, label: str
) -> None:
    """Empty / non-dict / missing filter -> error result; no delete-all."""
    bad_queries: tuple[tuple[dict[str, object], str], ...] = (
        ({"table": _TABLE, "filters": {}}, "empty filter"),
        ({"table": _TABLE, "filters": "nope"}, "non-dict filter"),
        ({"table": _TABLE}, "missing filter"),
    )
    for bad_query, desc in bad_queries:
        result = delete(_NS, bad_query)
        _check(
            _is_error(result, "delete.invalid_filters"),
            f"[{label}] autocommit delete_records rejects {desc} "
            f"(delete.invalid_filters, no delete-all) (got {result.get('error')!r})",
        )
    # The rejected deletes must NOT have touched the table.
    remaining = provider.aggregate(_NS, _TABLE, "count", None, {})
    _check(
        remaining == 3,
        f"[{label}] rejected deletes left the table intact (count={remaining}, want 3)",
    )
    # A real (non-empty) filter still deletes normally.
    ok = delete(_NS, {"table": _TABLE, "filters": {"id": "r1"}, "soft_delete": False})
    deleted = ok.get("data", {}).get("result", {}).get("deleted")  # type: ignore[union-attr]
    after = provider.aggregate(_NS, _TABLE, "count", None, {})
    _check(
        ok.get("action_status") == "completed" and deleted == 1 and after == 2,
        f"[{label}] a real filter still deletes (deleted={deleted}, "
        f"remaining={after})",
    )


def _make_local(schema: str) -> PostgresProvider:
    cfg = PostgresConfig(**_raw_config())
    cfg.pg_schema = schema
    p = PostgresProvider(cfg)
    p.initialize()
    return p


def _make_rds(schema: str) -> RdsProvider:
    cfg = RdsConfig(**_raw_config())
    cfg.pg_schema = schema
    p = RdsProvider(cfg)
    p.initialize()
    return p


def _postgres_facade(provider: PostgresProvider) -> DeleteFacade:
    """Bind the real PostgresStatePlugin.delete_records to a live provider."""
    plugin = object.__new__(PostgresStatePlugin)
    plugin._provider = provider
    return plugin.delete_records


def main() -> int:
    if os.environ.get("AUTOCOMMIT_DELETE_GUARD_SMOKE") != "1":
        print(
            "  SKIP  AUTOCOMMIT_DELETE_GUARD_SMOKE != 1; "
            "creates/drops sandbox schemas in the live DB.",
        )
        return 0

    local_schema = f"example_test_delguard_local_{secrets.token_hex(4)}"
    rds_schema = f"example_test_delguard_rds_{secrets.token_hex(4)}"
    local = _make_local(local_schema)
    rds = _make_rds(rds_schema)
    try:
        _create_probe_table(local, local_schema)
        _create_probe_table(rds, rds_schema)
        _seed(local)
        _seed(rds)
        case_guard(local, _postgres_facade(local), "local")
        case_guard(
            rds, lambda ns, q: rds_state_delete(rds, ns, q), "rds"
        )
    finally:
        _drop_schema(local, local_schema)
        _drop_schema(rds, rds_schema)

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
