#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for ``agent_messaging::list_threads`` (GAP-5 STUB-2).

Pins the new owning-service thread-enumeration read verb against a REAL
``PostgresProvider``, driven through the ACTUAL ``query_ordered`` production path
(``parse_ordered_query`` → ``select_ordered`` WITH the ``after`` cursor + the
``include_deleted`` gate). The point of the verb is cursor pagination over the
tie-safe composite ``(created_at, id)``; a harness that drops ``after`` /
``include_deleted`` (or hand-builds page-2 cursors) could not discriminate the
one bug class this guards — a same-``created_at`` group silently split at a page
boundary (the R4 / core-Slice-1 sort-key drop).

``created_at`` is a PLATFORM-READ-ONLY field (DB ``DEFAULT NOW() AT TIME ZONE
'UTC'``, never written by app/seed code). So tied vs distinct timestamps are
manufactured the only platform-faithful way — via ``NOW()`` transaction
stability:

* a TIED group = several rows inserted in ONE transaction (all share the
  transaction-start ``NOW()``; ``created_at`` is OMITTED so the default applies);
* a DISTINCT later group = a second transaction a few ms later (strictly-greater
  ``NOW()``).

Explicit, lexically-sortable test ids make the ``(created_at, id)`` order
deterministic; the test asserts the ORDERING + COMPLETENESS invariant and the
tie STRUCTURE (read back from the verb), never a specific ``created_at`` value.

Seed (5 threads): group T1 = {t01, t02, t03} (one txn, tied ``created_at``),
group T2 = {t04[is_deleted=1], t05} (later txn, tied, T2 > T1). Live order:
t01, t02, t03, t05 (t04 soft-deleted). The page-2 boundary at limit=2 falls
between t02 and t03 — BOTH at T1 — so a created_at-only / high-water cursor would
drop t03; the composite cursor must return it.

The schema is the REAL standardized ``get_agent_messaging_schema`` rendered via
the production DDL into a throwaway pg-schema (DROPped in ``finally``), so
``created_at`` carries its true ``DEFAULT NOW()`` + ``is_deleted`` its default.

Needs Postgres up; env-gated behind ``AGENT_MESSAGING_LIST_THREADS_LIVE_SMOKE=1``. Run::

    AGENT_MESSAGING_LIST_THREADS_LIVE_SMOKE=1 \\
      .venv/bin/python3 ananta/tests/llm/agent_messaging/list_threads_live_smoke.py
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, LiteralString, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)

from ananta.llm.agent_messaging.models import (  # noqa: E402
    ListAgentThreadsRequest,
)
from ananta.llm.agent_messaging.repository import (  # noqa: E402
    AgentMessagingRepository,
)
from ananta.llm.agent_messaging.schema import (  # noqa: E402
    NAMESPACE,
    get_agent_messaging_schema,
)
from ananta.llm.agent_messaging.service import AgentMessagingService  # noqa: E402
from ananta.services.state_service.ordered_query import (  # noqa: E402
    parse_ordered_query,
)
from ananta.types.schema_standardizer import SchemaStandardizer  # noqa: E402
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

