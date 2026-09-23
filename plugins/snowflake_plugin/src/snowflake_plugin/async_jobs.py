"""D0.3 deferred-completion machinery for snowflake_plugin.

Ms-scale dispatch (the plugin's ``@platform_process`` handlers) validates
state, creates a job, and returns a ``{"job_id", "status": "queued"}``
envelope immediately; a single lazily-started background daemon thread does
the real connect + query I/O off the action-dispatch path and completes the
job via ``AsyncJobManager.update_job``, which — via the job's
``completion_handlers`` metadata — submits the follow-up action into the
originating flow. Same shape as external_postgres_plugin's async_jobs.py,
itself traced end-to-end against ``comfyui_image_generation_plugin::generate_image``
(workbench/2026-08-09_sync_verb_d03_deferred_completion_doctrine_syncverb-doctrine.md
§5) — no new primitive invented.

ONE worker thread, never more, by design: ``FlowManager._sequence_cache`` is
an unlocked ``dict`` (doctrine §2) — two callers submitting completion
actions into the SAME flow concurrently can lose an update. comfyui stays
safe because it runs exactly one worker thread; this plugin does the same
rather than firing a thread per dispatch, which would reintroduce that named
platform gap on the very first concurrent pair of calls into one flow.

The AsyncJobManager reference is acquired LAZILY (mirrors comfyui's
``_try_acquire_job_manager``), not at ``prepare_for_readiness`` — plugin
boot order does not guarantee ``orchestrator_ref.async_job_manager`` is set
by the time this plugin's readiness hook runs, but it is always set by the
time any verb is actually dispatched.

No ``connection_name`` parameter anywhere here (unlike external_postgres_plugin):
this plugin is single-account v1 — every job resolves the one registered
``snowflake_account`` address-book entry, nothing per-call to validate.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ananta.constants import CONTEXT_KEY_FLOW_ID, CONTEXT_KEY_SESSION_ID

from . import completion_templates, query_actions
from .constants import ERROR_API_ERROR, PLUGIN_NAME

if TYPE_CHECKING:
    from .plugin import SnowflakePlugin

logger = logging.getLogger(PLUGIN_NAME)

POLL_INTERVAL_SECONDS = 2.0
_JOB_LIST_LIMIT = 10

# action_name -> (query_actions callable, needs_export_path_gate)
#
# Unlike external_postgres_plugin's ActionHandler, this stays a 2-tuple even
# after run_statement (the write verb) joins it: Snowflake has no
# connection-level read/write characteristic to select at connect time (no
# ``conn.read_only`` equivalent — see connection.py), so every verb here opens
# an IDENTICAL connection. run_statement manages its own explicit transaction
# (autocommit disabled, then commit/rollback) entirely inside query_actions.py
# — there is nothing for this dispatch table to thread through.
ActionHandler = tuple[Callable[..., dict[str, Any]], bool]
ACTION_HANDLERS: dict[str, ActionHandler] = {
    "list_databases": (query_actions.list_databases, False),
    "list_schemas": (query_actions.list_schemas, False),
    "list_tables": (query_actions.list_tables, False),
    "describe_table": (query_actions.describe_table, False),
    "test_connection": (query_actions.test_connection, False),
    "run_query": (query_actions.run_query, True),
    "export_query": (query_actions.export_query, True),
    "run_statement": (query_actions.run_statement, True),
}


def _validate_state_context(state: dict[str, Any]) -> tuple[str, str]:
    """Dispatch-time required step (doctrine §1): fail loud, not at completion time."""
    session_id = state.get(CONTEXT_KEY_SESSION_ID)
    flow_id = state.get(CONTEXT_KEY_FLOW_ID)
    if not isinstance(session_id, str) or not session_id:
        raise ValueError(f"'{CONTEXT_KEY_SESSION_ID}' missing from state context")
    if not isinstance(flow_id, str) or not flow_id:
        raise ValueError(f"'{CONTEXT_KEY_FLOW_ID}' missing from state context")
    return session_id, flow_id


def _acquire_async_job_manager(plugin: SnowflakePlugin) -> Any:
    if plugin._async_job_manager is not None:
        return plugin._async_job_manager
    orchestrator_ref = plugin.orchestrator_ref
    if orchestrator_ref is None:
        raise RuntimeError(f"{PLUGIN_NAME}: orchestrator_ref not injected")
    manager = getattr(orchestrator_ref, "async_job_manager", None)
    if manager is None:
        raise RuntimeError(f"{PLUGIN_NAME}: async_job_manager not available from orchestrator")
    plugin._async_job_manager = manager
    return manager


def ensure_worker_started(plugin: SnowflakePlugin) -> None:
    """Idempotently start the ONE background worker thread. Thread-safe."""
    with plugin._worker_lock:
        if plugin._worker_thread is not None and plugin._worker_thread.is_alive():
            return
        thread = threading.Thread(
            target=_worker_loop, args=(plugin,), name=f"{PLUGIN_NAME}-async-worker", daemon=True,
        )
        plugin._worker_thread = thread
        thread.start()


def create_job(
    plugin: SnowflakePlugin,
    *,
    action_name: str,
    params: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    """Ms-scale dispatch: validate, write the job row, start the worker, return.

    No connection is opened and no query runs here — that is entirely the
    background worker's job, off this call's return path.
    """
    session_id, flow_id = _validate_state_context(state)
    async_job_manager = _acquire_async_job_manager(plugin)
    ensure_worker_started(plugin)
    return async_job_manager.create_job(
        plugin_name=PLUGIN_NAME,
        action_name=action_name,
        request_data={"notes": f"{PLUGIN_NAME}.{action_name}", **params},
        job_metadata={
            "session_id": session_id,
            "flow_id": flow_id,
            "completion_handlers": completion_templates.build_completion_handlers(action_name),
        },
    )


def _worker_loop(plugin: SnowflakePlugin) -> None:
    while True:
        try:
            _drain_queued_jobs(plugin)
        except Exception:  # the loop must never die — a bad pass still yields the next one
            logger.exception("%s async worker: drain pass failed", PLUGIN_NAME)
        time.sleep(POLL_INTERVAL_SECONDS)


def _drain_queued_jobs(plugin: SnowflakePlugin) -> None:
    async_job_manager = _acquire_async_job_manager(plugin)
    for action_name in ACTION_HANDLERS:
        listing = async_job_manager.list_jobs(
            status="queued",
            provider_name=f"{PLUGIN_NAME}.{action_name}",
            limit=_JOB_LIST_LIMIT,
        )
        if listing.get("action_status") != "completed":
            continue
        jobs = listing.get("data", {}).get("jobs", [])
        for job in jobs:
            job_id = job.get("id") if isinstance(job, dict) else None
            if isinstance(job_id, str) and job_id:
                _process_job(plugin, async_job_manager, job_id, action_name)


def _process_job(
    plugin: SnowflakePlugin, async_job_manager: Any, job_id: str, action_name: str,
) -> None:
    """Run one job's real I/O and complete it. Runs on the ONE worker thread only."""
    async_job_manager.update_job(job_id, {"status": "processing"})
    payload_result = async_job_manager.get_job_payload(job_id, "request")
    if payload_result.get("action_status") != "completed":
        async_job_manager.update_job(
            job_id,
            {
                "status": "error",
                "error": {
                    "code": ERROR_API_ERROR,
                    "message": "could not load the job's request payload",
                },
            },
        )
        return
    params = payload_result.get("data", {}).get("payload", {})
    action_fn, needs_export_gate = ACTION_HANDLERS[action_name]
    if needs_export_gate:
        outcome = plugin._run_on_connection(
            lambda conn: action_fn(conn, params, plugin._export_path_gate), action_name,
        )
    else:
        outcome = plugin._run_on_connection(lambda conn: action_fn(conn, params), action_name)
    if outcome.get("action_status") == "completed":
        async_job_manager.update_job(job_id, {"status": "completed", "result": outcome.get("data", {})})
    else:
        async_job_manager.update_job(job_id, {"status": "error", "error": outcome.get("error", {})})
