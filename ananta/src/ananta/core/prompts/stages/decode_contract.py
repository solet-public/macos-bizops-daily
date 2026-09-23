"""DecodeContractStage — adjust output schema for constrained decoding.

Runs after GuidanceStage, before APIStage.  Updates ctx.output_schema
based on the current plan step, WBS state, and process keys.  Does NOT
touch ctx.messages — that's APIStage's job.

Self-sufficient: reads from ``ctx.plan_state`` (pre-computed by
``PlanStateStage``) and calls platform functions.  No plugin callbacks.
"""

from __future__ import annotations

import logging

from ananta.core.prompts.context import PromptContext
from ananta.core.prompts.decode.step_schema import (
    PlanAdvancer,
    ProcessArgLookup,
    build_step_schema,
)

logger = logging.getLogger(__name__)


def _is_delivery_confirmation(ctx: PromptContext) -> bool:
    """Check if the current vertex is a delivery confirmation.

    Delivery confirmation occurs after a post_message action has been
    executed.  The tool observation contains the post_message result.
    """
    if not ctx.tool_observation:
        return False
    obs = ctx.tool_observation.lower()
    return "post_message" in obs and (
        "message posted" in obs or "message delivered" in obs
    )


class DecodeContractStage:
    """Adjust output schema for the current step.

    Self-sufficient: calls ``build_step_schema`` from
    ``step_schema.py`` using injected ``ProcessArgLookup`` and
    ``PlanAdvancer`` protocols.  No plugin callback.
    """

    stage_name = "decode_contract"

    def __init__(
        self,
        *,
        process_arg_lookup: ProcessArgLookup,
        plan_advancer: PlanAdvancer | None = None,
    ) -> None:
        self._process_arg_lookup = process_arg_lookup
        self._plan_advancer = plan_advancer

    @property
    def name(self) -> str:
        return self.stage_name

    def execute(self, ctx: PromptContext) -> PromptContext:
        """Adjust ctx.output_schema for constrained decoding."""
        if ctx.plan_state is None:
            raise RuntimeError(
                "DecodeContractStage requires ctx.plan_state — "
                "PlanStateStage must run first"
            )

        result = build_step_schema(
            plan_state=ctx.plan_state,
            process_arg_lookup=self._process_arg_lookup,
            base_schema=ctx.output_schema,
            tool_observation=ctx.tool_observation,
            raw_observation_dict=ctx.raw_observation_dict,
            io_namespace=ctx.io_namespace,
            has_focused_plan=ctx.has_focused_plan,
            is_delivery_confirmation=_is_delivery_confirmation(ctx),
            plan_advancer=self._plan_advancer,
            session_id=ctx.session_id,
        )

        schema_changed = result.output_schema is not ctx.output_schema
        ctx.output_schema = result.output_schema

        if result.current_step_process_keys:
            ctx.current_step_process_keys = result.current_step_process_keys
        if result.model_visible_process_keys:
            ctx.model_visible_process_keys = result.model_visible_process_keys

        label = "output schema adjusted" if schema_changed else "output schema unchanged"
        ctx.add_decision(self.stage_name, label)
        return ctx
