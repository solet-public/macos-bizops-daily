"""PromptPipeline - Orchestrates prompt assembly through observable stages.

The pipeline executes stages in order, measuring timing for each,
and produces a manifest for debugging.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from ananta.core.prompts.context import PromptContext
from ananta.core.prompts.manifest import PromptManifest

if TYPE_CHECKING:
    from ananta.core.prompts.stages.base import PromptStage

logger = logging.getLogger(__name__)


class PromptPipeline:
    """Orchestrates prompt assembly through observable stages.

    Usage:
        pipeline = PromptPipeline([
            TemplateStage(template_engine),
            FormatStage(prompts_dir),
            ContextStage(context_builder),
            APIStage(),
        ])

        ctx, manifest = pipeline.execute(
            flow_id="flow_123",
            action_name="evaluate_input",
            session_id="sess_456",
            action_params=params,
        )

    Attributes:
        stages: Ordered list of stages to execute
    """

    def __init__(self, stages: list[PromptStage]) -> None:
        """Initialize pipeline with stages.

        Args:
            stages: List of PromptStage implementations to execute in order
        """
        self._stages = stages

    def execute(
        self,
        flow_id: str,
        action_name: str,
        session_id: str,
        action_params: dict[str, Any],
        context_id: str | None = None,
        io_namespace: str | None = None,
        *,
        include_conversation_history: bool = True,
        include_focused_memories: bool = True,
        include_semantic_recall: bool = True,
        max_context_messages: int | None = None,
    ) -> tuple[PromptContext, PromptManifest]:
        """Execute pipeline and return context and manifest.

        Args:
            flow_id: Unique identifier for the action flow
            action_name: Name of the action being executed
            session_id: Session identifier for context retrieval
            action_params: Raw action parameters from action definition
            context_id: Optional context stream ID for platform-managed context
            io_namespace: Pre-resolved IO plugin namespace for metadata trailers
            include_conversation_history: Profile flag — load conversation history
            include_focused_memories: Profile flag — load focused memories
            include_semantic_recall: Profile flag — load semantic recall
            max_context_messages: Profile flag — max conversation messages

        Returns:
            Tuple of (completed PromptContext, PromptManifest for debugging)

        Raises:
            Any exception raised by a stage will propagate up
        """
        ctx = PromptContext(
            flow_id=flow_id,
            action_name=action_name,
            session_id=session_id,
            context_id=context_id,
            io_namespace=io_namespace,
            raw_action_params=action_params,
            profile_include_conversation_history=include_conversation_history,
            profile_include_focused_memories=include_focused_memories,
            profile_include_semantic_recall=include_semantic_recall,
            profile_max_context_messages=max_context_messages,
        )

        for stage in self._stages:
            start = time.perf_counter()
            ctx = stage.execute(ctx)
            elapsed_ms = (time.perf_counter() - start) * 1000
            ctx.stage_timings[stage.name] = elapsed_ms

        manifest = PromptManifest.from_context(ctx)
        logger.debug(manifest.to_log_string())

        return ctx, manifest

    @property
    def stage_names(self) -> list[str]:
        """Get names of all stages in order.

        Returns:
            List of stage names
        """
        return [stage.name for stage in self._stages]
