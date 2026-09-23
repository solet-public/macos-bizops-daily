#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for the inverted-bounds-repair migration (3b).

Pins the SQL-lockdown #0 Slice-3b rework against a REAL ``PostgresProvider`` —
the five one-shot operator-repair verbs migrated off raw ``transactional()`` SQL
onto ``query_state`` / ``query_ordered`` / ``update_state`` (the ``base.py``
``_query`` / ``_query_ordered`` / ``_update`` seams). It exercises every
Architect-ruled rewrite (``workbench/2026-06-20_ledger_migration_slice_plan.md`` →
``★ 3b GAPS RESOLVED``) against the real provider's filter/order/CAS semantics —
the migration's real-schema test mandate, which the thin planted-rows stub does
NOT model:

* **GAP-i** cross-column inverted-bounds detect (Python-fold) + the
  ``min/max(event_at)`` recompute via 2× ``query_ordered`` limit-1 + the
  FOR-UPDATE→deterministic-recompute idempotent rework.
* **GAP-ii** ``LIKE 'emb-%'`` → ``str.startswith('emb-')`` over a read.
* **GAP-iii** PATH-1 ledger-side ``external_id = f"{session_id}:{chunk_index}"``
  recompute + conditional ``update_state`` CAS — directly asserted.
* the orphan ``started_at < cutoff`` range → read-all-running + Python-filter +
  ``=ANY`` ``status=RUNNING`` compare-and-set.

WHY A SANDBOX SCHEMA: the verbs mutate live ledger tables; the smoke builds
minimal ``session_ledger__{session,event,summary,import_batch}``-shaped tables in
a throwaway schema (no triggers/indexes needed — the verbs touch only the columns
created here), seeds the exact anomalies each verb targets, drives the MIGRATED
primitive path through a real provider, and DROPs the schema in a ``finally``.

The ``source_kind`` early-return guard (which delegates to ``get_source``) is
unchanged by this migration and is not exercised here; the main orphan case runs
``source_kind=None`` so the full migrated read→filter→update path is covered.

Env-gated behind ``INVERTED_BOUNDS_LIVE_SMOKE=1`` (needs the live DB up; writes
only to its own throwaway schema).

Run::

    INVERTED_BOUNDS_LIVE_SMOKE=1 \\
      .venv/bin/python3 ananta/tests/llm/session_ledger/inverted_bounds_repair_live_smoke.py
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from collections.abc import Callable
from datetime import UTC, datetime
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
from ananta.services.session_ledger_service.service import (  # noqa: E402
    SessionLedgerService,
)
from postgres_state_management_plugin.postgres_backend.config import (  # noqa: E402
    PostgresConfig,
)
from postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    PostgresProvider,
)

_passed = 0
_failed: list[str] = []

# A fixed clock so cutoffs + updated_at are deterministic across the run.
_NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)


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


def _make_service(repository: SessionLedgerRepository) -> SessionLedgerService:
    """Build a SessionLedgerService stand-in wired only to the live repository.

    The summary + orphan backfill verbs touch only ``self._repository`` (plus the
    module logger), so the operator-facing service wrappers can be exercised
    end-to-end against the migrated repository without the full collaborator set —
    mirroring the ``_make_service`` pattern the retired stub smokes used.
    """
    service = SessionLedgerService.__new__(SessionLedgerService)
    service._repository = repository  # type: ignore[assignment]
    return service


def _make_repo(
    provider: PostgresProvider,
    *,
    pre_update_hook: Callable[[dict[str, Any]], None] | None = None,
) -> SessionLedgerRepository:
    """Build the live repository over the real provider with a fixed clock."""
    return SessionLedgerRepository(
        state_service=cast("Any", _LiveStateAdapter(provider, pre_update_hook=pre_update_hook)),
        clock=lambda: _NOW,
    )


def _raw_set(
    provider: PostgresProvider, schema: str, table: str, row_id: str, **cols: object
) -> None:
    """Directly mutate one row's columns (a 'concurrent' writer bypassing the repo)."""
    assignments = ", ".join(f'"{c}" = %s' for c in cols)
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'UPDATE "{schema}"."{table}" SET {assignments} WHERE id = %s',
            ),
            (*cols.values(), row_id),
        )


