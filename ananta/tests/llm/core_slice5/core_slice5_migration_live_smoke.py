#!/usr/bin/env python3
"""Live-Postgres behavioral smoke for core execute_sql Slice-5 (SQL lockdown).

Pins the migrated ``ActionQueuePoller`` sites (GAP-CORE-1, the keystone
serial-drain class) against a REAL ``PostgresProvider`` — ALL raw ``execute_sql``
surfaces are gone.

Clean-9 (reads → ``query_state`` with dict-key marshalling + graceful-degrade;
writes → ``update_state`` tolerate-0):

* ``_fetch_action_blueprint``        — ``SELECT action_blueprint … LIMIT 1`` → first-record.
* ``_fetch_process_error_template``  — ``SELECT action_definition_template …`` → first-record + JSON.
* ``_retrieve_action_details``       — 11-col point SELECT → tuple marshalled by COLUMN NAME.
* ``_retrieve_failed_action_details``— 9-col point SELECT → dict marshalling.
* ``_get_flow_error_count``          — ``COUNT(*) … status='failed'`` → ``len``.
* ``_token_has_pending_jobs``        — ``NOT IN`` → ``= ANY`` closed-complement {queued,processing,error}.
* ``_mark_action_processing`` / ``_update_action_status_to_completed`` /
  ``_update_action_status_to_failed`` — status writes, ALL tolerate a 0-affected
  (missing-row) update without raising (the serial poll loop must not crash).

Dispatch tail (the keystone read; GAP-CORE-1 long-pole):

* ``_get_queued_actions`` — the legacy ``LEFT JOIN core__sessions`` + JSONB
  ``excluded_versions`` containment SQL is replaced by a bounded over-read:
  ``query_ordered(status='queued', order_by [sequence, id], limit cap)`` →
  Python ``excluded_versions`` filter → take ``max_actions_per_poll`` → ONE batch
  ``query_state`` namespace enrichment. Covered here: sequence ordering + the
  over-read take, the ``excluded_versions`` filter (NULL/mine/other), namespace
  enrichment incl. the LEFT-JOIN-NULL case, the fail-loud short-batch-at-cap
  tripwire (fires / does-not-false-fire), and the ``created_at ← sequence`` quirk.

Each path is driven through the REAL production method over a faithful state
adapter (``provider.select`` / ``provider.update`` / ``provider.select_ordered`` —
the same calls the plugin facade makes) on a live provider. The poller is
partial-constructed (``object.__new__`` + only the attributes the migrated
methods touch). Sandbox schema is DROPped in a ``finally``.

Test-fixture process keys are assembled at runtime via ``_fake_process_key`` so
no ``a::b::c`` literal sits in source — the C3.1 whole-tree gate greps test
sources for process_key literals and cannot tell a deliberate not-found FIXTURE
from a real call-site, so a literal here would false-positive tree-wide.

Env-gated behind ``CORE_SLICE5_LIVE_SMOKE=1`` (needs the live DB up; own
throwaway schema).

Run::

    CORE_SLICE5_LIVE_SMOKE=1 \\
      .venv/bin/python3 ananta/tests/llm/core_slice5/core_slice5_migration_live_smoke.py
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import secrets
import sys
from pathlib import Path
from typing import Any, LiteralString, cast

from psycopg.types.json import Json

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)

# Pre-load config_manager (via importlib so the import sorter can't reorder it
# after the poller) so the deep ``plugin_contracts`` chain is cached before
# ``ananta.utils`` initializes — otherwise importing the poller standalone trips
# the utils↔config circular import (resolved at platform boot by load order).
importlib.import_module("ananta.core.config.config_manager")
from ananta.core.actions.action_queue_poller import ActionQueuePoller  # noqa: E402
from ananta.services.state_service.ordered_query import (  # noqa: E402
    parse_ordered_query,
)
from postgres_state_management_plugin.postgres_backend.config import (  # noqa: E402
    PostgresConfig,
)
from postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    PostgresProvider,
)

_passed = 0
_failed: list[str] = []

_POLLER_LOGGER = "ananta.core.actions.action_queue_poller"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _fake_process_key(*parts: str) -> str:
    """Assemble a deliberately-UNREGISTERED process_key from parts at runtime.

    Keeps the ``a::b::c`` literal out of source so the C3.1 whole-tree gate's
    test-source process_key grep cannot false-positive on a fixture key that
    matches no registered process.
    """
    return "::".join(parts)


_PROFILE_PG_CONFIG = (
    REPO_ROOT / "profile" / "config" / "plugins"
    / "postgres_state_management_plugin.json"
)


def _load_pg_config(schema_name: str) -> PostgresConfig:
    config = PostgresConfig(**json.loads(_PROFILE_PG_CONFIG.read_text(encoding="utf-8")))
    config.pg_schema = schema_name
    return config


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"action_status": "completed", "data": data, "actions": [], "error": None, "timestamp": ""}


class _LiveStateAdapter:
    """Faithful StateManagementInterface stand-in mirroring the plugin facade 1:1.

    ``query_state`` → ``provider.select``; ``update_state`` → ``provider.update``;
    ``query_ordered`` → ``parse_ordered_query`` + ``provider.select_ordered`` (the
    exact composition the postgres plugin runs). No ``is_deleted`` filter on the
    equality path — exactly as ``build_select_sql`` and the raw queries behaved.
    """

    def __init__(self, provider: PostgresProvider) -> None:
        self._provider = provider

    def query_state(self, namespace: str, filters: dict[str, Any]) -> dict[str, Any]:
        conds = filters.get("filters") or {}
        rows = self._provider.select(
            namespace=namespace,
            table=str(filters["table"]),
            conditions=cast("dict[str, Any]", conds) if isinstance(conds, dict) else None,
            limit=cast("int | None", filters.get("limit")),
        )
        return _ok({"records": rows, "count": len(rows)})

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
        return _ok({"records": rows, "count": len(rows)})

    def update_state(
        self, namespace: str, query: dict[str, Any], updates: dict[str, Any]
    ) -> dict[str, Any]:
        affected = self._provider.update(
            namespace=namespace,
            table=str(query["table"]),
            conditions=cast("dict[str, Any]", query.get("filters") or {}),
            updates=updates,
        )
        return _ok({"namespace": namespace, "result": {"updated": affected}})


class _SessionsFailAdapter(_LiveStateAdapter):
    """Faithful for everything EXCEPT the sessions namespace read, which returns
    a non-completed envelope — exercises the all-or-nothing dispatch parity."""

    def query_state(self, namespace: str, filters: dict[str, Any]) -> dict[str, Any]:
        if filters.get("table") == "sessions":
            return {"action_status": "error", "data": None, "actions": [], "error": "boom"}
        return super().query_state(namespace, filters)


def _poller(
    provider: PostgresProvider, *, version: str = "v-test", max_per_poll: int = 10,
    adapter: object | None = None,
) -> ActionQueuePoller:
    """Partial-construct the poller with only the attributes the migrated methods
    touch (``state_service`` for all; ``max_actions_per_poll`` + ``_solet_version``
    for the dispatch read). ``adapter`` overrides the default live adapter."""
    poller = object.__new__(ActionQueuePoller)
    poller.state_service = cast("Any", adapter if adapter is not None else _LiveStateAdapter(provider))
    poller.max_actions_per_poll = max_per_poll
    poller._solet_version = version  # noqa: SLF001
    return poller


# ─── Sandbox DDL ─────────────────────────────────────────────────────────────

_DDL: tuple[tuple[str, str], ...] = (
    (
        "core__action_events",
        "id text PRIMARY KEY, process_key text, parameters text, notes text, "
        '"sequence" integer, result_processor text, result_processor_target text, '
        "core__sessions_id text, core__flows_id text, context_id text, "
        "result_processor_kind text, error_processor text, error_processor_kind text, "
        "flow_id_trace text, status text NOT NULL, error_message text, "
        "flow_token_id text, compiled_version text, validation_timestamp text, "
        "job_result_ref text, excluded_versions jsonb, "
        "is_deleted integer NOT NULL DEFAULT 0, "
        "created_at timestamp NOT NULL, updated_at timestamp NOT NULL",
    ),
    (
        "core__sessions",
        "id text PRIMARY KEY, namespace text, "
        "is_deleted integer NOT NULL DEFAULT 0, "
        "created_at timestamp NOT NULL, updated_at timestamp NOT NULL",
    ),
    (
        "core__job",
        "id text PRIMARY KEY, flow_token_id text, status text NOT NULL, "
        "is_deleted integer NOT NULL DEFAULT 0, "
        "created_at timestamp NOT NULL, updated_at timestamp NOT NULL",
    ),
    (
        "core__process_registry",
        "id text PRIMARY KEY, process_key text NOT NULL, action_blueprint text, "
        "action_definition_template text, "
        "is_deleted integer NOT NULL DEFAULT 0, "
        "created_at timestamp NOT NULL, updated_at timestamp NOT NULL",
    ),
)

_SEED_AT = "2026-06-01T00:00:00"


def _create_trigger_function(provider: PostgresProvider, schema: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(cast(
            LiteralString,
            f'CREATE OR REPLACE FUNCTION "{schema}".update_updated_at_column() '
            "RETURNS TRIGGER AS $$ BEGIN "
            "NEW.updated_at = (NOW() AT TIME ZONE 'UTC'); RETURN NEW; "
            "END; $$ LANGUAGE plpgsql;",
        ))


def _create_tables(provider: PostgresProvider, schema: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        for table, body in _DDL:
            cur.execute(cast(LiteralString, f'CREATE TABLE "{schema}"."{table}" ({body})'))
            cur.execute(cast(
                LiteralString,
                f'CREATE TRIGGER "{table}_upd" BEFORE UPDATE ON "{schema}"."{table}" '
                f'FOR EACH ROW EXECUTE FUNCTION "{schema}".update_updated_at_column();',
            ))


def _insert(provider: PostgresProvider, schema: str, table: str, row: dict[str, object]) -> None:
    cols = list(row.keys())
    placeholders = ", ".join(["%s"] * len(cols))
    col_csv = ", ".join(f'"{c}"' for c in cols)
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(LiteralString, f'INSERT INTO "{schema}"."{table}" ({col_csv}) VALUES ({placeholders})'),
            tuple(row[c] for c in cols),
        )


def _truncate(provider: PostgresProvider, schema: str, table: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(cast(LiteralString, f'TRUNCATE TABLE "{schema}"."{table}"'))


def _scalar(provider: PostgresProvider, schema: str, table: str, col: str, row_id: str) -> object:
    rows = provider.execute_query(f'SELECT "{col}" FROM "{schema}"."{table}" WHERE id = %s', (row_id,))
    return rows[0][0] if rows else "<<absent>>"


def _seed_action(
    provider: PostgresProvider, schema: str, *, aid: str, status: str = "queued",
    excluded_versions: list[str] | None = None, **cols: object,
) -> None:
    row: dict[str, object] = {
        "id": aid, "status": status, "is_deleted": 0,
        "created_at": _SEED_AT, "updated_at": _SEED_AT,
    }
    if excluded_versions is not None:
        row["excluded_versions"] = Json(excluded_versions)
    row.update(cols)
    _insert(provider, schema, "core__action_events", row)


def _seed_session(provider: PostgresProvider, schema: str, *, sid: str, namespace: str) -> None:
    _insert(provider, schema, "core__sessions", {
        "id": sid, "namespace": namespace, "is_deleted": 0,
        "created_at": _SEED_AT, "updated_at": _SEED_AT,
    })


def _seed_job(provider: PostgresProvider, schema: str, *, jid: str, token: str, status: str) -> None:
    _insert(provider, schema, "core__job", {
        "id": jid, "flow_token_id": token, "status": status,
        "is_deleted": 0, "created_at": _SEED_AT, "updated_at": _SEED_AT,
    })


# ─── Clean-9 read cases ──────────────────────────────────────────────────────


def test_fetch_action_blueprint(provider: PostgresProvider, schema: str) -> None:
    """Returns the action_blueprint column for a process_key; None when absent."""
    present_key = _fake_process_key("plugin", "demo", "do")
    absent_key = _fake_process_key("plugin", "missing", "x")
    _insert(provider, schema, "core__process_registry", {
        "id": "pr-1", "process_key": present_key, "action_blueprint": '{"bp": 1}',
        "action_definition_template": None, "is_deleted": 0,
        "created_at": _SEED_AT, "updated_at": _SEED_AT,
    })
    poller = _poller(provider)
    _check(poller._fetch_action_blueprint(present_key) == '{"bp": 1}',  # noqa: SLF001
           "_fetch_action_blueprint returns the blueprint via query_state")
    _check(poller._fetch_action_blueprint(absent_key) is None,  # noqa: SLF001
           "_fetch_action_blueprint → None for an unknown process_key")


def test_fetch_process_error_template(provider: PostgresProvider, schema: str) -> None:
    """Parses the JSON action_definition_template for the real process_error key."""
    _insert(provider, schema, "core__process_registry", {
        "id": "pr-err", "process_key": "service_interface::inference_service::process_error",
        "action_blueprint": None, "action_definition_template": '{"arguments": {"model": {}}}',
        "is_deleted": 0, "created_at": _SEED_AT, "updated_at": _SEED_AT,
    })
    poller = _poller(provider)
    template = poller._fetch_process_error_template()  # noqa: SLF001
    _check(template == {"arguments": {"model": {}}},
           "_fetch_process_error_template parses the JSON template dict")


def test_retrieve_action_details_dict_marshalling(provider: PostgresProvider, schema: str) -> None:
    """The 11-field tuple is marshalled by COLUMN NAME (the positional → dict rewrite)."""
    run_key = _fake_process_key("plugin", "demo", "run")
    _seed_action(
        provider, schema, aid="act-d", status="processing",
        process_key=run_key, parameters='{"x": 7}', notes="a-note",
        result_processor="rp", result_processor_target="rpt",
        core__sessions_id="sess-1", core__flows_id="flow-1", context_id="ctx-1",
        result_processor_kind="explicit_plan", error_processor="ep",
        error_processor_kind="llm_authored",
    )
    poller = _poller(provider)
    details = poller._retrieve_action_details("act-d")  # noqa: SLF001
    _check(details is not None, "_retrieve_action_details returns a tuple for a present action")
    if details is not None:
        (process_key, notes, result_processor, result_processor_target, session_id,
         flow_id, context_id, parameters, result_processor_kind, error_processor,
         error_processor_kind) = details
        _check(process_key == run_key, "process_key by key")
        _check(notes == "a-note", "notes by key")
        _check(result_processor == "rp", "result_processor by key")
        _check(result_processor_target == "rpt", "result_processor_target by key")
        _check(session_id == "sess-1", "session_id ← core__sessions_id by key")
        _check(flow_id == "flow-1", "flow_id ← core__flows_id by key")
        _check(context_id == "ctx-1", "context_id by key")
        _check(parameters == {"x": 7}, "parameters parsed from JSON text by key")
        _check(result_processor_kind == "explicit_plan", "result_processor_kind by key")
        _check(error_processor == "ep", "error_processor by key")
        _check(error_processor_kind == "llm_authored", "error_processor_kind by key")
    _check(poller._retrieve_action_details("act-absent") is None,  # noqa: SLF001
           "_retrieve_action_details → None for an absent action")


def test_retrieve_action_details_invalid_process_key_raises(provider: PostgresProvider, schema: str) -> None:
    """A NULL process_key on a present row raises ValueError (validation preserved)."""
    _seed_action(provider, schema, aid="act-nopk", status="processing", process_key=None)
    poller = _poller(provider)
    raised = False
    try:
        poller._retrieve_action_details("act-nopk")  # noqa: SLF001
    except ValueError:
        raised = True
    _check(raised, "_retrieve_action_details RAISES on a NULL/invalid process_key")


def test_retrieve_failed_action_details_dict_marshalling(provider: PostgresProvider, schema: str) -> None:
    """The 8-field failed-details tuple marshals by COLUMN NAME."""
    boom_key = _fake_process_key("plugin", "demo", "boom")
    _seed_action(
        provider, schema, aid="act-f", status="failed",
        process_key=boom_key, parameters='{"y": 9}', notes="f-note",
        result_processor="rp-unused", error_processor="ep-f",
        core__sessions_id="sess-2", core__flows_id="flow-2", context_id="ctx-2",
        error_processor_kind="registry_default",
    )
    poller = _poller(provider)
    details = poller._retrieve_failed_action_details("act-f")  # noqa: SLF001
    _check(details is not None, "_retrieve_failed_action_details returns a tuple")
    if details is not None:
        (process_key, parameters_raw, notes, error_processor, session_id,
         flow_id, context_id, error_processor_kind) = details
        _check(process_key == boom_key, "failed: process_key by key")
        _check(parameters_raw == '{"y": 9}', "failed: parameters_raw passed through (object)")
        _check(notes == "f-note", "failed: notes by key")
        _check(error_processor == "ep-f", "failed: error_processor by key")
        _check(session_id == "sess-2", "failed: session_id ← core__sessions_id by key")
        _check(flow_id == "flow-2", "failed: flow_id ← core__flows_id by key")
        _check(context_id == "ctx-2", "failed: context_id by key")
        _check(error_processor_kind == "registry_default", "failed: error_processor_kind by key")
    _check(poller._retrieve_failed_action_details("act-absent") is None,  # noqa: SLF001
           "_retrieve_failed_action_details → None for an absent action")


def test_get_flow_error_count(provider: PostgresProvider, schema: str) -> None:
    """Counts ONLY failed actions for the flow (= len of the query_state rows)."""
    _seed_action(provider, schema, aid="fe-1", status="failed", flow_id_trace="flow-E")
    _seed_action(provider, schema, aid="fe-2", status="failed", flow_id_trace="flow-E")
    _seed_action(provider, schema, aid="fe-ok", status="completed", flow_id_trace="flow-E")
    _seed_action(provider, schema, aid="fe-other", status="failed", flow_id_trace="flow-OTHER")
    poller = _poller(provider)
    _check(poller._get_flow_error_count("flow-E") == 2,  # noqa: SLF001
           "_get_flow_error_count == 2 (two failed; completed + other-flow excluded)")
    _check(poller._get_flow_error_count("flow-NONE") == 0,  # noqa: SLF001
           "_get_flow_error_count → 0 for a flow with no failed actions")


def test_token_has_pending_jobs_closed_complement(provider: PostgresProvider, schema: str) -> None:
    """Non-terminal complement {queued, processing, error}; terminal {completed, cancelled}.

    The 'error'-status job is the case the closed-domain complement makes explicit
    (the raw NOT-IN's 'failed' term was a no-op — 'failed' is not in the job
    domain), so an 'error'-only token MUST read as pending.
    """
    _seed_job(provider, schema, jid="j-q", token="tok-queued", status="queued")
    _seed_job(provider, schema, jid="j-p", token="tok-proc", status="processing")
    _seed_job(provider, schema, jid="j-e", token="tok-error", status="error")
    _seed_job(provider, schema, jid="j-c1", token="tok-term", status="completed")
    _seed_job(provider, schema, jid="j-c2", token="tok-term", status="cancelled")
    poller = _poller(provider)
    _check(poller._token_has_pending_jobs("tok-queued") is True,  # noqa: SLF001
           "queued job → pending True")
    _check(poller._token_has_pending_jobs("tok-proc") is True,  # noqa: SLF001
           "processing job → pending True")
    _check(poller._token_has_pending_jobs("tok-error") is True,  # noqa: SLF001
           "error job → pending True (the closed-complement case)")
    _check(poller._token_has_pending_jobs("tok-term") is False,  # noqa: SLF001
           "only completed+cancelled jobs → pending False")
    _check(poller._token_has_pending_jobs("tok-none") is False,  # noqa: SLF001
           "no jobs for the token → pending False")


# ─── Clean-9 write cases ─────────────────────────────────────────────────────


def test_mark_action_processing(provider: PostgresProvider, schema: str) -> None:
    """Flips status queued → processing (the former f-string-injection identity update)."""
    _seed_action(provider, schema, aid="w-proc", status="queued")
    poller = _poller(provider)
    poller._mark_action_processing("w-proc")  # noqa: SLF001
    _check(_scalar(provider, schema, "core__action_events", "status", "w-proc") == "processing",
           "_mark_action_processing wrote status=processing via update_state")


def test_update_action_status_to_completed(provider: PostgresProvider, schema: str) -> None:
    """Flips status → completed (the former f-string-injection identity update)."""
    _seed_action(provider, schema, aid="w-done", status="processing")
    poller = _poller(provider)
    poller._update_action_status_to_completed("w-done")  # noqa: SLF001
    _check(_scalar(provider, schema, "core__action_events", "status", "w-done") == "completed",
           "_update_action_status_to_completed wrote status=completed")


def test_update_action_status_to_failed(provider: PostgresProvider, schema: str) -> None:
    """Sets status=failed + error_message together."""
    _seed_action(provider, schema, aid="w-fail", status="processing")
    poller = _poller(provider)
    poller._update_action_status_to_failed("w-fail", "kaboom")  # noqa: SLF001
    _check(_scalar(provider, schema, "core__action_events", "status", "w-fail") == "failed",
           "_update_action_status_to_failed wrote status=failed")
    _check(_scalar(provider, schema, "core__action_events", "error_message", "w-fail") == "kaboom",
           "_update_action_status_to_failed wrote error_message")


def test_status_writes_tolerate_zero_affected(provider: PostgresProvider, schema: str) -> None:
    """A status write against a MISSING row affects 0 and must NOT raise (keystone)."""
    _ = schema
    poller = _poller(provider)
    raised = False
    try:
        poller._mark_action_processing("ghost")  # noqa: SLF001
        poller._update_action_status_to_completed("ghost")  # noqa: SLF001
        poller._update_action_status_to_failed("ghost", "x")  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001 — the whole point is "no exception escapes"
        raised = True
        print(f"  (unexpected) {exc!r}")
    _check(not raised, "all three status writes TOLERATE a 0-affected (missing-row) update — no raise")


# ─── Dispatch-tail cases ─────────────────────────────────────────────────────


def _drain(poller: ActionQueuePoller) -> list[Any]:
    return asyncio.run(poller._get_queued_actions())  # noqa: SLF001


def test_dispatch_ordering_and_overread_take(provider: PostgresProvider, schema: str) -> None:
    """Returns queued rows ordered by sequence, capped at max_actions_per_poll."""
    _truncate(provider, schema, "core__action_events")
    for seq in range(12, 0, -1):  # insert in REVERSE so ordering isn't insertion order
        _seed_action(provider, schema, aid=f"q-{seq:02d}", status="queued",
                     process_key=_fake_process_key("plugin", "demo", "q"), sequence=seq)
    actions = _drain(_poller(provider, max_per_poll=10))
    _check(len(actions) == 10, f"over-read takes exactly max_actions_per_poll=10; got {len(actions)}")
    # created_at == str(sequence) (the legacy record[4]=a.sequence quirk) → ordered 1..10
    _check([a.created_at for a in actions] == [str(s) for s in range(1, 11)],
           "dispatch ordered by sequence asc (1..10), created_at carries sequence")


def test_dispatch_excluded_versions_filter(provider: PostgresProvider, schema: str) -> None:
    """NULL / other-version excluded_versions are claimable; my version is skipped."""
    _truncate(provider, schema, "core__action_events")
    key = _fake_process_key("plugin", "demo", "q")
    _seed_action(provider, schema, aid="ev-null", status="queued", process_key=key, sequence=1)
    _seed_action(provider, schema, aid="ev-mine", status="queued", process_key=key, sequence=2,
                 excluded_versions=["v-test"])
    _seed_action(provider, schema, aid="ev-other", status="queued", process_key=key, sequence=3,
                 excluded_versions=["v-other"])
    actions = _drain(_poller(provider, version="v-test"))
    ids = [a.id for a in actions]
    _check(ids == ["ev-null", "ev-other"],
           f"excluded_versions=['v-test'] skipped; NULL + ['v-other'] claimed; got {ids}")


def test_dispatch_namespace_enrichment(provider: PostgresProvider, schema: str) -> None:
    """template_namespace is the batch-resolved session namespace; None on LEFT-JOIN-NULL."""
    _truncate(provider, schema, "core__action_events")
    _truncate(provider, schema, "core__sessions")
    _seed_session(provider, schema, sid="sx", namespace="ns-x")
    key = _fake_process_key("plugin", "demo", "q")
    _seed_action(provider, schema, aid="ns-hit", status="queued", process_key=key, sequence=1,
                 core__sessions_id="sx")
    _seed_action(provider, schema, aid="ns-miss", status="queued", process_key=key, sequence=2,
                 core__sessions_id="s-absent")  # no matching session row
    _seed_action(provider, schema, aid="ns-null", status="queued", process_key=key, sequence=3)  # null sessions_id
    by_id = {a.id: a for a in _drain(_poller(provider))}
    _check(by_id["ns-hit"].template_namespace == "ns-x", "matched session → template_namespace='ns-x'")
    _check(by_id["ns-miss"].template_namespace is None, "absent session (LEFT-JOIN-NULL) → None")
    _check(by_id["ns-null"].template_namespace is None, "null core__sessions_id → None")


def test_dispatch_sessions_read_failure_drops_batch(provider: PostgresProvider, schema: str) -> None:
    """A failed sessions (namespace) read drops the WHOLE batch to [] — the legacy
    LEFT JOIN was all-or-nothing on a DB error, not a silent all-namespace-None dispatch."""
    _truncate(provider, schema, "core__action_events")
    _seed_action(provider, schema, aid="sf-1", status="queued",
                 process_key=_fake_process_key("plugin", "demo", "q"),
                 sequence=1, core__sessions_id="sx")
    actions = _drain(_poller(provider, adapter=_SessionsFailAdapter(provider)))
    _check(actions == [],
           "failed sessions read → empty batch (all-or-nothing parity), not an all-None dispatch")


def _capture_warnings(fn: Any) -> tuple[Any, bool]:
    """Run ``fn`` while capturing whether a DISPATCH-TRIPWIRE warning fired."""
    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger(_POLLER_LOGGER)
    handler = _Handler()
    prior_level = logger.level
    logger.setLevel(logging.WARNING)
    logger.addHandler(handler)
    try:
        result = fn()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)
    fired = any("DISPATCH-TRIPWIRE" in r.getMessage() for r in records)
    return result, fired


def test_dispatch_tripwire_fires_at_cap(provider: PostgresProvider, schema: str) -> None:
    """Read hits the cap (more queued exists) + all filtered out → tripwire WARNS."""
    _truncate(provider, schema, "core__action_events")
    key = _fake_process_key("plugin", "demo", "q")
    for seq in range(1, 101):  # exactly _DISPATCH_READ_CAP rows, all excluded for me
        _seed_action(provider, schema, aid=f"x-{seq:03d}", status="queued",
                     process_key=key, sequence=seq, excluded_versions=["v-test"])
    actions, fired = _capture_warnings(lambda: _drain(_poller(provider, version="v-test")))
    _check(actions == [], "all 100 excluded → empty dispatch batch")
    _check(fired, "tripwire WARNS: read hit the 100-row cap but 0 slots filled")


def test_dispatch_tripwire_silent_when_shallow(provider: PostgresProvider, schema: str) -> None:
    """A short all-excluded queue below the cap does NOT false-fire the tripwire."""
    _truncate(provider, schema, "core__action_events")
    key = _fake_process_key("plugin", "demo", "q")
    for seq in range(1, 4):  # only 3 rows (< cap), all excluded
        _seed_action(provider, schema, aid=f"s-{seq}", status="queued",
                     process_key=key, sequence=seq, excluded_versions=["v-test"])
    actions, fired = _capture_warnings(lambda: _drain(_poller(provider, version="v-test")))
    _check(actions == [], "all 3 excluded → empty dispatch batch")
    _check(not fired, "tripwire SILENT: read did not hit the cap (short because shallow, not filtered)")


def main() -> int:
    if os.environ.get("CORE_SLICE5_LIVE_SMOKE") != "1":
        print("=== core_slice5_migration_live_smoke ===")
        print(
            "  SKIP  set CORE_SLICE5_LIVE_SMOKE=1 to run; needs the live "
            "solet DB (own throwaway schema)."
        )
        return 0
    print("=== core_slice5_migration_live_smoke ===")
    schema_name = f"example_test_slice5_{secrets.token_hex(3)}"
    provider = PostgresProvider(_load_pg_config(schema_name))
    provider.initialize()
    try:
        _create_trigger_function(provider, schema_name)
        _create_tables(provider, schema_name)
        test_fetch_action_blueprint(provider, schema_name)
        test_fetch_process_error_template(provider, schema_name)
        test_retrieve_action_details_dict_marshalling(provider, schema_name)
        test_retrieve_action_details_invalid_process_key_raises(provider, schema_name)
        test_retrieve_failed_action_details_dict_marshalling(provider, schema_name)
        test_get_flow_error_count(provider, schema_name)
        test_token_has_pending_jobs_closed_complement(provider, schema_name)
        test_mark_action_processing(provider, schema_name)
        test_update_action_status_to_completed(provider, schema_name)
        test_update_action_status_to_failed(provider, schema_name)
        test_status_writes_tolerate_zero_affected(provider, schema_name)
        test_dispatch_ordering_and_overread_take(provider, schema_name)
        test_dispatch_excluded_versions_filter(provider, schema_name)
        test_dispatch_namespace_enrichment(provider, schema_name)
        test_dispatch_sessions_read_failure_drops_batch(provider, schema_name)
        test_dispatch_tripwire_fires_at_cap(provider, schema_name)
        test_dispatch_tripwire_silent_when_shallow(provider, schema_name)
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
