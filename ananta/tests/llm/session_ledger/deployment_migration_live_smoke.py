#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for the deployment-mixin write migration.

Pins that ``SessionLedgerDeploymentMixin`` — migrated off raw ``transactional()``
SQL onto ``write_state`` / ``update_state`` / ``query_state`` (SQL-lockdown #0,
deployment slice) — drives the full M5 shipper pairing lifecycle correctly
against the running solet's REAL ledger schema:

* ``insert_pending_deployment`` → row lands PENDING with the JSONB
  ``authorized_source_kinds`` list round-tripping (no caller cast) + a ``dep-`` id.
* ``set_deployment_pairing_token`` → token + user_code populate.
* ``transition_*`` → the ``pairing_status`` filter IS the compare-and-set:
  PENDING→APPROVED clears user_code; a re-fire on the wrong prior state is a
  silent 0-row no-op; APPROVED→PAIRED binds the oauth client + clears tokens;
  PAIRED→REVOKED stamps revoked_at.
* ``get_deployment`` / ``get_deployment_by_oauth_client_id`` read it back.

Writes ONE sentinel deployment row and hard-deletes it in a ``finally`` — no
durable state, no impact on real rows. Env-gated behind
``LEDGER_DEPLOYMENT_LIVE_SMOKE=1`` (needs the live solet DB up).

Run::

    LEDGER_DEPLOYMENT_LIVE_SMOKE=1 \\
      .venv/bin/python3 ananta/tests/llm/session_ledger/deployment_migration_live_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta, timezone
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
from ananta.llm.session_ledger.types import PairingStatus  # noqa: E402
from postgres_state_management_plugin.postgres_backend.config import (  # noqa: E402
    PostgresConfig,
)
from postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    PostgresProvider,
)

_SCHEMA = os.environ[SOLET_NAME_ENV_VAR]

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
    """Faithful StateManagementInterface stand-in mirroring the plugin facade.

    write_state → provider.insert (INSERT … RETURNING id); update_state →
    provider.update (returns rows-affected); query_state → provider.select —
    so the migrated writes exercise the actual SQL-composition path.
    """

    def __init__(self, provider: PostgresProvider) -> None:
        self._provider = provider

    def write_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        record = data.get("record")
        row_id = self._provider.insert(
            namespace=namespace, table=str(data["table"]), data=cast(dict[str, Any], record),
        )
        return _envelope({"namespace": namespace, "result": {"generated_id": row_id, "inserted": 1}})

    def update_state(
        self, namespace: str, query: dict[str, Any], updates: dict[str, Any]
    ) -> dict[str, Any]:
        filters = query.get("filters") or {}
        affected = self._provider.update(
            namespace=namespace,
            table=str(query["table"]),
            conditions=cast(dict[str, Any], filters),
            updates=updates,
        )
        return _envelope({"namespace": namespace, "result": {"updated": affected}})

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        filters = query.get("filters") or {}
        rows = self._provider.select(
            namespace=namespace,
            table=str(query["table"]),
            conditions=cast(dict[str, Any], filters) if isinstance(filters, dict) else None,
        )
        return _envelope({"records": rows, "count": len(rows)})


_SENTINEL_MACHINE_ID = "mch-__deployment_migration_live_smoke__"
_SOURCE_KINDS = ["codex_local", "claude_code_local"]


def _hard_delete(provider: PostgresProvider, deployment_id: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'DELETE FROM "{_SCHEMA}".session_ledger__deployment WHERE id = %s',
            ),
            (deployment_id,),
        )


