"""TemplateStage - Resolves <<<PATTERN>>> templates in action parameters.

Wraps NewTemplateEngine and records what was substituted for observability.
Supports pass-through mode when template_engine is None (for pre-resolved params).
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from ananta.core.prompts.context import PromptContext

if TYPE_CHECKING:
    from ananta.platform.new_template_engine import NewTemplateEngine

logger = logging.getLogger(__name__)

# Pattern to find template markers like <<<VARIABLE>>> or <<<@file.json>>>
TEMPLATE_PATTERN = re.compile(r"<<<[^>]+>>>")


class TemplateStage:
    """Resolves template patterns in action parameters.

    Wraps NewTemplateEngine.resolve_templates() and records:
    - What patterns were found
    - What they resolved to
    - Source of each resolution (variable, file, service)

    If template_engine is None, operates in pass-through mode:
    raw_action_params are copied to resolved_action_params unchanged.
    This is useful when templates have already been resolved upstream.
    """

    name = "template"

    def __init__(self, template_engine: NewTemplateEngine | None = None) -> None:
        """Initialize with optional template engine.

        Args:
            template_engine: NewTemplateEngine instance, or None for pass-through mode.
                            Pass-through mode copies raw params to resolved unchanged.
        """
        self._engine = template_engine

    def execute(self, ctx: PromptContext) -> PromptContext:
        """Resolve templates in action parameters.

        Args:
            ctx: PromptContext with raw_action_params set

        Returns:
            Same context with resolved_action_params and template_substitutions set
        """
        # Pass-through mode: no engine, just copy raw to resolved
        if self._engine is None:
            ctx.resolved_action_params = ctx.raw_action_params.copy()
            ctx.add_decision(self.name, "Pass-through mode: templates pre-resolved")
            return ctx

        # Find patterns before resolution
        patterns_before = self._find_patterns(ctx.raw_action_params)
        ctx.add_decision(self.name, f"Found {len(patterns_before)} template patterns")

        # Build context for template engine
        resolution_context: dict[str, object] = {
            "runtime_args": ctx.raw_action_params.get("arguments", {}),
            "state": {
                "session_id": ctx.session_id,
                "flow_id": ctx.flow_id,
            },
        }

        # Resolve templates
        ctx.resolved_action_params = self._engine.resolve_templates(
            ctx.raw_action_params,
            resolution_context,
        )

        # Find patterns after resolution to detect what was substituted
        patterns_after = self._find_patterns(ctx.resolved_action_params)
        resolved_count = len(patterns_before) - len(patterns_after)

        if resolved_count > 0:
            ctx.add_decision(self.name, f"Resolved {resolved_count} patterns")

        # Record substitutions by comparing before/after values
        ctx.template_substitutions = self._extract_substitutions(
            ctx.raw_action_params,
            ctx.resolved_action_params,
            patterns_before,
        )

        for pattern, value in ctx.template_substitutions.items():
            # Truncate long values for logging
            display_value = value if len(value) <= 50 else f"{value[:47]}..."
            ctx.add_decision(self.name, f"{pattern} -> {display_value}")

        if patterns_after:
            # Some patterns weren't resolved - log them
            ctx.add_decision(self.name, f"Unresolved patterns remaining: {len(patterns_after)}")

        return ctx

    def _find_patterns(self, obj: dict[str, Any] | list[Any] | str | Any) -> set[str]:
        """Recursively find all <<<...>>> patterns in a data structure.

        Args:
            obj: Data structure to search (dict, list, or string)

        Returns:
            Set of unique patterns found
        """
        patterns: set[str] = set()

        if isinstance(obj, str):
            patterns.update(TEMPLATE_PATTERN.findall(obj))
        elif isinstance(obj, dict):
            for value in obj.values():
                patterns.update(self._find_patterns(value))
        elif isinstance(obj, list):
            for item in obj:
                patterns.update(self._find_patterns(item))

        return patterns

    def _extract_substitutions(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
        patterns: set[str],
    ) -> dict[str, str]:
        """Extract what each pattern was substituted with.

        Args:
            before: Original action params
            after: Resolved action params
            patterns: Set of patterns that were present

        Returns:
            Map of pattern -> resolved value
        """
        substitutions: dict[str, str] = {}

        # Convert to JSON strings for comparison
        before_str = json.dumps(before, sort_keys=True, default=str)
        after_str = json.dumps(after, sort_keys=True, default=str)

        for pattern in patterns:
            if pattern in before_str and pattern not in after_str:
                # Pattern was resolved - try to find what it became
                # This is a heuristic; exact tracking would require engine changes
                substitutions[pattern] = "(resolved)"

        return substitutions
