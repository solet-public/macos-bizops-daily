"""platform_health_plugin entry point.

Verbs:

* ``plugin::platform_health_plugin::execute_registry_sweep`` — the registry
  sweep. WARNING: a diagnostic name does not make this verb diagnose-safe.
  Read-shape calls can reach external systems. The default is
  ``dry_run=True``, which only classifies; live dispatch requires
  ``dry_run=False`` and is restricted to in-process ``service_interface``
  providers unless named external plugin namespaces and an
  operator-confirmation citation are supplied. Per Architect Q1 ruling
  (2026-05-30) it is **NOT** a startup-blocking gate: it runs only when an
  operator or CI calls it via ``process_call``.
* ``plugin::platform_health_plugin::check_host_disk_headroom`` — the
  discoverable EDGE host-disk check, for direct calls.
* ``plugin::platform_health_plugin::check_host_disk_headroom_cron`` — the
  EDGE_SINK cron target (``is_discoverable=False``, no processor
  customizations). It hands one check to a single-slot background thread and
  returns at once (``started`` / ``already_running`` / ``inactive``), so the
  check's filesystem stat, state reads/writes and alert delivery never run on
  the action-queue loop (the fast-return contract).
* ``plugin::platform_health_plugin::ensure_host_disk_guard_schedule`` — the
  idempotent install primitive for that cron (``is_discoverable=False``).

``scheduling_service`` is resolved at call time, never at readiness: it lives
on the orchestrator's ``service_manager``, which startup creates only at its
``init_service_manager`` step, AFTER every plugin's ``prepare_for_readiness``
and ``start_services`` have run. So this plugin comes up ready without it,
and nothing installs the cron from a lifecycle method. The install runs as a
starting action (``ensure_host_disk_guard_schedule``, the ``session_ledger``
``ensure_periodic_poll_schedule`` precedent) in the deployment's own runtime
``config/starting_action_definitions.json``, which the orchestrator submits
after startup completes. No shipped profile template carries it: the guard
needs a deployment-local ``alert_role`` (decisions ``dec_a975b80b``,
``dec_02f96c32``). The cron fire honours ``set_active``: the inactive colour
of a blue-green pair does nothing on a fire.
"""

from __future__ import annotations

import logging
import threading
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
from ananta.core.config.config_manager import get_config
from ananta.core.domain.enums import ActionStatus, ProcessorPolicyCategory
from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.edge_process_provider import (
    EdgeProcessDefinition,
    EdgeProcessProvider,
)

from platform_health_plugin.constants import (
    PLUGIN_NAME,
    SWEEP_PROCESS_NAME,
)
from platform_health_plugin.host_disk_guard import (
    CHECK_PROCESS_NAME,
    DEFAULT_CRITICAL_FLOOR_BYTES,
    DEFAULT_SCHEDULE_CRON,
    DEFAULT_SCHEDULE_TAG,
    DEFAULT_WARNING_FLOOR_BYTES,
    ENSURE_SCHEDULE_PROCESS_NAME,
    HostDiskGuardConfig,
)
from platform_health_plugin.host_disk_guard import (
    check_host_disk_headroom as _check_host_disk_headroom,
)
from platform_health_plugin.host_disk_guard import (
    ensure_host_disk_guard_schedule as _ensure_host_disk_guard_schedule,
)


