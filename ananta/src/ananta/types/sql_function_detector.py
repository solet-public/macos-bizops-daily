"""
SQL Function Detector Service

Responsibility: Handle SQL function and contract token detection for schema types
Dependencies: logging
Complexity: Medium-High - focused on pattern matching and SQL function identification

Extracted from ColumnDefinition god class (C11 complexity method)
"""

import logging

logger = logging.getLogger(__name__)


class SqlFunctionDetector:
    """
    Service for detecting SQL functions and contract tokens in default values.

    ARCHITECTURAL ROLE: Supporting service that extracts SQL function detection logic
    from ColumnDefinition while maintaining schema type integrity.

    This service handles:
    - Identifying SQL functions that should not be quoted in DEFAULT clauses
    - Detecting contract tokens used for database plugin interpretation
    - Pattern matching for function-like expressions
    - Case-insensitive function name recognition
    """

    def __init__(self) -> None:
        """Initialize SqlFunctionDetector with predefined function patterns."""
        # Common SQL functions that should not be quoted
        self.sql_functions = {
            "CURRENT_TIMESTAMP",
            "CURRENT_DATE",
            "CURRENT_TIME",
            "NOW()",
            "DATETIME('now')",
            "DATE('now')",
            "TIME('now')",
        }

    def is_sql_function(self, value: str) -> bool:
        """
        Detect SQL functions and contract tokens that should not be quoted in DEFAULT clauses.

        EXTRACTED FROM: ColumnDefinition._is_sql_function() - C(11) complexity

        Args:
            value: String value to check for SQL function patterns

        Returns:
            True if the value is a SQL function or contract token that should remain unquoted

        Examples:
            is_sql_function("CURRENT_TIMESTAMP") -> True
            is_sql_function("NOW()") -> True
            is_sql_function("__CONTRACT:TIMESTAMP__") -> True
            is_sql_function("pending") -> False
        """
        # Contract tokens should not be quoted - they're for database plugin interpretation
        if self._is_contract_token(value):
            return True

        # Check for exact function matches (case-insensitive)
        if self._is_exact_function_match(value):
            return True

        # Check for function patterns (expressions ending with parentheses)
        if self._is_function_pattern(value):
            return True

        return False

    def _is_contract_token(self, value: str) -> bool:
        """
        Check if value is a contract token.

        Args:
            value: String to check

        Returns:
            True if value matches contract token pattern
        """
        return value.startswith("__CONTRACT:") and value.endswith("__")

    def _is_exact_function_match(self, value: str) -> bool:
        """
        Check if value exactly matches a known SQL function (case-insensitive).

        Args:
            value: String to check

        Returns:
            True if value matches a known SQL function
        """
        return value.upper() in self.sql_functions

    def _is_function_pattern(self, value: str) -> bool:
        """
        Check if value matches SQL function patterns.

        Args:
            value: String to check for function patterns

        Returns:
            True if value matches function-like patterns
        """
        upper_value = value.upper()

        # Generic function pattern (ends with parentheses)
        if upper_value.endswith("()"):
            return True

        # Specific datetime function patterns
        datetime_patterns = [
            ("(NOW()", ")"),
            ("DATETIME(", ")"),
            ("DATE(", ")"),
            ("TIME(", ")"),
        ]

        for start_pattern, end_pattern in datetime_patterns:
            if upper_value.startswith(start_pattern) and upper_value.endswith(end_pattern):
                return True

        return False

    def add_function(self, function_name: str) -> None:
        """
        Add a new SQL function to the recognition list.

        Args:
            function_name: Name of the SQL function to add
        """
        self.sql_functions.add(function_name.upper())

    def remove_function(self, function_name: str) -> bool:
        """
        Remove a SQL function from the recognition list.

        Args:
            function_name: Name of the SQL function to remove

        Returns:
            True if function was removed, False if it wasn't in the list
        """
        try:
            self.sql_functions.remove(function_name.upper())
            return True
        except KeyError:
            logger.error(f"Function not found for removal: {function_name.upper()}")
            return False