class _LiveStateAdapter:
    """Faithful adapter routing the 3b primitives to the real provider.

    ``query_state`` → ``provider.select``; ``query_ordered`` →
    ``provider.select_ordered`` (translating the ``[[col, dir], …]`` composite
    into the provider's ``(order_columns, single direction)`` shape);
    ``update_state`` → ``provider.update``.
    """

    def __init__(
        self,
        provider: PostgresProvider,
        *,
        pre_update_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._provider = provider
        self._pre_update_hook = pre_update_hook

    def execute_sql(
        self, sql_query: str, sql_params: list[object] | None = None
    ) -> dict[str, Any]:
        rows = self._provider.execute_query(sql_query, tuple(sql_params or ()))
        return _envelope({"records": rows, "count": len(rows)})

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
        order_columns = tuple(str(pair[0]) for pair in order_by)
        direction = str(order_by[0][1]) if order_by else "asc"
        rows = self._provider.select_ordered(
            namespace=namespace,
            table=str(data["table"]),
            conditions=cast("dict[str, Any]", filters) if isinstance(filters, dict) else {},
            order_columns=order_columns,
            direction=direction,
            limit=int(cast("int", data["limit"])),
        )
        return _envelope({"records": rows, "count": len(rows)})

    def update_state(
        self, namespace: str, query: dict[str, Any], updates: dict[str, Any]
    ) -> dict[str, Any]:
        # A one-shot hook simulates a concurrent writer winning the row BEFORE
        # our CAS executes — forcing the deterministic CAS-miss the regressions
        # exercise. It fires before delegating to the real provider update.
        if self._pre_update_hook is not None:
            self._pre_update_hook(query)
        filters = query.get("filters") or {}
        affected = self._provider.update(
            namespace=namespace,
            table=str(query["table"]),
            conditions=cast("dict[str, Any]", filters),
            updates=updates,
        )
        return _envelope({"namespace": namespace, "result": {"updated": affected}})


# ─── Sandbox DDL ───────────────────────────────────────────────────────────────

_DDL: tuple[tuple[str, str], ...] = (
    (
        "session_ledger__session",
        "id text PRIMARY KEY, first_event_at timestamp, last_event_at timestamp, "
        "is_deleted integer NOT NULL DEFAULT 0, "
        "created_at timestamp NOT NULL, updated_at timestamp NOT NULL",
    ),
    (
        "session_ledger__event",
        "id text PRIMARY KEY, session_id text NOT NULL, event_at timestamp NOT NULL, "
        "is_deleted integer NOT NULL DEFAULT 0, "
        "created_at timestamp NOT NULL, updated_at timestamp NOT NULL",
    ),
    (
        "session_ledger__summary",
        "id text PRIMARY KEY, session_id text, chunk_index integer, "
        "embedding_vector_id text, is_deleted integer NOT NULL DEFAULT 0, "
        "created_at timestamp NOT NULL, updated_at timestamp NOT NULL",
    ),
    (
        "session_ledger__import_batch",
        "id text PRIMARY KEY, source_id text NOT NULL, status text NOT NULL, "
        "started_at timestamp, finished_at timestamp, "
        "error_message text, error_kind text, "
        "is_deleted integer NOT NULL DEFAULT 0, "
        "created_at timestamp NOT NULL, updated_at timestamp NOT NULL",
    ),
    (
        "session_ledger__source",
        "id text PRIMARY KEY, source_kind text NOT NULL, root_uri text NOT NULL, "
        "account_label text, enabled boolean NOT NULL DEFAULT true, "
        "config_json text, is_deleted integer NOT NULL DEFAULT 0, "
        "created_at timestamp NOT NULL, updated_at timestamp NOT NULL",
    ),
)


def _create_tables(provider: PostgresProvider, schema: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        for table, body in _DDL:
            cur.execute(
                cast(LiteralString, f'CREATE TABLE "{schema}"."{table}" ({body})')
            )


def _insert(
    provider: PostgresProvider, schema: str, table: str, row: dict[str, object]
) -> None:
    cols = list(row.keys())
    placeholders = ", ".join(["%s"] * len(cols))
    col_csv = ", ".join(f'"{c}"' for c in cols)
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'INSERT INTO "{schema}"."{table}" ({col_csv}) VALUES ({placeholders})',
            ),
            tuple(row[c] for c in cols),
        )


