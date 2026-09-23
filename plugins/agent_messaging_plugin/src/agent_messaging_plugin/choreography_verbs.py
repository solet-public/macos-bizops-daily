"""maintenance-verbs M1 (workbench
2026-08-09_maintenance_verbs_m0_design_mverbs-impl.md §2.1/§2.2) —
`rotate_session` / `restart_session` as deferred-completion verbs, per the
D0.3 doctrine. Also houses M2.2's `generate_curation_report` dispatch
(the 2026-08-10 maintenance-verbs M2 memory-curation charter draft)
— a third async job sharing this SAME `_create_job` infrastructure and the
SAME single choreography worker thread, deliberately not duplicated into a
second dispatch helper; the ranking logic it dispatches lives in
`memory_curation_verbs.py`, kept separate because it has no `AsyncJobManager`/
`state` awareness of its own.
(`workbench/2026-08-09_sync_verb_d03_deferred_completion_doctrine_syncverb-doctrine.md`)
and the architect-pass carry-over constraints (coordinator seat, 2026-08-09):

1. **Dispatch-time flow/session validation is REQUIRED.**
   ``AsyncJobManager.create_job`` validates only ``notes`` — nothing
   guarantees ``job_metadata`` carries usable ``flow_id``/``session_id``. This
   module's dispatch functions extract and validate both from the caller's
   own ``state`` dict BEFORE calling ``create_job``, mirroring
   ``comfyui_image_generation_plugin.param_validation.validate_state_context``
   — a missing flow context is a loud dispatch-time refusal (:class:`VerbError`),
   never a job created with a hole in it.
2. **Single-worker execution.** This module holds no execution-context
   opinion itself (that lives in the plugin's own worker-thread wiring) but
   the choreography functions here are written assuming exactly ONE call at
   a time processes any given job — no internal locking, no concurrency
   primitives, because none should be needed under that constraint. If a
   second worker is ever added, this module's functions must be revisited.

No ``completion_handlers`` are configured on these jobs (deliberately) — a
caller learns the outcome via ``check_choreography_job_status`` (the
poll-based caller-side answer, mirroring ``check_generation_status``), not
via an automatic flow continuation. ``AsyncJobManager.update_job`` on a
terminal status still calls ``_resolve_job_token`` unconditionally, but a job
with no ``flow_token_id`` (these jobs never set one) hits its documented
no-op branch ("Job not linked to a token - this is valid for jobs created
outside action context") — verified at source, not assumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .memory_curation_verbs import ACTION_GENERATE_CURATION_REPORT
from .session_lifecycle_verbs import VerbError

if TYPE_CHECKING:
    from ananta.core.state.async_job_manager import AsyncJobManager

PROVIDER_PLUGIN_NAME = "agent_messaging_plugin"
ACTION_ROTATE_SESSION = "rotate_session"
ACTION_RESTART_SESSION = "restart_session"

JOB_STATUS_QUEUED = "queued"
JOB_STATUS_PROCESSING = "processing"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_ERROR = "error"

# Every leg name a job's progress payload may report — one update_status call
# per leg, per the D0.3-ratified choreography shape. Kept here (not
# hand-typed at each call site) so the worker and any future status-reading
# caller agree on the vocabulary.
ROTATE_LEGS = (
    "resolve_ledger_row", "durable_pickup_dispatch", "clear_session",
    "drive_session", "verify",
)
RESTART_LEGS = (
    "capture_old_row", "terminate_session", "spawn_session",
    "role_reclaim_drive", "verify",
)


def validate_flow_session_context(state: dict[str, Any]) -> tuple[str, str]:
    """Extract + validate ``(session_id, flow_id)`` from a platform_process
    handler's ``state`` dict. Raises :class:`VerbError` (``missing_flow_context``)
    on either being absent — the architect-pass's binding requirement, so a
    job is never created with a hole a background completion could later
    strand on."""
    session_id = state.get("session_id")
    flow_id = state.get("flow_id")
    if not isinstance(session_id, str) or not session_id:
        raise VerbError(
            "missing_flow_context",
            "session_id missing from state context — refusing to dispatch a "
            "choreography job with no flow context to strand.",
        )
    if not isinstance(flow_id, str) or not flow_id:
        raise VerbError(
            "missing_flow_context",
            "flow_id missing from state context — refusing to dispatch a "
            "choreography job with no flow context to strand.",
        )
    return session_id, flow_id


def _error_message(result: dict[str, Any]) -> str:
    """The ``error.message`` field from an AsyncJobManager result envelope,
    or a stringified fallback — shared by every call site here that surfaces
    a non-completed result as a :class:`VerbError` message."""
    error_info = result.get("error", {})
    if isinstance(error_info, dict):
        return str(error_info.get("message", "unknown"))
    return str(error_info)


def _create_job(
    async_job_manager: AsyncJobManager, *, action_name: str, notes: str,
    request_data: dict[str, Any], session_id: str, flow_id: str,
) -> dict[str, Any]:
    """Shared ms-scale dispatch: no I/O, no driver-channel call — a single
    ``AsyncJobManager.create_job`` write and return. Raises :class:`VerbError`
    (``job_creation_failed``) on a non-completed result, mirroring
    ``ComfyUIJobManager.create_job``'s own fail-loud contract."""
    request_data = {**request_data, "notes": notes}
    result = async_job_manager.create_job(
        plugin_name=PROVIDER_PLUGIN_NAME,
        action_name=action_name,
        request_data=request_data,
        description=notes,
        job_metadata={"session_id": session_id, "flow_id": flow_id},
    )
    if result.get("action_status") != "completed":
        raise VerbError(
            "job_creation_failed",
            f"{action_name} job creation failed: {_error_message(result)}",
        )
    data = result.get("data", {})
    job_id = data.get("job_id") if isinstance(data, dict) else None
    if not isinstance(job_id, str) or not job_id:
        raise VerbError("job_creation_failed", f"{action_name} job creation returned no job_id.")
    return {"job_id": job_id, "status": JOB_STATUS_QUEUED}


