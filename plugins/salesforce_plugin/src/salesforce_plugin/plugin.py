"""Salesforce plugin entry point — SOQL + record CRUD + describe.

Auth (operator-ratified 2026-07-14, full CLI delegation): every verb shells
out to the operator's `sf` CLI — the ``salesforce_org`` address-book entry
names the CLI alias (``target_org``) and the pinned ``instance_host``; the
CLI's keychain-backed refresh token is the durable credential and the
platform stores no Salesforce secret of its own, nor does any access token
ever enter this process. Full read/write including delete_record (record
deletion is an acceptable-loss class, RATIFY-2).

Verbs (all EDGE; the data/org verbs use the D0.3 deferred-completion shape —
workbench/2026-08-09_sync_verb_d03_deferred_completion_doctrine_syncverb-doctrine.md):
every dispatch handler below returns ``{"job_id", "status": "queued"}`` in
milliseconds; ``async_jobs.py``'s single background worker thread does the
real `sf` CLI subprocess spawn off the action-dispatch path and completes the
job. The dispatch handlers never touch ``SalesforceCliExecutor`` directly —
only the worker thread does, preserving the plugin's original effective
concurrency of one.
  - soql_query                                      — read (always writes output_tsv_path)
  - export_soql                                     — read (full result as a workspace TSV,
    absolute output_tsv_path contained under export_allowed_roots; refuse-all when unset)
  - get_record / describe_sobject / list_sobjects   — read
  - create_record / update_record / delete_record   — write
  - test_connection                                 — diagnostic
  - probe_cli                                       — offline local CLI diagnostic

Security posture (umbrella design §2; process_export deny retired by operator
ruling 2026-07-15 — see
workbench/2026-07-15_result_error_processing_architecture_deep_dive.md): every
verb is directly process_call-able like any other process; auth/session/
permission error messages are GENERIC fixed strings — never the driver
exception, which could leak the org host (§1.6);
the plugin reaches ONLY the address-book-resolved org (no org/domain param on
any verb). There is no retry-on-expiry mechanism — the CLI refreshes its own
credential transparently inside every invocation, so there is no mid-flight
session for this process to detect and re-mint.
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
from ananta.core.domain.enums import ActionStatus, ProcessorPolicyCategory
from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.edge_process_provider import (
    EdgeProcessDefinition,
    EdgeProcessProvider,
)

from . import async_jobs, export_containment, import_containment
from .app_config import AppConfigError, AppConfigLoader
from .client import SalesforceCliExecutor
from .constants import (
    BULK_INGEST_OPERATIONS,
    CONFIG_KEY_API_VERSION,
    CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
    CONFIG_KEY_IMPORT_ALLOWED_ROOTS,
    CONFIG_KEY_SF_CLI_PATH,
    CSV_SUFFIX,
    DEFAULT_API_VERSION,
    DEFAULT_ROW_LIMIT,
    DEFAULT_SF_CLI_PATH,
    ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
    ERROR_API_ERROR,
    ERROR_INVALID_PARAMS,
    ERROR_NOT_CONFIGURED,
    ERROR_PROVISION_CONSENT_REQUIRED,
    PARAM_ACKNOWLEDGE_OVERRIDE,
    PARAM_ROW_LIMIT,
    PLUGIN_NAME,
    RESULT_TYPE_BULK_INGEST_SUBMIT,
    RESULT_TYPE_BULK_JOB_ABORT,
    RESULT_TYPE_BULK_JOB_RESULTS,
    RESULT_TYPE_BULK_JOB_STATUS,
    RESULT_TYPE_CREATE_RECORD,
    RESULT_TYPE_DELETE_RECORD,
    RESULT_TYPE_DESCRIBE_SOBJECT,
    RESULT_TYPE_EXPORT_SOQL,
    RESULT_TYPE_GET_RECORD,
    RESULT_TYPE_LIST_SOBJECTS,
    RESULT_TYPE_PROBE_CLI,
    RESULT_TYPE_PROVISION_CLI,
    RESULT_TYPE_SOQL_QUERY,
    RESULT_TYPE_TEST_CONNECTION,
    RESULT_TYPE_UPDATE_RECORD,
    SOQL_EXPORT_ROW_CAP,
    SOQL_MAX_RECORDS_CAP,
)
from .errors import SalesforceServiceError, classify_salesforce_error


class SalesforcePlugin(PluginBase, EdgeProcessProvider):
    """Salesforce org connector (SOQL / record CRUD / describe) plugin."""

    name: str = PLUGIN_NAME

    def __init__(self) -> None:
        super().__init__()
        # PluginBase.__init__ overwrites the class-level ``name`` with the
        # CLASS name ("SalesforcePlugin"); re-assert the entry-point name or
        # config lookups + log lines carry the wrong identity (comfyui precedent).
        self.name = PLUGIN_NAME
        self.logger: logging.Logger | None = None
        self._address_book_service: Any | None = None
        self._app_config_loader: AppConfigLoader | None = None
        self._cli_executor: SalesforceCliExecutor | None = None
        # D0.3 deferred-completion machinery (async_jobs.py) — lazily acquired /
        # started on first async-shaped dispatch, mirroring
        # comfyui_image_generation_plugin's _try_acquire_job_manager (boot order
        # does not guarantee orchestrator_ref.async_job_manager is set yet at
        # prepare_for_readiness time, but it always is by first dispatch).
        self._async_job_manager: Any | None = None
        self._worker_thread: threading.Thread | None = None
        self._worker_lock = threading.Lock()

    # ------------------------------------------------------------------
    # VaultKeysProvider — no plugin-owned vault keys
    # ------------------------------------------------------------------

    def get_required_vault_keys(self) -> list[str]:
        """No vault keys are required at readiness.

        The platform stores NO Salesforce secret at all: the durable
        credential is the sf CLI's own keychain-backed refresh token, and no
        access token of any kind ever enters this process (full CLI
        delegation — every verb shells out to `sf`).
        """
        return []

    def get_declared_vault_keys(self) -> list[str]:
        """No scoped vault keys are read or written directly by this plugin."""
        return []

    # ------------------------------------------------------------------
    # PluginBase lifecycle
    # ------------------------------------------------------------------

    def prepare_for_readiness(self) -> None:
        if self.orchestrator_ref is None:
            raise RuntimeError(f"{self.name}: orchestrator_ref not injected")
        self.logger = logging.getLogger(self.name)
        self._address_book_service = self.orchestrator_ref.get_service("address_book_service")
        if self._address_book_service is None:
            raise RuntimeError(
                f"{ERROR_ADDRESS_BOOK_NOT_AVAILABLE}: {self.name} requires "
                "address_book_service to resolve the salesforce_org credentials"
            )
        self._app_config_loader = AppConfigLoader(self._address_book_service)
        config = self._load_plugin_config()
        api_version = config.get(CONFIG_KEY_API_VERSION, DEFAULT_API_VERSION)
        sf_cli_path = config.get(CONFIG_KEY_SF_CLI_PATH, DEFAULT_SF_CLI_PATH)
        self._cli_executor = SalesforceCliExecutor(
            self._app_config_loader,
            api_version=api_version if isinstance(api_version, str) and api_version else DEFAULT_API_VERSION,
            sf_cli_path=sf_cli_path if isinstance(sf_cli_path, str) and sf_cli_path else DEFAULT_SF_CLI_PATH,
        )
        self.set_ready()

    def _load_plugin_config(self) -> dict[str, object]:
        """Merged plugin config (file overrides + env + CLI) from the config manager.

        Pulled explicitly per the comfyui precedent — the injected
        ``config_provider`` hook has no caller platform-wide, so relying on
        it silently pins every knob to its default.
        """
        config_manager = getattr(self.orchestrator_ref, "config_manager", None)
        if config_manager is None:
            raise RuntimeError(f"{self.name}: config_manager not available on orchestrator")
        result = config_manager.get_plugin_config(self.name)
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_executor(self) -> SalesforceCliExecutor:
        if self._cli_executor is None:
            raise RuntimeError(ERROR_ADDRESS_BOOK_NOT_AVAILABLE)
        return self._cli_executor

    def _export_path_gate(self, output_tsv_path: str) -> str:
        """Admit an export path via workspace-root containment; return the realpath.

        Binds the operator's ``export_allowed_roots`` config (yaml default
        ``[]`` = refuse-all; no hardcoded callsite default per authoring trap
        #10) to the own-copy containment gate. A malformed config value is a
        loud config fault, never a silent admit-all or refuse-all.
        """
        raw_roots = self._load_plugin_config().get(CONFIG_KEY_EXPORT_ALLOWED_ROOTS)
        roots: list[str] = []
        if raw_roots is not None:
            if not isinstance(raw_roots, list) or not all(
                isinstance(entry, str) for entry in raw_roots
            ):
                raise SalesforceServiceError(
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

    def _csv_export_path_gate(self, output_csv_path: str) -> str:
        """Admit a bulk_job_results output path — same export_allowed_roots gate, .csv suffix."""
        raw_roots = self._load_plugin_config().get(CONFIG_KEY_EXPORT_ALLOWED_ROOTS)
        roots: list[str] = []
        if raw_roots is not None:
            if not isinstance(raw_roots, list) or not all(
                isinstance(entry, str) for entry in raw_roots
            ):
                raise SalesforceServiceError(
                    ERROR_NOT_CONFIGURED,
                    f"{CONFIG_KEY_EXPORT_ALLOWED_ROOTS} must be a list of directory "
                    "path strings",
                )
            roots = list(raw_roots)
        return export_containment.assert_export_path_allowed(
            output_csv_path,
            roots,
            config_key=CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
            plugin_name=self.name,
            required_suffix=CSV_SUFFIX,
        )

    def _import_path_gate(self, csv_path: str) -> str:
        """Admit a bulk_ingest_submit csv_path via workspace-root containment; return the realpath.

        Binds the operator's ``import_allowed_roots`` config (yaml default
        ``[]`` = refuse-all) to the read-side containment gate — the mirror
        of ``_export_path_gate`` for Bulk v2 ingest input files.
        """
        raw_roots = self._load_plugin_config().get(CONFIG_KEY_IMPORT_ALLOWED_ROOTS)
        roots: list[str] = []
        if raw_roots is not None:
            if not isinstance(raw_roots, list) or not all(
                isinstance(entry, str) for entry in raw_roots
            ):
                raise SalesforceServiceError(
                    ERROR_NOT_CONFIGURED,
                    f"{CONFIG_KEY_IMPORT_ALLOWED_ROOTS} must be a list of directory "
                    "path strings",
                )
            roots = list(raw_roots)
        return import_containment.assert_import_path_allowed(
            csv_path,
            roots,
            config_key=CONFIG_KEY_IMPORT_ALLOWED_ROOTS,
            plugin_name=self.name,
        )

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

    def _run(
        self,
        produce: Callable[[SalesforceCliExecutor], dict[str, Any]],
        endpoint_name: str,
    ) -> dict[str, Any]:
        """Shared error-classification path.

        ``produce`` takes the CLI executor and returns the result dict. No
        retry-on-expiry: the `sf` CLI refreshes its own credential
        transparently inside every invocation, so there is no mid-flight
        session for this process to detect and re-mint — a fault here is
        either a classified REST-level rejection or a CLI-level fault
        (``SalesforceServiceError``), never a stale token.
        """
        executor = self._require_executor()
        try:
            data = produce(executor)
        except ValueError as exc:
            return self._error(ERROR_INVALID_PARAMS, str(exc))
        except AppConfigError as exc:
            return self._error(ERROR_NOT_CONFIGURED, str(exc))
        except (
            SalesforceServiceError,
            export_containment.ExportPathRefusedError,
            import_containment.ImportPathRefusedError,
        ) as exc:
            return self._error(exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001 — topology-safety boundary, classified below
            return self._classify_and_log(exc, endpoint_name)
        if self.logger:
            self.logger.debug("%s: success", endpoint_name)
        return self._success(data)

    def _classify_and_log(self, exc: Exception, endpoint_name: str) -> dict[str, Any]:
        code, message = classify_salesforce_error(exc)
        if code == ERROR_API_ERROR and self.logger:
            self.logger.warning("%s: unexpected fault (%s)", endpoint_name, type(exc).__name__)
        return self._error(code, message)

    def _dispatch_async(
        self, action_name: str, params: dict[str, Any], state: dict[str, Any],
    ) -> dict[str, Any]:
        """D0.3 ms-scale dispatch: create the job, return immediately — no `sf` CLI call here."""
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
            "probe_cli": _edge("probe_cli", RESULT_TYPE_PROBE_CLI, retryable=True),
            "provision_cli": _edge(
                "provision_cli", RESULT_TYPE_PROVISION_CLI, retryable=True
            ),
            "soql_query": _edge(
                "soql_query", RESULT_TYPE_SOQL_QUERY, retryable=True
            ),
            "export_soql": _edge(
                "export_soql", RESULT_TYPE_EXPORT_SOQL, retryable=True
            ),
            "get_record": _edge(
                "get_record", RESULT_TYPE_GET_RECORD, retryable=True
            ),
            "describe_sobject": _edge(
                "describe_sobject",
                RESULT_TYPE_DESCRIBE_SOBJECT,
                retryable=True,
            ),
            "list_sobjects": _edge(
                "list_sobjects", RESULT_TYPE_LIST_SOBJECTS, retryable=True
            ),
            "create_record": _edge(
                "create_record",
                RESULT_TYPE_CREATE_RECORD,
                retryable=False,
            ),
            "update_record": _edge(
                "update_record",
                RESULT_TYPE_UPDATE_RECORD,
                retryable=False,
            ),
            "delete_record": _edge(
                "delete_record",
                RESULT_TYPE_DELETE_RECORD,
                retryable=False,
            ),
            "test_connection": _edge(
                "test_connection",
                RESULT_TYPE_TEST_CONNECTION,
                retryable=True,
            ),
            "bulk_ingest_submit": _edge(
                "bulk_ingest_submit",
                RESULT_TYPE_BULK_INGEST_SUBMIT,
                retryable=False,
            ),
            "bulk_job_status": _edge(
                "bulk_job_status",
                RESULT_TYPE_BULK_JOB_STATUS,
                retryable=True,
            ),
            "bulk_job_results": _edge(
                "bulk_job_results",
                RESULT_TYPE_BULK_JOB_RESULTS,
                retryable=True,
            ),
            "bulk_job_abort": _edge(
                "bulk_job_abort",
                RESULT_TYPE_BULK_JOB_ABORT,
                retryable=False,
            ),
        }

    # ------------------------------------------------------------------
    # @platform_process implementations
    # ------------------------------------------------------------------

    @platform_process(
        name="probe_cli",
        display_name="Salesforce: Probe CLI",
        description="Verify the local sf executable and read its version without contacting a Salesforce org.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Bounded local CLI verdict: executable_path, version, executable, and absolute binding compliance.",
            properties={
                "executable_path": ParameterMetadata(type=ParameterType.STRING, description="Resolved local sf executable path, or empty."),
                "version": ParameterMetadata(type=ParameterType.STRING, description="Local sf version string, or empty."),
                "executable": ParameterMetadata(type=ParameterType.BOOLEAN, description="True when sf --version completed successfully."),
                "configured": ParameterMetadata(type=ParameterType.BOOLEAN, description="True when sf_cli_path is an absolute path resolving to the verified executable."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def probe_cli(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": self._require_executor().probe_cli(),
            "actions": [],
            "error": None,
        }

    @platform_process(
        name="provision_cli",
        display_name="Salesforce: Provision CLI",
        description=(
            "Provision the local sf formula only after explicit system-change acknowledgement, "
            "then bind the verified absolute executable path into salesforce_plugin config. "
            "A compliant executable is never reinstalled; this does not log in to Salesforce."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "acknowledge_system_change": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=True,
                description="Must be true before this process may install sf or update its executable binding.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Provisioning verdict with no secret or Salesforce-org data.",
            properties={
                "provisioned": ParameterMetadata(type=ParameterType.BOOLEAN, description="True only when Homebrew installed sf."),
                "reason": ParameterMetadata(type=ParameterType.STRING, description="Whether sf was installed or an existing executable was bound."),
                "executable_path": ParameterMetadata(type=ParameterType.STRING, description="Verified absolute sf executable path."),
                "version": ParameterMetadata(type=ParameterType.STRING, description="Verified local sf version string."),
                "configuration_boundary": ParameterMetadata(type=ParameterType.STRING, description="States the config update boundary; no restart or reload is hidden."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def provision_cli(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """Provision and bind sf through one consent-gated, probe-first path."""

        acknowledge = params.get("acknowledge_system_change") is True
        try:
            observed = self._require_executor().provision_cli(
                acknowledge_system_change=acknowledge
            )
            executable_path = observed.get("executable_path")
            version = observed.get("version")
            if not isinstance(executable_path, str) or not executable_path.startswith("/"):
                raise RuntimeError("sf provisioner did not return an absolute executable path")
            if not isinstance(version, str) or not version:
                raise RuntimeError("sf provisioner did not return a version")
            if not acknowledge:
                raise SalesforceServiceError(
                    ERROR_PROVISION_CONSENT_REQUIRED,
                    "Explicit acknowledgement is required before the sf executable binding may change.",
                )
            self._bind_sf_cli_path(executable_path)
            return self._success(
                {
                    "provisioned": observed["provisioned"],
                    "reason": observed["reason"],
                    "executable_path": executable_path,
                    "version": version,
                    "configuration_boundary": "config_updated_and_executor_rebound_no_reload_or_restart",
                }
            )
        except SalesforceServiceError as exc:
            return self._error(exc.code, str(exc))
        except RuntimeError as exc:
            return self._error(ERROR_NOT_CONFIGURED, str(exc))

    def _bind_sf_cli_path(self, executable_path: str) -> None:
        """Persist one verified executable path and rebind this plugin immediately."""

        if self.orchestrator_ref is None or self._app_config_loader is None:
            raise RuntimeError("salesforce_plugin is not ready to update its CLI configuration")
        config_manager = getattr(self.orchestrator_ref, "config_manager", None)
        if config_manager is None or not hasattr(config_manager, "save_plugin_config"):
            raise RuntimeError("ordinary plugin config update path is unavailable")
        updated = self._load_plugin_config()
        updated[CONFIG_KEY_SF_CLI_PATH] = executable_path
        if config_manager.save_plugin_config(self.name, updated) is not True:
            raise RuntimeError("ordinary plugin config update path refused the verified sf executable")
        api_version = updated.get(CONFIG_KEY_API_VERSION, DEFAULT_API_VERSION)
        self._cli_executor = SalesforceCliExecutor(
            self._app_config_loader,
            api_version=api_version if isinstance(api_version, str) and api_version else DEFAULT_API_VERSION,
            sf_cli_path=executable_path,
        )

    @platform_process(
        name="soql_query",
        display_name="Salesforce: SOQL Query",
        description=(
            "Run a SOQL query (e.g. \"SELECT Id, Name FROM Account WHERE …\") against the "
            "configured Salesforce org. Returns immediately with a job_id and status 'queued' "
            "(D0.3 deferred-completion shape) — the dispatch returning is NOT the same as the job "
            "finishing. When the job completes, the result is ALWAYS written to the "
            "caller-supplied output_tsv_path, never returned inline — this plugin never executes "
            "Apex, so the 50,000-record Apex governor limit does not apply to it; the actual "
            "Salesforce fact for this call path (sf CLI -> jsforce REST client) is a 2,000-record "
            "REST query batch size with no vendor total ceiling, and jsforce's autoFetch already "
            f"pages past that internally. The limit below is entirely our own policy. Defaults to "
            f"{DEFAULT_ROW_LIMIT} records to avoid exhausting API request limits and disk, and to "
            "discourage pulling all records for client-side filtering that a SOQL WHERE clause "
            "should do instead. To fetch more, pass acknowledge_default_limit_override=true "
            f"together with an explicit row_limit (up to {SOQL_MAX_RECORDS_CAP}) — both are "
            f"required together, and a row_limit above {SOQL_MAX_RECORDS_CAP} is refused rather "
            f"than silently clamped. For pulls beyond {SOQL_MAX_RECORDS_CAP} records, use "
            f"export_soql instead (same override mechanism, hard cap {SOQL_EXPORT_ROW_CAP}). When "
            "the goal is validating that records exist or picking one to act on next, prefer "
            "selecting stable ID fields over email addresses or other PII-bearing fields — the "
            "SOQL SELECT list decides what comes back, so a narrower query is both cheaper and "
            "lower-exposure."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "query": ParameterMetadata(
                type=ParameterType.STRING, required=True, description="The SOQL query string."
            ),
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=(
                    "ABSOLUTE .tsv destination path, contained under an export_allowed_roots entry."
                ),
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(
                    "Must be exactly true, together with row_limit, to fetch more than the "
                    f"default {DEFAULT_ROW_LIMIT} records. Requires understanding why the default "
                    "exists: avoiding exhausted API request limits/disk, and pulling all records "
                    "to filter client-side instead of writing a proper SOQL WHERE clause."
                ),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(
                    f"Explicit record ceiling, up to {SOQL_MAX_RECORDS_CAP}. Only honored together "
                    f"with acknowledge_default_limit_override=true; refused (not clamped) above "
                    f"{SOQL_MAX_RECORDS_CAP}."
                ),
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the TSV handle itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def soql_query(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("soql_query", params, state)

    @platform_process(
        name="export_soql",
        display_name="Salesforce: Export SOQL",
        description=(
            "The N>>500 route: run a SOQL query and write the result as ONE tab-separated .tsv "
            "file at an ABSOLUTE output_tsv_path in the operator's workspace. Returns immediately "
            "with a job_id and status 'queued' (D0.3 deferred-completion shape) — the dispatch "
            "returning is NOT the same as the job finishing. The path must lie "
            "under an operator-configured export_allowed_roots entry (empty config refuses every "
            "export). Nested relationship objects are serialized as JSON text in their cells. Same "
            "read rules and override mechanism as soql_query, with a higher hard cap: this plugin "
            "never executes Apex, so the 50,000-record Apex governor limit does not apply; the "
            "actual Salesforce fact for this call path is a 2,000-record REST query batch size "
            "with no vendor total ceiling, and jsforce's autoFetch already pages past that "
            f"internally via the SF_ORG_MAX_QUERY_LIMIT env override. Defaults to "
            f"{DEFAULT_ROW_LIMIT} records absent an acknowledged override — for that common "
            "small/default case, soql_query has an identical interface with a lower ceiling. To "
            "fetch more, pass acknowledge_default_limit_override=true together with an explicit "
            f"row_limit (up to {SOQL_EXPORT_ROW_CAP}) — both are required together, and a "
            f"row_limit above {SOQL_EXPORT_ROW_CAP} is refused rather than silently clamped. "
            "Requires query and output_tsv_path. When the goal is validating that records exist "
            "rather than inspecting their content, prefer selecting stable ID fields over email "
            "addresses or other PII-bearing fields — the SOQL SELECT list decides what lands in "
            "the file."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "query": ParameterMetadata(
                type=ParameterType.STRING, required=True, description="The SOQL query."
            ),
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=(
                    "ABSOLUTE .tsv destination path, contained under an export_allowed_roots entry."
                ),
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(
                    "Must be exactly true, together with row_limit, to fetch more than the "
                    f"default {DEFAULT_ROW_LIMIT} records. Requires understanding why the default "
                    "exists: avoiding exhausted API request limits/disk, and pulling all records "
                    "to filter client-side instead of writing a proper SOQL WHERE clause."
                ),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(
                    f"Explicit record ceiling, up to {SOQL_EXPORT_ROW_CAP}. Only honored together "
                    f"with acknowledge_default_limit_override=true; refused (not clamped) above "
                    f"{SOQL_EXPORT_ROW_CAP}."
                ),
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the TSV handle itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def export_soql(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("export_soql", params, state)

    @platform_process(
        name="get_record",
        display_name="Salesforce: Get Record",
        description=(
            "Fetch one record by sobject + id, optionally trimmed to specific fields. Returns "
            "immediately with a job_id and status 'queued' (D0.3 deferred-completion shape) — the "
            "dispatch returning is NOT the same as the job finishing; the record's fields are "
            "delivered when the job completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "sobject": ParameterMetadata(
                type=ParameterType.STRING, required=True, description="The sobject API name, e.g. 'Account'."
            ),
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The record id."),
            "fields": ParameterMetadata(
                type=ParameterType.LIST,
                required=False,
                description="Optional list of field API names to fetch; all fields if omitted.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the record itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def get_record(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("get_record", params, state)

    @platform_process(
        name="describe_sobject",
        display_name="Salesforce: Describe SObject",
        description=(
            "Describe an sobject's fields (name/type/label/nillable/updateable, trimmed). Returns "
            "immediately with a job_id and status 'queued' (D0.3 deferred-completion shape) — the "
            "dispatch returning is NOT the same as the job finishing; the field metadata is "
            "delivered when the job completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "sobject": ParameterMetadata(
                type=ParameterType.STRING, required=True, description="The sobject API name, e.g. 'Account'."
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the field metadata itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def describe_sobject(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("describe_sobject", params, state)

    @platform_process(
        name="list_sobjects",
        display_name="Salesforce: List SObjects",
        description=(
            "List the org's sobjects (API name + label). Returns immediately with a job_id and "
            "status 'queued' (D0.3 deferred-completion shape) — the dispatch returning is NOT the "
            "same as the job finishing; the sobject list is delivered when the job completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the sobject list itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_sobjects(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("list_sobjects", params, state)

    @platform_process(
        name="create_record",
        display_name="Salesforce: Create Record",
        description=(
            "Create a record of the given sobject type with the given fields. Write action. "
            "Returns immediately with a job_id and status 'queued' (D0.3 deferred-completion "
            "shape) — the dispatch returning is NOT the same as the record being created; the "
            "confirmation (new record id) is delivered when the job completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "sobject": ParameterMetadata(
                type=ParameterType.STRING, required=True, description="The sobject API name, e.g. 'Contact'."
            ),
            "fields": ParameterMetadata(
                type=ParameterType.OBJECT, required=True, description="Non-empty object of field values."
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the new record id itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def create_record(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("create_record", params, state)

    @platform_process(
        name="update_record",
        display_name="Salesforce: Update Record",
        description=(
            "Apply a non-empty fields object to an existing record by sobject + id. Write action. "
            "Returns immediately with a job_id and status 'queued' (D0.3 deferred-completion "
            "shape) — the dispatch returning is NOT the same as the update being applied; the "
            "confirmation is delivered when the job completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "sobject": ParameterMetadata(type=ParameterType.STRING, required=True, description="The sobject API name."),
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The record id."),
            "fields": ParameterMetadata(
                type=ParameterType.OBJECT, required=True, description="Non-empty object of field values to set."
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
    def update_record(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("update_record", params, state)

    @platform_process(
        name="delete_record",
        display_name="Salesforce: Delete Record",
        description=(
            "Permanently delete a record by sobject + id. This is a destructive write action "
            "taking an EXPLICIT target — sobject and id are required, no bulk form. Returns "
            "immediately with a job_id and status 'queued' (D0.3 deferred-completion shape) — "
            "the dispatch returning is NOT the same as the record being deleted; the confirmation "
            "is delivered when the job completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "sobject": ParameterMetadata(type=ParameterType.STRING, required=True, description="The sobject API name."),
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The record id to delete."),
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
    def delete_record(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("delete_record", params, state)

    @platform_process(
        name="test_connection",
        display_name="Salesforce: Test Connection",
        description=(
            "Verify the org binding by querying the org record through the sf CLI. Returns "
            "immediately with a job_id and status 'queued' (D0.3 deferred-completion shape) — "
            "the dispatch returning is NOT the same as the job finishing; ok, org_id, the "
            "verified username, and the pinned api_version are delivered when the job completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the connection check itself.",
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

    @platform_process(
        name="bulk_ingest_submit",
        display_name="Salesforce: Bulk Ingest Submit",
        description=(
            "Submit a Bulk API v2 ingest job (insert, update, upsert, or delete — NOT hardDelete, "
            "which is excluded) from a CSV file already on disk. Returns immediately with a job_id "
            "and status 'queued' (D0.3 deferred-completion shape) — this is the PLUGIN's own async "
            "job envelope, distinct from the Salesforce Bulk job id delivered when this job "
            "completes. The Salesforce job is submitted with --async and NEVER waited on inline — "
            "a bulk job can run minutes, well past this connector's per-call subprocess bound — so "
            "poll bulk_job_status with the delivered job_id to track it to completion. csv_path "
            "must be an ABSOLUTE path contained under an operator-configured import_allowed_roots "
            "entry (empty config refuses every ingest). external_id_field is required when, and "
            "only when, operation is 'upsert'."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "operation": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=(
                    f"One of: {', '.join(sorted(BULK_INGEST_OPERATIONS))}. hardDelete is excluded — "
                    "bypasses the recycle bin and needs a separate org permission."
                ),
            ),
            "sobject": ParameterMetadata(
                type=ParameterType.STRING, required=True, description="The sobject API name, e.g. 'Account'."
            ),
            "csv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=(
                    "ABSOLUTE path to the ingest CSV, contained under an import_allowed_roots entry."
                ),
            ),
            "external_id_field": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="Required iff operation is 'upsert'; refused for every other operation.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the Salesforce bulk job itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def bulk_ingest_submit(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("bulk_ingest_submit", params, state)

    @platform_process(
        name="bulk_job_status",
        display_name="Salesforce: Bulk Job Status",
        description=(
            "Fetch a Bulk API v2 ingest job's current state and record counts — the poll target "
            "for a job created by bulk_ingest_submit. Returns immediately with a job_id and status "
            "'queued' (D0.3 deferred-completion shape) — the dispatch returning is NOT the same as "
            "the job finishing; the Salesforce job's state (Open, UploadComplete, InProgress, "
            "JobComplete, Failed, or Aborted) and processed/failed record counts are delivered when "
            "THIS dispatch's job completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "job_id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The Salesforce Bulk v2 job id, from bulk_ingest_submit's delivered result.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the bulk job's state itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def bulk_job_status(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("bulk_job_status", params, state)

    @platform_process(
        name="bulk_job_results",
        display_name="Salesforce: Bulk Job Results",
        description=(
            "Fetch a completed Bulk API v2 ingest job's three result CSVs (successful, failed, "
            "unprocessed records) and write each to a caller-supplied ABSOLUTE path, contained "
            "under an operator-configured export_allowed_roots entry (empty config refuses every "
            "write) — never returned inline. Call this only after bulk_job_status reports the job "
            "reached JobComplete (or Failed/Aborted, which can still carry partial results). "
            "Returns immediately with a job_id and status 'queued' (D0.3 deferred-completion shape) "
            "— the three written paths are delivered when this dispatch's job completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "job_id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The Salesforce Bulk v2 job id, from bulk_ingest_submit's delivered result.",
            ),
            "successful_results_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ABSOLUTE .csv destination for successfully processed records.",
            ),
            "failed_results_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ABSOLUTE .csv destination for records that failed processing.",
            ),
            "unprocessed_results_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ABSOLUTE .csv destination for records the job never reached (e.g. an aborted job).",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the written CSV files themselves.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def bulk_job_results(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("bulk_job_results", params, state)

    @platform_process(
        name="bulk_job_abort",
        display_name="Salesforce: Bulk Job Abort",
        description=(
            "Abort a running Bulk API v2 ingest job — the recovery lever for a job submitted in "
            "error or running against bad data. Returns immediately with a job_id and status "
            "'queued' (D0.3 deferred-completion shape) — the dispatch returning is NOT the same as "
            "the abort taking effect; the Salesforce job's resulting state is delivered when this "
            "dispatch's job completes. Records already processed before the abort are NOT rolled "
            "back — check bulk_job_results for what landed."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "job_id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The Salesforce Bulk v2 job id, from bulk_ingest_submit's delivered result.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the abort confirmation itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def bulk_job_abort(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("bulk_job_abort", params, state)


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