def _scalar(provider: PostgresProvider, schema: str, table: str, col: str, row_id: str) -> object:
    rows = provider.execute_query(
        f'SELECT "{col}" FROM "{schema}"."{table}" WHERE id = %s', (row_id,)
    )
    return rows[0][0] if rows else "<<absent>>"


# ─── Cases ─────────────────────────────────────────────────────────────────────


def _iso(month: int, day: int, hour: int = 0) -> str:
    # Naive-UTC wall-clock ISO (no offset) — matches how the ledger stores
    # timestamps in Postgres ``TIMESTAMP`` (no tz) columns.
    return datetime(2026, month, day, hour).isoformat()


def test_inverted_bounds(repo: SessionLedgerRepository, provider: PostgresProvider, schema: str) -> None:
    now_iso = _NOW.isoformat()
    # sess_inv: inverted (last<first) WITH 3 events → repairable to [06-02, 06-08].
    _insert(provider, schema, "session_ledger__session", {
        "id": "sess_inv", "first_event_at": _iso(6, 10), "last_event_at": _iso(6, 1),
        "is_deleted": 0, "created_at": now_iso, "updated_at": now_iso,
    })
    for ev_id, day in (("ev1", 2), ("ev2", 5), ("ev3", 8)):
        _insert(provider, schema, "session_ledger__event", {
            "id": ev_id, "session_id": "sess_inv", "event_at": _iso(6, day),
            "is_deleted": 0, "created_at": now_iso, "updated_at": now_iso,
        })
    # sess_ok: normal (first<last) — must stay untouched.
    _insert(provider, schema, "session_ledger__session", {
        "id": "sess_ok", "first_event_at": _iso(6, 1), "last_event_at": _iso(6, 9),
        "is_deleted": 0, "created_at": now_iso, "updated_at": now_iso,
    })
    # sess_noevt: inverted but NO events → counted, but repair skips (logged).
    _insert(provider, schema, "session_ledger__session", {
        "id": "sess_noevt", "first_event_at": _iso(6, 10), "last_event_at": _iso(6, 1),
        "is_deleted": 0, "created_at": now_iso, "updated_at": now_iso,
    })

    _check(repo.count_inverted_first_last_event_at_sessions() == 2,
           "count: 2 inverted (sess_inv + sess_noevt); sess_ok excluded")

    repaired = repo.repair_inverted_first_last_event_at()
    _check(repaired == 1, f"repair: 1 row recomputed (sess_noevt skipped, no events); got {repaired}")

    new_first = _as_iso(_scalar(provider, schema, "session_ledger__session", "first_event_at", "sess_inv"))
    new_last = _as_iso(_scalar(provider, schema, "session_ledger__session", "last_event_at", "sess_inv"))
    _check(new_first == _iso(6, 2), f"sess_inv first_event_at := min event (06-02); got {new_first}")
    _check(new_last == _iso(6, 8), f"sess_inv last_event_at := max event (06-08); got {new_last}")
    _check(_as_iso(_scalar(provider, schema, "session_ledger__session", "first_event_at", "sess_ok")) == _iso(6, 1),
           "sess_ok untouched")

    _check(repo.count_inverted_first_last_event_at_sessions() == 1,
           "re-count: 1 remains (sess_noevt, no events to repair from)")
    _check(repo.repair_inverted_first_last_event_at() == 0,
           "re-run repair: 0 (sess_inv fixed; sess_noevt still eventless) — idempotent")


