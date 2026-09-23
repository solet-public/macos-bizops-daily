"""Service for parsing template function arguments."""

import ast
import re

from ananta.core.templates.template_exceptions import TemplateFunctionError


class TemplateArgumentParser:
    """Handles parsing of template function arguments."""

    def parse_read_state_args(self, args: str) -> tuple[str, dict[str, object]]:
        """Parse read_state function arguments.

        Expected format: read_state(namespace="core", query={"table": "process_registry", ...})

        Returns:
            Tuple of (namespace, query)
        """
        if not args:
            raise TemplateFunctionError("read_state", args, "read_state requires arguments")

        namespace = self._extract_namespace(args)
        query = self._extract_query(args)

        return namespace, query

    def _extract_namespace(self, args: str) -> str:
        """Extract namespace from arguments.

        Fails immediately if namespace parameter is missing.
        """
        namespace_match = re.search(r'namespace=["\']([^"\']+)["\']', args)

        if not namespace_match:
            raise TemplateFunctionError(
                "read_state", args, "Missing required 'namespace' parameter"
            )

        return namespace_match.group(1)

    def _extract_query(self, args: str) -> dict[str, object]:
        """Extract query object from arguments using ast.literal_eval for safe parsing.

        Expects Python dict literal format: query={'key': 'value', 'nested': {'key2': 'value2'}}

        Uses ast.literal_eval for safe evaluation of Python literals (no code execution).
        Fails immediately if the query parameter is malformed.
        """
        query_match = re.search(r"query=(.+?)(?:,\s*\w+=|$)", args, re.DOTALL)

        if not query_match:
            raise TemplateFunctionError("read_state", args, "Missing required 'query' parameter")

        query_str = query_match.group(1).strip()

        try:
            parsed = ast.literal_eval(query_str)
        except (ValueError, SyntaxError) as e:
            raise TemplateFunctionError(
                "read_state",
                args,
                f"Invalid query parameter syntax. Expected Python dict literal. "
                f"Error: {str(e)}. Query string: {query_str}",
            ) from e

        if not isinstance(parsed, dict):
            raise TemplateFunctionError(
                "read_state", args, f"query parameter must be a dict, got {type(parsed).__name__}"
            )

        return parsed
        # SAFE: ast.literal_eval only evaluates literals, runtime check ensures dict