@dataclass(frozen=True, slots=True)
class RotateSessionDispatchRequest:
    agent_instance_id: str
    role_name: str
    pickup_text: str
    park_first: bool = False


def dispatch_rotate_session(
    async_job_manager: AsyncJobManager, req: RotateSessionDispatchRequest, state: dict[str, Any],
) -> dict[str, Any]:
    """§2.1 ``rotate_session`` dispatch. Validates required fields + flow
    context, then creates a queued job — no driver-channel call happens on
    this path. ``role_name`` is the durable role this ledger row currently
    holds (the caller's own — ``managed_session`` carries no role column of
    its own, so this cannot be resolved from the ledger row alone; the joseki
    card's own step 1 already has the caller resolve it via
    ``list_sessions``/its own dispatch context before calling this verb).
    Errors: ``missing_argument``, ``missing_flow_context``, ``job_creation_failed``."""
    if not req.agent_instance_id.strip():
        raise VerbError(
            "missing_argument", "rotate_session requires a non-empty agent_instance_id.",
        )
    if not req.role_name.strip():
        raise VerbError("missing_argument", "rotate_session requires a non-empty role_name.")
    if not req.pickup_text.strip():
        raise VerbError("missing_argument", "rotate_session requires a non-empty pickup_text.")
    session_id, flow_id = validate_flow_session_context(state)
    return _create_job(
        async_job_manager,
        action_name=ACTION_ROTATE_SESSION,
        notes=f"rotate_session: {req.agent_instance_id}",
        request_data={
            "agent_instance_id": req.agent_instance_id,
            "role_name": req.role_name,
            "pickup_text": req.pickup_text,
            "park_first": req.park_first,
        },
        session_id=session_id, flow_id=flow_id,
    )


@dataclass(frozen=True, slots=True)
class RestartSessionDispatchRequest:
    agent_instance_id: str
    role_name: str
    role_class: str
    grace_seconds: int = 30


def dispatch_restart_session(
    async_job_manager: AsyncJobManager, req: RestartSessionDispatchRequest, state: dict[str, Any],
) -> dict[str, Any]:
    """§2.2 ``restart_session`` dispatch. Validates required fields + flow
    context, then creates a queued job — no ``terminate_session``/
    ``spawn_session`` call happens on this path. ``role_class`` is required
    from the caller because ``managed_session`` (what the worker reads back
    for the other spawn fields) has no ``role_class`` column of its own —
    verified at source; the caller already knows it from having spawned or
    managed this session originally, so it is not worth a second lookup
    against the ``role`` table. Errors: ``missing_argument``,
    ``missing_flow_context``, ``job_creation_failed``."""
    if not req.agent_instance_id.strip():
        raise VerbError(
            "missing_argument", "restart_session requires a non-empty agent_instance_id.",
        )
    if not req.role_name.strip():
        raise VerbError("missing_argument", "restart_session requires a non-empty role_name.")
    if not req.role_class.strip():
        raise VerbError("missing_argument", "restart_session requires a non-empty role_class.")
    session_id, flow_id = validate_flow_session_context(state)
    return _create_job(
        async_job_manager,
        action_name=ACTION_RESTART_SESSION,
        notes=f"restart_session: {req.agent_instance_id} -> role {req.role_name}",
        request_data={
            "agent_instance_id": req.agent_instance_id,
            "role_name": req.role_name,
            "role_class": req.role_class,
            "grace_seconds": req.grace_seconds,
        },
        session_id=session_id, flow_id=flow_id,
    )


@dataclass(frozen=True, slots=True)
class GenerateCurationReportDispatchRequest:
    head_lines: tuple[str, ...]
    bottom_n: int = 10
    byte_budget: int = 17_000
    line_budget: int = 132


