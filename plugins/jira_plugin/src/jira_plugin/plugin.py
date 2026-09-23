"""Jira Cloud connector plugin entry point.

Headless HTTP basic-auth (service-account email + API token, chain-consumed from
the ``jira_site`` address-book entry) against a Jira Cloud site — no OAuth, no
browser flow, no callback server. Full read/write including delete_issue (ticket
deletion is an acceptable-loss class, RATIFY-2).

Verbs (all EDGE):
  - jql_search / get_issue                          — read
  - create_issue / update_issue / delete_issue      — write
  - add_comment / list_comments                     — comment
  - list_transitions / transition_issue             — workflow
  - download_attachment / add_attachment            — attachments (blob-bridged)
  - test_connection                                 — diagnostic

Security posture (umbrella design §2; process_export deny retired by operator
ruling 2026-07-15 — see
workbench/2026-07-15_result_error_processing_architecture_deep_dive.md): every
verb is directly process_call-able like any other process;
connection/auth/permission error messages are GENERIC fixed strings —
never the driver exception, which would leak the site host (§1.6); attachment
ingest is blob-only (§2.1); the plugin reaches ONLY the address-book-resolved
site (no site/base_url param on any verb).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
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
from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.edge_process_provider import (
    EdgeProcessDefinition,
    EdgeProcessProvider,
)
from jira import JIRA, JIRAError

from . import async_jobs
from .app_config import AppConfigError, AppConfigLoader
from .attachment_actions import OutgoingAttachment, add_attachment, download_attachment
from .client import JiraClientFactory
from .constants import (
    BLOB_NAMESPACE,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_ROW_LIMIT,
    DEFAULT_TOKEN_EXPIRY_WARN_DAYS,
    ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
    ERROR_API_ERROR,
    ERROR_BLOB_STORAGE_NOT_AVAILABLE,
    ERROR_INVALID_PARAMS,
    ERROR_NOT_CONFIGURED,
    MAX_INTERNAL_CALLS,
    PARAM_ACKNOWLEDGE_OVERRIDE,
    PARAM_ROW_LIMIT,
    PLUGIN_NAME,
    RESULT_TYPE_ADD_ATTACHMENT,
    RESULT_TYPE_ADD_COMMENT,
    RESULT_TYPE_CREATE_ISSUE,
    RESULT_TYPE_DELETE_ISSUE,
    RESULT_TYPE_DOWNLOAD_ATTACHMENT,
    RESULT_TYPE_GET_ISSUE,
    RESULT_TYPE_JQL_SEARCH,
    RESULT_TYPE_LIST_COMMENTS,
    RESULT_TYPE_LIST_TRANSITIONS,
    RESULT_TYPE_TEST_CONNECTION,
    RESULT_TYPE_TRANSITION_ISSUE,
    RESULT_TYPE_UPDATE_ISSUE,
    ROW_LIMIT_CAP,
)
from .errors import JiraServiceError, classify_jira_error

# Field sensitivities (RATIFY-8: SaaS records/rows 0.3; free-text
# description/body/comments 0.6; attachment/export blob keys 0.3; ids/keys/counts
# /flags 0.0; metadata lists 0.1). Every returned *_blob_key MUST appear in its
# verb's tuple (the edge_process_mismatch FATAL, Authoring Traps §3).
# WORKED BLOB EXAMPLE: download_attachment returns attachment_blob_key (0.3 SaaS).


class JiraPlugin(PluginBase, EdgeProcessProvider):
    """Jira Cloud (issues / JQL / comments / transitions / attachments) plugin."""

    name: str = PLUGIN_NAME

    def __init__(self) -> None:
        super().__init__()
        self.logger: logging.Logger | None = None
        self._address_book_service: Any | None = None
        self._blob_storage_service: Any | None = None
        self._app_config_loader: AppConfigLoader | None = None
        self._client_factory: JiraClientFactory | None = None
        # D0.3 deferred-completion machinery (async_jobs.py) — lazily acquired /
        # lazily started, same reasoning as external_postgres_plugin's sibling
        # attrs: orchestrator_ref.async_job_manager is not guaranteed set at
        # prepare_for_readiness, and the ONE worker thread starts on first
        # dispatch, not at boot.
        self._async_job_manager: Any | None = None
        self._worker_thread: threading.Thread | None = None
        self._worker_lock = threading.Lock()

    # ------------------------------------------------------------------
    # VaultKeysProvider — no plugin-owned vault keys
    # ------------------------------------------------------------------

    def get_required_vault_keys(self) -> list[str]:
        """No vault keys are required at readiness.

        The ``api_token`` is chain-consumed through the ``jira_site`` address-book
        entry (never a direct vault verb under this plugin's identity), so it
        lives in the resolver's namespace and is declared nowhere here. This
        plugin holds no plugin-owned runtime vault keys.
        """
        return []

    def get_declared_vault_keys(self) -> list[str]:
        """No scoped vault keys are read or written directly by this plugin.

        The only secret (``api_token``) is chain-consumed via the address book —
        see constants.VAULT_KEY_API_TOKEN.
        """
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
                "address_book_service to resolve the jira_site credentials"
            )
        # blob_storage is needed only for attachment + JQL-export verbs and is
        # resolved lazily at first use (_blob_service): the platform constructs
        # blob_storage_service in the init_service_manager startup step, AFTER
        # every plugin's prepare_for_readiness — resolving it here caches None
        # forever and every export hard-fails (field-verified on a live
        # deployment).
        self._app_config_loader = AppConfigLoader(self._address_book_service)
        config = self.config_provider or {}
        self._client_factory = JiraClientFactory(
            self._app_config_loader,
            self.logger,
            warn_days=_int_config(config, "token_expiry_warn_days", DEFAULT_TOKEN_EXPIRY_WARN_DAYS),
            request_timeout=_float_config(
                config, "request_timeout_seconds", DEFAULT_REQUEST_TIMEOUT_SECONDS
            ),
        )
        self.set_ready()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_client(self) -> JIRA:
        if self._client_factory is None:
            raise RuntimeError(ERROR_ADDRESS_BOOK_NOT_AVAILABLE)
        return self._client_factory.client()

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
        """Resolve a blob id to bytes for an outgoing Jira attachment."""
        if self._blob_storage_service is None:
            raise JiraServiceError(
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
        """Store an outgoing attachment's bytes as a blob; return the blob id (the *_blob_key).

        Attachments only — jql_search/list_comments return inline (§5.4, jira
        exited the data-export requirement); this is the plugin's only blob-write path.
        """
        blob_service = self._blob_service()
        if blob_service is None:
            raise JiraServiceError(
                ERROR_BLOB_STORAGE_NOT_AVAILABLE,
                f"blob_storage_service is not available for blob storage: cannot store "
                f"'{filename}' ({len(content)} bytes)",
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
        """Shared error-classification path for every Jira verb.

        The thunk builds/uses the client (inside the try) and returns the result
        dict, so a config-resolution or connection fault surfaces as a typed
        error, not a crash. Connection/auth/permission classes return GENERIC
        messages (classify_jira_error / the catch-all below) — never str(exc),
        which would leak the site host (§1.6/§2.4).
        """
        try:
            data = produce()
        except ValueError as exc:
            return self._error(ERROR_INVALID_PARAMS, str(exc))
        except AppConfigError as exc:
            return self._error(ERROR_NOT_CONFIGURED, str(exc))
        except JiraServiceError as exc:
            return self._error(exc.code, str(exc))
        except JIRAError as exc:
            code, message = classify_jira_error(exc)
            return self._error(code, message)
        except Exception as exc:  # noqa: BLE001 — surface any Jira/client fault as a typed error
            if self.logger:
                self.logger.warning("%s: unexpected fault (%s)", endpoint_name, type(exc).__name__)
            return self._error(ERROR_API_ERROR, "Jira API call failed.")
        if self.logger:
            self.logger.debug("%s: success", endpoint_name)
        return self._success(data)

    def _dispatch_async(
        self, action_name: str, params: dict[str, Any], state: dict[str, Any],
    ) -> dict[str, Any]:
        """D0.3 ms-scale dispatch: create the job, return immediately — no I/O here."""
        try:
            create_result = async_jobs.create_job(
                self, action_name=action_name, params=params, state=state,
            )
        except ValueError as exc:
            return self._error(ERROR_INVALID_PARAMS, str(exc))
        except RuntimeError as exc:
            return self._error(ERROR_NOT_CONFIGURED, str(exc))
        if create_result.get("action_status") != "completed":
            error = create_result.get("error", {})
            message = str(error.get("message", "failed to create async job"))
            return self._error(ERROR_API_ERROR, message)
        return self._success(create_result["data"])

    # ------------------------------------------------------------------
    # EdgeProcessProvider
    # ------------------------------------------------------------------

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {
            "jql_search": _edge(
                "jql_search", RESULT_TYPE_JQL_SEARCH, retryable=True
            ),
            "get_issue": _edge(
                "get_issue", RESULT_TYPE_GET_ISSUE, retryable=True
            ),
            "create_issue": _edge(
                "create_issue",
                RESULT_TYPE_CREATE_ISSUE,
                retryable=False,
            ),
            "update_issue": _edge(
                "update_issue",
                RESULT_TYPE_UPDATE_ISSUE,
                retryable=False,
            ),
            "delete_issue": _edge(
                "delete_issue",
                RESULT_TYPE_DELETE_ISSUE,
                retryable=False,
            ),
            "add_comment": _edge(
                "add_comment",
                RESULT_TYPE_ADD_COMMENT,
                retryable=False,
            ),
            "list_comments": _edge(
                "list_comments",
                RESULT_TYPE_LIST_COMMENTS,
                retryable=True,
            ),
            "list_transitions": _edge(
                "list_transitions",
                RESULT_TYPE_LIST_TRANSITIONS,
                retryable=True,
            ),
            "transition_issue": _edge(
                "transition_issue",
                RESULT_TYPE_TRANSITION_ISSUE,
                retryable=False,
            ),
            "download_attachment": _edge(
                "download_attachment",
                RESULT_TYPE_DOWNLOAD_ATTACHMENT,
                retryable=True,
            ),
            "add_attachment": _edge(
                "add_attachment",
                RESULT_TYPE_ADD_ATTACHMENT,
                retryable=False,
            ),
            "test_connection": _edge(
                "test_connection",
                RESULT_TYPE_TEST_CONNECTION,
                retryable=True,
            ),
        }

    # ------------------------------------------------------------------
    # @platform_process implementations — issues
    # ------------------------------------------------------------------

    @platform_process(
        name="jql_search",
        display_name="Jira: JQL Search",
        description=(
            "Search issues with a JQL query (e.g. \"project = PROJ AND status = 'In Progress' "
            "ORDER BY updated DESC\"). Returns immediately with a job_id and status 'queued' "
            "(D0.3 deferred-completion shape) — the dispatch returning is NOT the same as the "
            "search finishing. When the job completes, trimmed rows "
            "(key/summary/status/assignee/updated) are delivered INLINE via the completion "
            f"continuation, one complete result — up to {DEFAULT_ROW_LIMIT} issues by default. "
            "Jira carries no PII risk (operator ruling: company-internal accounts only), so this "
            "verb never exports to a file — the completed job's own result payload IS the data, "
            "not a blob key. Atlassian's /search/jql endpoint caps a single HTTP call at "
            "100 issues; within the effective row limit the background job pages internally "
            "across that ceiling and hands back one result — there is no caller-visible "
            "continuation token. "
            f"At the default {DEFAULT_ROW_LIMIT}-row limit that is up to 5 sequential internal "
            f"HTTP calls; at the {ROW_LIMIT_CAP}-row override cap it is up to 50 — each call is a "
            "real round-trip, so a large row_limit has real latency cost, paid by the background "
            "job rather than the dispatch call. To raise the limit past "
            f"{DEFAULT_ROW_LIMIT}, pass acknowledge_default_limit_override=true together with "
            f"row_limit (up to {ROW_LIMIT_CAP}; above that is refused, never silently clamped). "
            f"A defense-in-depth circuit breaker bounds the internal loop at "
            f"{MAX_INTERNAL_CALLS} calls regardless of row_limit, well above the 50 calls today's "
            "cap requires. Optional max_results narrows how many rows the completed job returns, "
            "at or below the effective limit — it never widens it. If the vendor genuinely has "
            "more matches than the effective limit, truncated is true and total is an "
            "approximate count (narrow the JQL to see the rest — there is no resume token). "
            "Optional fields restricts which ADDITIONAL columns are fetched beyond the fixed "
            "render set — prefer requesting stable ID/status fields over free-text fields when "
            "the goal is validation rather than content inspection."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "jql": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The JQL query string.",
            ),
            "max_results": ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(
                    "Issues to return in THIS call, at or below the effective row limit "
                    "(default: the effective limit itself). Narrows only — never widens; "
                    "widening the limit requires acknowledge_default_limit_override + row_limit."
                ),
            ),
            "fields": ParameterMetadata(
                type=ParameterType.LIST,
                required=False,
                description=(
                    "Optional list of ADDITIONAL issue field names to fetch. The standard "
                    "render set (summary/status/assignee/updated) is always fetched too, so "
                    "the returned rows keep their fixed trimmed shape and are never hollow. "
                    "Prefer stable ID/status fields over email addresses or other PII-bearing "
                    "fields when the goal is validation."
                ),
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(
                    f"Must be true, together with row_limit, to raise the effective row limit "
                    f"above the {DEFAULT_ROW_LIMIT}-row default. Given alone (without row_limit) "
                    "is refused."
                ),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(
                    f"The raised effective row limit, up to {ROW_LIMIT_CAP}. Must be given "
                    f"together with acknowledge_default_limit_override=true. Above "
                    f"{ROW_LIMIT_CAP} is refused, never silently clamped."
                ),
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the issue rows themselves.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def jql_search(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("jql_search", params, state)

    @platform_process(
        name="get_issue",
        display_name="Jira: Get Issue",
        description=(
            "Fetch one issue by key: summary, description (plain text), status, assignee, "
            "reporter, labels, and attachment metadata (id/filename/mime/size — not the bytes; "
            "use download_attachment for those). Returns immediately with a job_id and status "
            "'queued' (D0.3 deferred-completion shape) — the dispatch returning is NOT the same "
            "as the fetch finishing; the issue detail is delivered when the job completes. "
            "Single-record reads like this stay inline (2026-08-02 operator ruling: "
            "validation-shaped single-item reads are not the mass-exposure risk the business-data "
            "limits migration targets). When the goal is only confirming an issue exists or "
            "checking its status, jql_search's trimmed rows (key/status) may already answer that "
            "without fetching the full description and people fields."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "key": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The issue key, e.g. 'PROJ-123'.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the issue detail itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def get_issue(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("get_issue", params, state)

    @platform_process(
        name="create_issue",
        display_name="Jira: Create Issue",
        description=(
            "Create an issue in a project of a given type with a summary and optional plain-text "
            "description. Optional 'fields' is an object of additional Jira fields (priority, "
            "labels, custom fields, ...). Returns immediately with a job_id and status 'queued' "
            "(D0.3 deferred-completion shape) — the dispatch returning is NOT the same as the "
            "issue being created; the new issue's key and id are delivered when the job "
            "completes. This is a write action."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "project": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="Project key, e.g. 'PROJ'.",
            ),
            "issue_type": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="Issue type name, e.g. 'Task', 'Bug', 'Story'.",
            ),
            "summary": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="One-line issue summary.",
            ),
            "description": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="Optional plain-text issue description.",
            ),
            "fields": ParameterMetadata(
                type=ParameterType.OBJECT,
                required=False,
                description="Optional additional Jira fields, merged over the core three.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the new issue's key/id.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def create_issue(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("create_issue", params, state)

    @platform_process(
        name="update_issue",
        display_name="Jira: Update Issue",
        description=(
            "Apply a non-empty 'fields' object (summary, description, assignee, labels, custom "
            "fields, ...) to an existing issue by key. Returns immediately with a job_id and "
            "status 'queued' (D0.3 deferred-completion shape) — the dispatch returning is NOT "
            "the same as the update being applied; the confirmation is delivered when the job "
            "completes. This is a write action."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "key": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The issue key, e.g. 'PROJ-123'.",
            ),
            "fields": ParameterMetadata(
                type=ParameterType.OBJECT,
                required=True,
                description="Non-empty object of Jira fields to set.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the update confirmation itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def update_issue(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("update_issue", params, state)

    @platform_process(
        name="delete_issue",
        display_name="Jira: Delete Issue",
        description=(
            "Permanently delete an issue by key. This is a destructive write action taking an "
            "EXPLICIT target — the issue key is required and there is no bulk form. Returns "
            "immediately with a job_id and status 'queued' (D0.3 deferred-completion shape) — "
            "the dispatch returning is NOT the same as the delete being applied; the "
            "confirmation is delivered when the job completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "key": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The issue key to delete, e.g. 'PROJ-123'.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the delete confirmation itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def delete_issue(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("delete_issue", params, state)

    # ------------------------------------------------------------------
    # @platform_process implementations — comments + transitions
    # ------------------------------------------------------------------

    @platform_process(
        name="add_comment",
        display_name="Jira: Add Comment",
        description=(
            "Add a plain-text comment to an issue. Returns immediately with a job_id and status "
            "'queued' (D0.3 deferred-completion shape) — the dispatch returning is NOT the same "
            "as the comment being posted; the new comment id is delivered when the job "
            "completes. Write action."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "key": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The issue key, e.g. 'PROJ-123'.",
            ),
            "body": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="Plain-text comment body.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the new comment id itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def add_comment(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("add_comment", params, state)

    @platform_process(
        name="list_comments",
        display_name="Jira: List Comments",
        description=(
            "List an issue's comments (id/author/body/created), most recent last. Returns "
            "immediately with a job_id and status 'queued' (D0.3 deferred-completion shape) — "
            "the dispatch returning is NOT the same as the list finishing. When the job "
            "completes, comment rows are delivered INLINE, one complete result — up to "
            f"{DEFAULT_ROW_LIMIT} comments by default. Jira carries no PII risk (operator "
            "ruling: company-internal accounts only), so this verb never exports to a file. "
            "Atlassian caps a single HTTP call at 100 comments; within the effective row limit "
            "the background job pages internally by offset across that ceiling and hands back "
            "one result — there is no caller-visible offset or continuation token. "
            f"At the default {DEFAULT_ROW_LIMIT}-row limit that is up to 5 sequential internal "
            f"HTTP calls; at the {ROW_LIMIT_CAP}-row override cap it is up to 50 — each call is a "
            "real round-trip, so a large row_limit has real latency cost, paid by the background "
            "job rather than the dispatch call. A defense-in-depth "
            f"circuit breaker bounds the internal loop at {MAX_INTERNAL_CALLS} calls regardless "
            "of row_limit, well above the 50 calls today's cap requires. To raise the limit past "
            f"{DEFAULT_ROW_LIMIT}, pass acknowledge_default_limit_override=true together with "
            f"row_limit (up to {ROW_LIMIT_CAP}; above that is refused, never silently clamped). "
            "Optional max narrows how many comments the completed job returns, at or below the "
            "effective limit — it never widens it. If the issue genuinely has more comments than "
            "the effective limit, truncated is true (narrow the pull or raise the limit to see "
            "the rest — there is no resume token)."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "key": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The issue key, e.g. 'PROJ-123'.",
            ),
            "max": ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(
                    "Comments to return in THIS call, at or below the effective row limit "
                    "(default: the effective limit itself). Narrows only — never widens; "
                    "widening the limit requires acknowledge_default_limit_override + row_limit."
                ),
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(
                    f"Must be true, together with row_limit, to raise the effective row limit "
                    f"above the {DEFAULT_ROW_LIMIT}-row default. Given alone (without row_limit) "
                    "is refused."
                ),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(
                    f"The raised effective row limit, up to {ROW_LIMIT_CAP}. Must be given "
                    f"together with acknowledge_default_limit_override=true. Above "
                    f"{ROW_LIMIT_CAP} is refused, never silently clamped."
                ),
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the comment rows themselves.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_comments(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("list_comments", params, state)

    @platform_process(
        name="list_transitions",
        display_name="Jira: List Transitions",
        description=(
            "List the workflow transitions available from an issue's current status "
            "(id/name/to_status). Use transition_issue with a chosen id to move the issue. "
            "Returns immediately with a job_id and status 'queued' (D0.3 deferred-completion "
            "shape) — the dispatch returning is NOT the same as the list finishing; the "
            "available transitions are delivered when the job completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "key": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The issue key, e.g. 'PROJ-123'.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the transitions list itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_transitions(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("list_transitions", params, state)

    @platform_process(
        name="transition_issue",
        display_name="Jira: Transition Issue",
        description=(
            "Move an issue through a workflow transition (by transition id from list_transitions), "
            "optionally adding a comment. Returns immediately with a job_id and status 'queued' "
            "(D0.3 deferred-completion shape) — the dispatch returning is NOT the same as the "
            "transition being applied; the new status is delivered when the job completes. This "
            "is a write action."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "key": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The issue key, e.g. 'PROJ-123'.",
            ),
            "transition_id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The transition id (from list_transitions).",
            ),
            "comment": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="Optional plain-text comment to add with the transition.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the new status itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def transition_issue(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("transition_issue", params, state)

    # ------------------------------------------------------------------
    # @platform_process implementations — attachments (blob-bridged)
    # ------------------------------------------------------------------

    @platform_process(
        name="download_attachment",
        display_name="Jira: Download Attachment",
        description=(
            "Download a Jira attachment (by attachment id, from get_issue's attachment metadata) "
            "into blob storage and return its attachment_blob_key, namespace, filename, mime, and size."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "attachment_id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The attachment id (from get_issue).",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="attachment_blob_key + namespace referencing the stored bytes, plus filename/mime/size.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def download_attachment(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            lambda: download_attachment(self._require_client(), params, self._store_blob),
            "download_attachment",
        )

    @platform_process(
        name="add_attachment",
        display_name="Jira: Add Attachment",
        description=(
            "Attach bytes from a blob (blob_key) to an issue. Content comes from blob storage "
            "ONLY — to attach a local file, ingest it via blob_storage_service.store_blob_from_file "
            "first and pass the resulting blob_key. Optional filename override. Write action."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "key": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The issue key to attach to, e.g. 'PROJ-123'.",
            ),
            "blob_key": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="Blob storage id whose bytes to attach.",
            ),
            "filename": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="Optional filename override; inferred from the blob when omitted.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="New attachment id and filename.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def add_attachment(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            lambda: add_attachment(self._require_client(), params, self._load_attachment),
            "add_attachment",
        )

    # ------------------------------------------------------------------
    # @platform_process implementations — diagnostic
    # ------------------------------------------------------------------

    @platform_process(
        name="test_connection",
        display_name="Jira: Test Connection",
        description=(
            "Verify the configured Jira credentials by fetching the authenticated account. "
            "Returns immediately with a job_id and status 'queued' (D0.3 deferred-completion "
            "shape) — the dispatch returning is NOT the same as the check finishing; ok, the "
            "site base_url, and the service account's id + display name are delivered when the "
            "job completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the connection check result itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def test_connection(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("test_connection", params, state)


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


def _int_config(config: Any, key: str, default: int) -> int:
    value = config.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _float_config(config: Any, key: str, default: float) -> float:
    value = config.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)
