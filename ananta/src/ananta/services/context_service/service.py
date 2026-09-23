"""Context service — the decoupled core provider of ``assemble_agent_context``.

Phase 2 of the coding-agent substrate plan. A read-only, retrieval/provenance-
first briefing service: it runs the ``agent_context`` assembly profile (the
bundle-producing prompt stages, WITHOUT the ``api`` serialization stage) and
groups the resulting context blocks into named bundles with provenance plus the
answer contract. It NEVER calls an inference provider — the briefing is available
on a deployment with no local reasoner.

Constructed as a core service (mirror ``DiscoveryService``: resolved via
``_DIRECT_ATTR_SERVICES`` → ``orchestrator.get_service("context_service")``, not a
plugin binding). Provider-independent at runtime: it builds the pipeline factory
from platform services via the shared ``core.prompts.pipeline_construction`` home,
never touching the inference service.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ananta.core.prompts.agent_context_briefing import (
    BUNDLE_NAMES,
    group_blocks_into_briefing,
)
from ananta.core.prompts.profiles import AGENT_CONTEXT_PROFILE

if TYPE_CHECKING:
    from ananta.core.prompts.pipeline_factory import PromptPipelineFactory
    from ananta.services.context_management.config import ContextManagementConfig

logger = logging.getLogger(__name__)


class ContextService:
    """Assemble read-only agent-context briefings from the prompt pipeline."""

    def __init__(
        self,
        *,
        app_home: str,
        context_config: ContextManagementConfig,
        orchestrator: object,
        state_service: object | None,
    ) -> None:
        self._app_home = app_home
        self._context_config = context_config
        self._orchestrator = orchestrator
        self._state_service = state_service
        self._pipeline_factory: PromptPipelineFactory | None = None

    def _ensure_factory(self) -> PromptPipelineFactory:
        """Build the pipeline factory once, from platform services (no provider)."""
        if self._pipeline_factory is None:
            from ananta.core.prompts.pipeline_construction import (
                build_pipeline_dependencies,
            )
            from ananta.core.prompts.pipeline_factory import PromptPipelineFactory

            deps = build_pipeline_dependencies(
                app_home=self._app_home,
                context_config=self._context_config,
                orchestrator=self._orchestrator,
                state_service=self._state_service,
            )
            self._pipeline_factory = PromptPipelineFactory(deps)
            logger.info("ContextService: PromptPipelineFactory initialized")
        return self._pipeline_factory

    def assemble_agent_context(
        self,
        *,
        session_id: str,
        flow_id: str,
        context_id: str | None = None,
        budget: int | None = None,
        requested_bundles: list[str] | None = None,
    ) -> dict[str, Any]:
        """Assemble a retrieval/provenance-first agent-context briefing.

        Runs the ``agent_context`` profile pipeline (no serialization, no
        inference) and returns the grouped briefing dict. ``budget`` (optional)
        caps the block count; ``requested_bundles`` (optional) filters to named
        bundles. Both default to "include everything".
        """
        factory = self._ensure_factory()
        pipeline = factory.create_pipeline(AGENT_CONTEXT_PROFILE)

        ctx, _ = pipeline.execute(
            flow_id=flow_id,
            action_name="assemble_agent_context",
            session_id=session_id,
            action_params={},
            context_id=context_id,
            io_namespace=None,
            include_conversation_history=AGENT_CONTEXT_PROFILE.include_conversation_history,
            include_focused_memories=AGENT_CONTEXT_PROFILE.include_focused_memories,
            include_semantic_recall=AGENT_CONTEXT_PROFILE.include_semantic_recall,
            max_context_messages=AGENT_CONTEXT_PROFILE.max_context_messages,
        )

        briefing = group_blocks_into_briefing(
            tuple(ctx.message_blocks),
            output_schema=ctx.output_schema,
            budget=budget,
        )
        return _apply_bundle_filter(briefing, requested_bundles)


def _apply_bundle_filter(
    briefing: dict[str, Any],
    requested_bundles: list[str] | None,
) -> dict[str, Any]:
    """Restrict ``bundles`` to ``requested_bundles``.

    ``None`` returns every bundle. An unknown bundle name is a caller error and
    fails loud (fail-fast, no defensive silent-drop) — the caller must ask for
    real bundles. Filtering only prunes the ``bundles`` map; the manifest,
    provenance, and available_contracts still describe the full assembly so the
    caller can see what was assembled vs. surfaced.
    """
    if requested_bundles is None:
        return briefing
    unknown = [name for name in requested_bundles if name not in BUNDLE_NAMES]
    if unknown:
        raise ValueError(
            f"Unknown requested_bundles {unknown}; "
            f"valid bundle names: {sorted(BUNDLE_NAMES)}"
        )
    wanted = set(requested_bundles)
    bundles = briefing["bundles"]
    briefing["bundles"] = {name: items for name, items in bundles.items() if name in wanted}
    return briefing
