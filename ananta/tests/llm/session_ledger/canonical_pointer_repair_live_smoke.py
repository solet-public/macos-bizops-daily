#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for the canonical-pointer-repair migration (3a).

Pins the FOR-UPDATE → non-locking conditional-CAS canonical-election rework
(SQL-lockdown #0, Slice 3a) against a REAL ``PostgresProvider``: the dup-finder
``GROUP BY … HAVING`` becomes a Python ``Counter``; the per-group lift drops the
row-lock for a deterministic-survivor + conditional ``update_state``; and
``lift_canonical_pointer_for_duplicate_sessions`` LOOPS (recount + re-lift) until
no duplicate group remains — the convergence the dropped-lock soundness rests on.

WHY A SANDBOX TABLE (not the live solet schema): the repair exists for the
PRE-INDEX state — multiple canonical rows sharing a ``(vendor, external_session_id)``
pair. The live schema's partial-unique index
``idx_session_canonical_one_per_vendor_pair`` makes that state UNCONSTRUCTABLE.
So this builds a ``session_ledger__session``-shaped table WITHOUT that index in a
throwaway schema and exercises the migrated ``query_state`` / ``update_state``
path through a real provider. The sandbox is dropped in a ``finally``.

Cases:
* normal — survivor = OLDEST canonical (created_at, id), stays canonical; every
  other sibling demoted; unrelated group untouched; idempotent re-run.
* convergence — a NEW canonical inserted BETWEEN the group read and the
  demotions is caught by the SAME invocation's recount loop (not left duplicate).
* interception — a sibling pre-demoted after the read but before its CAS
  contributes 0 to the returned demoted count (conditional ``WHERE canonical IS
  NULL`` excludes it), and the end state is still fully resolved.

Env-gated behind ``CANONICAL_REPAIR_LIVE_SMOKE=1``.

Run::

    CANONICAL_REPAIR_LIVE_SMOKE=1 \\
      .venv/bin/python3 ananta/tests/llm/session_ledger/canonical_pointer_repair_live_smoke.py
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, LiteralString, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)