def test_summary_pointer_repair(repo: SessionLedgerRepository, provider: PostgresProvider, schema: str) -> None:
    now_iso = _NOW.isoformat()

    def _seed_summary(row_id: str, session_id: object, chunk_index: object, ptr: object) -> None:
        _insert(provider, schema, "session_ledger__summary", {
            "id": row_id, "session_id": session_id, "chunk_index": chunk_index,
            "embedding_vector_id": ptr, "is_deleted": 0,
            "created_at": now_iso, "updated_at": now_iso,
        })

    _seed_summary("s1", "sessX", 0, "emb-aaa")          # recompute → sessX:0
    _seed_summary("s2", "sessY", 3, "emb-bbb")          # recompute → sessY:3
    _seed_summary("s3", None, 5, "emb-ccc")             # broken but unrecomputable → skip
    _seed_summary("s4", "sessZ", 0, "sessZ:0")          # already correct → untouched

    _check(repo.count_summary_rows_with_pgvector_internal_id_pointer() == 3,
           "count: 3 internal 'emb-' pointers (s1,s2,s3); s4 excluded")

    outcome = repo.repair_summary_embedding_vector_ids()
    _check(outcome["updated_count"] == 2, f"updated_count=2 (s1,s2); got {outcome['updated_count']}")
    _check(outcome["skipped_count"] == 1, f"skipped_count=1 (s3, no session_id); got {outcome['skipped_count']}")
    _check(outcome["total_rows_now_correct"] == 3,
           f"total_rows_now_correct=3 (4 active - 1 still-broken); got {outcome['total_rows_now_correct']}")

    _check(_scalar(provider, schema, "session_ledger__summary", "embedding_vector_id", "s1") == "sessX:0",
           "s1 pointer rewritten to deterministic external_id 'sessX:0'")
    _check(_scalar(provider, schema, "session_ledger__summary", "embedding_vector_id", "s2") == "sessY:3",
           "s2 pointer rewritten to 'sessY:3'")
    _check(_scalar(provider, schema, "session_ledger__summary", "embedding_vector_id", "s3") == "emb-ccc",
           "s3 (no session_id) left as internal pointer (skipped)")
    _check(_scalar(provider, schema, "session_ledger__summary", "embedding_vector_id", "s4") == "sessZ:0",
           "s4 (already correct) untouched")

    again = repo.repair_summary_embedding_vector_ids()
    _check(again["updated_count"] == 0 and again["skipped_count"] == 1,
           f"re-run idempotent: updated=0, skipped=1; got {again}")

    # Service-layer wrapper end-to-end (post-repair state: only s3 still broken).
    service = _make_service(repo)
    dry = service.backfill_summary_embedding_vector_ids(confirm=False)
    _check(dry == {"confirmed": False, "updated_count": 0, "skipped_count": 1,
                   "total_rows_now_correct": 0},
           f"service dry-run maps broken-count → skipped_count; got {dry}")
    live = service.backfill_summary_embedding_vector_ids(confirm=True)
    _check(live == {"confirmed": True, "updated_count": 0, "skipped_count": 1,
                    "total_rows_now_correct": 3},
           f"service confirm forwards the migrated repo dict + confirmed=True; got {live}")


def test_orphan_batches(repo: SessionLedgerRepository, provider: PostgresProvider, schema: str) -> None:
    now_iso = _NOW.isoformat()

    def _seed_batch(row_id: str, source_id: str, status: str, started: str) -> None:
        _insert(provider, schema, "session_ledger__import_batch", {
            "id": row_id, "source_id": source_id, "status": status,
            "started_at": started, "is_deleted": 0,
            "created_at": now_iso, "updated_at": now_iso,
        })

    _seed_batch("b1", "src1", "running", _iso(6, 1))      # stale (well past 24h cutoff)
    _seed_batch("b2", "src1", "running", _iso(6, 2))      # stale
    _seed_batch("b3", "src1", "running", _iso(6, 20, 11)) # recent (1h ago) → not stale
    _seed_batch("b4", "src1", "failed", _iso(6, 1))       # not running → untouched
    _seed_batch("b5", "src2", "running", _iso(6, 1))      # other source → untouched

    outcome = repo.backfill_orphan_running_batches_for_source("src1")
    _check(outcome["total_orphan_count_before"] == 3, f"total_orphan_count_before=3 (b1,b2,b3); got {outcome['total_orphan_count_before']}")
    _check(outcome["repaired_count"] == 2, f"repaired_count=2 (b1,b2 stale); got {outcome['repaired_count']}")
    _check(outcome["untouched_count"] == 1, f"untouched_count=1 (b3 recent); got {outcome['untouched_count']}")

    _check(_scalar(provider, schema, "session_ledger__import_batch", "status", "b1") == "failed",
           "b1 flipped RUNNING → FAILED")
    _check(_scalar(provider, schema, "session_ledger__import_batch", "error_kind", "b1") == "orphan_repair",
           "b1 error_kind = 'orphan_repair'")
    _check(_scalar(provider, schema, "session_ledger__import_batch", "finished_at", "b1") is not None,
           "b1 finished_at set")
    _check(_scalar(provider, schema, "session_ledger__import_batch", "status", "b3") == "running",
           "b3 (recent) stays RUNNING")
    _check(_scalar(provider, schema, "session_ledger__import_batch", "status", "b5") == "running",
           "b5 (other source) untouched")

    again = repo.backfill_orphan_running_batches_for_source("src1")
    _check(again == {"repaired_count": 0, "untouched_count": 1, "total_orphan_count_before": 1},
           f"re-run idempotent: only b3 running, 0 repaired; got {again}")

    # Service-layer wrapper end-to-end (post-repair state: only b3 running for src1).
    service = _make_service(repo)
    dry = service.backfill_orphan_running_batches_for_source("src1", confirm=False)
    _check(dry == {"confirmed": False, "repaired_count": 0, "untouched_count": 0,
                   "total_orphan_count_before": 0, "source_id": "src1"},
           f"service dry-run returns structured zeros + source_id; got {dry}")
    live = service.backfill_orphan_running_batches_for_source("src1", confirm=True)
    _check(live == {"confirmed": True, "repaired_count": 0, "untouched_count": 1,
                    "total_orphan_count_before": 1, "source_id": "src1"},
           f"service confirm forwards the migrated repo dict + confirmed/source_id; got {live}")


