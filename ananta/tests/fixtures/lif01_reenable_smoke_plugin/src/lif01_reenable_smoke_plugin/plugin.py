"""LifecycleManaged fixture plugin for the LIF-01 re-enable smoke.

``prepare_for_readiness`` raises unless ``orchestrator_ref`` was injected
first -- this is the exact LIF-01 re-enable repro: the legacy
``_rediscover_plugins`` -> ``discover_plugins`` clear-and-rebuild path
re-instantiates plugins via ``PluginInitializer.create_plugin_instance``,
which sets only ``name`` + the validation registry, never
``orchestrator_ref``. A real plugin that needs its orchestrator at
prepare-time hits this raise; the atomic installer's
``_wire_plugin_instance`` wire callback injects ``orchestrator_ref``
BEFORE calling ``prepare_for_readiness``, so the fixed enable path never
hits it.

Also plants an opt-in wiring-phase failure, toggled by an env var rather
than a constructor arg: the installer instantiates this class itself
(``PluginInitializer.create_plugin_instance``), so the smoke has no
constructor-injection point -- an env var read at ``prepare_for_readiness``
time is the only lever the test process has over an instance it did not
construct. Used to drive a `PluginInstallError(phase="wiring")` on a
RE-enable of an already-successfully-installed plugin (the initial
install must succeed so the entry-point survives on disk; the env var is
set only between disable and the targeted re-enable call).
"""

from __future__ import annotations

import os
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

_PLUGIN_NAME = "lif01_reenable_smoke_plugin"
_RESULT_TYPE = "lif01_reenable_smoke_test_verb_result"

WIRING_FAILURE_ENV = "LIF01_REENABLE_SMOKE_PLANT_WIRING_FAILURE"


def _test_verb_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="LIF-01 re-enable smoke verb payload.",
        properties={
            "alive": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                description="Always true; proves the verb dispatched.",
            ),
        },
    )


class Lif01ReenableSmokePlugin(PluginBase, EdgeProcessProvider):
    """Minimal LifecycleManaged plugin gated on orchestrator_ref injection."""

    name: str = _PLUGIN_NAME

    def __init__(self) -> None:
        super().__init__()
        self.name = _PLUGIN_NAME
        self._running = False

    def prepare_for_readiness(self) -> None:
        if os.environ.get(WIRING_FAILURE_ENV):
            raise RuntimeError(
                f"{_PLUGIN_NAME}: planted wiring-phase failure for the LIF-01 smoke"
            )
        if self.orchestrator_ref is None:
            raise RuntimeError(
                f"{_PLUGIN_NAME}: orchestrator_ref not injected"
            )
        self.set_ready()

    def start_services(self) -> None:
        self._running = True

    def stop_services(self) -> None:
        self._running = False

    def is_running(self) -> bool:
        return self._running

    def set_active(self, active: bool) -> None:
        del active

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {
            "test_verb": EdgeProcessDefinition(
                name="test_verb",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type=_RESULT_TYPE,
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
        }

    @platform_process(
        name="test_verb",
        is_discoverable=True,
        context_handling=ContextHandling.NONE,
        parameters={},
        output_type="object",
        output_description="LIF-01 re-enable smoke verb payload: alive.",
        return_value_schema=_test_verb_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type=_RESULT_TYPE,
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    def test_verb(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Return the platform envelope around ``{alive: True}``."""
        del params, state
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {"alive": True},
            "actions": [],
            "error": None,
        }