def dispatch_generate_curation_report(
    async_job_manager: AsyncJobManager, req: GenerateCurationReportDispatchRequest,
    state: dict[str, Any],
) -> dict[str, Any]:
    """M2.2 ``generate_curation_report`` dispatch. ``head_lines`` is the
    CALLER's own already-split current head (``index_render.split_head``'s
    output, one line per element) — this plugin cannot import that module
    itself (it lives in the local Claude Code CLI's `.claude/hooks/`
    filesystem tree, a different process/package entirely from this
    platform plugin), so the caller does the splitting locally and passes
    the lines across. Errors: ``missing_argument``, ``missing_flow_context``,
    ``job_creation_failed``."""
    if not req.head_lines:
        raise VerbError(
            "missing_argument", "generate_curation_report requires a non-empty head_lines list.",
        )
    session_id, flow_id = validate_flow_session_context(state)
    return _create_job(
        async_job_manager,
        action_name=ACTION_GENERATE_CURATION_REPORT,
        notes=f"generate_curation_report: {len(req.head_lines)} head line(s)",
        request_data={
            "head_lines": list(req.head_lines),
            "bottom_n": req.bottom_n,
            "byte_budget": req.byte_budget,
            "line_budget": req.line_budget,
        },
        session_id=session_id, flow_id=flow_id,
    )


def _extract_job_row(job_result: dict[str, Any]) -> dict[str, Any]:
    """The nested ``data.job`` row from an ``AsyncJobManager.get_job`` result,
    or ``{}`` on any unexpected shape — split out of
    :func:`check_choreography_job_status` to keep it a straight-line
    dispatcher (radon cc)."""
    data = job_result.get("data", {})
    job = data.get("job") if isinstance(data, dict) else None
    return job if isinstance(job, dict) else {}


def _attach_terminal_payload(
    async_job_manager: AsyncJobManager, envelope: dict[str, Any], job_id: str, status: str,
) -> None:
    """Mutates ``envelope`` in place with the result/error payload for a
    terminal job — split out of :func:`check_choreography_job_status` to
    keep it a straight-line dispatcher (radon cc). A no-op for a non-terminal
    status or an unreadable payload (the status/progress fields alone are
    still a valid, honest answer)."""
    if status not in (JOB_STATUS_COMPLETED, JOB_STATUS_ERROR):
        return
    payload_type = "result" if status == JOB_STATUS_COMPLETED else "error"
    payload_result = async_job_manager.get_job_payload(job_id, payload_type)
    if payload_result.get("action_status") != "completed":
        return
    payload_data = payload_result.get("data", {})
    envelope[payload_type] = payload_data.get("payload") if isinstance(payload_data, dict) else None


def check_choreography_job_status(
    async_job_manager: AsyncJobManager, job_id: str,
) -> dict[str, Any]:
    """§2.1/§2.2 caller-side polling verb (the ``check_generation_status``
    precedent) — the sole way a direct caller learns a choreography job's
    outcome, since these jobs configure no ``completion_handlers``. Reads the
    job ledger row; on a terminal status also reads back the result/error
    payload written by the worker. Errors: ``missing_argument``, ``job_not_found``."""
    if not job_id.strip():
        raise VerbError(
            "missing_argument", "check_choreography_job_status requires a non-empty job_id.",
        )
    job_result = async_job_manager.get_job(job_id)
    if job_result.get("action_status") != "completed":
        raise VerbError(
            "job_not_found", f"check_choreography_job_status: {_error_message(job_result)}",
        )
    job = _extract_job_row(job_result)
    status = str(job.get("status") or "")
    envelope: dict[str, Any] = {
        "job_id": job_id,
        "status": status,
        "progress_percent": job.get("progress_percent"),
        # Always present, even when unset: return_value_schema declares both
        # as properties, and ExecutionContext.store_result (core platform)
        # requires every declared property to be a present key regardless of
        # the schema's own per-property `required` flag — an absent key, not
        # just a falsy value, raises PlaceholderResolutionError. Never omit
        # either key, only ever narrow its value.
        "result": None,
        "error": None,
    }
    _attach_terminal_payload(async_job_manager, envelope, job_id, status)
    return envelope


__all__ = [
    "ACTION_GENERATE_CURATION_REPORT",
    "ACTION_RESTART_SESSION",
    "ACTION_ROTATE_SESSION",
    "GenerateCurationReportDispatchRequest",
    "JOB_STATUS_COMPLETED",
    "JOB_STATUS_ERROR",
    "JOB_STATUS_PROCESSING",
    "JOB_STATUS_QUEUED",
    "PROVIDER_PLUGIN_NAME",
    "RESTART_LEGS",
    "ROTATE_LEGS",
    "RestartSessionDispatchRequest",
    "RotateSessionDispatchRequest",
    "check_choreography_job_status",
    "dispatch_generate_curation_report",
    "dispatch_restart_session",
    "dispatch_rotate_session",
    "validate_flow_session_context",
]