def test_summary_cas_miss(provider: PostgresProvider, schema: str) -> None:
    """MINOR-1(a) regression: a pointer-CAS LOST to a concurrent winner.

    A concurrent repair rewrites the broken row to its correct external_id BEFORE
    our `update_state`, so our CAS (filtered on the old ``emb-`` value) returns 0
    rows-affected — but the row is now CORRECT. The POST-STATE recount must report
    it FIXED (not skipped) and count it among the now-correct rows. A
    counts-from-own-hits impl would wrongly report ``{updated:0, skipped:1, ...}``.
    """
    now_iso = _NOW.isoformat()
    _insert(provider, schema, "session_ledger__summary", {
        "id": "cmW", "session_id": "cmW", "chunk_index": 0,
        "embedding_vector_id": "emb-www", "is_deleted": 0,  # broken; correct = cmW:0
        "created_at": now_iso, "updated_at": now_iso,
    })
    _insert(provider, schema, "session_ledger__summary", {
        "id": "cmK", "session_id": "cmK", "chunk_index": 0,
        "embedding_vector_id": "cmK:0", "is_deleted": 0,  # already correct
        "created_at": now_iso, "updated_at": now_iso,
    })
    fired = {"done": False}

    def winner(query: dict[str, object]) -> None:
        if not fired["done"] and str(query.get("table")) == "summary":
            fired["done"] = True
            _raw_set(provider, schema, "session_ledger__summary", "cmW",
                     embedding_vector_id="cmW:0")

    repo = _make_repo(provider, pre_update_hook=winner)
    outcome = repo.repair_summary_embedding_vector_ids()
    _check(fired["done"], "concurrent winner fired (forced the pointer-CAS miss)")
    _check(outcome == {"updated_count": 1, "skipped_count": 0, "total_rows_now_correct": 2},
           f"CAS-missed row reported FIXED via post-state recount, not skipped; got {outcome}")
    _check(_scalar(provider, schema, "session_ledger__summary", "embedding_vector_id", "cmW") == "cmW:0",
           "cmW carries the correct external_id (written by the concurrent winner)")


def test_orphan_cas_miss(provider: PostgresProvider, schema: str) -> None:
    """MINOR-1(b) regression: a stale RUNNING batch a concurrent owner completes
    out of RUNNING before our status-CAS.

    The batch is no longer running, so the POST-STATE recount must NOT count it as
    ``untouched`` (which means still-RUNNING). A ``total - repaired`` impl would
    wrongly report ``{repaired:1, untouched:1}`` for the completed-then-missed row.
    """
    now_iso = _NOW.isoformat()
    for bid in ("om1", "om2"):
        _insert(provider, schema, "session_ledger__import_batch", {
            "id": bid, "source_id": "omsrc", "status": "running",
            "started_at": _iso(6, 1), "is_deleted": 0,
            "created_at": now_iso, "updated_at": now_iso,
        })
    fired = {"done": False}

    def completer(query: dict[str, object]) -> None:
        if not fired["done"] and str(query.get("table")) == "import_batch":
            fired["done"] = True
            _raw_set(provider, schema, "session_ledger__import_batch", "om1",
                     status="completed")

    repo = _make_repo(provider, pre_update_hook=completer)
    outcome = repo.backfill_orphan_running_batches_for_source("omsrc")
    _check(fired["done"], "concurrent completer fired (om1 left RUNNING before our CAS)")
    _check(outcome == {"repaired_count": 2, "untouched_count": 0, "total_orphan_count_before": 2},
           f"completed-then-missed batch not counted untouched (post-state recount); got {outcome}")
    _check(_scalar(provider, schema, "session_ledger__import_batch", "status", "om1") == "completed",
           "om1 stays 'completed' (concurrent owner), NOT flipped to failed")
    _check(_scalar(provider, schema, "session_ledger__import_batch", "status", "om2") == "failed",
           "om2 (still stale RUNNING at CAS time) flipped to failed")


