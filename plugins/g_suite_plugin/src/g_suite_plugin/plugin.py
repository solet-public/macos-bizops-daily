"""Google Workspace plugin entry point.

OAuth 2.0 bootstrap (PKCE, official Google libraries) + callback server, plus
the full Gmail / Drive / Sheets / Docs / Slides verb surface. The plugin runs
its own FastAPI/uvicorn server for the HTTPS OAuth callback (ALB-routable in
cloud deployment).

Verbs:
  - test_connection  — bounded Gmail profile read for connection qualification
  - connect_account   — returns the Google consent URL + state nonce (PKCE)
  - start_interface   — starts the OAuth callback uvicorn server
  - stop_interface    — shuts down the server
  - gmail_list_messages / gmail_get_message / gmail_send
  - drive_list_files / drive_download_file / drive_upload_file /
    drive_create_folder / drive_share
  - sheets_create / sheets_create_from_files / sheets_get_values /
    sheets_update_values / sheets_append_values / sheets_batch_update /
    sheets_export
  - docs_create / docs_get / docs_batch_update / docs_export
  - slides_create / slides_get / slides_batch_update / slides_export
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

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
from googleapiclient.errors import HttpError

from . import completion_templates, docs_actions, drive_actions, gmail_actions, sheets_actions, slides_actions
from .constants import (
    BLOB_NAMESPACE,
    DRIVE_DEFAULT_PAGE_SIZE,
    DRIVE_PAGE_SIZE_CAP,
    ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
    ERROR_API_ERROR,
    ERROR_BLOB_STORAGE_NOT_AVAILABLE,
    ERROR_INVALID_PARAMS,
    ERROR_NOT_CONNECTED,
    ERROR_NOT_FOUND,
    ERROR_PERMISSION_DENIED,
    ERROR_RATE_LIMITED,
    ERROR_SERVER_START_FAILED,
    ERROR_VAULT_NOT_AVAILABLE,
    GMAIL_DEFAULT_MAX_RESULTS,
    GMAIL_MAX_RESULTS_CAP,
    JOB_ACTION_NAME,
    PARAM_ACKNOWLEDGE_OVERRIDE,
    PARAM_ROW_LIMIT,
    PLUGIN_NAME,
    RESULT_TYPE_CONNECT,
    RESULT_TYPE_DOCS_BATCH_UPDATE,
    RESULT_TYPE_DOCS_CREATE,
    RESULT_TYPE_DOCS_EXPORT,
    RESULT_TYPE_DOCS_GET,
    RESULT_TYPE_DRIVE_CREATE_FOLDER,
    RESULT_TYPE_DRIVE_DOWNLOAD,
    RESULT_TYPE_DRIVE_LIST,
    RESULT_TYPE_DRIVE_SHARE,
    RESULT_TYPE_DRIVE_UPLOAD,
    RESULT_TYPE_GMAIL_LIST,
    RESULT_TYPE_GMAIL_MESSAGE,
    RESULT_TYPE_GMAIL_SEND,
    RESULT_TYPE_INTERFACE_START,
    RESULT_TYPE_INTERFACE_STOP,
    RESULT_TYPE_SHEETS_APPEND_VALUES,
    RESULT_TYPE_SHEETS_BATCH_UPDATE,
    RESULT_TYPE_SHEETS_CREATE,
    RESULT_TYPE_SHEETS_CREATE_FROM_FILES,
    RESULT_TYPE_SHEETS_EXPORT,
    RESULT_TYPE_SHEETS_GET_VALUES,
    RESULT_TYPE_SHEETS_UPDATE_VALUES,
    RESULT_TYPE_SLIDES_BATCH_UPDATE,
    RESULT_TYPE_SLIDES_CREATE,
    RESULT_TYPE_SLIDES_EXPORT,
    RESULT_TYPE_SLIDES_GET,
    RESULT_TYPE_TEST_CONNECTION,
    SHEETS_DEFAULT_ROW_LIMIT,
    SHEETS_ROW_LIMIT_CAP,
    VAULT_KEY_ACCESS_TOKEN,
    VAULT_KEY_REFRESH_TOKEN,
)
from .gmail_actions import OutgoingAttachment
from .oauth.app_config import AppConfigLoader
from .oauth.http_server import OAuthServer, OAuthServerStartError, build_start_result
from .oauth.oauth_client import build_authorization_request
from .oauth.service_factory import GoogleServiceFactory
from .oauth.token_store import TokenStore, TokenStoreError


@dataclass
class _DeferredJobRuntime:
    """D0.3 deferred-completion state, bundled into one attribute (god-class budget).

    A single serial worker thread — matches comfyui_image_generation_plugin and
    cosyvoice2_tts_plugin's one-worker-thread precedent, which is what keeps
    FlowManager._sequence_cache's unlocked check-then-increment safe per the
    doctrine's mechanic-1 caveat.
    """

    manager: Any | None = None
    thread: threading.Thread | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)


class GSuitePlugin(ServicePlugin, EdgeProcessProvider):
    """Google Workspace (Gmail / Drive / Sheets / Docs / Slides) plugin."""

    name: str = PLUGIN_NAME

    def __init__(self) -> None:
        super().__init__()
        self.logger: logging.Logger | None = None
        self._vault_service: object | None = None
        self._address_book_service: Any | None = None
        self._blob_storage_service: Any | None = None
        self._token_store: TokenStore | None = None
        self._app_config_loader: AppConfigLoader | None = None
        self._service_factory: GoogleServiceFactory | None = None
        self._oauth_server: OAuthServer = OAuthServer()
        # In-memory PKCE store: state_nonce -> code_verifier. Short-lived.
        self._pending_states: dict[str, str] = {}
        self._deferred: _DeferredJobRuntime = _DeferredJobRuntime()

    def set_vault_service(self, vault_service: object) -> None:
        """Receive the caller-bound VaultServiceProxy from lifecycle injection."""
        self._vault_service = vault_service

    # ------------------------------------------------------------------
    # VaultKeysProvider
    # ------------------------------------------------------------------

    def get_required_vault_keys(self) -> list[str]:
        """No vault keys are required at readiness — none are plugin-owned yet.

        ``client_secret`` is chain-consumed through the address book (never a
        direct vault verb under this plugin's identity); the runtime tokens are
        created on-demand by the OAuth callback. A missing client_secret surfaces
        loudly on the first ``resolve_with_secrets``.
        """
        return []

    def get_declared_vault_keys(self) -> list[str]:
        """Scoped vault keys this plugin reads or writes DIRECTLY.

        Excludes the OAuth ``client_secret`` (chain-consumed through the address
        book, resolver's namespace). Only the runtime tokens are declared.
        """
        return [VAULT_KEY_REFRESH_TOKEN, VAULT_KEY_ACCESS_TOKEN]

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
        if self._vault_service is None:
            raise RuntimeError(f"{ERROR_VAULT_NOT_AVAILABLE}: {self.name} requires vault_service")
        self._address_book_service = self.orchestrator_ref.get_service("address_book_service")
        if self._address_book_service is None:
            raise RuntimeError(
                f"{ERROR_ADDRESS_BOOK_NOT_AVAILABLE}: {self.name} requires "
                "address_book_service for OAuth app config"
            )
        # blob_storage is needed only for attachment/download/export verbs and is
        # resolved lazily at first use (_blob_service): the platform constructs
        # blob_storage_service in the init_service_manager startup step, AFTER
        # every plugin's prepare_for_readiness — resolving it here caches None
        # forever and every download/export hard-fails (field-verified on a
        # live deployment).
        self._app_config_loader = AppConfigLoader(self._address_book_service)
        self._token_store = TokenStore(self._vault_service, self._app_config_loader)
        self._service_factory = GoogleServiceFactory(self._token_store)
        self.set_ready()

    @service_lifecycle(operation="start")
    async def start_services(self) -> ActionResult:
        """Start the single serial background job-processing thread (D0.3 shape)."""
        if self._services_started:
            return {
                "action_status": ActionStatus.COMPLETED.value,
                "data": {"message": "Service already running"},
                "actions": [],
                "error": None,
            }
        self._deferred.stop_event.clear()
        self._deferred.thread = threading.Thread(
            target=self._job_processing_loop,
            name="gsuite-async-jobs",
            daemon=False,
        )
        self._deferred.thread.start()
        self._services_started = True
        self._service_started_at = datetime.now(UTC).isoformat()
        self._service_error = None
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {"message": "Service started successfully"},
            "actions": [],
            "error": None,
        }

    @service_lifecycle(operation="stop")
    async def stop_services(self) -> ActionResult:
        """Stop the background job-processing thread."""
        if not self._services_started:
            return {
                "action_status": ActionStatus.COMPLETED.value,
                "data": {"message": "Service already stopped"},
                "actions": [],
                "error": None,
            }
        self._deferred.stop_event.set()
        if self._deferred.thread is not None:
            self._deferred.thread.join(timeout=30.0)
            self._deferred.thread = None
        self._services_started = False
        self._service_started_at = None
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {"message": "Service stopped successfully"},
            "actions": [],
            "error": None,
        }

    # ------------------------------------------------------------------
    # D0.3 deferred-completion machinery
    # ------------------------------------------------------------------

    def _try_acquire_job_manager(self) -> None:
        """Deferred DI: AsyncJobManager becomes available on orchestrator_ref only
        after platform startup completes — acquire it lazily, cache permanently."""
        if self._deferred.manager is not None:
            return
        if self.orchestrator_ref is None:
            return
        job_manager = getattr(self.orchestrator_ref, "async_job_manager", None)
        if job_manager is not None:
            self._deferred.manager = job_manager

    def _job_processing_loop(self) -> None:
        """Poll for queued Workspace jobs and process them one at a time."""
        stop_event = self._deferred.stop_event
        while not stop_event.is_set():
            try:
                self._poll_and_process_once(stop_event)
            except Exception:
                if self.logger:
                    self.logger.exception("g_suite job processing loop error")
                stop_event.wait(5.0)

    def _poll_and_process_once(self, stop_event: threading.Event) -> None:
        """One poll cycle: acquire the manager, fetch at most one queued job, run it."""
        self._try_acquire_job_manager()
        manager = self._deferred.manager
        if manager is None:
            stop_event.wait(2.0)
            return
        result = manager.list_jobs(
            status="queued",
            provider_name=f"{PLUGIN_NAME}.{JOB_ACTION_NAME}",
            limit=1,
            order_by="created_at ASC",
        )
        jobs = (result.get("data") or {}).get("jobs", [])
        if not jobs:
            stop_event.wait(2.0)
            return
        for job in jobs:
            if stop_event.is_set():
                break
            self._process_job(job)

    def _process_job(self, job: dict[str, Any]) -> None:
        """Look up the queued job's verb + params and run the real Google API call."""
        job_id = str(job.get("id", job.get("job_id", "")))
        manager = self._deferred.manager
        assert manager is not None
        manager.update_job(job_id, {"status": "processing"})
        verb = ""
        try:
            payload_result = manager.get_job_payload(job_id)
            request_data: dict[str, Any] = (payload_result.get("data") or {}).get("payload") or {}
            verb = str(request_data.get("verb", ""))
            params: dict[str, Any] = request_data.get("params") or {}
            produce = self._deferred_producers().get(verb)
            if produce is None:
                raise ValueError(f"no deferred producer registered for verb '{verb}'")
            data = produce(params)
        except Exception as exc:  # noqa: BLE001 — classified below, never re-raised
            code, message = _classify_deferred_exception(exc)
            self._fail_job(job_id, code, message)
            return
        if self.logger:
            self.logger.debug("%s: deferred success (job_id=%s)", verb, job_id)
        manager.update_job(job_id, {"status": "completed", "result": data})

    def _fail_job(self, job_id: str, code: str, message: str) -> None:
        manager = self._deferred.manager
        assert manager is not None
        manager.update_job(
            job_id, {"status": "error", "error": {"code": code, "message": message}}
        )

    def _deferred_producers(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        """verb name -> zero-state callable performing the real Google API I/O.

        Built lazily (not a class-level dict) because each thunk closes over
        ``self`` for ``_require_factory()`` / ``_store_blob`` / ``_load_attachment``.
        """
        return {
            "gmail_list_messages": lambda params: gmail_actions.list_messages(
                self._require_factory().gmail(), params
            ),
            "gmail_get_message": lambda params: gmail_actions.get_message(
                self._require_factory().gmail(), params
            ),
            "gmail_send": lambda params: gmail_actions.send_message(
                self._require_factory().gmail(), params, self._load_attachment
            ),
            "drive_download_file": lambda params: drive_actions.download_file(
                self._require_factory().drive(), params, self._store_blob
            ),
            "drive_upload_file": lambda params: drive_actions.upload_file(
                self._require_factory().drive(), params, self._load_attachment
            ),
            "drive_list_files": lambda params: drive_actions.list_files(
                self._require_factory().drive(), params
            ),
            "drive_create_folder": lambda params: drive_actions.create_folder(
                self._require_factory().drive(), params
            ),
            "drive_share": lambda params: drive_actions.share_file(
                self._require_factory().drive(), params
            ),
            "sheets_create_from_files": lambda params: sheets_actions.create_spreadsheet_from_files(
                self._require_factory().sheets(), params
            ),
            "sheets_create": lambda params: sheets_actions.create_spreadsheet(
                self._require_factory().sheets(), params
            ),
            "sheets_get_values": lambda params: sheets_actions.get_values(
                self._require_factory().sheets(), params
            ),
            "sheets_update_values": lambda params: sheets_actions.update_values(
                self._require_factory().sheets(), params
            ),
            "sheets_append_values": lambda params: sheets_actions.append_values(
                self._require_factory().sheets(), params
            ),
            "sheets_batch_update": lambda params: sheets_actions.batch_update(
                self._require_factory().sheets(), params
            ),
            "slides_create": lambda params: slides_actions.create_presentation(
                self._require_factory().slides(), params
            ),
            "slides_get": lambda params: slides_actions.get_presentation(
                self._require_factory().slides(), params
            ),
            "slides_batch_update": lambda params: slides_actions.batch_update(
                self._require_factory().slides(), params
            ),
            "docs_create": lambda params: docs_actions.create_document(
                self._require_factory().docs(), params
            ),
            "docs_get": lambda params: docs_actions.get_document(
                self._require_factory().docs(), params
            ),
            "docs_batch_update": lambda params: docs_actions.batch_update(
                self._require_factory().docs(), params
            ),
        }

    @staticmethod
    def _validate_deferred_context(state: dict[str, Any]) -> tuple[str, str] | None:
        """Extract (session_id, flow_id) from state, or None if either is unusable."""
        session_id = state.get("session_id")
        flow_id = state.get("flow_id")
        if not isinstance(session_id, str) or not session_id:
            return None
        if not isinstance(flow_id, str) or not flow_id:
            return None
        return session_id, flow_id

    def _create_deferred_job(
        self, verb: str, session_id: str, flow_id: str, params: dict[str, Any]
    ) -> tuple[str, None] | tuple[None, str]:
        """Create the job row; returns (job_id, None) or (None, error_message)."""
        manager = self._deferred.manager
        assert manager is not None
        process_key = f"plugin::{PLUGIN_NAME}::{verb}"
        job_metadata = completion_templates.build_job_metadata(session_id, flow_id, verb, process_key)
        create_result = manager.create_job(
            plugin_name=PLUGIN_NAME,
            action_name=JOB_ACTION_NAME,
            request_data={"notes": f"{verb} ({session_id})", "verb": verb, "params": params},
            job_metadata=job_metadata,
        )
        if create_result.get("action_status") != ActionStatus.COMPLETED.value:
            message = (create_result.get("error") or {}).get("message", "job creation failed")
            return None, str(message)
        job_id = (create_result.get("data") or {}).get("job_id")
        if not isinstance(job_id, str) or not job_id:
            return None, "job creation returned no job_id"
        return job_id, None

    def _run_deferred(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
        verb: str,
    ) -> dict[str, Any]:
        """Ms-scale dispatch (D0.3 shape): validate, enqueue, return a job handle.

        The real Google API call happens later, off this dispatch path, in
        ``_process_job`` on the single background worker thread.
        """
        if not self._require_token_store().is_connected():
            return self._error(
                ERROR_NOT_CONNECTED,
                "No Google account connected. Run connect_account first.",
            )
        context = self._validate_deferred_context(state)
        if context is None:
            return self._error(
                ERROR_INVALID_PARAMS, "session_id/flow_id missing from state context"
            )
        self._try_acquire_job_manager()
        if self._deferred.manager is None:
            return self._error(
                ERROR_API_ERROR,
                "AsyncJobManager not available — platform startup may not be complete",
            )
        session_id, flow_id = context
        job_id, error_message = self._create_deferred_job(verb, session_id, flow_id, params)
        if job_id is None:
            return self._error(ERROR_API_ERROR, error_message or "job creation failed")
        return self._success({"job_id": job_id, "status": "queued"})

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_token_store(self) -> TokenStore:
        if self._token_store is None:
            raise RuntimeError(ERROR_VAULT_NOT_AVAILABLE)
        return self._token_store

    def _require_app_config_loader(self) -> AppConfigLoader:
        if self._app_config_loader is None:
            raise RuntimeError(ERROR_ADDRESS_BOOK_NOT_AVAILABLE)
        return self._app_config_loader

    def _require_factory(self) -> GoogleServiceFactory:
        if self._service_factory is None:
            raise RuntimeError(ERROR_VAULT_NOT_AVAILABLE)
        return self._service_factory

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

    def _load_attachment(self, blob_id: str) -> OutgoingAttachment:
        """Resolve a blob id to bytes for an outgoing Gmail attachment."""
        if self._blob_storage_service is None:
            raise TokenStoreError(
                ERROR_BLOB_STORAGE_NOT_AVAILABLE,
                "blob_storage_service is not available for attachment resolution",
            )
        result = self._blob_storage_service.retrieve_blob(blob_id)
        if not isinstance(result, dict) or result.get("action_status") != "completed":
            raise ValueError(f"attachment blob '{blob_id}' could not be retrieved")
        data = result.get("data") or {}
        metadata = data.get("metadata") or {}
        return OutgoingAttachment(
            filename=str(data.get("filename") or blob_id),
            mime_type=str(metadata.get("mime_type") or "application/octet-stream"),
            content=bytes.fromhex(str(data.get("content") or "")),
        )

    def _blob_service(self) -> Any | None:
        """Resolve blob_storage_service lazily at point of use (cached once found).

        Readiness-time resolution is a known trap: the platform constructs
        blob_storage_service after every plugin's prepare_for_readiness, so a
        readiness-time get_service() returns None and the miss would be cached
        for the life of the plugin.
        """
        if self._blob_storage_service is None and self.orchestrator_ref is not None:
            self._blob_storage_service = self.orchestrator_ref.get_service("blob_storage_service")
        return self._blob_storage_service

    def _store_blob(self, content: bytes, filename: str, mime_type: str) -> str:
        """Store downloaded/exported bytes as a blob; return the blob id (the *_blob_key)."""
        blob_service = self._blob_service()
        if blob_service is None:
            raise TokenStoreError(
                ERROR_BLOB_STORAGE_NOT_AVAILABLE,
                f"blob_storage_service is not available for blob storage: cannot store "
                f"'{filename}' ({len(content)} bytes); this verb returns its payload "
                f"only via blob storage",
            )
        result = blob_service.store_blob(
            BLOB_NAMESPACE, content, {"filename": filename, "mime_type": mime_type}
        )
        if not isinstance(result, dict) or result.get("action_status") != "completed":
            raise ValueError(f"failed to store blob for '{filename}' ({len(content)} bytes)")
        blob_id = (result.get("data") or {}).get("blob_id")
        if not isinstance(blob_id, str) or not blob_id:
            raise ValueError(f"blob storage returned no blob_id for '{filename}' ({len(content)} bytes)")
        return blob_id

    def _run(
        self,
        produce: Callable[[], dict[str, Any]],
        endpoint_name: str,
    ) -> dict[str, Any]:
        """Shared connect-check + error-classification path for every Workspace verb.

        The thunk builds its own service (inside the try) and returns the result
        dict, so a token-refresh fault surfaces as a typed error, not a crash.
        """
        if not self._require_token_store().is_connected():
            return self._error(
                ERROR_NOT_CONNECTED,
                "No Google account connected. Run connect_account first.",
            )
        try:
            data = produce()
        except ValueError as exc:
            return self._error(ERROR_INVALID_PARAMS, str(exc))
        except (TokenStoreError, sheets_actions.ResultTooLargeError) as exc:
            return self._error(exc.code, str(exc))
        except HttpError as exc:
            code, message = _classify_http_error(exc)
            return self._error(code, message)
        except Exception as exc:  # noqa: BLE001 — surface any Google/client fault as a typed error
            return self._error(ERROR_API_ERROR, f"Google API call failed: {exc}")
        if self.logger:
            self.logger.debug("%s: success", endpoint_name)
        return self._success(data)

    # ------------------------------------------------------------------
    # EdgeProcessProvider
    # ------------------------------------------------------------------

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {
            "test_connection": _edge(
                "test_connection", RESULT_TYPE_TEST_CONNECTION, retryable=True
            ),
            "connect_account": _edge(
                "connect_account", RESULT_TYPE_CONNECT, retryable=False
            ),
            "start_interface": _edge(
                "start_interface",
                RESULT_TYPE_INTERFACE_START,
                retryable=True,
            ),
            "stop_interface": _edge(
                "stop_interface",
                RESULT_TYPE_INTERFACE_STOP,
                retryable=False,
            ),
            "gmail_list_messages": _edge(
                "gmail_list_messages",
                RESULT_TYPE_GMAIL_LIST,
                retryable=True,
            ),
            "gmail_get_message": _edge(
                "gmail_get_message",
                RESULT_TYPE_GMAIL_MESSAGE,
                retryable=True,
            ),
            "gmail_send": _edge(
                "gmail_send", RESULT_TYPE_GMAIL_SEND, retryable=False
            ),
            "drive_list_files": _edge(
                "drive_list_files",
                RESULT_TYPE_DRIVE_LIST,
                retryable=True,
            ),
            "drive_download_file": _edge(
                "drive_download_file",
                RESULT_TYPE_DRIVE_DOWNLOAD,
                retryable=True,
            ),
            "drive_upload_file": _edge(
                "drive_upload_file",
                RESULT_TYPE_DRIVE_UPLOAD,
                retryable=False,
            ),
            "drive_create_folder": _edge(
                "drive_create_folder",
                RESULT_TYPE_DRIVE_CREATE_FOLDER,
                retryable=False,
            ),
            "drive_share": _edge(
                "drive_share",
                RESULT_TYPE_DRIVE_SHARE,
                retryable=False,
            ),
            "sheets_create": _edge(
                "sheets_create",
                RESULT_TYPE_SHEETS_CREATE,
                retryable=False,
            ),
            "sheets_get_values": _edge(
                "sheets_get_values",
                RESULT_TYPE_SHEETS_GET_VALUES,
                retryable=True,
            ),
            "sheets_update_values": _edge(
                "sheets_update_values",
                RESULT_TYPE_SHEETS_UPDATE_VALUES,
                retryable=False,
            ),
            "sheets_append_values": _edge(
                "sheets_append_values",
                RESULT_TYPE_SHEETS_APPEND_VALUES,
                retryable=False,
            ),
            "sheets_batch_update": _edge(
                "sheets_batch_update",
                RESULT_TYPE_SHEETS_BATCH_UPDATE,
                retryable=False,
            ),
            "sheets_create_from_files": _edge(
                "sheets_create_from_files",
                RESULT_TYPE_SHEETS_CREATE_FROM_FILES,
                retryable=False,
            ),
            "sheets_export": _edge(
                "sheets_export",
                RESULT_TYPE_SHEETS_EXPORT,
                retryable=True,
            ),
            "docs_create": _edge(
                "docs_create",
                RESULT_TYPE_DOCS_CREATE,
                retryable=False,
            ),
            "docs_get": _edge(
                "docs_get",
                RESULT_TYPE_DOCS_GET,
                retryable=True,
            ),
            "docs_batch_update": _edge(
                "docs_batch_update",
                RESULT_TYPE_DOCS_BATCH_UPDATE,
                retryable=False,
            ),
            "docs_export": _edge(
                "docs_export",
                RESULT_TYPE_DOCS_EXPORT,
                retryable=True,
            ),
            "slides_create": _edge(
                "slides_create",
                RESULT_TYPE_SLIDES_CREATE,
                retryable=False,
            ),
            "slides_get": _edge(
                "slides_get",
                RESULT_TYPE_SLIDES_GET,
                retryable=True,
            ),
            "slides_batch_update": _edge(
                "slides_batch_update",
                RESULT_TYPE_SLIDES_BATCH_UPDATE,
                retryable=False,
            ),
            "slides_export": _edge(
                "slides_export",
                RESULT_TYPE_SLIDES_EXPORT,
                retryable=True,
            ),
        }

    # ------------------------------------------------------------------
    # @platform_process implementations — OAuth bootstrap
    # ------------------------------------------------------------------

    @platform_process(
        name="test_connection",
        display_name="Google Workspace: Test Connection",
        description=(
            "Verify the connected Workspace account with Gmail's minimal profile read. "
            "This does not list Drive files or return any third-party content."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Bounded connection verdict: account_identity and harmless_read_ok.",
            properties={
                "account_identity": ParameterMetadata(
                    type=ParameterType.STRING, description="Connected account email identity."
                ),
                "harmless_read_ok": ParameterMetadata(
                    type=ParameterType.BOOLEAN, description="True when the minimal profile read succeeded."
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def test_connection(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        def _profile() -> dict[str, Any]:
            profile = self._require_factory().gmail().users().getProfile(userId="me").execute()
            account_identity = profile.get("emailAddress")
            if not isinstance(account_identity, str) or not account_identity:
                raise ValueError("Gmail profile response did not contain emailAddress")
            return {"account_identity": account_identity, "harmless_read_ok": True}

        return self._run(_profile, "test_connection")

    @platform_process(
        name="connect_account",
        display_name="Connect Google Workspace Account",
        description=(
            "Generate the Google consent URL (PKCE) the operator opens in their browser to "
            "grant this solet access to the Workspace account. Returns authorize_url + a "
            "fresh state nonce. After the operator approves, the plugin's callback handler stores "
            "the refresh/access tokens in vault automatically. Requires the callback server "
            "(start_interface) to be running and the 'google_oauth_app' address-book entry to exist."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Authorization URL and state nonce.",
        ),
        context_handling=ContextHandling.NONE,
    )
    def connect_account(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        try:
            app_config = self._require_app_config_loader().load()
        except Exception as exc:  # noqa: BLE001 — address-book/app-config faults become a typed error
            return self._error("app_config_error", str(exc))

        request = build_authorization_request(app_config)
        self._pending_states[request.state] = request.code_verifier
        if self.logger:
            self.logger.info("Google OAuth bootstrap initiated")
        return self._success(
            {
                "authorize_url": request.authorize_url,
                "state": request.state,
                "redirect_uri": app_config.redirect_uri,
                "instructions": (
                    "Open authorize_url in your browser and approve access. The callback "
                    "will store tokens automatically."
                ),
            }
        )

    @platform_process(
        name="start_interface",
        display_name="Start Google OAuth Callback Server",
        description=(
            "Start the FastAPI/uvicorn HTTPS callback server that handles the Google OAuth "
            "redirect. Must be running before the operator completes the browser flow. In cloud "
            "deployment, the ALB routes /oauth/google/* to this port."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "host": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description=(
                    "Bind host (default: 127.0.0.1 for a local solet). "
                    "ALB-fronted cloud deployments must pass or configure "
                    "0.0.0.0 explicitly."
                ),
            ),
            "port": ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description="Preferred TCP port; auto-allocated if unset.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Server host, port, callback URL.",
        ),
        context_handling=ContextHandling.NONE,
    )
    def start_interface(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        config = self.config_provider or {}
        host = str(params.get("host") or config.get("callback_host") or "127.0.0.1")
        port_val = params.get("port") or config.get("callback_port")
        preferred_port: int | None = int(str(port_val)) if port_val is not None else None
        try:
            token_store = self._require_token_store()
            app_config_loader = self._require_app_config_loader()
        except RuntimeError as exc:
            return self._error("service_not_available", str(exc))
        try:
            port = self._oauth_server.start(
                host=host,
                preferred_port=preferred_port,
                token_store=token_store,
                app_config_loader=app_config_loader,
                pending_states=self._pending_states,
            )
        except OAuthServerStartError as exc:
            return self._error(ERROR_SERVER_START_FAILED, str(exc))
        return self._success(build_start_result(port, host))

    @platform_process(
        name="stop_interface",
        display_name="Stop Google OAuth Callback Server",
        description="Shut down the OAuth callback uvicorn server and release its port.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Stopped confirmation.",
        ),
        context_handling=ContextHandling.NONE,
    )
    def stop_interface(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        self._oauth_server.stop()
        return self._success({"stopped": True})

    # ------------------------------------------------------------------
    # @platform_process implementations — Gmail
    # ------------------------------------------------------------------

    @platform_process(
        name="gmail_list_messages",
        display_name="Gmail: List Messages",
        description=(
            "List Gmail message ids matching a Gmail search query (e.g. 'from:alice is:unread "
            f"newer_than:7d'). Defaults to {GMAIL_DEFAULT_MAX_RESULTS} results, matching Gmail's "
            f"own real single-call maximum (Google, current as of this writing) — there is no "
            "acknowledge_default_limit_override/row_limit pair on this verb, since the default "
            "already sits at the vendor's per-call ceiling (nothing for an override to raise to "
            "without building pageToken pagination across multiple calls, which this verb does not "
            "do). Returns message + thread ids only, never message content; use gmail_get_message "
            "for one message's content once you have its id. Runs as a background job: this "
            "dispatches the search and returns a job_id immediately; the matching ids arrive via a "
            "follow-up message once it completes. Requires a connected account (run connect_account "
            "first)."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "query": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="Gmail search query. Empty matches the whole mailbox.",
            ),
            "max": ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(
                    f"Messages to return (default {GMAIL_DEFAULT_MAX_RESULTS}, hard-capped at "
                    f"{GMAIL_MAX_RESULTS_CAP} — Gmail's own single-call maximum, not this "
                    "connector's choice)."
                ),
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="job_id for the queued search plus its status ('queued').",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def gmail_list_messages(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_list_messages")

    @platform_process(
        name="gmail_get_message",
        display_name="Gmail: Get Message",
        description=(
            "Fetch one Gmail message by id: key headers (from/to/cc/subject/date), the plain-text "
            "body, and attachment metadata (id/name/mime/size — not the attachment bytes). Runs as "
            "a background job: this dispatches the fetch and returns a job_id immediately; the "
            "message content arrives via a follow-up message once it completes. Requires a "
            "connected account."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The Gmail message id (from gmail_list_messages).",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="job_id for the queued fetch plus its status ('queued').",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def gmail_get_message(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_get_message")

    @platform_process(
        name="gmail_send",
        display_name="Gmail: Send Message",
        description=(
            "Send a plain-text email from the connected account. Optional 'attachments' is a list "
            "of blob ids (from blob storage) attached by filename + mime type. Runs as a background "
            "job: this dispatches the send and returns a job_id immediately; the sent message id + "
            "thread id arrive via a follow-up message once delivery completes. Requires a connected "
            "account."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "to": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="Recipient email address.",
            ),
            "subject": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="Message subject line.",
            ),
            "body": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="Plain-text message body.",
            ),
            "attachments": ParameterMetadata(
                type=ParameterType.LIST,
                required=False,
                description="Optional list of blob ids to attach.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="job_id for the queued send plus its status ('queued').",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def gmail_send(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "gmail_send")

    # ------------------------------------------------------------------
    # @platform_process implementations — Drive
    # ------------------------------------------------------------------

    @platform_process(
        name="drive_list_files",
        display_name="Drive: List Files",
        description=(
            "List Google Drive files matching a Drive search query (for example "
            "\"name contains 'budget' and mimeType='application/pdf'\"), newest first. Returns "
            f"id/name/mime/modified/size per file. Defaults to {DRIVE_DEFAULT_PAGE_SIZE} files to "
            "avoid exhausting rate limits and pulling more than needed. To fetch more, pass "
            f"acknowledge_default_limit_override=true together with an explicit row_limit (up to "
            f"{DRIVE_PAGE_SIZE_CAP}, Drive's own real single-call maximum, Google, current as of "
            "this writing) — both are required together, and a row_limit above the cap is refused "
            "rather than silently clamped. Runs as a background job: this dispatches the listing "
            "and returns a job_id immediately; the matching files arrive via a follow-up message "
            "once the listing completes. Requires a connected account."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "query": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="Drive query string. Empty lists recent files.",
            ),
            "max": ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(
                    "Request FEWER files than the current ceiling in this one call (e.g. 20) — "
                    "never widens the ceiling; raising the ceiling itself requires "
                    "acknowledge_default_limit_override + row_limit below."
                ),
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(
                    "Must be exactly true, together with row_limit, to raise the ceiling past the "
                    f"default {DRIVE_DEFAULT_PAGE_SIZE} files. Requires understanding why the "
                    "default exists: avoiding exhausted rate limits and pulling more than the "
                    "task needs."
                ),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(
                    f"Explicit ceiling, up to {DRIVE_PAGE_SIZE_CAP}. Only honored "
                    f"together with acknowledge_default_limit_override=true; refused (not "
                    f"clamped) above {DRIVE_PAGE_SIZE_CAP}."
                ),
            ),
        },
        is_async=True,
        is_long_running=True,
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="job_id for the queued listing plus its status ('queued').",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_list_files(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_list_files")

    @platform_process(
        name="drive_download_file",
        display_name="Drive: Download File",
        description=(
            "Download a binary Drive file (PDF, image, zip, etc.) into blob storage. Runs as a "
            "background job: this dispatches the download and returns a job_id immediately; the "
            "file_blob_key, name, and mime arrive via a follow-up message once the download "
            "completes. Google-native docs (Docs/Sheets/Slides) cannot be downloaded — use the "
            "matching export verb instead. Requires a connected account."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The Drive file id (from drive_list_files).",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="job_id for the queued download plus its status ('queued').",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def drive_download_file(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_download_file")

    @platform_process(
        name="drive_upload_file",
        display_name="Drive: Upload File",
        description=(
            "Upload bytes to Drive from a blob (blob_key). Content comes from blob storage only — "
            "to upload a local file, ingest it via blob_storage_service.store_blob_from_file first "
            "and pass the resulting blob_key. Optional parent folder id and mime override. Runs as "
            "a background job: this dispatches the upload and returns a job_id immediately; the "
            "new file's id and web view link arrive via a follow-up message once the upload "
            "completes. Requires a connected account."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "name": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="Display name for the uploaded Drive file.",
            ),
            "blob_key": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="Blob storage id whose bytes to upload.",
            ),
            "parent": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="Parent Drive folder id. Omit to upload into the account's root.",
            ),
            "mime": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="Mime type override; inferred from the source when omitted.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="job_id for the queued upload plus its status ('queued').",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_upload_file(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_upload_file")

    @platform_process(
        name="drive_create_folder",
        display_name="Drive: Create Folder",
        description=(
            "Create a Drive folder, optionally nested under a parent folder id. Runs as a "
            "background job: this dispatches the creation and returns a job_id immediately; the "
            "new folder's id arrives via a follow-up message once creation completes. Requires a "
            "connected account."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "name": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="Name of the new folder.",
            ),
            "parent": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="Parent Drive folder id. Omit to create at the account's root.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="job_id for the queued folder creation plus its status ('queued').",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_create_folder(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_create_folder")

    @platform_process(
        name="drive_share",
        display_name="Drive: Share File",
        description=(
            "Share a Drive file or folder with an email address at a given permission role "
            "(reader, commenter, writer). No notification email is sent. Runs as a background "
            "job: this dispatches the share and returns a job_id immediately; confirmation and the "
            "new permission id arrive via a follow-up message once it completes. Requires a "
            "connected account."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The Drive file or folder id to share.",
            ),
            "email": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="Email address to grant access to.",
            ),
            "role": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="One of 'reader', 'commenter', 'writer'.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="job_id for the queued share plus its status ('queued').",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def drive_share(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "drive_share")

    # ------------------------------------------------------------------
    # @platform_process implementations — Sheets
    # ------------------------------------------------------------------

    @platform_process(
        name="sheets_create",
        display_name="Sheets: Create Spreadsheet",
        description=(
            "Create a new spreadsheet with the given title. Runs as a background job: this "
            "dispatches the creation and returns a job_id immediately; the new spreadsheet's id "
            "arrives via a follow-up message once creation completes. Requires a connected "
            "account."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "title": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="Title of the new spreadsheet.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="job_id for the queued spreadsheet creation plus its status ('queued').",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def sheets_create(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "sheets_create")

    @platform_process(
        name="sheets_create_from_files",
        display_name="Sheets: Create Spreadsheet From Files",
        description=(
            "Create a new spreadsheet with one tab per entry, each tab's values loaded from a "
            "local .csv or .tsv file (delimiter from extension). Runs as a background job: this "
            "dispatches the creation and returns a job_id immediately; the new spreadsheet's id, "
            "per-tab {name, sheet_id} pairs, and total cells written arrive via a follow-up "
            "message once it completes. Requires a connected account."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "title": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="Title of the new spreadsheet.",
            ),
            "tabs": ParameterMetadata(
                type=ParameterType.LIST,
                required=True,
                description=(
                    "Non-empty list of {name, file_path} dicts — one tab per entry, in order. "
                    "name is the tab title (must be unique); file_path is an absolute path to a "
                    ".csv or .tsv file whose rows become the tab's values."
                ),
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="job_id for the queued spreadsheet creation plus its status ('queued').",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def sheets_create_from_files(
        self, params: dict[str, Any], state: dict[str, Any]
    ) -> dict[str, Any]:
        return self._run_deferred(params, state, "sheets_create_from_files")

    @platform_process(
        name="sheets_get_values",
        display_name="Sheets: Get Values",
        description=(
            "Read a cell range (A1 notation, e.g. 'Sheet1!A1:C10') as a 2D grid of values. "
            f"Defaults to a {SHEETS_DEFAULT_ROW_LIMIT}-row effective limit to avoid an unbounded "
            "grid landing in context — Sheets' values.get has no server-side size parameter, so "
            "this is enforced AFTER the fetch: a range returning more rows than the effective "
            "limit is refused loud (gsuite.result_too_large), never silently truncated, and the "
            "underlying vendor call itself is NOT narrowed by this limit — request a smaller A1 "
            "range if the goal is a smaller call. To raise the limit, pass "
            "acknowledge_default_limit_override=true together with an explicit row_limit (up to "
            f"{SHEETS_ROW_LIMIT_CAP}) — both are required together, and a row_limit above the cap "
            "is refused rather than silently clamped. Runs as a background job: this dispatches "
            "the read and returns a job_id immediately; the grid arrives via a follow-up message "
            "once the read completes. Requires a connected account."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The spreadsheet id.",
            ),
            "range": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="A1-notation range, e.g. 'Sheet1!A1:C10'.",
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(
                    "Must be exactly true, together with row_limit, to raise the effective "
                    f"{SHEETS_DEFAULT_ROW_LIMIT}-row limit. Requires understanding why the "
                    "default exists: avoiding an unbounded grid landing in context."
                ),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(
                    f"Explicit row-count ceiling, up to {SHEETS_ROW_LIMIT_CAP}. Only honored "
                    f"together with acknowledge_default_limit_override=true; refused (not "
                    f"clamped) above {SHEETS_ROW_LIMIT_CAP}."
                ),
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="job_id for the queued read plus its status ('queued').",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def sheets_get_values(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "sheets_get_values")

    @platform_process(
        name="sheets_update_values",
        display_name="Sheets: Update Values",
        description=(
            "Overwrite a cell range (A1 notation) with a 2D grid of values. Runs as a background "
            "job: this dispatches the update and returns a job_id immediately; the count of "
            "cells updated arrives via a follow-up message once it completes. Requires a "
            "connected account."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The spreadsheet id.",
            ),
            "range": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="A1-notation range to overwrite, e.g. 'Sheet1!A1:C10'.",
            ),
            "values": ParameterMetadata(
                type=ParameterType.LIST,
                required=True,
                description="2D grid of cell values (a list of row lists).",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="job_id for the queued update plus its status ('queued').",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def sheets_update_values(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "sheets_update_values")

    @platform_process(
        name="sheets_append_values",
        display_name="Sheets: Append Values",
        description=(
            "Append a 2D grid of values after the last row of a range's table. Runs as a "
            "background job: this dispatches the append and returns a job_id immediately; the "
            "count of cells updated arrives via a follow-up message once it completes. Requires "
            "a connected account."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The spreadsheet id.",
            ),
            "range": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="A1-notation table range to append after, e.g. 'Sheet1!A1:C1'.",
            ),
            "values": ParameterMetadata(
                type=ParameterType.LIST,
                required=True,
                description="2D grid of cell values (a list of row lists) to append.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="job_id for the queued append plus its status ('queued').",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def sheets_append_values(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "sheets_append_values")

    @platform_process(
        name="sheets_batch_update",
        display_name="Sheets: Batch Update",
        description=(
            "Apply a list of raw Google Sheets API batchUpdate request objects (rename/add "
            "tabs, cell number formats, column widths, frozen rows, etc.) to a spreadsheet. Runs "
            "as a background job: this dispatches the batch update and returns a job_id "
            "immediately; the replies arrive via a follow-up message once it completes. "
            "Requires a connected account."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The spreadsheet id.",
            ),
            "requests": ParameterMetadata(
                type=ParameterType.LIST,
                required=True,
                description="Non-empty list of Sheets API batchUpdate request objects.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="job_id for the queued batch update plus its status ('queued').",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def sheets_batch_update(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "sheets_batch_update")

    @platform_process(
        name="sheets_export",
        display_name="Sheets: Export",
        description=(
            "Export a spreadsheet to csv or xlsx into blob storage and return its sheet_blob_key. "
            "Requires a connected account."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The spreadsheet id.",
            ),
            "format": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="Export format: 'csv' (default) or 'xlsx'.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="sheet_blob_key + namespace + filename referencing the exported bytes.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def sheets_export(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            lambda: sheets_actions.export_spreadsheet(
                self._require_factory().drive(), params, self._store_blob
            ),
            "sheets_export",
        )

    # ------------------------------------------------------------------
    # @platform_process implementations — Docs
    # ------------------------------------------------------------------

    @platform_process(
        name="docs_create",
        display_name="Docs: Create Document",
        description=(
            "Create a new document with the given title and optional initial plain-text content. "
            "Runs as a background job: this dispatches the creation and returns a job_id "
            "immediately; the new document's id arrives via a follow-up message once creation "
            "completes. Requires a connected account."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "title": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="Title of the new document.",
            ),
            "content": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="Optional plain-text content inserted at the start of the document.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="job_id for the queued document creation plus its status ('queued').",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def docs_create(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "docs_create")

    @platform_process(
        name="docs_get",
        display_name="Docs: Get Document",
        description=(
            "Fetch a document's title and plain-text body content. Runs as a background job: this "
            "dispatches the fetch and returns a job_id immediately; the title + body arrive via a "
            "follow-up message once it completes. Requires a connected account."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The document id.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="job_id for the queued fetch plus its status ('queued').",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def docs_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "docs_get")

    @platform_process(
        name="docs_batch_update",
        display_name="Docs: Batch Update",
        description=(
            "Apply a list of raw Google Docs API batchUpdate request objects (insert/replace "
            "text, formatting, etc.) to a document. Runs as a background job: this dispatches the "
            "batch update and returns a job_id immediately; the replies arrive via a follow-up "
            "message once it completes. Requires a connected account."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The document id.",
            ),
            "requests": ParameterMetadata(
                type=ParameterType.LIST,
                required=True,
                description="Non-empty list of Docs API batchUpdate request objects.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="job_id for the queued batch update plus its status ('queued').",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def docs_batch_update(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "docs_batch_update")

    @platform_process(
        name="docs_export",
        display_name="Docs: Export",
        description=(
            "Export a document to pdf, docx, or txt into blob storage and return its "
            "doc_blob_key. Requires a connected account."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The document id.",
            ),
            "format": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="Export format: 'pdf' (default), 'docx', or 'txt'.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="doc_blob_key + namespace + filename referencing the exported bytes.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def docs_export(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            lambda: docs_actions.export_document(
                self._require_factory().drive(), params, self._store_blob
            ),
            "docs_export",
        )

    # ------------------------------------------------------------------
    # @platform_process implementations — Slides
    # ------------------------------------------------------------------

    @platform_process(
        name="slides_create",
        display_name="Slides: Create Presentation",
        description=(
            "Create a new presentation with the given title. Runs as a background job: this "
            "dispatches the creation and returns a job_id immediately; the new presentation's id "
            "arrives via a follow-up message once creation completes. Requires a connected "
            "account."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "title": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="Title of the new presentation.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="job_id for the queued presentation creation plus its status ('queued').",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def slides_create(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "slides_create")

    @platform_process(
        name="slides_get",
        display_name="Slides: Get Presentation",
        description=(
            "Fetch a presentation's slide list — object id and page-element count per slide. "
            "Runs as a background job: this dispatches the fetch and returns a job_id "
            "immediately; the slide list arrives via a follow-up message once it completes. "
            "Requires a connected account."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The presentation id.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="job_id for the queued fetch plus its status ('queued').",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def slides_get(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "slides_get")

    @platform_process(
        name="slides_batch_update",
        display_name="Slides: Batch Update",
        description=(
            "Apply a list of raw Google Slides API batchUpdate request objects (add slide, "
            "insert text/image, etc.) to a presentation. Runs as a background job: this "
            "dispatches the batch update and returns a job_id immediately; the replies arrive "
            "via a follow-up message once it completes. Requires a connected account."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={
            "id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The presentation id.",
            ),
            "requests": ParameterMetadata(
                type=ParameterType.LIST,
                required=True,
                description="Non-empty list of Slides API batchUpdate request objects.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="job_id for the queued batch update plus its status ('queued').",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def slides_batch_update(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_deferred(params, state, "slides_batch_update")

    @platform_process(
        name="slides_export",
        display_name="Slides: Export",
        description=(
            "Export a presentation to pdf or pptx into blob storage and return its "
            "deck_blob_key. Requires a connected account."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The presentation id.",
            ),
            "format": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="Export format: 'pdf' (default) or 'pptx'.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="deck_blob_key + namespace + filename referencing the exported bytes.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def slides_export(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            lambda: slides_actions.export_presentation(
                self._require_factory().drive(), params, self._store_blob
            ),
            "slides_export",
        )


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


def _classify_http_error(exc: HttpError) -> tuple[str, str]:
    status = getattr(getattr(exc, "resp", None), "status", None)
    status_code = getattr(exc, "status_code", None)
    code_value = status_code if isinstance(status_code, int) else status
    if code_value == 403:
        return ERROR_PERMISSION_DENIED, f"Google denied access (403): {exc}"
    if code_value == 404:
        return ERROR_NOT_FOUND, f"Resource not found (404): {exc}"
    if code_value == 429:
        return ERROR_RATE_LIMITED, f"Google rate limit hit (429): {exc}"
    return ERROR_API_ERROR, f"Google API error ({code_value}): {exc}"


def _classify_deferred_exception(exc: Exception) -> tuple[str, str]:
    """Map a background job's raised exception to (error_code, message).

    Same taxonomy _run() used inline before the D0.3 migration — moved to a
    module function so a background job failure classifies identically to a
    synchronous one, and so _process_job's own branching stays low.
    """
    if isinstance(exc, ValueError):
        return ERROR_INVALID_PARAMS, str(exc)
    if isinstance(exc, (TokenStoreError, sheets_actions.ResultTooLargeError)):
        return exc.code, str(exc)
    if isinstance(exc, HttpError):
        return _classify_http_error(exc)
    return ERROR_API_ERROR, f"Google API call failed: {exc}"