_THREAD_TABLE = "core__agent_thread"
# Lexically-sortable ids so the (created_at, id) order is deterministic.
_T1_IDS = ("agt-_t01", "agt-_t02", "agt-_t03")
_T2_IDS = ("agt-_t04", "agt-_t05")
_DELETED_ID = "agt-_t04"
_LIVE_ORDER = ["agt-_t01", "agt-_t02", "agt-_t03", "agt-_t05"]
_ALL_ORDER = ["agt-_t01", "agt-_t02", "agt-_t03", "agt-_t04", "agt-_t05"]


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
    """``query_ordered`` over a real provider through the PRODUCTION path.

    Delegates to ``parse_ordered_query`` (the real validation + ``after``
    naive-UTC normalization + cap) then ``provider.select_ordered`` WITH
    ``after`` + ``include_deleted`` — NOT a simplified stand-in that drops them
    (which would make the cursor/soft-delete assertions vacuous).
    """

    def __init__(self, provider: PostgresProvider) -> None:
        self._provider = provider

    def query_ordered(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        spec = parse_ordered_query(data)
        rows = self._provider.select_ordered(
            namespace=namespace,
            table=spec.table,
            conditions=spec.filters,
            order_columns=spec.order_columns,
            direction=spec.direction,
            limit=spec.limit,
            after=spec.after,
            include_deleted=spec.include_deleted,
        )
        return _envelope({"records": rows, "count": len(rows)})


def _service(provider: PostgresProvider) -> AgentMessagingService:
    """Partial-construct the service with only what ``list_threads`` touches:
    the repository (over the live adapter) + an enabled config."""
    repo = AgentMessagingRepository(cast("Any", _LiveStateAdapter(provider)))
    service = object.__new__(AgentMessagingService)
    service._repo = repo
    service._config = cast("Any", SimpleNamespace(enabled=True))
    return service


def _create_schema_tables(provider: PostgresProvider) -> None:
    """Build the real agent_messaging tables in the throwaway pg-schema via the
    PRODUCTION DDL renderer (real ``created_at`` DEFAULT + ``is_deleted``)."""
    schema = SchemaStandardizer().standardize_schema(get_agent_messaging_schema())
    schema_name = provider.config.schema_name
    ops = [
        op
        for table in schema.tables.values()
        for op in emit_create_table_ops(NAMESPACE, table, schema_name)
    ]
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        provider.apply_schema_change_ops(cur, schema, ops)


def _seed_group(
    provider: PostgresProvider, schema: str, rows: tuple[tuple[str, int], ...],
) -> None:
    """Insert ``rows`` (id, is_deleted) in ONE transaction so they SHARE the
    transaction-stable ``NOW()`` ``created_at`` default — the platform-faithful
    way to manufacture a tied-``created_at`` group without writing the
    read-only column (``created_at`` is OMITTED -> DEFAULT applies)."""
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        for thread_id, is_deleted in rows:
            cur.execute(
                cast(
                    LiteralString,
                    f'INSERT INTO "{schema}"."{_THREAD_TABLE}" '
                    "(id, namespace, originator_type, target_backend, "
                    "target_plugin_name, status, is_deleted) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                ),
                (
                    thread_id, "core", "mcp_bridge", "peer:claude_code",
                    "agent_messaging_plugin", "open", is_deleted,
                ),
            )


def _seed(provider: PostgresProvider, schema: str) -> None:
    _seed_group(provider, schema, tuple((tid, 0) for tid in _T1_IDS))
    # Strictly-later transaction → strictly-greater NOW() → T2 > T1 (10ms >>
    # microsecond clock resolution, so this is deterministic, not a flake).
    time.sleep(0.01)
    _seed_group(
        provider, schema,
        tuple((tid, 1 if tid == _DELETED_ID else 0) for tid in _T2_IDS),
    )


def _ids(service: AgentMessagingService, *, limit: int, include_deleted: bool) -> list[str]:
    page = service.list_threads(
        ListAgentThreadsRequest(limit=limit, include_deleted=include_deleted),
    )
    return [thread.id for thread in page.threads]


def _collect_pages(
    service: AgentMessagingService, *, limit: int, include_deleted: bool,
) -> list[str]:
    """Page through list_threads feeding the verb's OWN returned OPAQUE cursor
    back verbatim, until a short page. Returns the flat id list."""
    collected: list[str] = []
    after_cursor: str | None = None
    for _ in range(20):  # runaway guard
        page = service.list_threads(
            ListAgentThreadsRequest(
                after_cursor=after_cursor,
                limit=limit,
                include_deleted=include_deleted,
            ),
        )
        collected.extend(thread.id for thread in page.threads)
        if len(page.threads) < limit:
            return collected
        after_cursor = page.next_cursor
    raise AssertionError("list_threads pagination did not terminate")


def test_seed_tie_structure(service: AgentMessagingService) -> None:
    """Prove the seed actually built a tied T1 group + a distinct later T2 group
    (else the pagination tie test would be vacuous)."""
    page = service.list_threads(ListAgentThreadsRequest(limit=10, include_deleted=True))
    by_id = {thread.id: thread for thread in page.threads}
    t1 = {by_id[i].created_at for i in _T1_IDS}
    t2 = {by_id[i].created_at for i in _T2_IDS}
    _check(len(t1) == 1, f"T1 group (t01-t03) shares ONE created_at (tied); got {t1}")
    _check(len(t2) == 1, f"T2 group (t04-t05) shares ONE created_at (tied); got {t2}")
    _check(
        next(iter(t1)) < next(iter(t2)),
        "T1 created_at strictly < T2 (distinct groups straddle the page boundary)",
    )


def test_first_page_ordering(service: AgentMessagingService) -> None:
    _check(
        _ids(service, limit=10, include_deleted=False) == _LIVE_ORDER,
        f"unscoped enumeration, (created_at,id) asc, soft-deleted excluded; got "
        f"{_ids(service, limit=10, include_deleted=False)}",
    )


def test_include_deleted(service: AgentMessagingService) -> None:
    excl = _ids(service, limit=10, include_deleted=False)
    incl = _ids(service, limit=10, include_deleted=True)
    _check(_DELETED_ID not in excl, "include_deleted=False excludes the soft-deleted thread (t04)")
    _check(_DELETED_ID in incl, "include_deleted=True includes the soft-deleted thread (t04)")
    _check(incl == _ALL_ORDER, f"include_deleted=True returns all 5 in order; got {incl}")


def test_pagination_roundtrip_across_tie(service: AgentMessagingService) -> None:
    """THE discriminator: page (limit=2) by feeding the verb's OWN cursor back.
    The t02|t03 boundary is INSIDE the tied T1 group — a created_at-only /
    high-water cursor would advance past T1 and DROP t03; the composite
    (created_at, id) cursor must return it."""
    collected = _collect_pages(service, limit=2, include_deleted=False)
    _check(
        collected == _LIVE_ORDER,
        f"limit=2 cursor round-trip returns all live threads once, in order, with "
        f"NO drop across the t02|t03 same-created_at boundary; got {collected}",
    )
    _check(len(collected) == len(set(collected)), "no duplicate thread across page boundaries")


def test_pagination_include_deleted_full(service: AgentMessagingService) -> None:
    collected = _collect_pages(service, limit=2, include_deleted=True)
    _check(
        collected == _ALL_ORDER,
        f"limit=2 cursor round-trip (include_deleted) returns all 5 once, in order; got {collected}",
    )


def test_malformed_cursor_rejected(service: AgentMessagingService) -> None:
    """A garbage ``after_cursor`` is REJECTED fail-closed (not silently
    restarted at the beginning — which would re-emit already-ingested rows)."""
    raised = False
    try:
        service.list_threads(ListAgentThreadsRequest(after_cursor="not-a-valid-token"))
    except Exception:  # noqa: BLE001 — ThreadCursorRejectedError; import-light assertion
        raised = True
    _check(raised, "malformed after_cursor RAISES (fail-closed opaque cursor)")


def main() -> int:
    if os.environ.get("AGENT_MESSAGING_LIST_THREADS_LIVE_SMOKE") != "1":
        print("=== list_threads_live_smoke ===")
        print(
            "  SKIP  set AGENT_MESSAGING_LIST_THREADS_LIVE_SMOKE=1 to run; "
            "needs the live solet DB."
        )
        return 0
    print("=== list_threads_live_smoke ===")
    schema_name = f"example_test_listthreads_{secrets.token_hex(3)}"
    provider = PostgresProvider(_load_pg_config(schema_name))
    provider.initialize()
    try:
        _create_schema_tables(provider)
        _seed(provider, schema_name)
        service = _service(provider)
        test_seed_tie_structure(service)
        test_first_page_ordering(service)
        test_include_deleted(service)
        test_pagination_roundtrip_across_tie(service)
        test_pagination_include_deleted_full(service)
        test_malformed_cursor_rejected(service)
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
