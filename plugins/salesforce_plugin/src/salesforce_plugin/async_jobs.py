"""D0.3 deferred-completion machinery for salesforce_plugin.

Ms-scale dispatch (the plugin's ``@platform_process`` handlers) validates the
state context, creates a job, and returns a ``{"job_id", "status": "queued"}``
envelope immediately; a single lazily-started background daemon thread does
the real `sf` CLI subprocess spawn off the action-dispatch path and completes
the job via ``AsyncJobManager.update_job``, which — via the job's
``completion_handlers`` metadata — submits the follow-up action into the
originating flow. Same shape as the doctrine's traced worked example
(``comfyui_image_generation_plugin::generate_image``) and as
``external_postgres_plugin``'s landed batch 1 — the closest exemplar, since
both plugins run one real I/O call per verb (no internal pagination loop) via
a driver/CLI executor object rather than a chatty REST client
(workbench/2026-08-09_sync_verb_d03_deferred_completion_doctrine_syncverb-doctrine.md
§5, §7).

**Containment (this lane's brief, "keep the executor, contain the
dispatch"):** the dispatch handlers never touch ``SalesforceCliExecutor`` —
only this module's worker thread does, exactly as before this migration.
Salesforce verbs already ran with an effective concurrency of one (the
plugin's ``_cli_executor`` is a single instance, and every dispatch used to
run inline on the poller); routing every call through ONE background worker
thread preserves that same effective seriality rather than introducing new
concurrency the process-spawn-per-call executor was never built for.

ONE worker thread, never more, by design: ``FlowManager._sequence_cache`` is
an unlocked ``dict`` (doctrine §2) — two callers submitting completion
actions into the SAME flow concurrently can lose an update. A single worker
thread keeps this plugin safe the same way it keeps comfyui and
external_postgres_plugin safe.

The ``AsyncJobManager`` reference is acquired LAZILY (mirrors comfyui's
``_try_acquire_job_manager`` / external_postgres_plugin's own copy), not at
``prepare_for_readiness`` — plugin boot order does not guarantee
``orchestrator_ref.async_job_manager`` is set by the time this plugin's
readiness hook runs, but it is always set by the time any verb is actually
dispatched.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ananta.constants import CONTEXT_KEY_FLOW_ID, CONTEXT_KEY_SESSION_ID

from . import bulk_actions, completion_templates, record_actions, soql_actions
from .client import SalesforceCliExecutor
from .constants import ERROR_API_ERROR, PLUGIN_NAME

if TYPE_CHECKING:
    from .plugin import SalesforcePlugin

logger = logging.getLogger(PLUGIN_NAME)

POLL_INTERVAL_SECONDS = 2.0
_JOB_LIST_LIMIT = 10


def _test_connection_action(
    executor: SalesforceCliExecutor, _params: dict[str, Any]
) -> dict[str, Any]:
    """Verify the org binding — same body as the pre-migration inline closure."""
    org_id = soql_actions.fetch_org_id(executor)
    return {
        "ok": True,
        "org_id": org_id,
        "username": executor.username,
        "api_version": executor.api_version,
    }


# action_name -> (action callable, gate_factory). gate_factory, when not
# None, takes the plugin and returns the PathGate the action needs — export
# gate for TSV/CSV writes, import gate for a bulk ingest CSV read. None means
# the action takes no path (params-only, or a plain executor call).
GateFactory = Callable[["SalesforcePlugin"], Any]
ActionHandler = tuple[Callable[..., dict[str, Any]], GateFactory | None]
ACTION_HANDLERS: dict[str, ActionHandler] = {
    "soql_query": (soql_actions.soql_query, lambda plugin: plugin._export_path_gate),  # noqa: SLF001
    "export_soql": (soql_actions.export_soql, lambda plugin: plugin._export_path_gate),  # noqa: SLF001
    "get_record": (record_actions.get_record, None),
    "describe_sobject": (record_actions.describe_sobject, None),
    "list_sobjects": (record_actions.list_sobjects, None),
    "create_record": (record_actions.create_record, None),
    "update_record": (record_actions.update_record, None),
    "delete_record": (record_actions.delete_record, None),
    "test_connection": (_test_connection_action, None),
    "bulk_ingest_submit": (bulk_actions.bulk_ingest_submit, lambda plugin: plugin._import_path_gate),  # noqa: SLF001
    "bulk_job_status": (bulk_actions.bulk_job_status, None),
    "bulk_job_results": (bulk_actions.bulk_job_results, lambda plugin: plugin._csv_export_path_gate),  # noqa: SLF001
    "bulk_job_abort": (bulk_actions.bulk_job_abort, None),
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


def _acquire_async_job_manager(plugin: SalesforcePlugin) -> Any:
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


def ensure_worker_started(plugin: SalesforcePlugin) -> None:
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
    plugin: SalesforcePlugin,
    *,
    action_name: str,
    params: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    """Ms-scale dispatch: validate the state context, write the job row, start the worker, return.

    No `sf` CLI subprocess is spawned here — that is entirely the background
    worker's job, off this call's return path. Per-verb param validation
    (required fields, the row-limit override pair, etc.) stays in the action
    functions, which the worker calls — same division as
    external_postgres_plugin's ``create_job``.
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


def _worker_loop(plugin: SalesforcePlugin) -> None:
    while True:
        try:
            _drain_queued_jobs(plugin)
        except Exception:  # the loop must never die — a bad pass still yields the next one
            logger.exception("%s async worker: drain pass failed", PLUGIN_NAME)
        time.sleep(POLL_INTERVAL_SECONDS)


def _drain_queued_jobs(plugin: SalesforcePlugin) -> None:
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
    plugin: SalesforcePlugin, async_job_manager: Any, job_id: str, action_name: str,
) -> None:
    """Run one job's real `sf` CLI I/O and complete it. Runs on the ONE worker thread only."""
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
    action_fn, gate_factory = ACTION_HANDLERS[action_name]
    if gate_factory is not None:
        gate = gate_factory(plugin)
        outcome = plugin._run(lambda executor: action_fn(executor, params, gate), action_name)
    else:
        outcome = plugin._run(lambda executor: action_fn(executor, params), action_name)
    if outcome.get("action_status") == "completed":
        async_job_manager.update_job(job_id, {"status": "completed", "result": outcome.get("data", {})})
    else:
        async_job_manager.update_job(job_id, {"status": "error", "error": outcome.get("error", {})})
