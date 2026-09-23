"""D0.3 deferred-completion machinery for jira_plugin.

Mirrors external_postgres_plugin's PluginBase lazy-worker shape (the
doctrine's traced worked example,
workbench/2026-08-09_sync_verb_d03_deferred_completion_doctrine_syncverb-doctrine.md
§5/§7) — ms-scale dispatch (the plugin's ``@platform_process`` handlers)
validates params, creates a job, and returns a ``{"job_id", "status":
"queued"}`` envelope immediately; a single lazily-started background daemon
thread does the real Jira client I/O off the action-dispatch path and
completes the job via ``AsyncJobManager.update_job``, which — via the job's
``completion_handlers`` metadata — submits the follow-up action into the
originating flow. No new primitive invented.

ONE worker thread, never more, by design: ``FlowManager._sequence_cache`` is
an unlocked ``dict`` (doctrine §2) — two callers submitting completion
actions into the SAME flow concurrently can lose an update. comfyui and
external_postgres_plugin stay safe because each runs exactly one worker
thread; this plugin does the same rather than firing a thread per dispatch.

Migrated action-by-action across per-family batches (D0.2 appendix rows,
workbench/2026-08-08_sync_verb_d02_blocking_inventory_syncverb-inventory.md:623-634)
— ACTION_HANDLERS grows as each batch's dispatch handler is flipped onto
``JiraPlugin._dispatch_async``; it never carries an entry for a verb whose
dispatch handler still runs synchronously.

The AsyncJobManager reference is acquired LAZILY (mirrors comfyui's
``_try_acquire_job_manager`` / external_postgres_plugin's async_jobs.py),
not at ``prepare_for_readiness`` — plugin boot order does not guarantee
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

from . import comment_actions, completion_templates, issue_actions
from .constants import ERROR_API_ERROR, PLUGIN_NAME

if TYPE_CHECKING:
    from .plugin import JiraPlugin

logger = logging.getLogger(PLUGIN_NAME)

POLL_INTERVAL_SECONDS = 2.0
_JOB_LIST_LIMIT = 10

def _test_connection(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Fetch the authenticated account to confirm connectivity + credentials.

    Moved here (from plugin.py, where it lived before the D0.3 migration)
    since the worker is now its only caller — keeping it in plugin.py would
    need a lazy in-function import to dodge the circular top-level import
    (plugin.py already imports this module for ``_dispatch_async``).
    """
    myself = client.myself()
    account_id = myself.get("accountId")
    display_name = myself.get("displayName")
    return {
        "ok": True,
        "base_url": client.server_url,
        "account_id": account_id if isinstance(account_id, str) else "",
        "display_name": display_name if isinstance(display_name, str) else "",
    }


# action_name -> the issue_actions/comment_actions callable, signature
# (client, params) -> dict. Grows one entry per migrated verb; see the
# module docstring.
ActionHandler = Callable[[Any, dict[str, Any]], dict[str, Any]]
ACTION_HANDLERS: dict[str, ActionHandler] = {
    "jql_search": issue_actions.jql_search,
    "get_issue": issue_actions.get_issue,
    "create_issue": issue_actions.create_issue,
    "update_issue": issue_actions.update_issue,
    "delete_issue": issue_actions.delete_issue,
    "add_comment": comment_actions.add_comment,
    "list_comments": comment_actions.list_comments,
    "list_transitions": comment_actions.list_transitions,
    "transition_issue": comment_actions.transition_issue,
    "test_connection": _test_connection,
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


def _acquire_async_job_manager(plugin: JiraPlugin) -> Any:
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


def ensure_worker_started(plugin: JiraPlugin) -> None:
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
    plugin: JiraPlugin,
    *,
    action_name: str,
    params: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    """Ms-scale dispatch: validate, write the job row, start the worker, return.

    No Jira client is built and no HTTP call happens here — that is entirely
    the background worker's job, off this call's return path.
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


def _worker_loop(plugin: JiraPlugin) -> None:
    while True:
        try:
            _drain_queued_jobs(plugin)
        except Exception:  # the loop must never die — a bad pass still yields the next one
            logger.exception("%s async worker: drain pass failed", PLUGIN_NAME)
        time.sleep(POLL_INTERVAL_SECONDS)


def _drain_queued_jobs(plugin: JiraPlugin) -> None:
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
    plugin: JiraPlugin, async_job_manager: Any, job_id: str, action_name: str,
) -> None:
    """Run one job's real Jira I/O and complete it. Runs on the ONE worker thread only."""
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
    action_fn = ACTION_HANDLERS[action_name]
    outcome = plugin._run(lambda: action_fn(plugin._require_client(), params), action_name)
    if outcome.get("action_status") == "completed":
        async_job_manager.update_job(job_id, {"status": "completed", "result": outcome.get("data", {})})
    else:
        async_job_manager.update_job(job_id, {"status": "error", "error": outcome.get("error", {})})
