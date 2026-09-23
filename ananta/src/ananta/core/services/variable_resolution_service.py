"""Service for resolving template variable substitutions."""

import logging
from collections.abc import Sequence

from ananta.core.contexts.action_contexts import TemplateResolutionContext

logger = logging.getLogger(__name__)


class VariableResolutionService:
    """Handles variable resolution for template engines."""

    def resolve_variable(
        self, var_name: str, context: TemplateResolutionContext, config: dict[str, object]
    ) -> str | None:
        """Resolve variable substitution using metadata-configured precedence."""

        # Use metadata-defined precedence order
        precedence_order_raw = config.get("precedence_order", ["runtime_args", "state"])

        # Type narrow to ensure it's a sequence
        if not isinstance(precedence_order_raw, Sequence):
            logger.error("precedence_order is not a sequence, using default")
            precedence_order_raw = ["runtime_args", "state"]

        precedence_order = precedence_order_raw

        for source in precedence_order:
            # Type narrow source to str
            if not isinstance(source, str):
                logger.error("Skipping non-string source: %s", source)
                continue

            result = self._resolve_from_source(var_name, context, source)
            if result is not None:
                return result

        return None

    def _resolve_from_source(
        self, var_name: str, context: TemplateResolutionContext, source: str
    ) -> str | None:
        """Resolve variable from a specific source."""
        if source == "runtime_args":
            return self._resolve_from_runtime_args(var_name, context)
        elif source == "state":
            return self._resolve_from_state(var_name, context)
        elif source == "global_vars":
            return self._resolve_from_global_vars(var_name, context)
        elif source == "environment":
            return self._resolve_from_environment(var_name, context)
        elif source == "user_state":
            return self._resolve_from_user_state(var_name, context)
        else:
            logger.error("Unknown variable source: %s", source)
            return None

    def _resolve_from_runtime_args(
        self, var_name: str, context: TemplateResolutionContext
    ) -> str | None:
        """Resolve variable from runtime_args context."""
        runtime_args = context.runtime_args

        # Try exact match first
        value = runtime_args.get(var_name)
        if value is not None:
            # Type narrow the value to str
            if not isinstance(value, str):
                logger.error("Value for %s is not a string: %s", var_name, type(value))
                return None
            return value

        # Try case-insensitive match for template variable convention
        lower_var_name = var_name.lower()
        value = runtime_args.get(lower_var_name)
        if value is not None:
            # Type narrow the value to str
            if not isinstance(value, str):
                logger.error("Value for %s is not a string: %s", lower_var_name, type(value))
                return None
            return value

        return None

    def _resolve_from_state(
        self, var_name: str, context: TemplateResolutionContext
    ) -> str | None:
        """Resolve variable from state context."""
        state = context.state

        value = state.get(var_name)
        if value is not None:
            # Type narrow the value to str
            if not isinstance(value, str):
                logger.error("Value for %s is not a string: %s", var_name, type(value))
                return None
            return value

        return None

    def _resolve_from_global_vars(
        self, var_name: str, context: TemplateResolutionContext
    ) -> str | None:
        """Resolve variable from global_vars context."""
        global_vars = context.global_vars

        value = global_vars.get(var_name)
        if value is not None:
            # Type narrow the value to str
            if not isinstance(value, str):
                logger.error("Value for %s is not a string: %s", var_name, type(value))
                return None
            return value

        return None

    def _resolve_from_environment(
        self, var_name: str, context: TemplateResolutionContext
    ) -> str | None:
        """Resolve variable from environment context."""
        environment = context.environment

        value = environment.get(var_name)
        if value is not None:
            # Type narrow the value to str
            if not isinstance(value, str):
                logger.error("Value for %s is not a string: %s", var_name, type(value))
                return None
            return value

        return None

    def _resolve_from_user_state(
        self, var_name: str, context: TemplateResolutionContext
    ) -> str | None:
        """Resolve variable from user_state context."""
        user_state = context.user_state

        value = user_state.get(var_name)
        if value is not None:
            # Type narrow the value to str
            if not isinstance(value, str):
                logger.error("Value for %s is not a string: %s", var_name, type(value))
                return None
            return value

        return None
