"""GuidanceStage — inject step guidance for plan-driven vertices.

Runs after CatalogStage, before DecodeContractStage.  Populates
``ctx.step_guidance_messages`` and may update ``ctx.user_prompt``
with step driver instructions.

Self-sufficient: reads from ``ctx.plan_state`` (pre-computed by
``PlanStateStage``) and calls platform functions.  No plugin callbacks
for step guidance.  Vertex enrichment (trailer, action shapes) is a
separate concern handled by the ``vertex_enricher`` callback.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ananta.core.prompts.context import PromptContext
from ananta.core.prompts.plan_drivers.step_guidance import (
    GuidanceArticleReader,
    ProcessDataLookup,
    compute_step_guidance,
)

logger = logging.getLogger(__name__)


class GuidanceStage:
    """Compute step guidance and enrich vertex instructions.

    Step guidance is self-sufficient via ``compute_step_guidance``
    from ``step_guidance.py``.  Vertex enrichment (trailer reminders,
    action shapes) remains a thin plugin callback until F4 extraction.
    """

    stage_name = "guidance"

    def __init__(
        self,
        *,
        article_reader: GuidanceArticleReader,
        process_lookup: ProcessDataLookup,
        vertex_enricher: Callable[[PromptContext], None] | None = None,
    ) -> None:
        self._article_reader = article_reader
        self._process_lookup = process_lookup
        self._vertex_enricher = vertex_enricher

    @property
    def name(self) -> str:
        return self.stage_name

    def execute(self, ctx: PromptContext) -> PromptContext:
        """Compute step guidance, then apply vertex enrichment."""
        if ctx.plan_state is None:
            raise RuntimeError(
                "GuidanceStage requires ctx.plan_state — "
                "PlanStateStage must run first"
            )

        original_prompt = ctx.user_prompt

        # Step guidance — self-sufficient platform call
        result = compute_step_guidance(
            ctx.plan_state,
            tool_observation=ctx.tool_observation,
            has_focused_plan=ctx.has_focused_plan,
            output_schema=ctx.output_schema,
            system_prompt=ctx.system_prompt,
            user_prompt=ctx.user_prompt,
            session_id=ctx.session_id or "",
            raw_observation_dict=ctx.raw_observation_dict,
            is_process_error=_is_process_error(ctx),
            article_reader=self._article_reader,
            process_lookup=self._process_lookup,
        )

        if result.guidance_messages:
            ctx.step_guidance_messages.extend(result.guidance_messages)
        if result.user_prompt is not None:
            ctx.user_prompt = result.user_prompt

        # Vertex enrichment — thin plugin callback (F4 extraction target)
        if self._vertex_enricher is not None:
            self._vertex_enricher(ctx)

        # Decision logging
        decisions: list[str] = []
        new_messages = len(result.guidance_messages)
        if new_messages:
            decisions.append(f"{new_messages} guidance message(s)")
        if ctx.user_prompt != original_prompt:
            decisions.append("user prompt enriched")
        if not decisions:
            decisions.append("no changes")
        ctx.add_decision(self.stage_name, "; ".join(decisions))

        return ctx


def _is_process_error(ctx: PromptContext) -> bool:
    """Check if the current vertex is a process_error."""
    if not ctx.raw_observation_dict:
        return False
    action_result = ctx.raw_observation_dict.get("action_result")
    if not isinstance(action_result, dict):
        return False
    status = action_result.get("action_status", "")
    return status in ("error", "failed")
