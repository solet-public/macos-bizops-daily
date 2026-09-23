"""PlanStateStage — compute structured plan state for prompt assembly.

Runs after ContextStage (which loads focused memories) and before
CatalogStage/GuidanceStage/DecodeContractStage.  Parses the focused
plan once and populates ``PlanState`` on ``PromptContext`` so
downstream stages consume structured data instead of reparsing.
"""

from __future__ import annotations

import logging

from ananta.core.prompts.context import PromptContext
from ananta.core.prompts.plan_state import compute_plan_state

logger = logging.getLogger(__name__)


class PlanStateStage:
    """Compute plan state from focused memories and pre-resolved IO namespace."""

    stage_name = "plan_state"

    @property
    def name(self) -> str:
        return self.stage_name

    def execute(self, ctx: PromptContext) -> PromptContext:
        """Parse focused plan and populate plan state on context."""
        state = compute_plan_state(
            focused_memories=ctx.focused_memories,
            io_namespace=ctx.io_namespace,
        )

        ctx.plan_state = state
        ctx.has_focused_plan = state.has_focused_plan
        ctx.is_wbs_execution_context = state.is_wbs_execution

        if state.all_process_keys:
            ctx.current_step_process_keys = list(state.all_process_keys)
        if state.model_visible_keys:
            ctx.model_visible_process_keys = list(state.model_visible_keys)

        ctx.add_decision(self.stage_name, f"has_plan={state.has_focused_plan}")
        if state.current_step_number is not None:
            ctx.add_decision(
                self.stage_name,
                f"step={state.current_step_number}, "
                f"wbs={state.is_wbs_execution}, "
                f"keys={list(state.all_process_keys)}",
            )

        return ctx