class PlatformHealthPlugin(PluginBase, EdgeProcessProvider):
    """Operator/CI diagnostic gate over the live process registry."""

    name: str = PLUGIN_NAME

    def __init__(self) -> None:
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self._running = False
        self._active = True
        # Single slot for the cron-fired check: a fire that lands while the
        # previous check is still running is a no-op, never a second check.
        self._check_lock = threading.Lock()

    def prepare_for_readiness(self) -> None:
        if self.orchestrator_ref is None:
            raise RuntimeError(
                f"{self.name}: orchestrator_ref not injected before prepare_for_readiness",
            )
        # scheduling_service is deliberately NOT acquired here: it does not
        # exist yet at this startup step (see the module docstring).
        self.set_ready()

    def start_services(self) -> None:
        """Mark the plugin running. Installs nothing: ``scheduling_service``
        does not exist yet at this startup step (see the module docstring)."""
        self._running = True

    def stop_services(self) -> None:
        self._running = False

    def is_running(self) -> bool:
        return self._running

    def set_active(self, active: bool) -> None:
        self._active = active
        self.logger.info("%s host-disk cron %s", self.name, "active" if active else "inactive")

    def _dispatch_check_in_background(self, state: dict[str, Any]) -> str:
        """Hand one headroom check to a background thread and return at once
        (``fleet_maintenance_plugin`` ``_dispatch_in_background`` precedent):
        the check's stat, state reads/writes and alert delivery run off the
        action-queue loop."""
        if not self._active:
            return "inactive"
        if not self._check_lock.acquire(blocking=False):
            return "already_running"

        def body() -> None:
            try:
                self._run_headroom_check({}, state)
            except Exception:  # noqa: BLE001 — logged at ERROR with the traceback, never swallowed silently
                self.logger.exception("%s host-disk check failed", self.name)
            finally:
                self._check_lock.release()

        threading.Thread(target=body, name="host-disk-headroom-check", daemon=True).start()
        return "started"

    def _require_scheduling_service(self) -> Any:
        """``scheduling_service`` resolved NOW, at call time. Raises loudly
        when it is absent: the orchestrator is not injected, or the call ran
        before startup's ``init_service_manager`` step."""
        if self.orchestrator_ref is None:
            raise RuntimeError(f"{self.name}: orchestrator_ref unavailable; cannot reach the scheduler")
        service = self.orchestrator_ref.get_service("scheduling_service")
        if service is None:
            raise RuntimeError(
                f"{self.name}: scheduling_service is not available yet -- it exists only after "
                "startup's init_service_manager step; run ensure_host_disk_guard_schedule as a "
                "starting action or later, never from a plugin lifecycle method",
            )
        return service

    def _run_headroom_check(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        if self.orchestrator_ref is None:
            raise RuntimeError(f"{self.name}: orchestrator_ref unavailable; cannot dispatch an alert")
        config = HostDiskGuardConfig(
            path=str(params.get("path") or HostDiskGuardConfig().path),
            warning_floor_bytes=int(params.get("warning_floor_bytes") or DEFAULT_WARNING_FLOOR_BYTES),
            critical_floor_bytes=int(params.get("critical_floor_bytes") or DEFAULT_CRITICAL_FLOOR_BYTES),
            alert_role=_resolve_alert_role(params),
        )
        return _check_host_disk_headroom(self.orchestrator_ref, config=config, state=state)

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {
            SWEEP_PROCESS_NAME: EdgeProcessDefinition(
                name=SWEEP_PROCESS_NAME,
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type="platform_health_sweep_result",
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            CHECK_PROCESS_NAME: EdgeProcessDefinition(
                name=CHECK_PROCESS_NAME,
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type="host_disk_headroom_result",
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            ENSURE_SCHEDULE_PROCESS_NAME: EdgeProcessDefinition(
                name=ENSURE_SCHEDULE_PROCESS_NAME,
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type="host_disk_guard_schedule_ensure_result",
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
        }

    @platform_process(
        name=SWEEP_PROCESS_NAME,
        context_handling=ContextHandling.NONE,
        parameters={
            "write_enabled": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(
                    "When True, write-shape verbs (anything not starting with "
                    "list_/get_) are invoked too. Use against a wipe-on-tear-down "
                    "test schema; do not run against production state."
                ),
            ),
            "dry_run": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(
                    "Defaults to true. When true, returns every classification "
                    "row and never dispatches a process. Set false only for an "
                    "explicitly scoped live sweep."
                ),
            ),
            "external_namespaces": ParameterMetadata(
                type=ParameterType.LIST,
                required=False,
                description=(
                    "Explicit list of declared outward-facing plugin provider "
                    "namespaces to include in a live sweep. This is a list, not "
                    "a broad enable flag, and requires operator_confirmation."
                ),
            ),
            "operator_confirmation": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description=(
                    "Authorizing operator turn or message id, required whenever "
                    "external_namespaces is non-empty. Echoed in those result rows."
                ),
            ),
            "include_pattern": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description=(
                    "Substring filter on process_key; only matching processes "
                    "are swept. Useful for narrowing to one namespace, e.g. "
                    "'session_ledger_service' or 'scheduling_service'."
                ),
            ),
        },
        output_type="object",
        output_description="Per-process sweep report.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Sweep summary + per-process rows.",
            properties={
                "total": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Processes considered after include_pattern filter.",
                ),
                "ok": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Processes that completed without raising.",
                ),
                "failed": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Processes that raised; see results[].error_message.",
                ),
                "skipped": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Skipped (write-shape, self, unresolved).",
                ),
                "results": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "Per-process rows: process_key, shape, status, "
                        "would_dispatch, error_class, error_message, and "
                        "operator_confirmation."
                    ),
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Platform health registry sweep",
            result_type="platform_health_sweep_result",
            result_description="Per-process pass/fail report from the registry sweep.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    def execute_registry_sweep(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002 — required by @platform_process signature
    ) -> dict[str, Any]:
        from platform_health_plugin import sweep  # late import: keeps sweep module reloadable  # noqa: PLC0415

        if self.orchestrator_ref is None:
            raise RuntimeError(
                f"{self.name}: orchestrator_ref unavailable; cannot read registry",
            )
        write_enabled = bool(params.get("write_enabled", False))
        dry_run = params.get("dry_run", True)
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean when supplied")
        include_pattern = params.get("include_pattern")
        if include_pattern is not None and not isinstance(include_pattern, str):
            raise ValueError("include_pattern must be a string when supplied")
        external_namespaces = _parse_external_namespaces(params.get("external_namespaces", []))
        operator_confirmation = params.get("operator_confirmation")
        if operator_confirmation is not None and not isinstance(operator_confirmation, str):
            raise ValueError("operator_confirmation must be a string when supplied")
        report = sweep.run_sweep(
            self.orchestrator_ref,
            write_enabled=write_enabled,
            include_pattern=include_pattern,
            dry_run=dry_run,
            external_namespaces=external_namespaces,
            operator_confirmation=operator_confirmation,
        )
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": report,
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    @platform_process(
        name="check_host_disk_headroom",
        context_handling=ContextHandling.NONE,
        parameters={
            "path": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="Filesystem path to measure free space at. Default '/Users'.",
            ),
            "warning_floor_bytes": ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=f"Below this many free bytes: WARNING. Default {DEFAULT_WARNING_FLOOR_BYTES} (100 GiB).",
            ),
            "critical_floor_bytes": ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=f"Below this many free bytes: CRITICAL. Default {DEFAULT_CRITICAL_FLOOR_BYTES} (50 GiB).",
            ),
            "alert_role": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description=(
                    "Durable role to peer_send_by_name on a severity transition. No "
                    "baked-in default: falls back to this plugin's own config key "
                    "'alert_role' (plugin.yaml `config:` block); if that is also "
                    "unset, the check still measures but cannot alert -- see "
                    "alert_reason='alert_target_unconfigured' on the result."
                ),
            ),
        },
        output_type="object",
        output_description="Host free-space headroom measurement and alert outcome.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Headroom status, the measurement, and whether an alert was dispatched.",
            properties={
                "status": ParameterMetadata(type=ParameterType.STRING, description="One of: ok, warning, critical."),
                "free_bytes": ParameterMetadata(type=ParameterType.INTEGER, description="Measured free bytes at path."),
                "path": ParameterMetadata(type=ParameterType.STRING, description="Path measured."),
                "transitioned": ParameterMetadata(type=ParameterType.BOOLEAN, description="Whether status differs from the last status an alert was successfully delivered for (the durable marker) -- true on entering warning/critical AND on recovery."),
                "alerted": ParameterMetadata(type=ParameterType.BOOLEAN, description="Whether the alert was confirmed DELIVERED TO A LIVE holder this call (queued_wake/queued_notification/queued_watcher). False on no transition, on a vacant/malformed role, or on queued_for_replay (persisted but nobody live received it)."),
                "alert_role": ParameterMetadata(type=ParameterType.STRING, description="Role a transition was reported to, or null if there was no transition this call."),
                "alert_reason": ParameterMetadata(type=ParameterType.STRING, description="Set when transitioned is true and alerted is false: why the alert was not confirmed delivered to a live holder."),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Host disk headroom check",
            result_type="host_disk_headroom_result",
            result_description="Free-space measurement against the WARNING/CRITICAL floors, and whether an operator alert fired.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    def check_host_disk_headroom(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        return _envelope(self._run_headroom_check(params, state))

    # Literal ``name=``: the whole-tree gate resolves a cron target's
    # processor_policy_category from the literal decorator name.
    @platform_process(
        name="check_host_disk_headroom_cron",
        is_discoverable=False,
        context_handling=ContextHandling.NONE,
        parameters={},
        output_type="object",
        output_description="Whether this fire started a background host-disk headroom check.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Cron fire outcome.",
            properties={
                "dispatch": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="started | already_running | inactive",
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
    )
    def check_host_disk_headroom_cron(
        self,
        params: dict[str, Any],  # noqa: ARG002 — required by @platform_process signature
        state: dict[str, Any],
    ) -> dict[str, Any]:
        outcome = self._dispatch_check_in_background(state)
        self.logger.info("%s host-disk cron fire: %s", self.name, outcome)
        return _envelope({"dispatch": outcome})

    @platform_process(
        name="ensure_host_disk_guard_schedule",
        context_handling=ContextHandling.NONE,
        is_discoverable=False,
        parameters={
            "cron_expression": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description=f"Cron expression (UTC). Default '{DEFAULT_SCHEDULE_CRON}' (every 10 minutes).",
            ),
            "tag": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description=f"Schedule tag, used for idempotent lookup. Default '{DEFAULT_SCHEDULE_TAG}'.",
            ),
        },
        output_type="object",
        output_description="Ensure result for the host-disk-guard cron.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Whether the cron was created or already present.",
            properties={
                "status": ParameterMetadata(type=ParameterType.STRING, description="One of: created, already_present, refused (no alert_role configured -- see reason)."),
                "tag": ParameterMetadata(type=ParameterType.STRING, description="Schedule tag."),
                "reason": ParameterMetadata(type=ParameterType.STRING, description="Set when status is refused: 'alert_target_unconfigured'."),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Ensure host disk guard schedule",
            result_type="host_disk_guard_schedule_ensure_result",
            result_description="Whether the host-disk-guard cron was just created or was already present.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    def ensure_host_disk_guard_schedule(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002 — required by @platform_process signature
    ) -> dict[str, Any]:
        if "alert_role" in params:
            # The cron fires with empty arguments, so every fire resolves the
            # target from config alone. A call-time override here could only
            # get an inert cron past the refusal below.
            raise ValueError(
                "alert_role is not a parameter of ensure_host_disk_guard_schedule; "
                "the installed cron alerts the platform_health_plugin config key 'alert_role'",
            )
        cron_expression = str(params.get("cron_expression") or DEFAULT_SCHEDULE_CRON)
        tag = str(params.get("tag") or DEFAULT_SCHEDULE_TAG)
        report = _ensure_host_disk_guard_schedule(
            self._require_scheduling_service(),
            alert_role=_configured_alert_role(),
            cron_expression=cron_expression,
            tag=tag,
        )
        return _envelope(report)


def _envelope(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "action_status": ActionStatus.COMPLETED.value,
        "data": data,
        "actions": [],
        "error": None,
        "timestamp": datetime.now(UTC).isoformat(),
    }


def _configured_alert_role() -> str | None:
    """This plugin's own ``alert_role`` config key, or ``None`` when unset
    (deliberately no baked-in identity; see ``host_disk_guard``'s module
    docstring). Read fresh on every call via the process-wide
    :func:`get_config` singleton rather than cached at plugin init, matching
    how other plugins read their own settings on demand (e.g.
    ``postgres_state_management_plugin._ensure_plugin_ready``). This is the
    ONLY source the cron fire and the installer use."""
    configured = get_config().get_plugin_config(PLUGIN_NAME, default_config={}).get("alert_role")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return None


def _resolve_alert_role(params: dict[str, Any]) -> str | None:
    """The alert target for a direct ``check_host_disk_headroom`` call: an
    explicit ``params.alert_role`` override, else :func:`_configured_alert_role`."""
    explicit = params.get("alert_role")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return _configured_alert_role()


def _parse_external_namespaces(value: object) -> tuple[str, ...]:
    """Validate the explicit external-provider enumeration without coercion."""
    if not isinstance(value, list) or not all(isinstance(name, str) and name for name in value):
        raise ValueError("external_namespaces must be a list of non-empty strings")
    if len(set(value)) != len(value):
        raise ValueError("external_namespaces must not contain duplicates")
    return tuple(value)
