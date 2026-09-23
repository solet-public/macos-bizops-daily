"""Stub state_service for session_ledger smoke tests (no pytest).

Provides an in-memory ``StubStateService`` that records every ``execute_sql``
call and a ``StubTransaction`` returned from ``transactional()`` so smokes
can verify both the SQL shape and (via simple in-memory tables for the
agent_messaging source smoke) read-back behavior.

The stub does NOT execute real SQL — it records calls and (when a smoke
plants rows) returns them on SELECT. INSERT/UPDATE statements are
recorded but otherwise no-ops, matching the smoke pattern from
``secrets_manager_vault_smoke.py``.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ananta.llm.session_ledger.schema import TABLE_SESSION

# Naive baseline used when a smoke drives ``append_event`` without planting a
# ``__session`` row: the migrated ``_touch_session_counters`` reads the row's
# current ``last_event_at`` to recompute the high-water mark (the read-compute-
# write replacement for SQL ``GREATEST``). Returning this epoch makes
# ``max(epoch, event_at) == event_at`` so blind-append smokes keep working
# without planting a session row, exactly as the pre-migration blind UPDATE did.
_STUB_SESSION_EPOCH = datetime(1970, 1, 1)  # noqa: DTZ001 — naive UTC (F1 seam)


@dataclass(slots=True)
class RecordedCall:
    sql: str
    params: list[object]


class StubTransaction:
    """In-memory transaction handle. Records execute / fetch_one calls."""

    def __init__(self, state: StubStateService) -> None:
        self._state = state
        self.executes: list[RecordedCall] = []
        self.fetches: list[RecordedCall] = []

    def execute(self, sql: str, params: list[object] | None = None) -> None:
        recorded = RecordedCall(sql=sql, params=list(params or []))
        self.executes.append(recorded)
        self._state.calls.append(recorded)

    def executemany(self, sql: str, params_seq: list[list[object]]) -> None:
        for params in params_seq:
            self.execute(sql, params)

    def fetch_one(
        self, sql: str, params: list[object] | None = None
    ) -> dict[str, object] | None:
        recorded = RecordedCall(sql=sql, params=list(params or []))
        self.fetches.append(recorded)
        self._state.calls.append(recorded)
        for matcher, response in self._state.fetch_one_responses:
            if matcher(sql, recorded.params):
                return response
        # Sensible defaults for common transactional reads
        if "UPDATE" in sql and "RETURNING last_message_cursor" in sql:
            return {"last_message_cursor": 0}
        if "UPDATE" in sql and "RETURNING event_count" in sql:
            self._state.allocated_sequence += 1
            return {"event_count": self._state.allocated_sequence}
        # Canonical-INSERT SELECT-back: Phase 1 of the M18 two-phase upsert
        # at ``repository._insert_session_with_canonical_dispatch`` runs
        # ``INSERT ... ON CONFLICT DO NOTHING`` then SELECT-backs by id. The
        # stub does not model the partial-unique constraint, so the natural
        # default is "row landed." Tests that want to exercise the Phase 2
        # canonical-race fallback can prime ``fetch_one_responses`` with a
        # matcher that returns ``None`` for this pattern.
        if "SELECT id FROM session_ledger__session WHERE id = %s" in sql:
            return {"id": recorded.params[0] if recorded.params else None}
        # Polling-lease acquisition + adopt-route-batch defaults are NOT
        # provided here because the ``polling_lease_smoke.py`` tests rely
        # on the unprimed-call → ``None`` contract to exercise the
        # ``stale-owner refresh`` + ``heartbeat lease-lost`` paths. Tests
        # that need the lease acquisition to succeed must prime
        # ``fetch_one_responses`` with a matcher returning a non-None row
        # (see ``importer_smoke._make_importer`` and
        # ``integration_smoke._build_service`` for the canonical primer
        # shape).
        return None

    def fetch_all(
        self, sql: str, params: list[object] | None = None
    ) -> list[dict[str, object]]:
        recorded = RecordedCall(sql=sql, params=list(params or []))
        self.fetches.append(recorded)
        self._state.calls.append(recorded)
        return []

    # ------------------------------------------------------------------
    # Typed, non-SQL txn ops (SQL-lockdown Slice 5). Mirror the live
    # StateTransaction shapes: return plain values (not ActionResult) and
    # record structured calls on the shared StubStateService logs.
    # ------------------------------------------------------------------

    def write_state(self, namespace: str, data: dict[str, Any]) -> str:
        table = str(data.get("table", ""))
        record = dict(data.get("record") or {})
        self._state.writes.append(
            WriteRecord(namespace=namespace, table=table, record=record)
        )
        return str(record.get("id", ""))

    def update_state(
        self,
        namespace: str,
        query: dict[str, Any],
        updates: dict[str, Any],
    ) -> int:
        table = str(query.get("table", ""))
        filters = dict(query.get("filters") or {})
        self._state.updates.append(
            UpdateRecord(
                namespace=namespace, table=table, filters=filters, updates=dict(updates)
            )
        )
        return self._state._update_rows_affected

    def query_state(
        self, namespace: str, query: dict[str, Any]
    ) -> list[dict[str, object]]:
        """Typed in-txn read — returns the raw planted rows (no envelope).

        A by-``id`` read of the ``__session`` table with no planted match is the
        ``_touch_session_counters`` high-water read; it gets a synthetic baseline
        row (``last_event_at = epoch``) so blind-``append_event`` smokes need not
        plant a session row. A by-``source_id`` read (``upsert_session``'s
        existing-row lookup) returns ``[]`` when unplanted → the INSERT branch,
        matching the pre-migration ``fetch_one`` → ``None`` contract.
        """
        table = query.get("table")
        filters = dict(query.get("filters") or {})
        rows = self._state._planted_rows(namespace, table, filters)
        if rows:
            return rows
        if table == TABLE_SESSION and "id" in filters and "source_id" not in filters:
            return [
                {
                    "id": filters["id"],
                    "first_event_at": _STUB_SESSION_EPOCH,
                    "last_event_at": _STUB_SESSION_EPOCH,
                }
            ]
        return []

    def increment_and_return(self, namespace: str, data: dict[str, Any]) -> int:
        table = str(data.get("table", ""))
        filters = dict(data.get("filters") or {})
        column = str(data.get("column", ""))
        by = int(data.get("by", 1))
        self._state.increments.append(
            IncrementRecord(
                namespace=namespace,
                table=table,
                filters=filters,
                column=column,
                by=by,
            )
        )
        # The session ``event_count`` increment IS the per-event sequence
        # allocator (mirrors the pre-migration ``RETURNING event_count``
        # default); any other counter (e.g. the import_batch event_count)
        # just echoes a post-increment value the caller discards.
        if table == TABLE_SESSION and column == "event_count":
            self._state.allocated_sequence += by
            return self._state.allocated_sequence
        return by


@dataclass(slots=True)
class UpsertCall:
    namespace: str
    table: str
    record: dict[str, object]
    conflict_columns: list[str]
    on_conflict: str | None = None
    conflict_predicate: list[dict[str, object]] | None = None


@dataclass(slots=True)
class WriteRecord:
    """One ``write_state`` call (autocommit ``_write`` OR ``txn.write_state``)."""

    namespace: str
    table: str
    record: dict[str, object]


@dataclass(slots=True)
class UpdateRecord:
    """One ``update_state`` call (autocommit ``_update`` OR ``txn.update_state``)."""

    namespace: str
    table: str
    filters: dict[str, object]
    updates: dict[str, object]


@dataclass(slots=True)
class IncrementRecord:
    """One ``txn.increment_and_return`` call (the cursor/counter allocator)."""

    namespace: str
    table: str
    filters: dict[str, object]
    column: str
    by: int


@dataclass(slots=True)
class DeleteRecord:
    """One autocommit ``delete_records`` call (the ``_delete`` seam)."""

    namespace: str
    table: str
    filters: dict[str, object]
    soft_delete: bool


@dataclass(slots=True)
class AcquireLeaseRecord:
    """One autocommit ``acquire_lease`` call (the ``_acquire_lease`` seam)."""

    namespace: str
    table: str
    filters: dict[str, object]
    lease_column: str
    set_values: dict[str, object]


@dataclass(slots=True)
class QueryOrderedCall:
    """One autocommit ``query_ordered`` call (the ``_query_ordered`` read seam).

    Records the typed-op shape (filters / order_by / limit) so a unit smoke can
    assert a migrated read passes the RIGHT predicate — e.g. ``called_at`` with a
    ``gte`` comparison op, the ``[[col, dir], [id, dir]]`` total-order tie-break,
    and the ≤ 100 cap — without a live DB. Behavioral filter+order correctness
    (does the predicate actually select the right rows) stays in the live-schema
    smokes per the migration's real-schema test mandate.
    """

    namespace: str
    table: str
    filters: dict[str, object]
    order_by: list[list[str]]
    limit: int
    # LED-01: the primitive's native row-value keyset cursor. ``None`` when the
    # caller sent no cursor (first page). Recorded so a smoke can assert a
    # paged read passes the previous page's ``(event_at, id)`` tail through.
    after: list[object] | None = None


@dataclass(slots=True)
class QueryStateCall:
    """One autocommit ``query_state`` call (the ``_query`` read seam).

    Records the typed filter dict so a unit smoke can assert a migrated read
    passes the RIGHT predicate (e.g. the list_sessions junction route's
    ``canonical_external_session_id IS NULL`` default + the equality filters)
    without a live DB. Filters are NOT applied by the shim (planted rows return
    as-is); behavioral filter correctness stays in the live-schema smokes.
    """

    namespace: str
    table: str
    filters: dict[str, object]


@dataclass(slots=True)
class StubStateService:
    """Records calls; returns canned responses on SELECTs.

    Usage:
        stub = StubStateService()
        stub.add_select_response("FROM session_ledger__source", [{"id": "src_1", ...}])
    """

    calls: list[RecordedCall] = field(default_factory=list)
    select_responses: list[tuple[str, list[dict[str, object]]]] = field(default_factory=list)
    # Filter-aware planted responses for the typed ``query_state`` /
    # ``query_ordered`` shims: ``(table, when, rows)`` where ``when`` is an
    # optional ``filters -> bool`` predicate (first match wins). Lets a smoke
    # distinguish two queries against the SAME table by their filters — e.g. the
    # session-upsert keystone's existing-row read (source_id/external_session_id)
    # vs its canonical resolve (canonical_external_session_id IS NULL).
    query_responses: list[
        tuple[str, Any, list[dict[str, object]]]
    ] = field(default_factory=list)
    fetch_one_responses: list[
        tuple[Any, dict[str, object] | None]
    ] = field(default_factory=list)
    allocated_sequence: int = 0
    upserts: list[UpsertCall] = field(default_factory=list)
    # Typed-primitive call logs (SQL-lockdown Slice 5). Both the autocommit
    # ``write_state`` / ``update_state`` AND the ``StubTransaction`` typed ops
    # append here, so a smoke can assert "a ``__event`` row was written with
    # these fields" without caring which execution path carried it.
    writes: list[WriteRecord] = field(default_factory=list)
    updates: list[UpdateRecord] = field(default_factory=list)
    increments: list[IncrementRecord] = field(default_factory=list)
    deletes: list[DeleteRecord] = field(default_factory=list)
    acquire_leases: list[AcquireLeaseRecord] = field(default_factory=list)
    query_ordered_calls: list[QueryOrderedCall] = field(default_factory=list)
    query_state_calls: list[QueryStateCall] = field(default_factory=list)
    # In-memory key-value store backing set_key_value/get_key_value, keyed by
    # (namespace, key, scope). Backs the LED-01 Lane-1 drain cursor round-trip.
    key_values: dict[tuple[str, str, str], object] = field(default_factory=dict)
    _update_rows_affected: int = 1
    _delete_rows_affected: int = 1
    _acquire_lease_result: bool = True
    _upsert_inserted_result: bool = True
    # Controllable scalar for the ``max_value`` aggregate stub (append_event's
    # ``_next_sequence`` MAX(sequence) read). None = empty set → first sequence 1.
    _max_value_result: object | None = None
    # Controllable scalar for the ``count`` aggregate stub (the reset verbs'
    # null-external_id precondition guard + count_rows_per_table). Default 0 =
    # no null-external_id events, so the reset guard passes.
    _count_result: int = 0
    # Matcher-keyed FAILURE injection for ``upsert_state`` (2026-08-06,
    # session-ledger duplicate-external_id fix): distinct from
    # ``_upsert_inserted_result``, which only models the CLEAN "conflict fired,
    # DO NOTHING" outcome (still ``action_status: completed``). This models a
    # genuine provider-level failure envelope (``action_status: failed``) for
    # ONE matched record — e.g. a unique-constraint violation on a DIFFERENT,
    # untargeted index the caller's ``ON CONFLICT`` clause doesn't cover — so a
    # smoke can prove the repository's specific-error handling without a real
    # Postgres connection. ``(table, when, error_message)``; first match wins.
    upsert_failures: list[tuple[str, Any, str]] = field(default_factory=list)

    def add_upsert_failure(
        self, table: str, when: Any, error_message: str,
    ) -> None:
        """Queue a FAILED ``upsert_state`` envelope for the first record
        matching ``when(record) -> bool`` on ``table``. Never fires for a
        non-matching record — those still hit the normal
        ``_upsert_inserted_result`` path."""
        self.upsert_failures.append((table, when, error_message))

    def _match_upsert_failure(
        self, table_name: str, record: dict[str, object],
    ) -> dict[str, Any] | None:
        """Split out of ``upsert_state`` to keep it under the radon-cc
        threshold — the first queued ``upsert_failures`` entry matching
        ``table_name`` + ``when(record)``, rendered as a FAILED envelope, or
        ``None`` if nothing matches."""
        for fail_table, when, error_message in self.upsert_failures:
            if fail_table != table_name:
                continue
            if when is None or when(record):
                return {
                    "action_status": "failed",
                    "data": {},
                    "actions": [],
                    "error": error_message,
                    "timestamp": "",
                }
        return None

    def add_select_response(
        self, sql_fragment: str, rows: list[dict[str, object]]
    ) -> None:
        """Queue a canned SELECT response keyed by a substring of the SQL."""
        self.select_responses.append((sql_fragment, list(rows)))

    def add_query_response(
        self,
        table: str,
        rows: list[dict[str, object]],
        *,
        when: Any = None,
    ) -> None:
        """Queue a filter-aware ``query_state`` / ``query_ordered`` response.

        ``table`` is the bare table name (e.g. ``"session"``); ``when`` is an
        optional ``filters -> bool`` predicate so a smoke can target one of two
        queries against the same table by inspecting its filters (first match
        wins). Checked before the substring ``select_responses``.
        """
        self.query_responses.append((table, when, list(rows)))

    def add_fetch_one_response(
        self,
        matcher: Any,
        response: dict[str, object] | None,
    ) -> None:
        """Queue a canned fetch_one response keyed by a (sql, params)->bool matcher."""
        self.fetch_one_responses.append((matcher, response))

    def execute_sql(
        self,
        sql_query: str,
        sql_params: list[object] | None = None,
    ) -> dict[str, Any]:
        recorded = RecordedCall(sql=sql_query, params=list(sql_params or []))
        self.calls.append(recorded)
        records: list[dict[str, object]] = []
        for fragment, rows in self.select_responses:
            if fragment in sql_query:
                records = list(rows)
                break
        return {
            "action_status": "completed",
            "data": {"records": records},
            "actions": [],
            "error": None,
            "timestamp": "",
        }

    def upsert_state(
        self,
        namespace: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Record upsert_state calls for the template_flow_record pre-seed pattern.

        Mirrors the live ABC (``StateManagementInterface.upsert_state``) call
        shape and success envelope. The stub does NOT model the partial-
        unique constraints; smokes assert on ``self.upserts`` directly.
        """
        record = data.get("record") or {}
        conflict_columns = data.get("conflict_columns") or []
        on_conflict = data.get("on_conflict")
        conflict_predicate = data.get("conflict_predicate")
        self.upserts.append(
            UpsertCall(
                namespace=namespace,
                table=str(data.get("table", "")),
                record=dict(record),
                conflict_columns=list(conflict_columns),
                on_conflict=on_conflict if isinstance(on_conflict, str) else None,
                conflict_predicate=(
                    list(conflict_predicate)
                    if isinstance(conflict_predicate, list)
                    else None
                ),
            )
        )
        failure = self._match_upsert_failure(str(data.get("table", "")), record)
        if failure is not None:
            return failure
        if on_conflict == "do_nothing":
            # DO-NOTHING mode returns {inserted, id} (the repository's
            # _upsert_do_nothing seam reads data.result.inserted). The recorder
            # cannot model a real partial-unique conflict, so it reports the
            # controllable verdict; smokes prime a conflict via
            # set_upsert_inserted_result(False).
            inserted = self._upsert_inserted_result
            return {
                "action_status": "completed",
                "data": {
                    "namespace": namespace,
                    "result": {
                        "inserted": inserted,
                        "id": str(record.get("id", "")) if inserted else None,
                    },
                },
                "actions": [],
                "error": None,
                "timestamp": "",
            }
        return {
            "action_status": "completed",
            "data": {"namespace": namespace, "result": {"upserted": 1}},
            "actions": [],
            "error": None,
            "timestamp": "",
        }

    def write_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        """Autocommit ``write_state`` shim — records the call, echoes ``id``.

        Mirrors the live envelope ``data.result.generated_id`` that the
        repository's ``_write`` seam extracts (the provider's INSERT does not
        generate ids; the caller supplies ``id`` + every NOT-NULL column).
        """
        table = str(data.get("table", ""))
        record = dict(data.get("record") or {})
        self.writes.append(WriteRecord(namespace=namespace, table=table, record=record))
        generated_id = str(record.get("id", ""))
        return {
            "action_status": "completed",
            "data": {"result": {"generated_id": generated_id}},
            "actions": [],
            "error": None,
            "timestamp": "",
        }

    def update_state(
        self,
        namespace: str,
        query: dict[str, Any],
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        """Autocommit ``update_state`` shim — records the call, reports 1 affected.

        Mirrors the live envelope ``data.result.updated`` (the compare-and-set
        rows-affected signal the repository's ``_update`` seam requires as a real
        int). The recorder cannot model a true row match, so it defaults to 1
        (a row existed); smokes that need a CAS miss prime an override via
        :meth:`set_update_rows_affected`.
        """
        table = str(query.get("table", ""))
        filters = dict(query.get("filters") or {})
        self.updates.append(
            UpdateRecord(
                namespace=namespace, table=table, filters=filters, updates=dict(updates)
            )
        )
        return {
            "action_status": "completed",
            "data": {"result": {"updated": self._update_rows_affected}},
            "actions": [],
            "error": None,
            "timestamp": "",
        }

    def set_update_rows_affected(self, rows_affected: int) -> None:
        """Override the rows-affected reported by ``update_state`` (CAS-miss tests)."""
        self._update_rows_affected = rows_affected

    def delete_records(
        self,
        namespace: str,
        query: dict[str, Any],
    ) -> dict[str, Any]:
        """Autocommit ``delete_records`` shim — records the call, reports a count.

        Mirrors the live plugin envelope ``data.result.{deleted, soft_delete}``
        that the repository's ``_delete`` seam extracts. ``soft_delete`` defaults
        to True (the plugin default) when absent. The recorder cannot model real
        row removal, so it echoes a fixed deleted-count (1); tests that need a
        specific count prime :meth:`set_delete_rows_affected`.
        """
        table = str(query.get("table", ""))
        filters = dict(query.get("filters") or {})
        soft_delete = bool(query.get("soft_delete", True))
        self.deletes.append(
            DeleteRecord(
                namespace=namespace,
                table=table,
                filters=filters,
                soft_delete=soft_delete,
            )
        )
        return {
            "action_status": "completed",
            "data": {"result": {"deleted": self._delete_rows_affected, "soft_delete": soft_delete}},
            "actions": [],
            "error": None,
            "timestamp": "",
        }

    def set_delete_rows_affected(self, rows_affected: int) -> None:
        """Override the rows-affected reported by ``delete_records``."""
        self._delete_rows_affected = rows_affected

    def acquire_lease(
        self,
        namespace: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Autocommit ``acquire_lease`` shim — records the call, reports a verdict.

        Mirrors the live plugin envelope ``data.result.acquired`` (bool) that the
        repository's ``_acquire_lease`` seam extracts. The recorder cannot model
        the expiry-fenced CAS, so it echoes a fixed verdict (True = lease
        claimed); tests that need the not-acquired path prime
        :meth:`set_acquire_lease_result`.
        """
        self.acquire_leases.append(
            AcquireLeaseRecord(
                namespace=namespace,
                table=str(data.get("table", "")),
                filters=dict(data.get("filters") or {}),
                lease_column=str(data.get("lease_column", "")),
                set_values=dict(data.get("set") or {}),
            )
        )
        return {
            "action_status": "completed",
            "data": {"result": {"acquired": self._acquire_lease_result}},
            "actions": [],
            "error": None,
            "timestamp": "",
        }

    def set_acquire_lease_result(self, acquired: bool) -> None:
        """Override the verdict reported by ``acquire_lease`` (not-acquired tests)."""
        self._acquire_lease_result = acquired

    def set_upsert_inserted_result(self, inserted: bool) -> None:
        """Override the ``inserted`` verdict reported by ``upsert_state`` DO-NOTHING.

        ``False`` models the partial-unique conflict firing (Phase 1 loser),
        so the session-upsert keystone falls through to its Phase-2 demotion.
        """
        self._upsert_inserted_result = inserted

    def set_max_value_result(self, value: object | None) -> None:
        """Override the scalar returned by the ``max_value`` aggregate stub."""
        self._max_value_result = value

    def max_value(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        """Autocommit ``max_value`` aggregate stub (append_event ``_next_sequence``).

        Returns the controllable ``_max_value_result`` at ``data.result.value``,
        mirroring the real envelope; ``None`` (default) models an empty set so the
        first event's ``MAX(sequence)+1`` is sequence 1.
        """
        self.calls.append(
            RecordedCall(sql=f"max {data.get('table')}.{data.get('column')}", params=[])
        )
        return {
            "action_status": "completed",
            "data": {"namespace": namespace, "result": {"value": self._max_value_result}},
            "actions": [],
            "error": None,
            "timestamp": "",
        }

    def set_count_result(self, value: int) -> None:
        """Override the scalar returned by the ``count`` aggregate stub."""
        self._count_result = value

    def count(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        """Autocommit ``count`` aggregate stub (reset verbs' null-external_id guard).

        Returns the controllable ``_count_result`` (default 0) at
        ``data.result.value``, mirroring the real ``run_aggregate`` envelope.
        """
        self.calls.append(
            RecordedCall(sql=f"count {data.get('table')}", params=[])
        )
        return {
            "action_status": "completed",
            "data": {"namespace": namespace, "result": {"value": self._count_result}},
            "actions": [],
            "error": None,
            "timestamp": "",
        }

    def query_state(
        self,
        namespace: str,
        query: dict[str, Any],
    ) -> dict[str, Any]:
        """Structured-read shim mirroring the live ``query_state`` envelope.

        The ledger migration moves reads off raw ``execute_sql`` onto the
        typed ``query_state`` / ``query_ordered`` primitives. This shim returns
        the rows a smoke planted for the target table (matched by the physical
        ``namespace__table`` name appearing in the planted fragment), preserving
        the pre-migration "return exactly what was planted" contract. Filter /
        order application is NOT modeled here — behavioral filter+order coverage
        for migrated reads lives in the live-schema smokes per the migration's
        real-schema test mandate.
        """
        filters = query.get("filters")
        self.query_state_calls.append(
            QueryStateCall(
                namespace=namespace,
                table=str(query.get("table", "")),
                filters=dict(filters) if isinstance(filters, dict) else {},
            )
        )
        return self._planted_records(
            namespace, query.get("table"),
            filters if isinstance(filters, dict) else None,
        )

    def query_ordered(
        self,
        namespace: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Ordered-read shim — same planted-row contract as ``query_state``.

        Also records the typed-op shape (table / filters / order_by / limit) on
        ``query_ordered_calls`` so unit smokes can assert the migrated read's
        predicate without a live DB.
        """
        filters = data.get("filters")
        order_by_raw = data.get("order_by")
        after_raw = data.get("after")
        self.query_ordered_calls.append(
            QueryOrderedCall(
                namespace=namespace,
                table=str(data.get("table", "")),
                filters=dict(filters) if isinstance(filters, dict) else {},
                order_by=(
                    [list(pair) for pair in order_by_raw]
                    if isinstance(order_by_raw, list)
                    else []
                ),
                limit=int(data.get("limit", 0)),
                after=list(after_raw) if isinstance(after_raw, list) else None,
            )
        )
        return self._planted_records(
            namespace, data.get("table"),
            filters if isinstance(filters, dict) else None,
        )

    def set_key_value(
        self,
        namespace: str,
        key: str,
        value: object,
        scope: str = "GLOBAL",
        ttl: int | None = None,
    ) -> dict[str, Any]:
        """In-memory KV write; mirrors the state-interface envelope shape."""
        del ttl
        self.key_values[(namespace, key, scope)] = value
        return {
            "action_status": "completed",
            "data": {},
            "actions": [],
            "error": None,
            "timestamp": "",
        }

    def get_key_value(
        self, namespace: str, key: str, scope: str = "GLOBAL",
    ) -> dict[str, Any]:
        """In-memory KV read; absent key → empty ``data`` (no ``value``)."""
        value = self.key_values.get((namespace, key, scope))
        return {
            "action_status": "completed",
            "data": {} if value is None else {"value": value},
            "actions": [],
            "error": None,
            "timestamp": "",
        }

    def _planted_records(
        self,
        namespace: str,
        table: object,
        filters: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        return {
            "action_status": "completed",
            "data": {"records": self._planted_rows(namespace, table, filters)},
            "actions": [],
            "error": None,
            "timestamp": "",
        }

    def _planted_rows(
        self,
        namespace: str,
        table: object,
        filters: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        """Return planted rows for ``namespace__table`` (the raw list, no envelope).

        Shared by the autocommit ``query_state`` / ``query_ordered`` envelope
        shims AND the typed ``StubTransaction.query_state`` (which returns the
        list directly). Records the access so call-count assertions still work.
        Filter-aware ``query_responses`` (matched on bare table + an optional
        ``filters`` predicate) take precedence over the substring
        ``select_responses``.
        """
        needle = f"{namespace}__{table}"
        self.calls.append(RecordedCall(sql=f"query {needle}", params=[]))
        for resp_table, when, rows in self.query_responses:
            if resp_table == table and (when is None or when(filters or {})):
                return list(rows)
        for fragment, rows in self.select_responses:
            if needle in fragment:
                return list(rows)
        return []

    @contextmanager
    def transactional(self) -> Any:
        txn = StubTransaction(self)
        yield txn


@dataclass(slots=True)
class StoredBlob:
    namespace: str
    content: bytes
    metadata: dict[str, object]


@dataclass(slots=True)
class StubBlobStorageService:
    """Stub blob_storage_service that records every store_blob call.

    Smokes verify (a) no store_blob occurs when SecretGate fires; (b) the
    expected metadata shape lands on clean writes.
    """

    blobs: list[StoredBlob] = field(default_factory=list)
    _next_id: int = 1

    def store_blob(
        self,
        namespace: str,
        content: bytes,
        metadata: dict[str, object],
    ) -> dict[str, Any]:
        blob_id = f"bmd_{self._next_id:06d}"
        self._next_id += 1
        self.blobs.append(
            StoredBlob(
                namespace=namespace,
                content=bytes(content),
                metadata=dict(metadata),
            )
        )
        return {
            "action_status": "completed",
            "data": {"blob_id": blob_id},
            "actions": [],
            "error": None,
            "timestamp": "",
        }

    def retrieve_blob(self, _blob_id: str) -> dict[str, Any]:
        raise NotImplementedError("smoke stubs do not implement retrieve")

    def delete_blob(self, _namespace: str, _blob_id: str) -> dict[str, Any]:
        raise NotImplementedError("smoke stubs do not implement delete")


def select_calls(state: StubStateService, fragment: str) -> list[RecordedCall]:
    """Helper: every recorded call whose SQL contains ``fragment``."""
    return [c for c in state.calls if fragment in c.sql]


__all__ = [
    "AcquireLeaseRecord",
    "DeleteRecord",
    "IncrementRecord",
    "QueryOrderedCall",
    "RecordedCall",
    "StoredBlob",
    "StubBlobStorageService",
    "StubStateService",
    "StubTransaction",
    "UpdateRecord",
    "UpsertCall",
    "WriteRecord",
    "select_calls",
]
