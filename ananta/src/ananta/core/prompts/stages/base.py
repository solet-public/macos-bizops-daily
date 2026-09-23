"""Base protocol for prompt pipeline stages.

All stages must implement this protocol to be used in the pipeline.
"""

from typing import Protocol

from ananta.core.prompts.context import PromptContext


class PromptStage(Protocol):
    """Protocol for pipeline stages.

    Each stage:
    1. Has a unique name for logging/tracing
    2. Executes on a PromptContext, mutating it
    3. Returns the same context object

    Stages should use ctx.add_decision(stage_name, decision) to record
    what they did for observability.
    """

    @property
    def name(self) -> str:
        """Unique name for this stage (used in logging and manifest)."""
        ...

    def execute(self, ctx: PromptContext) -> PromptContext:
        """Execute stage logic on the context.

        Args:
            ctx: The PromptContext accumulator to mutate

        Returns:
            The same PromptContext object (mutated in place)

        Raises:
            Any exception indicates stage failure; pipeline will not continue
        """
        ...