def test_full_pairing_lifecycle(repo: SessionLedgerRepository, provider: PostgresProvider) -> None:
    deployment_id = repo.insert_pending_deployment(
        machine_id=_SENTINEL_MACHINE_ID,
        initiating_client_id="cli-smoke",
        authorized_source_kinds=_SOURCE_KINDS,
    )
    try:
        _check(deployment_id.startswith("dep"), f"insert returns a dep- id (got {deployment_id!r})")

        row = repo.get_deployment(deployment_id)
        assert row is not None
        _check(row["pairing_status"] == PairingStatus.PENDING.value, "row lands PENDING")
        _check(row["machine_id"] == _SENTINEL_MACHINE_ID, "machine_id persisted")
        _check(
            row["authorized_source_kinds"] == _SOURCE_KINDS,
            f"JSONB list round-trips (no caller cast) — got {row['authorized_source_kinds']!r}",
        )

        repo.set_deployment_pairing_token(
            deployment_id=deployment_id,
            pairing_token_hash="hash-x",
            pairing_token_salt="salt-x",
            user_code="USER-CODE-1",
        )
        row = repo.get_deployment(deployment_id)
        assert row is not None
        _check(
            row["pairing_token_hash"] == "hash-x" and row["user_code"] == "USER-CODE-1",
            "pairing token + user_code populated",
        )

        repo.transition_deployment_to_approved(deployment_id=deployment_id)
        row = repo.get_deployment(deployment_id)
        assert row is not None
        _check(row["pairing_status"] == PairingStatus.APPROVED.value, "PENDING→APPROVED CAS hit")
        _check(row["user_code"] is None, "approved transition cleared user_code (None→NULL)")

        # CAS miss: a re-fire when no longer PENDING is a silent 0-row no-op.
        repo.transition_deployment_to_approved(deployment_id=deployment_id)
        row = repo.get_deployment(deployment_id)
        assert row is not None
        _check(
            row["pairing_status"] == PairingStatus.APPROVED.value,
            "re-fire to_approved on APPROVED row is a no-op (CAS miss, status unchanged)",
        )

        repo.transition_deployment_to_paired(
            deployment_id=deployment_id, oauth_client_id="oauth-client-7",
        )
        row = repo.get_deployment(deployment_id)
        assert row is not None
        _check(row["pairing_status"] == PairingStatus.PAIRED.value, "APPROVED→PAIRED CAS hit")
        _check(
            row["oauth_client_id"] == "oauth-client-7" and row["pairing_token_hash"] is None,
            "paired binds oauth client + clears token hash (None→NULL)",
        )

        by_oauth = repo.get_deployment_by_oauth_client_id("oauth-client-7")
        _check(
            by_oauth is not None and str(by_oauth["id"]) == deployment_id,
            "get_deployment_by_oauth_client_id resolves the paired row",
        )

        repo.transition_deployment_to_revoked(deployment_id=deployment_id)
        row = repo.get_deployment(deployment_id)
        assert row is not None
        _check(
            row["pairing_status"] == PairingStatus.REVOKED.value and row["revoked_at"] is not None,
            "PAIRED→REVOKED stamps revoked_at",
        )
    finally:
        _hard_delete(provider, deployment_id)
        _check(repo.get_deployment(deployment_id) is None, "fixture row hard-deleted (cleanup)")


def test_non_utc_clock_stores_utc_instant(provider: PostgresProvider) -> None:
    """A tz-aware NON-UTC repo clock must store the UTC instant, not the wall-clock.

    The ledger timestamp columns are ``timestamp without time zone`` (naive UTC,
    the F1 seam). Before the fix, the autocommit write bound a tz-aware value's
    offset ISO string, so 12:00+05:00 mis-stored as the wall-clock 12:00 instead
    of its 07:00 UTC instant. ``_naive_utc`` at the write seam restores the
    contract regardless of the injected clock's tzinfo.
    """
    aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=5)))
    repo = SessionLedgerRepository(
        state_service=_LiveStateAdapter(provider),  # type: ignore[arg-type]
        clock=lambda: aware,
    )
    deployment_id = repo.insert_pending_deployment(
        machine_id=f"{_SENTINEL_MACHINE_ID}-tz",
        initiating_client_id="cli-tz",
        authorized_source_kinds=[],
    )
    try:
        rows = provider.execute_query(
            f'SELECT created_at FROM "{provider.config.schema_name}".'
            '"session_ledger__deployment" WHERE id = %s',
            (deployment_id,),
        )
        stored = rows[0][0]
        # The timestamp-without-tz column reads back as an ISO string (or naive
        # datetime); normalize both to a naive-UTC datetime for the instant check.
        stored_dt = datetime.fromisoformat(stored) if isinstance(stored, str) else stored
        stored_utc = (
            stored_dt.astimezone(UTC).replace(tzinfo=None)
            if isinstance(stored_dt, datetime) and stored_dt.tzinfo is not None
            else stored_dt
        )
        _check(
            stored_utc == datetime(2026, 1, 1, 7, 0, 0),
            f"non-UTC clock 12:00+05:00 stored as UTC instant 07:00 (got {stored!r})",
        )
    finally:
        _hard_delete(provider, deployment_id)


def main() -> int:
    if os.environ.get("LEDGER_DEPLOYMENT_LIVE_SMOKE") != "1":
        print("=== deployment_migration_live_smoke ===")
        print(
            "  SKIP  set LEDGER_DEPLOYMENT_LIVE_SMOKE=1 to run; "
            "needs the live solet DB."
        )
        return 0
    print("=== deployment_migration_live_smoke ===")
    provider = PostgresProvider(_load_pg_config())
    provider.initialize()
    repo = SessionLedgerRepository(state_service=_LiveStateAdapter(provider))  # type: ignore[arg-type]
    test_full_pairing_lifecycle(repo, provider)
    test_non_utc_clock_stores_utc_instant(provider)
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
