"""Minimal EDGE plugin with a real pip dependency for C4 smoke.

The verb body imports ``tabulate`` (declared as a pip dependency in
pyproject.toml) and calls it on a small fixed table. The rendered
output is included in the return envelope, so a passing verb call
proves three things end-to-end:

1. ``pip install -e <fixture>`` resolved the dep (tabulate is installed
   alongside the fixture).
2. The dep is importable in-process post-install (F3's
   ``_invalidate_importlib_caches`` helper, especially the
   ``site.addsitedir`` step, refreshes ``sys.path`` so ``import
   tabulate`` finds the freshly-installed package).
3. The fixture survives blue-green pickup: when ``apply_manifest`` is
followed by ``restart_with_manifest``, the green solet spawns from the
   working tree with the fixture in its manifest and ``tabulate``
   still resolvable in the green venv (which is the same venv blue
   ran in).

Modeled on ``fresh_install_smoke_plugin`` (F3's fixture); only the
pyproject ``dependencies`` field + the verb body differ.
"""

from __future__ import annotations

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

_PLUGIN_NAME = "fresh_install_with_pip_dep_smoke_plugin"
_RESULT_TYPE = "fresh_install_with_pip_dep_smoke_tabulate_proof_result"



def _tabulate_proof_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="C4 smoke verb payload demonstrating tabulate dependency resolution.",
        properties={
            "alive": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                description="Always true; proves the verb dispatched.",
            ),
            "plugin_name": ParameterMetadata(
                type=ParameterType.STRING,
                description="Always 'fresh_install_with_pip_dep_smoke_plugin'.",
            ),
            "tabulate_rendered": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "Output of ``tabulate.tabulate(...)`` on a small fixed "
                    "table. Non-empty if + only if the tabulate pip "
                    "dependency was resolved + imported successfully."
                ),
            ),
        },
    )


class FreshInstallWithPipDepSmokePlugin(PluginBase, EdgeProcessProvider):
    """Stateless EDGE plugin with a real pip-dep used by the C4 smoke."""

    name: str = _PLUGIN_NAME

    def __init__(self) -> None:
        super().__init__()
        self.name = _PLUGIN_NAME

    def prepare_for_readiness(self) -> None:
        """Stateless; mark ready so list_plugins shows status=ready."""
        self.set_ready()

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        """Declare the EDGE process metadata required by plugin_registration_validator."""
        return {
            "tabulate_proof": EdgeProcessDefinition(
                name="tabulate_proof",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type=_RESULT_TYPE,
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
        }

    @platform_process(
        name="tabulate_proof",
        is_discoverable=True,
        context_handling=ContextHandling.NONE,
        parameters={},
        output_type="object",
        output_description="C4 smoke verb payload: alive + plugin_name + tabulate_rendered.",
        return_value_schema=_tabulate_proof_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type=_RESULT_TYPE,
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    def tabulate_proof(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Render a small fixed table via the ``tabulate`` pip dependency.

        Reading ``tabulate_rendered`` and finding a non-empty rendered
        table is empirical proof that the new-pip-dep resolution +
        in-process import works post-install (and, when the verb is
called against the green solet, that blue-green pickup also handled
        the new-pip-dep).
        """
        del params, state
        # Local import — this is the load-bearing test of pip-dep
        # resolution. If tabulate isn't importable here, the verb fails
        # at dispatch and the smoke catches the regression.
        from tabulate import tabulate
        rendered = tabulate(
            [["alive", True], ["plugin", _PLUGIN_NAME]],
            headers=["key", "value"],
            tablefmt="simple",
        )
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {
                "alive": True,
                "plugin_name": _PLUGIN_NAME,
                "tabulate_rendered": rendered,
            },
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }
