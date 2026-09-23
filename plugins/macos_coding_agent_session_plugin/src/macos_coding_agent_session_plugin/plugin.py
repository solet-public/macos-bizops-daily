"""macOS coding-agent session plugin entry point.

Implements :class:`CodingAgentSessionServiceInterface` per
``workbench/2026-06-05_bridge_port_routing_and_session_lifecycle_design.md``
§3.6 + §4.4 + §6 Slice 5. Owns MCP bridge subprocess lifecycle for every
coding-agent tab the iTerm2 plugin opens, plus the FSEvents watcher on
``<solet>.bridge.port`` that silently restarts every tracked bridge
whenever router blue-green swap rewrites the port file.

Mirrors the ``macos_self_deployment_plugin`` shape: each verb has a
plain interface method (matching the ABC signature) PLUS an
``@platform_process`` action method (matching the action dispatcher's
``(params, state)`` signature). The bound-provider skip semantics
(introduced by ``0e72ac15`` 2026-06-06) handle ``plugin::*`` namespace
exposure without explicit work here; the ``service_interface::*`` keys
register through the ABC + ``services/.../public.py`` surface.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

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
from ananta.interfaces.coding_agent_session_result_types import (
    BridgeListResult,
    BridgeRestartResult,
    BridgeRestartStatus,
    BridgeSpawnResult,
    BridgeSpawnStatus,
    BridgeStatus,
    BridgeTerminateResult,
    BridgeTerminateStatus,
)
from ananta.interfaces.coding_agent_session_service_interface import (
    CodingAgentSessionServiceInterface,
)
from ananta.interfaces.edge_process_provider import (
    EdgeProcessDefinition,
    EdgeProcessProvider,
)

from macos_coding_agent_session_plugin.bridge_tracker import (
    BridgeTracker,
    SpawnFn,
    default_spawn,
)
from macos_coding_agent_session_plugin.constants import (
    ENV_SOLET_NAME,
    PLUGIN_NAME,
    RESULT_TYPE_LIST,
    RESULT_TYPE_RESTART,
    RESULT_TYPE_SPAWN,
    RESULT_TYPE_TERMINATE,
    STATUS_FAILED,
)
from macos_coding_agent_session_plugin.fsevents_watcher import (
    FSEventsWatcher,
    build_restart_callback,
)

_AGENT_INSTANCE_ID_PARAM = ParameterMetadata(
    description="Stable identifier for the coding-agent tab whose bridge this verb operates on.",
    required=True,
    type=ParameterType.STRING,
)
_SOLET_NAME_PARAM = ParameterMetadata(
    description="Target solet the bridge connects to.",
    required=True,
    type=ParameterType.STRING,
)



def _spawn_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="spawn_bridge outcome.",
        properties={
            "status": ParameterMetadata(type=ParameterType.STRING, description="."),
            "agent_instance_id": ParameterMetadata(type=ParameterType.STRING, description="."),
            "solet_name": ParameterMetadata(type=ParameterType.STRING, description="."),
            "pid": ParameterMetadata(type=ParameterType.INTEGER, description="."),
            "started_at": ParameterMetadata(type=ParameterType.STRING, description="."),
            "message": ParameterMetadata(type=ParameterType.STRING, description="."),
        },
    )


def _terminate_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="terminate_bridge outcome.",
        properties={
            "status": ParameterMetadata(type=ParameterType.STRING, description="."),
            "agent_instance_id": ParameterMetadata(type=ParameterType.STRING, description="."),
            "pid": ParameterMetadata(type=ParameterType.INTEGER, description="."),
            "terminated_at": ParameterMetadata(type=ParameterType.STRING, description="."),
            "message": ParameterMetadata(type=ParameterType.STRING, description="."),
        },
    )


def _restart_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="restart_bridge outcome.",
        properties={
            "status": ParameterMetadata(type=ParameterType.STRING, description="."),
            "agent_instance_id": ParameterMetadata(type=ParameterType.STRING, description="."),
            "prior_pid": ParameterMetadata(type=ParameterType.INTEGER, description="."),
            "new_pid": ParameterMetadata(type=ParameterType.INTEGER, description="."),
            "restarted_at": ParameterMetadata(type=ParameterType.STRING, description="."),
            "message": ParameterMetadata(type=ParameterType.STRING, description="."),
        },
    )


def _list_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="list_bridges outcome.",
        properties={
            "bridges": ParameterMetadata(type=ParameterType.LIST, description="."),
            "message": ParameterMetadata(type=ParameterType.STRING, description="."),
        },
    )


def _runtime_dir() -> Path:
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        return Path(xdg_runtime) / "ananta"
    return Path.home() / ".ananta" / "runtime"


def _bridge_port_filename(solet_name: str) -> str:
    return f"{solet_name}.bridge.port"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _bridge_status_to_dict(row: BridgeStatus) -> dict[str, Any]:
    return {
        "agent_instance_id": row.agent_instance_id,
        "solet_name": row.solet_name,
        "pid": row.pid,
        "alive": row.alive,
        "started_at": row.started_at,
    }


class MacosCodingAgentSessionPlugin(
    PluginBase,
    EdgeProcessProvider,
    CodingAgentSessionServiceInterface,
):
    """macOS-scope owner of MCP bridge subprocess lifecycle for coding-agent tabs."""

    name: str = PLUGIN_NAME

    service_interfaces: ClassVar[tuple[type, ...]] = (
        CodingAgentSessionServiceInterface,
    )
    supported_interface_versions: ClassVar[dict[type, str]] = {
        CodingAgentSessionServiceInterface: CodingAgentSessionServiceInterface.INTERFACE_VERSION,
    }

    def __init__(self) -> None:
        super().__init__()
        self.name = PLUGIN_NAME
        self.logger: logging.Logger = logging.getLogger(self.name)
        self._solet_name: str = ""
        self._tracker: BridgeTracker | None = None
        self._watcher: FSEventsWatcher | None = None
        # Smoke-only override: tests inject a fake spawn function so no
        # real `python -m agent_messaging_plugin.mcp_bridge` is started.
        self._spawn_fn: SpawnFn = default_spawn

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def set_spawn_fn_for_smoke(self, spawn_fn: SpawnFn) -> None:
        """Smoke-only: replace the bridge subprocess spawn helper."""
        self._spawn_fn = spawn_fn
        if self._tracker is not None:
            self._tracker = BridgeTracker(logger=self.logger, spawn_fn=spawn_fn)

    def prepare_for_readiness(self) -> None:
        """Validate env, build tracker + watcher, mark ready."""
        solet_name = os.environ.get(ENV_SOLET_NAME)
        if not solet_name:
            msg = (
                f"{PLUGIN_NAME}: {ENV_SOLET_NAME} env var is required "
                "(set by the launching script)."
            )
            raise RuntimeError(msg)
        self._solet_name = solet_name
        runtime_dir = _runtime_dir()
        runtime_dir.mkdir(parents=True, exist_ok=True)
        tracker = BridgeTracker(logger=self.logger, spawn_fn=self._spawn_fn)
        self._tracker = tracker
        target_filename = _bridge_port_filename(solet_name)
        watcher = FSEventsWatcher(
            watch_path=runtime_dir,
            target_filename=target_filename,
            on_change=build_restart_callback(tracker, self.logger),
            logger=self.logger,
        )
        self._watcher = watcher
        watcher.start()
        self.set_ready()

    def shutdown(self) -> None:
        """Best-effort teardown: stop watcher + terminate every tracked bridge."""
        watcher = self._watcher
        if watcher is not None:
            try:
                watcher.stop()
            except Exception:  # noqa: BLE001 — shutdown must not raise
                self.logger.exception("FSEvents watcher stop raised")
        tracker = self._tracker
        if tracker is not None:
            try:
                tracker.shutdown()
            except Exception:  # noqa: BLE001 — shutdown must not raise
                self.logger.exception("bridge tracker shutdown raised")
        self._watcher = None
        self._tracker = None

    # ------------------------------------------------------------------
    # EdgeProcessProvider
    # ------------------------------------------------------------------

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {
            "spawn_bridge": EdgeProcessDefinition(
                name="spawn_bridge",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type=RESULT_TYPE_SPAWN,
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "terminate_bridge": EdgeProcessDefinition(
                name="terminate_bridge",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type=RESULT_TYPE_TERMINATE,
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "restart_bridge": EdgeProcessDefinition(
                name="restart_bridge",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type=RESULT_TYPE_RESTART,
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "list_bridges": EdgeProcessDefinition(
                name="list_bridges",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type=RESULT_TYPE_LIST,
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
        }

    # ------------------------------------------------------------------
    # Interface methods (typed-DTO returns)
    # ------------------------------------------------------------------

    def spawn_bridge(
        self,
        *,
        agent_instance_id: str,
        solet_name: str,
    ) -> BridgeSpawnResult:
        tracker = self._require_tracker()
        status, row, message = tracker.spawn(
            agent_instance_id=agent_instance_id, solet_name=solet_name,
        )
        if status == "failed" or row is None:
            return BridgeSpawnResult(
                status=BridgeSpawnStatus.FAILED,
                agent_instance_id=agent_instance_id,
                solet_name=solet_name,
                pid=0,
                started_at="",
                message=message,
            )
        spawn_status = (
            BridgeSpawnStatus.ALREADY_RUNNING
            if status == "already_running"
            else BridgeSpawnStatus.SUCCESS
        )
        return BridgeSpawnResult(
            status=spawn_status,
            agent_instance_id=row.agent_instance_id,
            solet_name=row.solet_name,
            pid=row.pid,
            started_at=row.started_at,
            message=message,
        )

    def terminate_bridge(self, *, agent_instance_id: str) -> BridgeTerminateResult:
        tracker = self._require_tracker()
        status, pid, message = tracker.terminate(agent_instance_id=agent_instance_id)
        if status == "not_running":
            return BridgeTerminateResult(
                status=BridgeTerminateStatus.NOT_RUNNING,
                agent_instance_id=agent_instance_id,
                pid=0,
                terminated_at="",
                message=message,
            )
        if status == "failed":
            return BridgeTerminateResult(
                status=BridgeTerminateStatus.FAILED,
                agent_instance_id=agent_instance_id,
                pid=pid,
                terminated_at="",
                message=message,
            )
        return BridgeTerminateResult(
            status=BridgeTerminateStatus.SUCCESS,
            agent_instance_id=agent_instance_id,
            pid=pid,
            terminated_at=_utc_now_iso(),
            message=message,
        )

    def restart_bridge(self, *, agent_instance_id: str) -> BridgeRestartResult:
        tracker = self._require_tracker()
        status, prior_pid, fresh_row, message = tracker.restart(
            agent_instance_id=agent_instance_id,
        )
        if status == "not_running":
            return BridgeRestartResult(
                status=BridgeRestartStatus.NOT_RUNNING,
                agent_instance_id=agent_instance_id,
                prior_pid=0,
                new_pid=0,
                restarted_at="",
                message=message,
            )
        if status == "failed" or fresh_row is None:
            return BridgeRestartResult(
                status=BridgeRestartStatus.FAILED,
                agent_instance_id=agent_instance_id,
                prior_pid=prior_pid,
                new_pid=0,
                restarted_at="",
                message=message,
            )
        return BridgeRestartResult(
            status=BridgeRestartStatus.SUCCESS,
            agent_instance_id=agent_instance_id,
            prior_pid=prior_pid,
            new_pid=fresh_row.pid,
            restarted_at=_utc_now_iso(),
            message=message,
        )

    def list_bridges(self) -> BridgeListResult:
        tracker = self._require_tracker()
        rows = tracker.list_bridges()
        statuses = tuple(
            BridgeStatus(
                agent_instance_id=row.agent_instance_id,
                solet_name=row.solet_name,
                pid=row.pid,
                alive=tracker.is_alive(row.pid),
                started_at=row.started_at,
            )
            for row in rows
        )
        return BridgeListResult(
            bridges=statuses,
            message=f"{len(statuses)} bridge(s) tracked",
        )

    # ------------------------------------------------------------------
    # @platform_process action-method wrappers
    # ------------------------------------------------------------------

    @platform_process(
        name="spawn_bridge",
        context_handling=ContextHandling.NONE,
        parameters={
            "agent_instance_id": _AGENT_INSTANCE_ID_PARAM,
            "solet_name": _SOLET_NAME_PARAM,
        },
        output_type="object",
        output_description="spawn_bridge envelope.",
        return_value_schema=_spawn_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type=RESULT_TYPE_SPAWN,
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    def spawn_bridge_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        del state
        agent_instance_id = str(params.get("agent_instance_id") or "")
        solet_name = str(params.get("solet_name") or "")
        if not agent_instance_id or not solet_name:
            return self._error_envelope(
                "missing_args",
                "spawn_bridge requires agent_instance_id + solet_name.",
            )
        try:
            result = self.spawn_bridge(
                agent_instance_id=agent_instance_id,
                solet_name=solet_name,
            )
        except Exception as err:  # noqa: BLE001 — return structured failure
            self.logger.exception("spawn_bridge crashed")
            return self._error_envelope(STATUS_FAILED, str(err))
        return self._success_envelope(
            {
                "status": result.status.value,
                "agent_instance_id": result.agent_instance_id,
                "solet_name": result.solet_name,
                "pid": result.pid,
                "started_at": result.started_at,
                "message": result.message,
            },
        )

    @platform_process(
        name="terminate_bridge",
        context_handling=ContextHandling.NONE,
        parameters={"agent_instance_id": _AGENT_INSTANCE_ID_PARAM},
        output_type="object",
        output_description="terminate_bridge envelope.",
        return_value_schema=_terminate_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type=RESULT_TYPE_TERMINATE,
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    def terminate_bridge_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        del state
        agent_instance_id = str(params.get("agent_instance_id") or "")
        if not agent_instance_id:
            return self._error_envelope(
                "missing_args", "terminate_bridge requires agent_instance_id.",
            )
        try:
            result = self.terminate_bridge(agent_instance_id=agent_instance_id)
        except Exception as err:  # noqa: BLE001 — return structured failure
            self.logger.exception("terminate_bridge crashed")
            return self._error_envelope(STATUS_FAILED, str(err))
        return self._success_envelope(
            {
                "status": result.status.value,
                "agent_instance_id": result.agent_instance_id,
                "pid": result.pid,
                "terminated_at": result.terminated_at,
                "message": result.message,
            },
        )

    @platform_process(
        name="restart_bridge",
        context_handling=ContextHandling.NONE,
        parameters={"agent_instance_id": _AGENT_INSTANCE_ID_PARAM},
        output_type="object",
        output_description="restart_bridge envelope.",
        return_value_schema=_restart_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type=RESULT_TYPE_RESTART,
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    def restart_bridge_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        del state
        agent_instance_id = str(params.get("agent_instance_id") or "")
        if not agent_instance_id:
            return self._error_envelope(
                "missing_args", "restart_bridge requires agent_instance_id.",
            )
        try:
            result = self.restart_bridge(agent_instance_id=agent_instance_id)
        except Exception as err:  # noqa: BLE001 — return structured failure
            self.logger.exception("restart_bridge crashed")
            return self._error_envelope(STATUS_FAILED, str(err))
        return self._success_envelope(
            {
                "status": result.status.value,
                "agent_instance_id": result.agent_instance_id,
                "prior_pid": result.prior_pid,
                "new_pid": result.new_pid,
                "restarted_at": result.restarted_at,
                "message": result.message,
            },
        )

    @platform_process(
        name="list_bridges",
        context_handling=ContextHandling.NONE,
        parameters={},
        output_type="object",
        output_description="list_bridges envelope.",
        return_value_schema=_list_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type=RESULT_TYPE_LIST,
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    def list_bridges_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        del params, state
        try:
            result = self.list_bridges()
        except Exception as err:  # noqa: BLE001 — return structured failure
            self.logger.exception("list_bridges crashed")
            return self._error_envelope(STATUS_FAILED, str(err))
        return self._success_envelope(
            {
                "bridges": [_bridge_status_to_dict(row) for row in result.bridges],
                "message": result.message,
            },
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _require_tracker(self) -> BridgeTracker:
        tracker = self._tracker
        if tracker is None:
            msg = (
                f"{PLUGIN_NAME}: tracker not initialized — "
                "prepare_for_readiness not called?"
            )
            raise RuntimeError(msg)
        return tracker

    def _success_envelope(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": data,
            "actions": [],
            "error": None,
            "timestamp": _utc_now_iso(),
        }

    def _error_envelope(self, code: str, message: str) -> dict[str, Any]:
        return {
            "action_status": ActionStatus.ERROR.value,
            "data": {},
            "actions": [],
            "error": {"code": code, "message": message, "details": {}},
            "timestamp": _utc_now_iso(),
        }