def test_source_kind_mismatch(provider: PostgresProvider, schema: str) -> None:
    """COVERAGE (Codex): the unchanged source_kind defense-in-depth guard.

    A ``source_kind`` that mismatches the source row's actual kind returns
    all-zero counts and fires NO update — even with a stale RUNNING batch that
    WOULD otherwise be repaired. Exercises ``get_source`` (still raw-SQL
    ``_fetch_all``) via the adapter's ``execute_sql``.
    """
    now_iso = _NOW.isoformat()
    _insert(provider, schema, "session_ledger__source", {
        "id": "sk1", "source_kind": "claude_code_local", "root_uri": "file:///x",
        "account_label": None, "enabled": True, "config_json": "{}",
        "is_deleted": 0, "created_at": now_iso, "updated_at": now_iso,
    })
    _insert(provider, schema, "session_ledger__import_batch", {
        "id": "skb", "source_id": "sk1", "status": "running",
        "started_at": _iso(6, 1), "is_deleted": 0,
        "created_at": now_iso, "updated_at": now_iso,
    })
    repo = _make_repo(provider)
    outcome = repo.backfill_orphan_running_batches_for_source(
        "sk1", source_kind="codex_local",  # mismatches the source's claude_code_local
    )
    _check(outcome == {"repaired_count": 0, "untouched_count": 0, "total_orphan_count_before": 0},
           f"source_kind mismatch → all-zero counts; got {outcome}")
    _check(_scalar(provider, schema, "session_ledger__import_batch", "status", "skb") == "running",
           "stale batch stays RUNNING — guard fired NO update")


def _as_iso(value: object) -> str:
    """Normalize a provider-returned timestamp (datetime or str) to an ISO string."""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _drop_schema(provider: PostgresProvider, schema_name: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(cast(LiteralString, f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))


def _run_behavioral_suite() -> None:
    """The 3 happy-path behavioral tests, sharing one throwaway schema."""
    schema_name = f"example_test_inv_behavioral_{secrets.token_hex(3)}"
    provider = PostgresProvider(_load_pg_config(schema_name))
    provider.initialize()
    try:
        _create_tables(provider, schema_name)
        repo = _make_repo(provider)
        test_inverted_bounds(repo, provider, schema_name)
        test_summary_pointer_repair(repo, provider, schema_name)
        test_orphan_batches(repo, provider, schema_name)
    finally:
        _drop_schema(provider, schema_name)


def _run_in_schema(label: str, fn: Callable[[PostgresProvider, str], None]) -> None:
    """Run one regression case in its OWN fresh schema (fully-controlled corpus)."""
    schema_name = f"example_test_inv_{label}_{secrets.token_hex(3)}"
    provider = PostgresProvider(_load_pg_config(schema_name))
    provider.initialize()
    try:
        _create_tables(provider, schema_name)
        fn(provider, schema_name)
    finally:
        _drop_schema(provider, schema_name)


def main() -> int:
    if os.environ.get("INVERTED_BOUNDS_LIVE_SMOKE") != "1":
        print("=== inverted_bounds_repair_live_smoke ===")
        print("  SKIP  set INVERTED_BOUNDS_LIVE_SMOKE=1 to run; needs the live DB (own throwaway schema).")
        return 0
    print("=== inverted_bounds_repair_live_smoke ===")
    _run_behavioral_suite()
    # Concurrency-regression + guard cases each get their OWN schema so the
    # corpus is fully controlled (the summary repair scans the whole table).
    _run_in_schema("summary_cas_miss", test_summary_cas_miss)
    _run_in_schema("orphan_cas_miss", test_orphan_cas_miss)
    _run_in_schema("source_kind_guard", test_source_kind_mismatch)
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
