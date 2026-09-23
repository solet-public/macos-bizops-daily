#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for the #11 ``list_active_sessions`` migration.

SQL lockdown: ``list_active_sessions`` retires off the raw
``session INNER JOIN active_lease`` ``execute_sql`` (``_fetch_all``) onto two
single-namespace ``query_state`` reads + a Python inner-merge. Because the merge
+ the ``expires_at > now`` boundary + the INNER-JOIN cardinality are the exact
behaviors that move from SQL into Python, this smoke SEEDS sentinel session +
active_lease rows and hard-deletes them in a ``finally``. A FIXED injected clock
makes the Gap-A ``gt`` boundary deterministic.

Verifies:

* live lease + live session → INCLUDED, with the exact 9-key projection (session
  fields from the session row, lease fields from the lease row);
* ordering is ``expires_at`` DESC across multiple live leases;
* expired lease (``expires_at < now``) → EXCLUDED;
* lease expiring EXACTLY at ``now`` → EXCLUDED (strict ``>``, not ``>=`` — proves
  the Gap-A op is ``gt`` and the tz-strip lands the comparison value on the same
  naive-UTC wall clock as the stored column);
* live lease whose session is ``is_deleted=1`` → EXCLUDED (INNER-JOIN drop via
  the session read's ``is_deleted=0`` filter);
* live lease whose session row is MISSING → EXCLUDED (INNER-JOIN drop);
* ``is_deleted=1`` lease + live session → EXCLUDED (lease read's ``is_deleted=0``).
  The three excluded discriminators (deleted-session / missing-session /
  deleted-lease) are seeded with the LATEST ``expires_at`` so a regression that
  failed to drop them would surface at the FRONT of the desc-sorted result;
* the datetime fields (``expires_at`` / ``last_seen_at`` / ``last_event_at``)
  read back as ISO STRINGS — the same type the old ``execute_sql`` path returned
  (both run the shared ``_serialize_for_json``), proving the migration did not
  silently narrow the field type.

The ``_LiveStateAdapter.query_state`` mirrors the production
``plugin.query_state`` → ``read_state`` → ``provider.select(serialize=None)``
path exactly (raw bind, ``_serialize_for_json``, ``{records, count}`` envelope),
so a green run here discriminates the real read path.

There are NO DB-level foreign keys (FKs are repository-enforced per schema.py),
so sentinel ``source_id`` / ``session_id`` strings are valid. ``active_lease``
carries a UNIQUE ``session_id``, so each lease uses a distinct session id.
Env-gated behind ``LEDGER_READ_LIVE_SMOKE=1``.

Run::

    LEDGER_READ_LIVE_SMOKE=1 \\
      .venv/bin/python3 \\
      ananta/tests/llm/session_ledger/read_migration_active_sessions_live_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, LiteralString, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)

from ananta.constants import SOLET_NAME_ENV_VAR  # noqa: E402
from ananta.llm.session_ledger.repository import (  # noqa: E402
    SessionLedgerRepository,
)
from ananta.llm.session_ledger.types import (  # noqa: E402
    SourceVendor,
)
from postgres_state_management_plugin.plugin import (  # noqa: E402
    _PostgresStateTransaction,
)
from postgres_state_management_plugin.postgres_backend.config import (  # noqa: E402
    PostgresConfig,
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


def _load_pg_config() -> PostgresConfig:
    return PostgresConfig(**json.loads(_PROFILE_PG_CONFIG.read_text(encoding="utf-8")))


def _envelope(data: dict[str, Any]) -> dict[str, Any]:
    return {"action_status": "completed", "data": data, "actions": [], "error": None}


class _LiveStateAdapter:
    """Minimal StateManagementInterface stand-in over a real provider.

    ``list_active_sessions`` calls only ``query_state``; ``transactional`` is
    present so the concrete ``SessionLedgerRepository`` constructs cleanly. The
    ``query_state`` body is the production ``plugin.query_state`` → ``read_state``
    path verbatim (``provider.select`` with the default raw serializer).
    """

    def __init__(self, provider: PostgresProvider) -> None:
        self._provider = provider

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        filters = query.get("filters") or {}
        rows = self._provider.select(
            namespace=namespace,
            table=str(query["table"]),
            conditions=cast("dict[str, Any]", filters) if isinstance(filters, dict) else None,
        )
        return _envelope({"records": rows, "count": len(rows)})

    @contextmanager
    def transactional(self) -> Any:
        with self._provider.get_transactional_connection() as conn:
            yield _PostgresStateTransaction(conn, self._provider)


_MARK = "__read_migration_active_sessions_live_smoke__"
_SCHEMA = os.environ[SOLET_NAME_ENV_VAR]
_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)

_EXPECTED_KEYS = {
    "id",
    "source_id",
    "external_session_id",
    "vendor",
    "vendor_session_label",
    "project_path",
    "last_event_at",
    "last_seen_at",
    "expires_at",
}


def _naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def _hard_delete_by_id(provider: PostgresProvider, table: str, row_id: str) -> None:
    delete_sql: LiteralString = cast(
        LiteralString,
        f'DELETE FROM "{_SCHEMA}"."session_ledger__{table}" WHERE "id" = %s',
    )
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(delete_sql, (row_id,))


def _insert(
    provider: PostgresProvider,
    tracker: list[tuple[str, str]],
    table: str,
    row: dict[str, Any],
) -> str:
    provider.insert(namespace="session_ledger", table=table, data=row)
    tracker.append((table, str(row["id"])))
    return str(row["id"])


def _session_row(
    *,
    row_id: str,
    label: str,
    project_path: str,
    is_deleted: int = 0,
) -> dict[str, Any]:
    return {
        "id": row_id,
        "namespace": "session_ledger",
        "source_id": f"src-{row_id}",
        "external_session_id": f"ext-{row_id}",
        "vendor": SourceVendor.CLAUDE_CODE.value,
        "vendor_session_label": label,
        "project_path": project_path,
        "first_event_at": _naive(_NOW - timedelta(days=1)),
        "last_event_at": _naive(_NOW - timedelta(minutes=5)),
        "event_count": 3,
        "canonical_external_session_id": None,
        "created_at": _naive(_NOW - timedelta(days=1)),
        "updated_at": _naive(_NOW),
        "is_deleted": is_deleted,
    }


def _lease_row(
    *,
    row_id: str,
    session_id: str,
    expires_at: datetime,
    is_deleted: int = 0,
) -> dict[str, Any]:
    return {
        "id": row_id,
        "namespace": "session_ledger",
        "session_id": session_id,
        "source_id": f"src-{session_id}",
        "last_seen_at": _naive(expires_at - timedelta(minutes=5)),
        "expires_at": _naive(expires_at),
        "lease_ttl_seconds": 300,
        "created_at": _naive(_NOW - timedelta(hours=1)),
        "updated_at": _naive(_NOW),
        "is_deleted": is_deleted,
    }


def test_list_active_sessions(
    repo: SessionLedgerRepository,
    provider: PostgresProvider,
    tracker: list[tuple[str, str]],
) -> None:
    # Two live leases on live sessions — the expected result set, desc by expiry.
    s_live1 = _insert(provider, tracker, "session", _session_row(
        row_id=f"les-live1-{_MARK}", label="label-1", project_path="/proj/one",
    ))
    l_live1 = _insert(provider, tracker, "active_lease", _lease_row(
        row_id=f"lse-live1-{_MARK}", session_id=s_live1,
        expires_at=_NOW + timedelta(hours=2),
    ))
    s_live2 = _insert(provider, tracker, "session", _session_row(
        row_id=f"les-live2-{_MARK}", label="label-2", project_path="/proj/two",
    ))
    _insert(provider, tracker, "active_lease", _lease_row(
        row_id=f"lse-live2-{_MARK}", session_id=s_live2,
        expires_at=_NOW + timedelta(hours=1),
    ))

    # Expired lease — excluded by expires_at > now.
    s_exp = _insert(provider, tracker, "session", _session_row(
        row_id=f"les-exp-{_MARK}", label="label-exp", project_path="/proj/exp",
    ))
    _insert(provider, tracker, "active_lease", _lease_row(
        row_id=f"lse-exp-{_MARK}", session_id=s_exp,
        expires_at=_NOW - timedelta(hours=1),
    ))

    # Lease expiring EXACTLY at now — excluded by the STRICT gt boundary.
    s_bound = _insert(provider, tracker, "session", _session_row(
        row_id=f"les-bound-{_MARK}", label="label-bound", project_path="/proj/bound",
    ))
    _insert(provider, tracker, "active_lease", _lease_row(
        row_id=f"lse-bound-{_MARK}", session_id=s_bound, expires_at=_NOW,
    ))

    # The three INNER-JOIN/soft-delete discriminators, each with the LATEST
    # expiry so a missed exclusion would surface at the FRONT of the result.

    # Deleted session + live lease — excluded (session read filters is_deleted=0).
    s_del = _insert(provider, tracker, "session", _session_row(
        row_id=f"les-delsess-{_MARK}", label="label-del", project_path="/proj/del",
        is_deleted=1,
    ))
    _insert(provider, tracker, "active_lease", _lease_row(
        row_id=f"lse-delsess-{_MARK}", session_id=s_del,
        expires_at=_NOW + timedelta(hours=3),
    ))

    # Missing session row + live lease — excluded (inner-merge drops it).
    _insert(provider, tracker, "active_lease", _lease_row(
        row_id=f"lse-missing-{_MARK}", session_id=f"les-missing-{_MARK}",
        expires_at=_NOW + timedelta(hours=4),
    ))

    # Deleted lease + live session — excluded (lease read filters is_deleted=0).
    s_dl = _insert(provider, tracker, "session", _session_row(
        row_id=f"les-dellease-{_MARK}", label="label-dl", project_path="/proj/dl",
    ))
    _insert(provider, tracker, "active_lease", _lease_row(
        row_id=f"lse-dellease-{_MARK}", session_id=s_dl,
        expires_at=_NOW + timedelta(hours=5), is_deleted=1,
    ))

    rows = repo.list_active_sessions()
    mark_rows = [r for r in rows if str(r.get("id", "")).endswith(_MARK)]

    _check(
        [str(r["id"]) for r in mark_rows] == [s_live1, s_live2],
        "only the two live leases on live sessions are returned, expires_at DESC",
    )

    excluded = {s_exp, s_bound, s_del, s_dl, f"les-missing-{_MARK}"}
    _check(
        not (excluded & {str(r["id"]) for r in mark_rows}),
        "expired / exact-now-boundary / deleted-session / deleted-lease / "
        "missing-session leases are all excluded",
    )

    if not mark_rows:
        _check(False, "no rows returned — cannot assert projection (see failures above)")
        return

    top = mark_rows[0]
    _check(set(top.keys()) == _EXPECTED_KEYS, "row has exactly the 9 projected keys")
    _check(
        top["id"] == s_live1
        and top["source_id"] == f"src-{s_live1}"
        and top["external_session_id"] == f"ext-{s_live1}"
        and top["vendor"] == SourceVendor.CLAUDE_CODE.value
        and top["vendor_session_label"] == "label-1"
        and top["project_path"] == "/proj/one",
        "session-sourced fields flow through from the session row",
    )
    _check(
        isinstance(top["expires_at"], str)
        and isinstance(top["last_seen_at"], str)
        and isinstance(top["last_event_at"], str),
        "datetime fields read back as ISO strings (return type unchanged vs the "
        "old execute_sql path)",
    )
    _check(
        datetime.fromisoformat(str(top["expires_at"])) == _naive(_NOW + timedelta(hours=2))
        and datetime.fromisoformat(str(top["last_seen_at"]))
        == _naive(_NOW + timedelta(hours=2) - timedelta(minutes=5)),
        "lease-sourced datetime values (expires_at/last_seen_at) come from the "
        f"matching lease row ({l_live1})",
    )


def main() -> int:
    if os.environ.get("LEDGER_READ_LIVE_SMOKE") != "1":
        print("=== read_migration_active_sessions_live_smoke ===")
        print(
            "  SKIP  set LEDGER_READ_LIVE_SMOKE=1 to run "
            "(needs the live solet DB)"
        )
        return 0

    print("=== read_migration_active_sessions_live_smoke ===")
    provider = PostgresProvider(_load_pg_config())
    provider.initialize()
    repo = SessionLedgerRepository(
        state_service=cast("Any", _LiveStateAdapter(provider)),
        clock=lambda: _NOW,
    )
    tracker: list[tuple[str, str]] = []
    try:
        print("test_list_active_sessions")
        test_list_active_sessions(repo, provider, tracker)
    finally:
        for table, row_id in reversed(tracker):
            _hard_delete_by_id(provider, table, row_id)

    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
