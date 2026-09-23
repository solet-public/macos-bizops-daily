#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for the polling-driver migration (SQL-lockdown Slice 2).

Pins that ``SessionLedgerPollingDriverMixin`` — migrated off raw
``transactional()`` / ``execute_sql`` SQL onto the state-interface primitives
(``_acquire_lease`` / ``update_state`` / ``query_ordered`` / ``delete_records``
+ the typed-txn read-then-write upserts) — drives the lease/cursor/batch loop
correctly against the running solet's REAL ledger schema (the real expiry-fenced
``(lease IS NULL OR lease < now)`` CAS, the real BEFORE-UPDATE trigger, the real
``timestamp`` (naive-UTC F1) columns, the real recency-window compare on a
DB-stored ``started_at``). The thin planted-rows stub cannot model the CAS or
the cutoff compare — exactly the migration's real-schema test mandate.

Coverage:

* ``try_acquire_polling_lease`` — free (NULL) acquired / live (until>now, held)
  NOT acquired / expired (until<now) re-acquired with a fresh token.
* ``refresh_polling_lease`` — owner-token extends the window / wrong-token → None.
* ``release_polling_lease`` — clears the lease so a fresh acquire succeeds.
* ``start_batch`` / ``finish_batch`` — token-fenced CAS: matching finish lands
  terminal / wrong-token finish is dropped (no clobber) / double-finish dropped.
* ``ensure_open_route_batch_for_source`` — creates one, reuses it (idempotent).
* ``adopt_route_batch_for_source`` — in-window route batch claimed (token set) /
  out-of-window (old started_at) → None / non-adoptable (token held) → None.
* ``write_cursor`` / ``read_cursor`` — discovery (scope_key NULL) insert→read,
  re-write→revive(update); event_read (scope_key) insert→read.
* ``count_active_source_cursors`` / ``reset_source_cursor`` — count reflects
  writes; reset soft-deletes all + returns the before-count; idempotent → 0.
* ``record_lease_ping`` — insert (new session) then update (expiry advances).
* ``updated_at`` advances via the BEFORE-UPDATE trigger after acquire + reset.

Writes only sentinel rows (one source + its children) and hard-deletes them in
a ``finally``. Env-gated behind ``LEDGER_POLLING_LIVE_SMOKE=1``.

