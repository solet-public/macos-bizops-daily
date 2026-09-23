"""Marketo plugin entry point — lead CRUD/query, campaign trigger, list membership.

Headless OAuth 2.0 client-credentials auth (chain-consumed from the
``marketo_instance`` address-book entry) against a Marketo Engage instance —
no browser flow, no callback server, the same auth-simplicity class as
zuora_plugin. Unlike Zuora, this connector DOES expose destructive verbs
(``delete_leads``, ``merge_leads``) — Marketo's REST API supports lead
deletion and merging natively and lead records are not treated as immutable
audit trail the way Zuora's billing records are; see the knowledge-base
overview for the explicit read/write posture note.

Verbs (all EDGE):
  - describe_lead_fields                                    — read, ASYNC (D0.3 job dispatch — see below)
  - get_leads                                                — read, ASYNC
  - get_api_usage                                            — read, ASYNC (current-day API consumption)
  - list_activity_types                                      — read, ASYNC (per-instance activity metadata)
  - get_activities                                           — read, ASYNC (activity log; verifies what a write CAUSED, after the fact)
  - create_or_update_leads                                   — write
  - delete_leads                                             — write (destructive)
  - merge_leads                                              — write (destructive, irreversible)
  - list_campaigns                                           — read, ASYNC
  - trigger_campaign                                         — write (side-effecting; the flow it runs is NOT readable first — see the KB's campaign flow inspection article)
  - list_static_lists                                        — read, ASYNC
  - add_leads_to_list / remove_leads_from_list               — write
  - test_connection                                          — diagnostic (credentials reachable)
  - check_setup                                              — diagnostic (which READ capabilities the Role grants; PARTIAL, see docstring)
  - check_marketo_job_status                                 — diagnostic (poll an ASYNC verb's dispatched job)

D0.3 sync-verb migration (2026-08-09): the seven ASYNC verbs above dispatch an
AsyncJobManager job and return {job_id, status} in milliseconds instead of
running their vendor fetch inline on the dispatch path — batch 1 (get_leads,
get_activities, list_campaigns, list_static_lists) migrated the internally-
paginating, multi-vendor-round-trip verbs first (the worst loop-hold shape in
the D0.2 blocking-verb corpus); batch 2 (describe_lead_fields, get_api_usage,
list_activity_types) migrated the single-vendor-call reads the same way, minus
the pagination. A background worker thread (ServicePlugin.start_services) does
the real vendor I/O off the dispatch path for all seven; completion routes
through AsyncJobManager's completion_handlers exactly as
comfyui_image_generation_plugin's generate_image does (the production exemplar
this migration follows), and check_marketo_job_status is the caller-polling
fallback for direct process_call callers, mirroring comfyui's
check_generation_status. Every other verb below is still unmigrated and still
runs its vendor call inline on the dispatch await via self._run — that
includes create_or_update_leads/delete_leads/merge_leads/add_leads_to_list/
remove_leads_from_list/trigger_campaign/test_connection/check_setup, all named
BLOCKING-SUSPECTED or cannot-determine rows in the D0.2 inventory and slated
for a later batch of this same migration, not this one.

Security posture: every verb is directly process_call-able like any other
process (matches the 2026-07-15 operator ruling retiring the RATIFY-3
process_export deny); auth/rate-limit error messages are GENERIC fixed
strings — never the raw response, which could leak instance-specific
diagnostic detail; the plugin reaches ONLY the address-book-resolved
instance (no base_url param on any verb). No SQL-shaped strings anywhere
(pure REST), so the SQL-lockdown gate is silent for this plugin — no
allowlist entry needed.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from ananta.core.actions.action_metadata import (
    ContextHandling,
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.config.config_provider import ConfigProvider
from ananta.core.domain.enums import ActionStatus, ProcessorPolicyCategory
from ananta.core.domain.types import ActionResult
from ananta.core.plugins.decorators import service_lifecycle
from ananta.core.plugins.plugin_base import ServicePlugin
from ananta.interfaces.edge_process_provider import (
    EdgeProcessDefinition,
    EdgeProcessProvider,
)

from . import completion_templates, export_containment, marketing_actions
from .app_config import AppConfigError, AppConfigLoader
from .constants import (
    CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
    CONFIG_KEY_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_ROW_LIMIT,
    ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
    ERROR_API_ERROR,
    ERROR_INVALID_PARAMS,
    ERROR_JOB_NOT_FOUND,
    ERROR_NOT_CONFIGURED,
    MARKETO_LIST_PAGE_ROW_CAP,
    MARKETO_LIST_ROW_LIMIT_CAP,
    PARAM_ACKNOWLEDGE_OVERRIDE,
    PARAM_JOB_ID,
    PARAM_ROW_LIMIT,
    PLUGIN_NAME,
    RESULT_TYPE_ADD_LEADS_TO_LIST,
    RESULT_TYPE_CHECK_MARKETO_JOB_STATUS,
    RESULT_TYPE_CHECK_SETUP,
    RESULT_TYPE_CREATE_OR_UPDATE_LEADS,
    RESULT_TYPE_DELETE_LEADS,
    RESULT_TYPE_DESCRIBE_LEAD_FIELDS,
    RESULT_TYPE_GET_ACTIVITIES,
    RESULT_TYPE_GET_API_USAGE,
    RESULT_TYPE_GET_LEADS,
    RESULT_TYPE_LIST_ACTIVITY_TYPES,
    RESULT_TYPE_LIST_CAMPAIGNS,
    RESULT_TYPE_LIST_STATIC_LISTS,
    RESULT_TYPE_MERGE_LEADS,
    RESULT_TYPE_REMOVE_LEADS_FROM_LIST,
    RESULT_TYPE_TEST_CONNECTION,
    RESULT_TYPE_TRIGGER_CAMPAIGN,
)
from .errors import (
    MarketoEnvelopeError,
    MarketoServiceError,
    MarketoTransportError,
    classify_marketo_envelope,
)
from .http_client import MarketoAuthError, MarketoClient

if TYPE_CHECKING:
    from ananta.core.state.async_job_manager import AsyncJobManager

# Background worker's handler per async-dispatched action name — the actual
# vendor I/O, run off the dispatch path (D0.3 deferred-completion shape).
_JOB_EXECUTORS: dict[str, Callable[[MarketoClient, dict[str, Any]], dict[str, Any]]] = {
    "get_leads": marketing_actions.execute_get_leads,
    "get_activities": marketing_actions.execute_get_activities,
    "list_campaigns": marketing_actions.execute_list_campaigns,
    "list_static_lists": marketing_actions.execute_list_static_lists,
    "describe_lead_fields": marketing_actions.execute_describe_lead_fields,
    "list_activity_types": marketing_actions.execute_list_activity_types,
    "get_api_usage": marketing_actions.execute_get_api_usage,
}

_JOB_PROVIDER_PREFIX = f"{PLUGIN_NAME}."


def _eligible_marketo_job(
    job: Any,
) -> tuple[str, str, Callable[[MarketoClient, dict[str, Any]], dict[str, Any]]] | None:
    """Filter one raw queued-job record down to (job_id, action_name, executor).

    Returns None for anything not a marketo_plugin async-dispatched job this
    worker knows how to execute — a different plugin's job, an unmigrated
    marketo_plugin verb, or a malformed record.
    """
    if not isinstance(job, dict):
        return None
    provider_name = job.get("provider_name")
    if not isinstance(provider_name, str) or not provider_name.startswith(_JOB_PROVIDER_PREFIX):
        return None
    action_name = provider_name[len(_JOB_PROVIDER_PREFIX):]
    executor = _JOB_EXECUTORS.get(action_name)
    job_id = job.get("id")
    if executor is None or not isinstance(job_id, str):
        return None
    return job_id, action_name, executor


class MarketoPlugin(ServicePlugin, EdgeProcessProvider):
    """Marketo Engage connector (lead CRUD/query, campaigns, list membership) plugin.

    D0.3 sync-verb migration (2026-08-09): a ``ServicePlugin`` with a
    persistent background worker thread, modeled directly on
    comfyui_image_generation_plugin's (the one production D0.3 exemplar) —
    single worker thread, one job processed at a time, which is what keeps
    the platform's ``FlowManager._sequence_cache`` concurrency caveat from
    applying here (see D0.3 doctrine §2). The worker processes
    ``get_leads``/``get_activities``/``list_campaigns``/``list_static_lists``
    jobs queued by their dispatch handlers below; every other verb is still
    unmigrated and still runs inline on the dispatch path via ``self._run``.
    """

    name: str = PLUGIN_NAME

    # Worker configuration — bursty connector, not a continuously-busy one;
    # matches comfyui_image_generation_plugin's poll interval.
    WORKER_POLL_INTERVAL: float = 2.0

    def __init__(self) -> None:
        super().__init__()
        self.logger: logging.Logger | None = None
        self._address_book_service: Any | None = None
        self._app_config_loader: AppConfigLoader | None = None
        self._client: MarketoClient | None = None
        # D0.3 section 7 named constraint: lazily building self._client is
        # itself a check-then-set that the migration's background worker now
        # races against the (still-synchronous) main dispatch thread for.
        self._client_lock = threading.Lock()

        # Worker thread management (ServicePlugin pattern, comfyui-modeled).
        self._stop_event: threading.Event = threading.Event()
        self._worker_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # VaultKeysProvider — no plugin-owned vault keys
    # ------------------------------------------------------------------

    def get_required_vault_keys(self) -> list[str]:
        """No vault keys are required at readiness.

        The client_secret is chain-consumed through the ``marketo_instance``
        address-book entry — never a direct vault verb under this plugin's
        identity — so it is declared nowhere here. The re-minted bearer
        token lives only in process memory and is never vaulted.
        """
        return []

    def get_declared_vault_keys(self) -> list[str]:
        """No scoped vault keys are read or written directly by this plugin."""
        return []

    # ------------------------------------------------------------------
    # PluginBase lifecycle
    # ------------------------------------------------------------------

    def initialize(self, config: dict[str, object]) -> None:
        """Bind config_provider so yaml defaults + operator overrides take effect.

        Without this override the base ``initialize`` is a no-op and
        ``config_provider`` stays None forever — the ``or {}`` reads below then
        silently run on empty config (defect class found live on the snowflake
        sibling, 2026-07-16).
        """
        self.config_provider = ConfigProvider(self.name, config)

    def prepare_for_readiness(self) -> None:
        if self.orchestrator_ref is None:
            raise RuntimeError(f"{self.name}: orchestrator_ref not injected")
        self.logger = logging.getLogger(self.name)
        self._address_book_service = self.orchestrator_ref.get_service("address_book_service")
        if self._address_book_service is None:
            raise RuntimeError(
                f"{ERROR_ADDRESS_BOOK_NOT_AVAILABLE}: {self.name} requires "
                "address_book_service to resolve the marketo_instance credentials"
            )
        self._app_config_loader = AppConfigLoader(self._address_book_service)
        self.set_ready()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _request_timeout_seconds(self) -> float:
        config = self.config_provider or {}
        raw = config.get(CONFIG_KEY_REQUEST_TIMEOUT_SECONDS, DEFAULT_REQUEST_TIMEOUT_SECONDS)
        return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else DEFAULT_REQUEST_TIMEOUT_SECONDS

    def _require_client(self) -> MarketoClient:
        """Lazily build + cache the Marketo client from the resolved instance config."""
        if self._app_config_loader is None:
            raise RuntimeError(ERROR_ADDRESS_BOOK_NOT_AVAILABLE)
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    config = self._app_config_loader.load()
                    self._client = MarketoClient(config, timeout_seconds=self._request_timeout_seconds())
        return self._client

    def _require_async_job_manager(self) -> AsyncJobManager:
        """Fetch the platform's AsyncJobManager off the orchestrator reference.

        Not a setter-injected field — ``OrchestratorProtocol.async_job_manager``
        is a property every plugin can read directly once ``orchestrator_ref``
        is set (D0.3 doctrine §2: constructed once as a platform-wide
        singleton at boot, same object graph every plugin shares).
        """
        if self.orchestrator_ref is None:
            raise RuntimeError(f"{self.name}: orchestrator_ref not injected")
        manager = getattr(self.orchestrator_ref, "async_job_manager", None)
        if manager is None:
            raise RuntimeError(f"{self.name}: AsyncJobManager not yet available")
        return manager  # type: ignore[return-value]

    def _require_state_context(self, state: dict[str, Any]) -> tuple[str, str]:
        """Validate session_id/flow_id are present and usable BEFORE create_job.

        D0.3 doctrine §1 required step, not a convention — AsyncJobManager.create_job
        itself validates only ``notes``; this dispatch-time check is what
        prevents a job whose completion can never route (comfyui's
        ``param_validation.validate_state_context`` is the production instance
        of this same step).
        """
        session_id = state.get("session_id")
        flow_id = state.get("flow_id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id missing from state context")
        if not isinstance(flow_id, str) or not flow_id:
            raise ValueError("flow_id missing from state context")
        return session_id, flow_id

    def _success(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": data,
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def _error(self, code: str, message: str) -> dict[str, Any]:
        return {
            "action_status": ActionStatus.ERROR.value,
            "data": {},
            "actions": [],
            "error": {"code": code, "message": message},
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def _service_action_result(self, data: dict[str, object]) -> ActionResult:
        """A typed-as-ActionResult twin of _success, for start_services/stop_services only.

        ServicePlugin declares those two ``-> ActionResult`` (a TypedDict);
        every other verb here declares ``-> dict[str, Any]`` and uses
        ``_success``/``_error`` instead — both are correct for their own
        caller's declared return type, this one exists only because the two
        typed shapes are not interchangeable under pyright strict.
        """
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": data,
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def _export_path_gate(self, output_tsv_path: str) -> str:
        """Admit an export path via workspace-root containment; return the realpath.

        Binds the operator's ``export_allowed_roots`` config (yaml default
        ``[]`` = refuse-all; no hardcoded callsite default per authoring trap
        #10) to the own-copy containment gate. A malformed config value is a
        loud config fault, never a silent admit-all or refuse-all.
        """
        config = self.config_provider or {}
        raw_roots = config.get(CONFIG_KEY_EXPORT_ALLOWED_ROOTS)
        roots: list[str] = []
        if raw_roots is not None:
            if not isinstance(raw_roots, list) or not all(
                isinstance(entry, str) for entry in raw_roots
            ):
                raise MarketoServiceError(
                    ERROR_NOT_CONFIGURED,
                    f"{CONFIG_KEY_EXPORT_ALLOWED_ROOTS} must be a list of directory "
                    "path strings",
                )
            roots = list(raw_roots)
        return export_containment.assert_export_path_allowed(
            output_tsv_path,
            roots,
            config_key=CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
            plugin_name=self.name,
        )

    def _run(self, produce: Callable[[MarketoClient], dict[str, Any]], endpoint_name: str) -> dict[str, Any]:
        """Shared error-classification path for every Marketo verb."""
        try:
            client = self._require_client()
            data = produce(client)
        except ValueError as exc:
            return self._error(ERROR_INVALID_PARAMS, str(exc))
        except AppConfigError as exc:
            return self._error(ERROR_NOT_CONFIGURED, str(exc))
        except (MarketoServiceError, export_containment.ExportPathRefusedError) as exc:
            return self._error(exc.code, str(exc))
        except MarketoAuthError:
            return self._error("marketo.auth_failed", "Marketo OAuth token request failed.")
        except MarketoEnvelopeError as exc:
            code, message = classify_marketo_envelope(exc)
            return self._error(code, message)
        except MarketoTransportError:
            return self._error(ERROR_API_ERROR, "Marketo API call failed (transport fault).")
        except Exception as exc:  # noqa: BLE001 — any other transport fault -> generic
            if self.logger:
                self.logger.warning("%s: unexpected fault (%s)", endpoint_name, type(exc).__name__)
            return self._error(ERROR_API_ERROR, "Marketo API call failed.")
        if self.logger:
            self.logger.debug("%s: success", endpoint_name)
        return self._success(data)

    def _dispatch_async_job(
        self,
        *,
        action_name: str,
        verb_label: str,
        notes: str,
        prepare: Callable[[], dict[str, Any]],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Ms-scale D0.3 dispatch: validate, create a job, return {job_id, status}.

        No vendor call happens here — ``prepare`` does param validation +
        export-path resolution only (see marketing_actions.py's
        ``prepare_<verb>`` functions); the background worker calls the
        matching ``execute_<verb>`` with the resolved request later.
        """
        try:
            session_id, flow_id = self._require_state_context(state)
            request_data = prepare()
            manager = self._require_async_job_manager()
        except (ValueError, AppConfigError, MarketoServiceError, export_containment.ExportPathRefusedError, RuntimeError) as exc:
            code, message = self._classify_exception(exc, action_name)
            return self._error(code, message)
        request_data["notes"] = notes
        job_metadata = completion_templates.build_job_metadata(session_id, flow_id, verb_label)
        result = manager.create_job(
            plugin_name=PLUGIN_NAME,
            action_name=action_name,
            request_data=request_data,
            description=notes,
            job_metadata=job_metadata,
        )
        return self._job_creation_response(result, action_name)

    def _job_creation_response(self, result: dict[str, Any], action_name: str) -> dict[str, Any]:
        """Interpret AsyncJobManager.create_job's result into a dispatch response."""
        if result.get("action_status") != "completed":
            error_info = result.get("error", {})
            message = error_info.get("message", "unknown") if isinstance(error_info, dict) else str(error_info)
            return self._error(ERROR_API_ERROR, f"Failed to create Marketo async job: {message}")
        data = result.get("data", {})
        job_id = data.get("job_id") if isinstance(data, dict) else None
        if not isinstance(job_id, str) or not job_id:
            return self._error(ERROR_API_ERROR, "Marketo async job creation returned no job_id")
        if self.logger:
            self.logger.debug("%s: dispatched job %s", action_name, job_id)
        return self._success({"job_id": job_id, "status": "queued"})

    def _classify_exception(self, exc: Exception, endpoint_name: str) -> tuple[str, str]:
        """Shared exception -> (code, message) classification for _run and the worker."""
        if isinstance(exc, ValueError):
            return ERROR_INVALID_PARAMS, str(exc)
        if isinstance(exc, AppConfigError):
            return ERROR_NOT_CONFIGURED, str(exc)
        if isinstance(exc, (MarketoServiceError, export_containment.ExportPathRefusedError)):
            return exc.code, str(exc)
        if isinstance(exc, MarketoAuthError):
            return "marketo.auth_failed", "Marketo OAuth token request failed."
        if isinstance(exc, MarketoEnvelopeError):
            return classify_marketo_envelope(exc)
        if isinstance(exc, MarketoTransportError):
            return ERROR_API_ERROR, "Marketo API call failed (transport fault)."
        # A bare RuntimeError (e.g. _require_client/_require_async_job_manager's
        # "not yet available" faults) preserves its own message rather than
        # falling through to the generic catch-all below — must be checked
        # AFTER MarketoAuthError, which is itself a RuntimeError subclass.
        if isinstance(exc, RuntimeError):
            return ERROR_API_ERROR, str(exc)
        if self.logger:
            self.logger.warning("%s: unexpected fault (%s)", endpoint_name, type(exc).__name__)
        return ERROR_API_ERROR, "Marketo API call failed."

    # ------------------------------------------------------------------
    # Background worker (D0.3 deferred-completion machinery)
    # ------------------------------------------------------------------

    @service_lifecycle(operation="start")
    async def start_services(self) -> ActionResult:
        """Start the background worker that processes queued Marketo jobs."""
        if self._services_started:
            return self._service_action_result({"message": "marketo_plugin worker already running"})
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name=f"{PLUGIN_NAME}-worker",
            daemon=False,
        )
        self._worker_thread.start()
        self._services_started = True
        self._service_started_at = datetime.now(UTC).isoformat()
        if self.logger:
            self.logger.debug("marketo_plugin worker started")
        return self._service_action_result(
            {"message": "marketo_plugin worker started", "started_at": self._service_started_at}
        )

    @service_lifecycle(operation="stop")
    async def stop_services(self) -> ActionResult:
        """Stop the background worker gracefully."""
        if not self._services_started:
            return self._service_action_result({"message": "marketo_plugin worker already stopped"})
        self._stop_event.set()
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=30.0)
            if self._worker_thread.is_alive() and self.logger:
                self.logger.error("marketo_plugin worker thread did not stop within timeout")
        self._worker_thread = None
        self._services_started = False
        self._service_started_at = None
        if self.logger:
            self.logger.debug("marketo_plugin worker stopped")
        return self._service_action_result({"message": "marketo_plugin worker stopped"})

    def _worker_loop(self) -> None:
        """Poll for queued marketo_plugin jobs and process them one at a time.

        Single worker thread, one job at a time — the property D0.3 doctrine
        §2 names as what keeps this migration safe against
        ``FlowManager._sequence_cache``'s lack of internal locking.
        """
        while not self._stop_event.is_set():
            try:
                self._process_pending_jobs()
            except Exception:  # noqa: BLE001 — never let the loop die on one bad cycle
                if self.logger:
                    self.logger.exception("marketo_plugin worker: unexpected fault in poll cycle")
            self._stop_event.wait(self.WORKER_POLL_INTERVAL)

    def _process_pending_jobs(self) -> None:
        if self.orchestrator_ref is None:
            return
        raw_manager = getattr(self.orchestrator_ref, "async_job_manager", None)
        if raw_manager is None:
            return
        manager = cast("AsyncJobManager", raw_manager)
        result = manager.list_jobs(status="queued", limit=10, order_by="created_at ASC")
        if result.get("action_status") != "completed":
            return
        data = result.get("data", {})
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        for job in jobs:
            if self._stop_event.is_set():
                return
            eligible = _eligible_marketo_job(job)
            if eligible is None:
                continue
            job_id, action_name, executor = eligible
            self._process_job(manager, job_id, action_name, executor)

    def _process_job(
        self,
        manager: AsyncJobManager,
        job_id: str,
        action_name: str,
        executor: Callable[[MarketoClient, dict[str, Any]], dict[str, Any]],
    ) -> None:
        manager.update_job(job_id, {"status": "processing"})
        payload_result = manager.get_job_payload(job_id, "request")
        payload_data = payload_result.get("data", {})
        request_data = payload_data.get("payload", {}) if isinstance(payload_data, dict) else {}
        if not isinstance(request_data, dict) or not request_data:
            manager.update_job(
                job_id,
                {"status": "error", "error": {"code": ERROR_API_ERROR, "message": "job request payload missing"}},
            )
            return
        try:
            client = self._require_client()
            job_result = executor(client, request_data)
        except Exception as exc:  # noqa: BLE001 — classified below, never left unhandled
            code, message = self._classify_exception(exc, action_name)
            manager.update_job(job_id, {"status": "error", "error": {"code": code, "message": message}})
            if self.logger:
                self.logger.warning("%s: job %s failed (%s)", action_name, job_id, code)
            return
        manager.update_job(job_id, {"status": "completed", "result": job_result})
        if self.logger:
            self.logger.debug("%s: job %s completed", action_name, job_id)

    # ------------------------------------------------------------------
    # EdgeProcessProvider
    # ------------------------------------------------------------------

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {
            "describe_lead_fields": _edge("describe_lead_fields", RESULT_TYPE_DESCRIBE_LEAD_FIELDS, retryable=True),
            "get_leads": _edge("get_leads", RESULT_TYPE_GET_LEADS, retryable=True),
            "get_api_usage": _edge("get_api_usage", RESULT_TYPE_GET_API_USAGE, retryable=True),
            "list_activity_types": _edge("list_activity_types", RESULT_TYPE_LIST_ACTIVITY_TYPES, retryable=True),
            "get_activities": _edge("get_activities", RESULT_TYPE_GET_ACTIVITIES, retryable=True),
            "create_or_update_leads": _edge("create_or_update_leads", RESULT_TYPE_CREATE_OR_UPDATE_LEADS, retryable=False),
            "delete_leads": _edge("delete_leads", RESULT_TYPE_DELETE_LEADS, retryable=False),
            "merge_leads": _edge("merge_leads", RESULT_TYPE_MERGE_LEADS, retryable=False),
            "list_campaigns": _edge("list_campaigns", RESULT_TYPE_LIST_CAMPAIGNS, retryable=True),
            "trigger_campaign": _edge("trigger_campaign", RESULT_TYPE_TRIGGER_CAMPAIGN, retryable=False),
            "list_static_lists": _edge("list_static_lists", RESULT_TYPE_LIST_STATIC_LISTS, retryable=True),
            "add_leads_to_list": _edge("add_leads_to_list", RESULT_TYPE_ADD_LEADS_TO_LIST, retryable=False),
            "remove_leads_from_list": _edge("remove_leads_from_list", RESULT_TYPE_REMOVE_LEADS_FROM_LIST, retryable=False),
            "test_connection": _edge("test_connection", RESULT_TYPE_TEST_CONNECTION, retryable=True),
            "check_setup": _edge("check_setup", RESULT_TYPE_CHECK_SETUP, retryable=True),
            "check_marketo_job_status": _edge(
                "check_marketo_job_status", RESULT_TYPE_CHECK_MARKETO_JOB_STATUS, retryable=False,
            ),
        }

    # ------------------------------------------------------------------
    # @platform_process implementations
    # ------------------------------------------------------------------

    @platform_process(
        name="describe_lead_fields",
        display_name="Marketo: Describe Lead Fields",
        description=(
            "Dispatch an async job that fetches the full lead field metadata list and the "
            "instance-specific searchable_fields accepted by get_leads.filter_type, and returns "
            "{job_id, status} in milliseconds; poll check_marketo_job_status(job_id) for the "
            "finished result. The finished job ALWAYS carries the field descriptors written to "
            "output_tsv_path, never inline — this is a business-connector record-read verb under "
            "the 07-29 data-export floor even though its content is schema metadata, not customer PII."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ABSOLUTE .tsv destination path, contained under an export_allowed_roots entry.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="A job handle — poll check_marketo_job_status(job_id) for the finished TSV.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Pass to check_marketo_job_status to retrieve the result."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued' — the dispatch never waits for the vendor call."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def describe_lead_fields(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async_job(
            action_name="describe_lead_fields",
            verb_label="lead field metadata export",
            notes="Marketo describe_lead_fields",
            prepare=lambda: marketing_actions.prepare_describe_lead_fields(params, self._export_path_gate),
            state=state,
        )

    @platform_process(
        name="get_api_usage",
        display_name="Marketo: Get Current API Usage",
        description=(
            "Dispatch an async job that reads the configured Marketo subscription's current-day "
            "REST API call total and per-user breakdown, and returns {job_id, status} in "
            "milliseconds; poll check_marketo_job_status(job_id) for the finished result. Use the "
            "finished job's calls_today when checking whether a planned batch fits the operator's "
            "known daily quota."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="A job handle — poll check_marketo_job_status(job_id) for the finished usage data.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Pass to check_marketo_job_status to retrieve the result."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued' — the dispatch never waits for the vendor call."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def get_api_usage(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        return self._dispatch_async_job(
            action_name="get_api_usage",
            verb_label="API usage read",
            notes="Marketo get_api_usage",
            prepare=lambda: marketing_actions.prepare_get_api_usage(params),
            state=state,
        )

    @platform_process(
        name="get_leads",
        display_name="Marketo: Get Leads",
        description=(
            "Dispatch an async job that queries leads by an instance-supported filter_type and up "
            "to 300 filter_values, and returns {job_id, status} in milliseconds — the query itself "
            "runs in the background; poll check_marketo_job_status(job_id) for the finished TSV "
            "handle. Read describe_lead_fields.searchable_fields to discover valid standard and "
            "custom filter types first. The finished job ALWAYS carries the matching leads written "
            f"to output_tsv_path, never inline. Pages internally across Marketo's own "
            f"{MARKETO_LIST_PAGE_ROW_CAP}-per-call ceiling (fixed server-side, no query-side "
            "parameter to raise) up to the effective row limit — this verb never returns a partial "
            f"page for you to continue; defaults to {DEFAULT_ROW_LIMIT} to avoid exhausting vendor "
            "rate limits and disk, and to discourage pulling all records for client-side filtering "
            "a vendor query should do instead. To fetch more, pass "
            f"acknowledge_default_limit_override=true together with an explicit row_limit (up to "
            f"{MARKETO_LIST_ROW_LIMIT_CAP}) — both required together, refused rather than clamped "
            f"above {MARKETO_LIST_ROW_LIMIT_CAP}. Beyond the hard cap there is no resumption: "
            "re-invoke with a narrower filter_values slice (e.g. a 45,000-lead job needs several "
            f"non-overlapping calls, ~{MARKETO_LIST_ROW_LIMIT_CAP // MARKETO_LIST_PAGE_ROW_CAP} "
            "internal vendor calls each) — check get_api_usage first if the daily quota is a "
            "concern for a large pull. Optional fields restricts returned columns — prefer "
            "requesting stable ID and status fields over email, phone, or address fields when the "
            "goal is validation rather than a task that genuinely needs them."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "filter_type": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=(
                    "A field from describe_lead_fields.searchable_fields for "
                    "this Marketo instance."
                ),
            ),
            "filter_values": ParameterMetadata(type=ParameterType.LIST, required=True, description="Up to 300 filter values to match."),
            "fields": ParameterMetadata(type=ParameterType.LIST, required=False, description="Optional list of lead field API names to return."),
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ABSOLUTE .tsv destination path, contained under an export_allowed_roots entry.",
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(
                    f"Must be exactly true, together with row_limit, to fetch more than the "
                    f"default {DEFAULT_ROW_LIMIT} leads."
                ),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(
                    f"Explicit record ceiling, up to {MARKETO_LIST_ROW_LIMIT_CAP}. Only honored "
                    f"together with acknowledge_default_limit_override=true; refused (not clamped) "
                    f"above {MARKETO_LIST_ROW_LIMIT_CAP}."
                ),
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="A job handle — poll check_marketo_job_status(job_id) for the finished TSV.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Pass to check_marketo_job_status to retrieve the result."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued' — the dispatch never waits for the fetch."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def get_leads(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async_job(
            action_name="get_leads",
            verb_label="lead query export",
            notes=f"Marketo get_leads: filter_type={params.get('filter_type')!r}",
            prepare=lambda: marketing_actions.prepare_get_leads(params, self._export_path_gate),
            state=state,
        )

    @platform_process(
        name="list_activity_types",
        display_name="Marketo: List Activity Types",
        description=(
            "Dispatch an async job that lists the configured Marketo instance's activity type ids "
            "and metadata, and returns {job_id, status} in milliseconds; poll "
            "check_marketo_job_status(job_id) for the finished result. Use the finished job's ids as "
            "the mandatory activity_type_ids for get_activities. The finished job ALWAYS carries the "
            "catalog written to output_tsv_path, never inline — this is a business-connector "
            "record-read verb under the 07-29 data-export requirement even though its content is instance "
            "metadata, not customer PII."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ABSOLUTE .tsv destination path, contained under an export_allowed_roots entry.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="A job handle — poll check_marketo_job_status(job_id) for the finished TSV.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Pass to check_marketo_job_status to retrieve the result."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued' — the dispatch never waits for the vendor call."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_activity_types(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        return self._dispatch_async_job(
            action_name="list_activity_types",
            verb_label="activity type catalog export",
            notes="Marketo list_activity_types",
            prepare=lambda: marketing_actions.prepare_list_activity_types(params, self._export_path_gate),
            state=state,
        )

    @platform_process(
        name="get_activities",
        display_name="Marketo: Get Lead Activities",
        description=(
            "Dispatch an async job that reads the Marketo activity log — what leads actually DID, "
            "or had done to them (emails sent/delivered, alerts, campaign requests, data value "
            "changes) — and returns {job_id, status} in milliseconds; poll "
            "check_marketo_job_status(job_id) for the finished TSV handle. Pass since_datetime "
            "(ISO-8601, second-granularity — a fractional-seconds component is floor-truncated "
            "before the paging-token request because Marketo otherwise rewinds that window to "
            "midnight UTC). activity_type_ids is mandatory (max 10); discover valid ids with "
            "list_activity_types. Optional lead_ids (max 30) filter server-side. AFTER-THE-FACT "
            "audit: it reports what a write already caused; it cannot promise that a future "
            "merge/update will stay silent. The finished job ALWAYS carries the records written to "
            f"output_tsv_path, never inline. Pages internally across Marketo's own "
            f"{MARKETO_LIST_PAGE_ROW_CAP}-per-call ceiling up to the effective row limit — no "
            f"pagination token appears on this verb; defaults to {DEFAULT_ROW_LIMIT}. To fetch "
            "more, pass acknowledge_default_limit_override=true together with an explicit "
            f"row_limit (up to {MARKETO_LIST_ROW_LIMIT_CAP}) — both required together, refused "
            f"rather than clamped above {MARKETO_LIST_ROW_LIMIT_CAP}. Beyond the hard cap: no "
            "resumption, re-invoke with a later since_datetime. moreResult is the ONLY usable "
            "vendor continuation signal internally (Adobe documents this endpoint always returns a "
            "token, so token presence can't terminate the loop, unlike "
            "get_leads/list_campaigns/list_static_lists) and its reliability on this endpoint is "
            "documented but UNMEASURED — the one live measurement of moreResult anywhere found it "
            "violated on list_campaigns, so truncated=false on the finished job is only as honest "
            "as Marketo's own flag. Prefer requesting stable ID and status fields for validation "
            "over email or other PII-bearing fields."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "since_datetime": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=(
                    "Second-granularity ISO-8601 instant to read activities from "
                    "(e.g. 2026-07-28T00:00:00-07:00). A fractional-seconds "
                    "component is floor-truncated before the paging-token request; "
                    "all other bytes are preserved. To read further than the "
                    "effective row limit reaches, re-invoke with a later instant."
                ),
            ),
            "lead_ids": ParameterMetadata(
                type=ParameterType.LIST,
                required=False,
                description="Up to 30 lead ids to restrict the read to (Marketo's own server-side cap).",
            ),
            "activity_type_ids": ParameterMetadata(
                type=ParameterType.LIST,
                required=True,
                description="One to 10 ids from list_activity_types for this Marketo instance.",
            ),
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ABSOLUTE .tsv destination path, contained under an export_allowed_roots entry.",
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(
                    f"Must be exactly true, together with row_limit, to fetch more than the "
                    f"default {DEFAULT_ROW_LIMIT} activity items."
                ),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(
                    f"Explicit record ceiling, up to {MARKETO_LIST_ROW_LIMIT_CAP}. Only honored "
                    f"together with acknowledge_default_limit_override=true; refused (not clamped) "
                    f"above {MARKETO_LIST_ROW_LIMIT_CAP}."
                ),
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="A job handle — poll check_marketo_job_status(job_id) for the finished TSV.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Pass to check_marketo_job_status to retrieve the result."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued' — the dispatch never waits for the fetch."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def get_activities(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async_job(
            action_name="get_activities",
            verb_label="activity log export",
            notes=f"Marketo get_activities: since={params.get('since_datetime')!r}",
            prepare=lambda: marketing_actions.prepare_get_activities(params, self._export_path_gate),
            state=state,
        )

    @platform_process(
        name="create_or_update_leads",
        display_name="Marketo: Create or Update Leads",
        description=(
            "Create and/or update up to 300 lead records in one batch. action is one of "
            "createOrUpdate (default), createOnly, updateOnly, createDuplicate; lookup_field "
            "names the dedupe field (defaults to email). Before writing, one lead-describe "
            "call validates the batch and refuses it whole if any non-key field is REST "
            "read-only. Write action."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "records": ParameterMetadata(type=ParameterType.LIST, required=True, description="Up to 300 objects of lead field values."),
            "action": ParameterMetadata(type=ParameterType.STRING, required=False, description="createOrUpdate (default), createOnly, updateOnly, or createDuplicate."),
            "lookup_field": ParameterMetadata(type=ParameterType.STRING, required=False, description="Dedupe field name (e.g. 'email' or a custom id field)."),
        },
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="results (per-record id/status/reasons), row_count, tallies."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def create_or_update_leads(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda client: marketing_actions.create_or_update_leads(client, params), "create_or_update_leads")

    @platform_process(
        name="delete_leads",
        display_name="Marketo: Delete Leads",
        description="Delete up to 300 leads by id. Destructive write action — Marketo permanently removes the records.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "lead_ids": ParameterMetadata(type=ParameterType.LIST, required=True, description="Up to 300 lead ids to delete."),
        },
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="results (per-record id/status/reasons), row_count, tallies."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def delete_leads(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda client: marketing_actions.delete_leads(client, params), "delete_leads")

    @platform_process(
        name="merge_leads",
        display_name="Marketo: Merge Leads",
        description=(
            "Merge up to 25 losing leads into one winning lead. Read-only fields retain "
            "the winner's value even when empty rather than being filled from a loser under "
            "the general precedence rule. merge_in_crm additionally merges the natively-synced "
            "CRM records — Marketo itself restricts a CRM merge to exactly ONE losing lead per "
            "call, not 25. Destructive write action: losing leads' identities are absorbed into "
            "the winner and cannot be split back apart."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "winning_lead_id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The lead id whose field values win the merge."),
            "losing_lead_ids": ParameterMetadata(type=ParameterType.LIST, required=True, description="Up to 25 lead ids to merge into the winner (only 1 if merge_in_crm is true)."),
            "merge_in_crm": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="Also merge the natively-synced CRM records (caps losing_lead_ids at 1)."),
        },
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="success, request_id."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def merge_leads(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda client: marketing_actions.merge_leads(client, params), "merge_leads")

    @platform_process(
        name="list_campaigns",
        display_name="Marketo: List Campaigns",
        description=(
            "Dispatch an async job that lists campaigns, optionally filtered by names and/or "
            "program_names, and returns {job_id, status} in milliseconds; poll "
            "check_marketo_job_status(job_id) for the finished TSV handle. The finished job ALWAYS "
            f"carries the campaigns written to output_tsv_path, never inline. Pages internally "
            f"across Marketo's own {MARKETO_LIST_PAGE_ROW_CAP}-per-call ceiling up to the effective "
            f"row limit — no pagination token appears on this verb; defaults to {DEFAULT_ROW_LIMIT} "
            "to avoid exhausting vendor rate limits and disk. To fetch more, pass "
            "acknowledge_default_limit_override=true together with an explicit row_limit (up to "
            f"{MARKETO_LIST_ROW_LIMIT_CAP}) — both required together, refused rather than clamped "
            f"above {MARKETO_LIST_ROW_LIMIT_CAP}. Beyond the hard cap: no resumption, re-invoke "
            "with a narrower names/program_names filter. Prefer selecting on stable campaign IDs "
            "downstream over other fields when the goal is validation."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "names": ParameterMetadata(type=ParameterType.LIST, required=False, description="Optional list of exact campaign names to filter by."),
            "program_names": ParameterMetadata(type=ParameterType.LIST, required=False, description="Optional list of exact program names to filter by."),
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ABSOLUTE .tsv destination path, contained under an export_allowed_roots entry.",
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(
                    f"Must be exactly true, together with row_limit, to fetch more than the "
                    f"default {DEFAULT_ROW_LIMIT} campaigns."
                ),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(
                    f"Explicit record ceiling, up to {MARKETO_LIST_ROW_LIMIT_CAP}. Only honored "
                    f"together with acknowledge_default_limit_override=true; refused (not clamped) "
                    f"above {MARKETO_LIST_ROW_LIMIT_CAP}."
                ),
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="A job handle — poll check_marketo_job_status(job_id) for the finished TSV.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Pass to check_marketo_job_status to retrieve the result."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued' — the dispatch never waits for the fetch."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_campaigns(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async_job(
            action_name="list_campaigns",
            verb_label="campaign listing export",
            notes=f"Marketo list_campaigns: names={params.get('names')!r}",
            prepare=lambda: marketing_actions.prepare_list_campaigns(params, self._export_path_gate),
            state=state,
        )

    @platform_process(
        name="trigger_campaign",
        display_name="Marketo: Trigger Campaign",
        description=(
            "Trigger (Request Campaign) a campaign for up to 100 leads, with optional campaign "
            "tokens. Destructive-class write action: the campaign's flow runs against real people, "
            "is irreversible, and is visible outside this system (it may send email, alert sales, "
            "change scoring, or move program status). A Smart Campaign has two halves: the Smart "
            "List (who/when it fires) IS inspectable via Marketo's Smart Lists Asset API with "
            "includeRules=true; the Flow (what it actually does — Send Email, Change Data Value, "
            "Wait, etc) is NOT — no REST endpoint dereferences a campaign's flowId into its steps, "
            "and there is no dry-run. So a caller can see who/when but never what happens before "
            "triggering — trigger only campaigns you authored."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "campaign_id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The Marketo campaign id."),
            "lead_ids": ParameterMetadata(type=ParameterType.LIST, required=True, description="Up to 100 lead ids to run through the campaign."),
            "tokens": ParameterMetadata(type=ParameterType.LIST, required=False, description="Optional list of {name, value} campaign token overrides."),
        },
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="success, request_id."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def trigger_campaign(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda client: marketing_actions.trigger_campaign(client, params), "trigger_campaign")

    @platform_process(
        name="list_static_lists",
        display_name="Marketo: List Static Lists",
        description=(
            "Dispatch an async job that lists static lists, optionally filtered by names, and "
            "returns {job_id, status} in milliseconds; poll check_marketo_job_status(job_id) for "
            "the finished TSV handle. Same page-cap shape as list_campaigns. The finished job "
            f"ALWAYS carries the lists written to output_tsv_path, never inline. Pages internally "
            f"across Marketo's own {MARKETO_LIST_PAGE_ROW_CAP}-per-call ceiling up to the "
            f"effective row limit — no pagination token appears on this verb; defaults to "
            f"{DEFAULT_ROW_LIMIT} to avoid exhausting vendor rate limits and disk. To fetch more, "
            "pass acknowledge_default_limit_override=true together with an explicit row_limit "
            f"(up to {MARKETO_LIST_ROW_LIMIT_CAP}) — both required together, refused rather than "
            f"clamped above {MARKETO_LIST_ROW_LIMIT_CAP}. Beyond the hard cap: no resumption, "
            "re-invoke with a narrower names filter. Prefer selecting on stable list IDs "
            "downstream over other fields when the goal is validation."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "names": ParameterMetadata(type=ParameterType.LIST, required=False, description="Optional list of exact static list names to filter by."),
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ABSOLUTE .tsv destination path, contained under an export_allowed_roots entry.",
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(
                    f"Must be exactly true, together with row_limit, to fetch more than the "
                    f"default {DEFAULT_ROW_LIMIT} static lists."
                ),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(
                    f"Explicit record ceiling, up to {MARKETO_LIST_ROW_LIMIT_CAP}. Only honored "
                    f"together with acknowledge_default_limit_override=true; refused (not clamped) "
                    f"above {MARKETO_LIST_ROW_LIMIT_CAP}."
                ),
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="A job handle — poll check_marketo_job_status(job_id) for the finished TSV.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Pass to check_marketo_job_status to retrieve the result."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued' — the dispatch never waits for the fetch."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_static_lists(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async_job(
            action_name="list_static_lists",
            verb_label="static list listing export",
            notes=f"Marketo list_static_lists: names={params.get('names')!r}",
            prepare=lambda: marketing_actions.prepare_list_static_lists(params, self._export_path_gate),
            state=state,
        )

    @platform_process(
        name="add_leads_to_list",
        display_name="Marketo: Add Leads to List",
        description="Add up to 300 leads to a static list by id. Write action.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "list_id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The Marketo static list id."),
            "lead_ids": ParameterMetadata(type=ParameterType.LIST, required=True, description="Up to 300 lead ids to add."),
        },
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="results (per-record id/status), row_count, tallies."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def add_leads_to_list(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda client: marketing_actions.add_leads_to_list(client, params), "add_leads_to_list")

    @platform_process(
        name="remove_leads_from_list",
        display_name="Marketo: Remove Leads from List",
        description="Remove up to 300 leads from a static list by id. Write action.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "list_id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The Marketo static list id."),
            "lead_ids": ParameterMetadata(type=ParameterType.LIST, required=True, description="Up to 300 lead ids to remove."),
        },
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="results (per-record id/status), row_count, tallies."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def remove_leads_from_list(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda client: marketing_actions.remove_leads_from_list(client, params), "remove_leads_from_list")

    @platform_process(
        name="test_connection",
        display_name="Marketo: Test Connection",
        description="Verify the configured Marketo credentials by minting a bearer token. Returns ok, base_url, client_id.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="ok, base_url, client_id."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def test_connection(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        loader = self._app_config_loader

        def _do(client: MarketoClient) -> dict[str, Any]:
            config = loader.load() if loader is not None else None
            client.ensure_authenticated()
            return {
                "ok": True,
                "base_url": config.base_url if config is not None else "",
                "client_id": config.client_id if config is not None else "",
            }

        return self._run(_do, "test_connection")

    @platform_process(
        name="check_setup",
        display_name="Marketo: Check Setup",
        description=(
            "Probe the configured API user's READ-ONLY capabilities (lead schema/query, activity "
            "type listing, API usage, campaign listing, static list listing) and report which Access API Role "
            "permission is missing for any that fail. PARTIAL by design: write/execute verbs "
            "(create_or_update_leads, delete_leads, add/remove_leads_from_list, trigger_campaign) "
            "cannot be probed without performing them, so reads_verified=true does NOT mean the "
            "whole setup is confirmed ready — see writes_unverified."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description=(
                "reads_verified (bool, READ capabilities only), checks (per-probe status + "
                "guidance), writes_unverified (verb names whose permissions can't be probed "
                "safely), writes_unverified_note."
            ),
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def check_setup(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            lambda client: marketing_actions.check_setup(client), "check_setup"
        )

    @platform_process(
        name="check_marketo_job_status",
        display_name="Marketo: Check Async Job Status",
        description=(
            "Poll the status of an async job dispatched by get_leads, get_activities, "
            "list_campaigns, or list_static_lists. status is one of queued, processing, "
            "completed, error, or cancelled. result carries the finished job's TSV handle "
            "(path, row_count, columns, truncated) once status is completed, and is null "
            "in every other state. error carries the classified marketo.* code + message "
            "once status is error, and is null in every other state — poll again after "
            "check_after_seconds while status is queued or processing."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            PARAM_JOB_ID: ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The job_id returned by get_leads/get_activities/list_campaigns/list_static_lists.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="The job's current status, plus its result or error once terminal.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Echoes the requested job_id."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="queued, processing, completed, error, or cancelled."),
                "result": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="The finished TSV handle (path, row_count, columns, truncated) once status is completed; null otherwise.",
                ),
                "error": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="The classified marketo.* {code, message} once status is error; null otherwise.",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def check_marketo_job_status(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        job_id = params.get(PARAM_JOB_ID)
        if not isinstance(job_id, str) or not job_id:
            return self._error(ERROR_INVALID_PARAMS, f"'{PARAM_JOB_ID}' is required and must be a non-empty string")
        try:
            manager = self._require_async_job_manager()
        except RuntimeError as exc:
            return self._error(ERROR_API_ERROR, str(exc))
        job = self._extract_job_record(manager.get_job(job_id))
        if job is None:
            return self._error(ERROR_JOB_NOT_FOUND, f"Marketo job not found: {job_id}")
        status = job.get("status")
        status = status if isinstance(status, str) and status else "unknown"
        result_payload, error_payload = self._terminal_job_payloads(manager, job_id, status)
        return self._success(
            {
                "job_id": job_id,
                "status": status,
                "result": result_payload,
                "error": error_payload,
            }
        )

    def _extract_job_record(self, job_result: dict[str, Any]) -> dict[str, Any] | None:
        """Pull the job dict out of AsyncJobManager.get_job's envelope, or None."""
        if job_result.get("action_status") != "completed":
            return None
        data = job_result.get("data", {})
        candidate = data.get("job") if isinstance(data, dict) else None
        return candidate if isinstance(candidate, dict) else None

    def _terminal_job_payloads(
        self, manager: AsyncJobManager, job_id: str, status: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """(result, error) for a terminal status; (None, None) otherwise.

        Coordinator-seat advisory (2026-08-09): both keys must stay present on the
        caller's response regardless — only the VALUE is conditional here.
        """
        if status == "completed":
            return self._fetch_job_result_payload(manager, job_id, "result"), None
        if status == "error":
            return None, self._fetch_job_result_payload(manager, job_id, "error")
        return None, None

    def _fetch_job_result_payload(
        self, manager: AsyncJobManager, job_id: str, payload_type: str,
    ) -> dict[str, Any]:
        payload_result = manager.get_job_payload(job_id, payload_type)
        if payload_result.get("action_status") != "completed":
            return {}
        data = payload_result.get("data", {})
        payload = data.get("payload") if isinstance(data, dict) else None
        return payload if isinstance(payload, dict) else {}


def _edge(
    name: str,
    result_type: str,
    *,
    retryable: bool,
) -> EdgeProcessDefinition:
    return EdgeProcessDefinition(
        name=name,
        result_processor_template_customizations=MergeResultProcessorCustomizations(
            result_type=result_type,
        ),
        error_processor_template_customizations=MergeErrorProcessorCustomizations(
            retryable=retryable,
        ),
    )