from ananta.llm.session_ledger.repository import (  # noqa: E402
    SessionLedgerRepository,
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


def _load_pg_config(schema_name: str) -> PostgresConfig:
    config = PostgresConfig(**json.loads(_PROFILE_PG_CONFIG.read_text(encoding="utf-8")))
    config.pg_schema = schema_name
    return config


def _envelope(data: dict[str, Any]) -> dict[str, Any]:
    return {"action_status": "completed", "data": data, "actions": [], "error": None}


class _LiveStateAdapter:
    """Faithful adapter: query_state → provider.select; update_state → provider.update."""

    def __init__(self, provider: PostgresProvider) -> None:
        self._provider = provider

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        filters = query.get("filters") or {}
        rows = self._provider.select(
            namespace=namespace,
            table=str(query["table"]),
            conditions=cast(dict[str, Any], filters) if isinstance(filters, dict) else None,
        )
        return _envelope({"records": rows, "count": len(rows)})

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


class _HookAdapter(_LiveStateAdapter):
    """Fires a one-shot side-effect at a precise point to simulate a concurrent
    writer: after the Nth ``query_state`` (a mid-pass insert) or before the Nth
    ``update_state`` (a pre-demote interception)."""

    def __init__(
        self,
        provider: PostgresProvider,
        *,
        after_query_call: int | None = None,
        query_hook: Callable[[], None] | None = None,
        before_update_call: int | None = None,
        update_hook: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(provider)
        self._q = 0
        self._u = 0
        self._after_query_call = after_query_call
        self._query_hook = query_hook
        self._before_update_call = before_update_call
        self._update_hook = update_hook

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        result = super().query_state(namespace, query)
        self._q += 1
        if self._q == self._after_query_call and self._query_hook is not None:
            self._query_hook()
        return result

    def update_state(
        self, namespace: str, query: dict[str, Any], updates: dict[str, Any]
    ) -> dict[str, Any]:
        self._u += 1
        if self._u == self._before_update_call and self._update_hook is not None:
            self._update_hook()
        return super().update_state(namespace, query, updates)


_PHYSICAL = "session_ledger__session"

# group thr-1 has 3 canonicals (les_a oldest → survivor); thr-2 is a lone canonical.
_SEED: tuple[tuple[str, str, str, str | None, str], ...] = (
    ("les_a", "codex", "thr-1", None, "2026-01-01T00:00:00Z"),
    ("les_b", "codex", "thr-1", None, "2026-01-02T00:00:00Z"),
    ("les_c", "codex", "thr-1", None, "2026-01-03T00:00:00Z"),
    ("les_solo", "codex", "thr-2", None, "2026-01-01T00:00:00Z"),
)


def _reset_table(provider: PostgresProvider, schema: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(cast(LiteralString, f'DROP TABLE IF EXISTS "{schema}"."{_PHYSICAL}"'))
        cur.execute(
            cast(
                LiteralString,
                f'CREATE TABLE "{schema}"."{_PHYSICAL}" ('
                "id text PRIMARY KEY, vendor text NOT NULL, "
                "external_session_id text NOT NULL, "
                "canonical_external_session_id text, "
                "is_deleted integer NOT NULL DEFAULT 0, "
                "created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL"
                ")",
            )
        )
        for row_id, vendor, ext_id, canonical, created in _SEED:
            cur.execute(
                cast(
                    LiteralString,
                    f'INSERT INTO "{schema}"."{_PHYSICAL}" '
                    "(id, vendor, external_session_id, canonical_external_session_id, "
                    "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s)",
                ),
                (row_id, vendor, ext_id, canonical, created, created),
            )


def _insert_canonical(provider: PostgresProvider, schema: str, row_id: str, ext_id: str, created: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'INSERT INTO "{schema}"."{_PHYSICAL}" '
                "(id, vendor, external_session_id, canonical_external_session_id, "
                "created_at, updated_at) VALUES (%s, 'codex', %s, NULL, %s, %s)",
            ),
            (row_id, ext_id, created, created),
        )


def _pre_demote(provider: PostgresProvider, schema: str, row_id: str, pointer: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'UPDATE "{schema}"."{_PHYSICAL}" '
                "SET canonical_external_session_id = %s WHERE id = %s",
            ),
            (pointer, row_id),
        )


def _canonical_of(provider: PostgresProvider, schema: str, row_id: str) -> object:
    rows = provider.execute_query(
        f'SELECT canonical_external_session_id FROM "{schema}"."{_PHYSICAL}" WHERE id = %s',
        (row_id,),
    )
    return rows[0][0] if rows else "<<absent>>"


def _repo(adapter: object) -> SessionLedgerRepository:
    return SessionLedgerRepository(state_service=adapter)  # type: ignore[arg-type]


def test_normal_lift(provider: PostgresProvider, schema: str) -> None:
    repo = _repo(_LiveStateAdapter(provider))
    _check(repo.count_canonical_duplicate_sessions() == 1, "count: exactly 1 duplicate-canonical group (thr-1)")
    demoted = repo.lift_canonical_pointer_for_duplicate_sessions()
    _check(demoted == 2, f"lift: 2 siblings demoted (3-row group → survivor + 2); got {demoted}")
    _check(_canonical_of(provider, schema, "les_a") is None, "survivor les_a (oldest) stays canonical (NULL)")
    _check(_canonical_of(provider, schema, "les_b") == "thr-1", "sibling les_b demoted → pointer 'thr-1'")
    _check(_canonical_of(provider, schema, "les_c") == "thr-1", "sibling les_c demoted → pointer 'thr-1'")
    _check(_canonical_of(provider, schema, "les_solo") is None, "unrelated lone canonical les_solo untouched")
    _check(repo.count_canonical_duplicate_sessions() == 0, "re-run count: 0 groups remain")
    _check(repo.lift_canonical_pointer_for_duplicate_sessions() == 0, "re-run lift: 0 demoted (idempotent)")


def test_mid_pass_insertion_converges(provider: PostgresProvider, schema: str) -> None:
    """A NEW canonical inserted after the group read is caught by the SAME loop."""
    # query_state call 1 = pass-1 count read; call 2 = pass-1 group read. Inject after call 2.
    adapter = _HookAdapter(
        provider,
        after_query_call=2,
        query_hook=lambda: _insert_canonical(provider, schema, "les_d", "thr-1", "2026-01-04T00:00:00Z"),
    )
    repo = _repo(adapter)
    demoted = repo.lift_canonical_pointer_for_duplicate_sessions()
    _check(
        demoted == 3,
        f"convergence: all 3 siblings (les_b, les_c, + the mid-pass les_d) demoted by ONE invocation; got {demoted}",
    )
    _check(_canonical_of(provider, schema, "les_a") is None, "survivor les_a still canonical after convergence")
    _check(_canonical_of(provider, schema, "les_d") == "thr-1", "the mid-pass-inserted les_d was demoted (not left duplicate)")
    _check(repo.count_canonical_duplicate_sessions() == 0, "after convergence: 0 duplicate groups remain")


def test_pre_demoted_sibling_contributes_zero(provider: PostgresProvider, schema: str) -> None:
    """A sibling demoted by a 'concurrent' pass before its CAS contributes 0."""
    # Before the 1st demote (update_state call 1), a concurrent pass demotes les_c.
    adapter = _HookAdapter(
        provider,
        before_update_call=1,
        update_hook=lambda: _pre_demote(provider, schema, "les_c", "thr-1"),
    )
    repo = _repo(adapter)
    demoted = repo.lift_canonical_pointer_for_duplicate_sessions()
    _check(
        demoted == 1,
        f"interception: pre-demoted les_c contributes 0 (conditional CAS); only les_b counted; got {demoted}",
    )
    _check(_canonical_of(provider, schema, "les_a") is None, "survivor les_a still canonical")
    _check(_canonical_of(provider, schema, "les_b") == "thr-1", "les_b demoted by the lift")
    _check(_canonical_of(provider, schema, "les_c") == "thr-1", "les_c demoted (by the concurrent pass) — end state resolved")
    _check(repo.count_canonical_duplicate_sessions() == 0, "end state fully resolved (0 groups)")


def test_concurrent_full_resolution_terminates_clean(provider: PostgresProvider, schema: str) -> None:
    """A peer that fully resolves the group between the count and the lift must
    TERMINATE CLEANLY via the confirmation recount — not false-raise a data
    anomaly off the stale pre-pass counts."""
    def _resolve() -> None:
        _pre_demote(provider, schema, "les_b", "thr-1")
        _pre_demote(provider, schema, "les_c", "thr-1")

    # Hook after the pass-1 COUNT read (call 1): the group is fully resolved, so
    # the per-group read returns <2 canonical → _lift returns 0 → pass_demoted==0
    # → the confirmation recount is clean → return 0 (no anomaly raise).
    adapter = _HookAdapter(provider, after_query_call=1, query_hook=_resolve)
    repo = _repo(adapter)
    demoted = repo.lift_canonical_pointer_for_duplicate_sessions()
    _check(
        demoted == 0,
        f"concurrent full resolution after the count → clean termination, demoted=0 (NO false anomaly); got {demoted}",
    )
    _check(repo.count_canonical_duplicate_sessions() == 0, "ledger clean after concurrent resolution")
    _check(_canonical_of(provider, schema, "les_a") is None, "survivor les_a still canonical")


def main() -> int:
    if os.environ.get("CANONICAL_REPAIR_LIVE_SMOKE") != "1":
        print("=== canonical_pointer_repair_live_smoke ===")
        print("  SKIP  set CANONICAL_REPAIR_LIVE_SMOKE=1 to run; needs the live DB (own throwaway schema).")
        return 0
    print("=== canonical_pointer_repair_live_smoke ===")
    schema_name = f"example_test_canon_repair_{secrets.token_hex(4)}"
    provider = PostgresProvider(_load_pg_config(schema_name))
    provider.initialize()
    try:
        for test in (
            test_normal_lift,
            test_mid_pass_insertion_converges,
            test_pre_demoted_sibling_contributes_zero,
            test_concurrent_full_resolution_terminates_clean,
        ):
            _reset_table(provider, schema_name)
            test(provider, schema_name)
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
