"""Coding Agent Session Service Public API.

``@service_interface_process``-decorated surface for the four verbs of
:class:`CodingAgentSessionServiceInterface`. Bound providers (the macOS
plugin, future linux / windows siblings) inherit from this class via
the ABC and are reachable through
``service_interface::coding_agent_session_service::*`` keys.

Per the D1 architectural mandate, the verbs here mirror the ABC's
signatures one-for-one. The matching KB JSONs live at
``ananta/knowledge_base/processes/coding_agent_session_service/*.json``
(dual-write per the mandate).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.core.services.service_interface_decorator import service_interface_process
from ananta.interfaces.coding_agent_session_result_types import (
    BridgeListResult,
    BridgeRestartResult,
    BridgeSpawnResult,
    BridgeTerminateResult,
)

PROVIDER = "coding_agent_session_service"

_AGENT_INSTANCE_ID_PARAM = ParameterMetadata(
    description=(
        "Stable identifier for the coding-agent tab whose MCP bridge "
        "subprocess this verb operates on. Obtained from the agent's "
        "peer-registration handshake (agent_messaging_plugin peer registry)."
    ),
    required=True,
    type=ParameterType.STRING,
)
_SOLET_NAME_PARAM = ParameterMetadata(
    description=(
        "Target solet the bridge connects to. E.g. 'example' "
        "for a locally-run solet."
    ),
    required=True,
    type=ParameterType.STRING,
)



def _spawn_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="spawn_bridge outcome.",
        properties={
            "status": ParameterMetadata(type=ParameterType.STRING, description="Terminal status."),
            "agent_instance_id": ParameterMetadata(type=ParameterType.STRING, description="Tracking key."),
            "solet_name": ParameterMetadata(type=ParameterType.STRING, description="Target solet."),
            "pid": ParameterMetadata(type=ParameterType.INTEGER, description="Spawned bridge pid."),
            "started_at": ParameterMetadata(type=ParameterType.STRING, description="ISO-8601 UTC."),
            "message": ParameterMetadata(type=ParameterType.STRING, description="Human detail."),
        },
    )


def _terminate_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="terminate_bridge outcome.",
        properties={
            "status": ParameterMetadata(type=ParameterType.STRING, description="Terminal status."),
            "agent_instance_id": ParameterMetadata(type=ParameterType.STRING, description="Tracking key."),
            "pid": ParameterMetadata(type=ParameterType.INTEGER, description="Terminated pid."),
            "terminated_at": ParameterMetadata(type=ParameterType.STRING, description="ISO-8601 UTC."),
            "message": ParameterMetadata(type=ParameterType.STRING, description="Human detail."),
        },
    )


def _restart_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="restart_bridge outcome.",
        properties={
            "status": ParameterMetadata(type=ParameterType.STRING, description="Terminal status."),
            "agent_instance_id": ParameterMetadata(type=ParameterType.STRING, description="Tracking key."),
            "prior_pid": ParameterMetadata(type=ParameterType.INTEGER, description="Prior bridge pid."),
            "new_pid": ParameterMetadata(type=ParameterType.INTEGER, description="Fresh bridge pid."),
            "restarted_at": ParameterMetadata(type=ParameterType.STRING, description="ISO-8601 UTC."),
            "message": ParameterMetadata(type=ParameterType.STRING, description="Human detail."),
        },
    )


def _list_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="list_bridges outcome.",
        properties={
            "bridges": ParameterMetadata(type=ParameterType.LIST, description="Per-bridge rows."),
            "message": ParameterMetadata(type=ParameterType.STRING, description="Human detail."),
        },
    )


class CodingAgentSessionServicePublicAPI(ABC):
    """AI-discoverable coding-agent-session service surface."""

    @service_interface_process(
        name="spawn_bridge",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={
            "agent_instance_id": _AGENT_INSTANCE_ID_PARAM,
            "solet_name": _SOLET_NAME_PARAM,
        },
        return_value_schema=_spawn_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="coding_agent_session_bridge_spawn_result",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def spawn_bridge(
        self,
        *,
        agent_instance_id: str,
        solet_name: str,
    ) -> BridgeSpawnResult:
        """Spawn an MCP bridge subprocess for a coding-agent tab."""

    @service_interface_process(
        name="terminate_bridge",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={"agent_instance_id": _AGENT_INSTANCE_ID_PARAM},
        return_value_schema=_terminate_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="coding_agent_session_bridge_terminate_result",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def terminate_bridge(self, *, agent_instance_id: str) -> BridgeTerminateResult:
        """Terminate a tracked MCP bridge subprocess."""

    @service_interface_process(
        name="restart_bridge",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={"agent_instance_id": _AGENT_INSTANCE_ID_PARAM},
        return_value_schema=_restart_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="coding_agent_session_bridge_restart_result",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    @abstractmethod
    def restart_bridge(self, *, agent_instance_id: str) -> BridgeRestartResult:
        """Terminate + re-spawn a tracked MCP bridge subprocess."""

    @service_interface_process(
        name="list_bridges",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={},
        return_value_schema=_list_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="coding_agent_session_bridge_list_result",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    @abstractmethod
    def list_bridges(self) -> BridgeListResult:
        """Enumerate every tracked MCP bridge subprocess."""