Run::

    LEDGER_POLLING_LIVE_SMOKE=1 \\
      .venv/bin/python3 ananta/tests/llm/session_ledger/polling_driver_migration_live_smoke.py
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
    PollingLeaseHandle,
    SessionLedgerRepository,
)
from ananta.llm.session_ledger.types import (  # noqa: E402
    CursorScope,
    ImportBatchStatus,
    IngestSourceKind,
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
    """Full StateManagementInterface stand-in over a real provider."""

    def __init__(self, provider: PostgresProvider) -> None:
        self._provider = provider

    def write_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        row_id = self._provider.insert(
            namespace=namespace,
            table=str(data["table"]),
            data=cast("dict[str, Any]", data.get("record")),
        )
        return _envelope({"result": {"generated_id": row_id, "inserted": 1}})

    def update_state(
        self, namespace: str, query: dict[str, Any], updates: dict[str, Any]
    ) -> dict[str, Any]:
        filters = query.get("filters") or {}
        affected = self._provider.update(
            namespace=namespace,
            table=str(query["table"]),
            conditions=filters if isinstance(filters, dict) else {},
            updates=updates,
        )
        return _envelope({"result": {"updated": affected}})

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        filters = query.get("filters") or {}
        rows = self._provider.select(
            namespace=namespace,
            table=str(query["table"]),
            conditions=cast("dict[str, Any]", filters) if isinstance(filters, dict) else None,
        )
        return _envelope({"records": rows, "count": len(rows)})

    def query_ordered(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        filters = data.get("filters") or {}
        order_by = cast("list[list[str]]", data.get("order_by") or [])
        rows = self._provider.select_ordered(
            namespace=namespace,
            table=str(data["table"]),
            conditions=cast("dict[str, Any]", filters) if isinstance(filters, dict) else {},
            order_columns=tuple(str(pair[0]) for pair in order_by),
            direction=str(order_by[0][1]) if order_by else "asc",
            limit=int(cast("int", data["limit"])),
        )
        return _envelope({"records": rows, "count": len(rows)})

    def delete_records(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        filters = query.get("filters") or {}
        soft = bool(query.get("soft_delete", True))
        deleted = self._provider.delete(
            namespace=namespace,
            table=str(query["table"]),
            conditions=cast("dict[str, Any]", filters) if isinstance(filters, dict) else {},
            soft_delete=soft,
        )
        return _envelope({"result": {"deleted": deleted, "soft_delete": soft}})

    def acquire_lease(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        acquired = self._provider.acquire_lease(
            namespace=namespace,
            table=str(data["table"]),
            filters=cast("dict[str, Any]", data["filters"]),
            lease_column=str(data["lease_column"]),
            now=cast("datetime", data["now"]),
            set_values=cast("dict[str, Any]", data["set"]),
        )
        return _envelope({"result": {"acquired": acquired}})

    @contextmanager
    def transactional(self) -> Any:
        with self._provider.get_transactional_connection() as conn:
            yield _PostgresStateTransaction(conn, self._provider)


_MARK = "__polling_driver_migration_live_smoke__"
_SCHEMA = os.environ[SOLET_NAME_ENV_VAR]
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def _row(provider: PostgresProvider, table: str, row_id: str) -> dict[str, Any]:
    rows = provider.select(namespace="session_ledger", table=table, conditions={"id": row_id})
    assert len(rows) == 1, f"expected 1 {table} row for {row_id}, got {len(rows)}"
    return rows[0]


def _as_dt(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise AssertionError(f"expected datetime/ISO cell, got {type(value).__name__}")


def _hard_delete_where(provider: PostgresProvider, table: str, column: str, value: str) -> None:
    delete_sql: LiteralString = cast(
        LiteralString,
        f'DELETE FROM "{_SCHEMA}"."session_ledger__{table}" WHERE "{column}" = %s',
    )
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(delete_sql, (value,))


def test_polling_driver_lifecycle(  # noqa: PLR0915 — one linear lifecycle
    repo: SessionLedgerRepository, provider: PostgresProvider
) -> None:
    source_id = ""
    session_id = f"les-{_MARK}"
    try:
        source_id = repo.insert_source(
            source_kind=IngestSourceKind.CODEX_PUSHED,
            root_uri=f"pushed:{_MARK}",
            account_label=_MARK,
            config={},
        )

        # ── try_acquire: free → acquired ──
        h1 = repo.try_acquire_polling_lease(source_id, ttl_seconds=600)
        _check(h1 is not None, "acquire on a free (NULL) lease succeeds")
        src = _row(provider, "source", source_id)
        _check(
            _as_dt(src["polling_lease_until"]) == _naive(_NOW + timedelta(seconds=600)),
            "acquire set polling_lease_until = now + ttl",
        )
        acquire_updated_at = _as_dt(src["updated_at"])
        _check(src["polling_lease_token"] is not None, "acquire set a fence token")

        # ── try_acquire: live (held, until>now) → NOT acquired ──
        h2 = repo.try_acquire_polling_lease(source_id, ttl_seconds=600)
        _check(h2 is None, "acquire on a live (unexpired) lease is refused")

        # ── try_acquire: expired (until<now) → re-acquired, fresh token ──
        provider.update(
            namespace="session_ledger", table="source",
            conditions={"id": source_id},
            updates={"polling_lease_until": _naive(_NOW - timedelta(seconds=60))},
        )
        h3 = repo.try_acquire_polling_lease(source_id, ttl_seconds=600)
        _check(h3 is not None, "acquire on an EXPIRED lease re-claims")
        _check(
            h3 is not None and h1 is not None and h3.lease_token != h1.lease_token,
            "re-claim mints a fresh fence token",
        )
        _check(
            _as_dt(_row(provider, "source", source_id)["updated_at"]) > acquire_updated_at,
            "updated_at advanced after re-acquire (BEFORE-UPDATE trigger)",
        )

        # ── refresh: owner token extends; wrong token → None ──
        assert h3 is not None
        refreshed = repo.refresh_polling_lease(h3, ttl_seconds=1200)
        _check(refreshed is not None, "owner-token refresh succeeds")
        _check(
            _as_dt(_row(provider, "source", source_id)["polling_lease_until"])
            == _naive(_NOW + timedelta(seconds=1200)),
            "refresh extended polling_lease_until to now + new ttl",
        )
        wrong = PollingLeaseHandle(
            source_id=source_id, lease_token="not-the-owner",
            lease_until=_NOW,
        )
        _check(
            repo.refresh_polling_lease(wrong, ttl_seconds=600) is None,
            "wrong-token refresh returns None (lease-lost signal)",
        )

        # ── release: clears the lease → a fresh acquire then succeeds ──
        repo.release_polling_lease(h3)
        cleared = _row(provider, "source", source_id)
        _check(
            cleared["polling_lease_until"] is None and cleared["polling_lease_token"] is None,
            "release cleared both lease columns",
        )
        h4 = repo.try_acquire_polling_lease(source_id, ttl_seconds=600)
        _check(h4 is not None, "acquire after release succeeds (lease was free)")
        assert h4 is not None
        repo.release_polling_lease(h4)

        # ── start_batch / finish_batch token-fenced CAS ──
        batch_id = repo.start_batch(source_id, polling_lease_token="tok-A")
        _check(batch_id.startswith("imb_"), "start_batch mints an imb_ id")
        _check(
            _row(provider, "import_batch", batch_id)["status"] == "running",
            "start_batch lands status=running",
        )
        _check(
            repo.finish_batch(
                batch_id, polling_lease_token="tok-WRONG",
                status=ImportBatchStatus.COMPLETED,
            ) is False,
            "wrong-token finish is dropped (no clobber)",
        )
        _check(
            _row(provider, "import_batch", batch_id)["status"] == "running",
            "batch still running after the dropped wrong-token finish",
        )
        _check(
            repo.finish_batch(
                batch_id, polling_lease_token="tok-A",
                status=ImportBatchStatus.COMPLETED,
            ) is True,
            "matching-token finish lands terminal",
        )
        _check(
            _row(provider, "import_batch", batch_id)["status"] == "completed",
            "batch is now completed",
        )
        _check(
            repo.finish_batch(
                batch_id, polling_lease_token="tok-A",
                status=ImportBatchStatus.FAILED,
            ) is False,
            "double-finish is dropped (status already terminal)",
        )

        # ── get_import_status: curated public projection (no token leak) ──
        status = repo.get_import_status(batch_id)
        _check(
            status is not None and status.get("status") == "completed",
            "get_import_status returns the batch's terminal status",
        )
        _check(
            status is not None and "polling_lease_token" not in status,
            "get_import_status does NOT leak the internal polling_lease_token fence",
        )
        _check(
            status is not None
            and set(status.keys()) == {
                "id", "source_id", "started_at", "finished_at",
                "status", "event_count", "error_message", "error_kind",
            },
            "get_import_status projects to exactly the curated public columns",
        )
        _check(
            repo.get_import_status("imb_does_not_exist") is None,
            "get_import_status returns None for an unknown batch",
        )

        # ── ensure_open_route_batch_for_source: create then reuse ──
        route_a = repo.ensure_open_route_batch_for_source(source_id)
        route_b = repo.ensure_open_route_batch_for_source(source_id)
        _check(route_a == route_b, "ensure_open_route_batch is idempotent (reuses the open route)")
        _check(
            _row(provider, "import_batch", route_a)["polling_lease_token"] is None,
            "route batch carries a NULL polling_lease_token (adoptable)",
        )

        # ── adopt: in-window claim ──
        adopted = repo.adopt_route_batch_for_source(
            source_id, polling_lease_token="tok-importer", recency_window_minutes=10,
        )
        _check(adopted == route_a, "in-window route batch is adopted (claimed)")
        _check(
            _row(provider, "import_batch", route_a)["polling_lease_token"] == "tok-importer",
            "adopt set the importer's token on the claimed batch",
        )

        # ── adopt: non-adoptable (token now held) → None ──
        _check(
            repo.adopt_route_batch_for_source(
                source_id, polling_lease_token="tok-other", recency_window_minutes=10,
            ) is None,
            "no adoptable (token-NULL) batch → adopt returns None",
        )

        # ── adopt: out-of-window (old started_at) → None ──
        old_route = repo.ensure_open_route_batch_for_source(source_id)
        provider.update(
            namespace="session_ledger", table="import_batch",
            conditions={"id": old_route},
            updates={"started_at": _naive(_NOW - timedelta(minutes=60))},
        )
        _check(
            repo.adopt_route_batch_for_source(
                source_id, polling_lease_token="tok-late", recency_window_minutes=10,
            ) is None,
            "most-recent adoptable older than the recency window → adopt returns None",
        )

        # ── write_cursor / read_cursor: discovery insert→read, re-write→revive ──
        repo.write_cursor(
            source_id=source_id, scope=CursorScope.DISCOVERY,
            cursor_payload={"high_water_iso": "2026-05-01T00:00:00+00:00"},
        )
        disc = repo.read_cursor(source_id=source_id, scope=CursorScope.DISCOVERY)
        _check(
            disc == {"high_water_iso": "2026-05-01T00:00:00+00:00"},
            "discovery cursor round-trips through JSONB",
        )
        repo.write_cursor(
            source_id=source_id, scope=CursorScope.DISCOVERY,
            cursor_payload={"high_water_iso": "2026-06-01T00:00:00+00:00"},
        )
        disc2 = repo.read_cursor(source_id=source_id, scope=CursorScope.DISCOVERY)
        _check(
            disc2 == {"high_water_iso": "2026-06-01T00:00:00+00:00"},
            "re-write updates the existing discovery cursor (revive/update branch)",
        )
        repo.write_cursor(
            source_id=source_id, scope=CursorScope.EVENT_READ,
            cursor_payload={"cursor_high_water": 5}, scope_key="ext-1",
        )
        ev = repo.read_cursor(
            source_id=source_id, scope=CursorScope.EVENT_READ, scope_key="ext-1",
        )
        _check(ev == {"cursor_high_water": 5}, "event_read cursor round-trips by scope_key")
        _check(
            repo.read_cursor(source_id=source_id, scope=CursorScope.EVENT_READ, scope_key="ext-NONE")
            is None,
            "read_cursor returns None for an unwritten scope_key",
        )

        # ── count_active_source_cursors / reset_source_cursor ──
        _check(
            repo.count_active_source_cursors(source_id) == 2,
            "count reflects the 2 live cursors (discovery + event_read)",
        )
        deleted = repo.reset_source_cursor(source_id)
        _check(deleted == 2, "reset returns the before-count (2)")
        _check(
            repo.count_active_source_cursors(source_id) == 0,
            "no live cursors remain after reset",
        )
        # Hard delete (operator soft-delete-is-opt-out): the rows are physically
        # gone, not left as is_deleted=1 ghosts. provider.select does NOT auto-
        # filter is_deleted, so an empty result discriminates HARD from soft.
        remaining = provider.select(
            namespace="session_ledger", table="source_cursor",
            conditions={"source_id": source_id},
        )
        _check(
            remaining == [],
            f"reset HARD-deleted the cursor rows (none remain incl. is_deleted; got {len(remaining)})",
        )
        _check(repo.reset_source_cursor(source_id) == 0, "reset is idempotent (0 on empty)")

        # ── record_lease_ping: insert then update ──
        repo.record_lease_ping(session_id=session_id, source_id=source_id, ttl_seconds=120)
        leases = provider.select(
            namespace="session_ledger", table="active_lease",
            conditions={"session_id": session_id},
        )
        _check(len(leases) == 1, "record_lease_ping inserts a lease row for a new session")
        _check(
            (_as_dt(leases[0]["expires_at"]) - _as_dt(leases[0]["last_seen_at"])).total_seconds()
            == 120,
            "expires_at = last_seen_at + ttl on the insert branch",
        )
        repo.record_lease_ping(session_id=session_id, source_id=source_id, ttl_seconds=300)
        leases2 = provider.select(
            namespace="session_ledger", table="active_lease",
            conditions={"session_id": session_id},
        )
        _check(len(leases2) == 1, "record_lease_ping UPDATES (no duplicate row) for the same session")
        _check(
            int(leases2[0]["lease_ttl_seconds"]) == 300,
            "the update branch advanced lease_ttl_seconds to 300",
        )
    finally:
        if source_id:
            _hard_delete_where(provider, "import_batch", "source_id", source_id)
            _hard_delete_where(provider, "source_cursor", "source_id", source_id)
            _hard_delete_where(provider, "source", "id", source_id)
        _hard_delete_where(provider, "active_lease", "session_id", session_id)
        leftover = provider.select(
            namespace="session_ledger", table="source", conditions={"id": source_id},
        ) if source_id else []
        _check(not leftover, "all sentinel rows hard-deleted (cleanup)")


def main() -> int:
    if os.environ.get("LEDGER_POLLING_LIVE_SMOKE") != "1":
        print("=== polling_driver_migration_live_smoke ===")
        print(
            "  SKIP  set LEDGER_POLLING_LIVE_SMOKE=1 to run; "
            "needs the live solet DB."
        )
        return 0
    print("=== polling_driver_migration_live_smoke ===")
    provider = PostgresProvider(_load_pg_config())
    provider.initialize()
    repo = SessionLedgerRepository(
        state_service=cast("Any", _LiveStateAdapter(provider)),
        clock=lambda: _NOW,
    )
    test_polling_driver_lifecycle(repo, provider)
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
