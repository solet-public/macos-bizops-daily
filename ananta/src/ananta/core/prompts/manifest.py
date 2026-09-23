"""PromptManifest - Debugging output for prompt assembly.

The manifest provides a human-readable trace of what happened during
prompt assembly, including timing, decisions, and counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ananta.core.prompts.context import PromptContext


@dataclass(slots=True)
class PromptManifest:
    """Debugging trace of prompt assembly.

    Attributes:
        flow_id: Unique identifier for the action flow
        action_name: Name of the action being executed
        session_id: Session identifier
        total_ms: Total pipeline execution time in milliseconds
        stage_timings: Execution time per stage
        stage_decisions: Decisions made by each stage
        template_count: Number of templates resolved
        memory_count: Total memory items injected
        message_count: Final message count in payload
    """

    flow_id: str
    action_name: str
    session_id: str
    total_ms: float
    stage_timings: dict[str, float]
    stage_decisions: dict[str, list[str]]
    template_count: int
    memory_count: int
    message_count: int

    @classmethod
    def from_context(cls, ctx: PromptContext) -> PromptManifest:
        """Build manifest from completed pipeline context.

        Args:
            ctx: The PromptContext after all stages have executed

        Returns:
            A PromptManifest summarizing the pipeline execution
        """
        return cls(
            flow_id=ctx.flow_id,
            action_name=ctx.action_name,
            session_id=ctx.session_id,
            total_ms=sum(ctx.stage_timings.values()),
            stage_timings=dict(ctx.stage_timings),
            stage_decisions=dict(ctx.stage_decisions),
            template_count=len(ctx.template_substitutions),
            memory_count=ctx.get_total_memory_count(),
            message_count=len(ctx.messages),
        )

    def to_log_string(self) -> str:
        """Format manifest for logging.

        Returns:
            Human-readable multi-line string representation
        """
        lines = [
            f"=== Prompt Manifest: {self.action_name} ===",
            f"Flow: {self.flow_id} | Session: {self.session_id}",
            f"Total: {self.total_ms:.1f}ms | Templates: {self.template_count} | "
            f"Memory: {self.memory_count} | Messages: {self.message_count}",
            "",
        ]

        for stage_name, timing in self.stage_timings.items():
            lines.append(f"[{stage_name}] {timing:.1f}ms")
            for decision in self.stage_decisions.get(stage_name, []):
                lines.append(f"  - {decision}")

        lines.append("=" * 40)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, object]:
        """Convert manifest to dictionary for JSON logging.

        Returns:
            Dictionary representation suitable for JSON serialization
        """
        return {
            "flow_id": self.flow_id,
            "action_name": self.action_name,
            "session_id": self.session_id,
            "total_ms": self.total_ms,
            "stage_timings": self.stage_timings,
            "stage_decisions": self.stage_decisions,
            "template_count": self.template_count,
            "memory_count": self.memory_count,
            "message_count": self.message_count,
        }
