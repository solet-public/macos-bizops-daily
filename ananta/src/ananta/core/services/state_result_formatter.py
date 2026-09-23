"""Service for formatting state query results in template functions."""

from typing import cast


class StateResultFormatter:
    """Handles formatting of state service results for template substitution."""

    def format_result(self, result: object) -> str:
        """Format state service result for template substitution."""
        if not self._is_valid_result(result):
            return ""

        # After _is_valid_result check, we know result is a dict with "data" key
        result_dict = cast(dict[str, object], result)
        data = result_dict["data"]

        # Handle nested result format (current format)
        if self._is_nested_result_format(data):
            # After format check, we know data is a dict
            data_dict = cast(dict[str, object], data)
            return self._format_nested_result(data_dict)

        # Handle direct records structure (alternative format)
        elif self._is_direct_records_format(data):
            # After format check, we know data is a dict
            data_dict = cast(dict[str, object], data)
            return self._format_direct_records(data_dict)

        # Fallback to string representation
        return str(data)

    def _is_valid_result(self, result: object) -> bool:
        """Check if result has valid data structure."""
        return isinstance(result, dict) and "data" in result and result["data"]

    def _is_nested_result_format(self, data: object) -> bool:
        """Check if data follows nested result format."""
        return isinstance(data, dict) and "result" in data

    def _is_direct_records_format(self, data: object) -> bool:
        """Check if data follows direct records format."""
        return isinstance(data, dict) and "records" in data

    def _format_nested_result(self, data: dict[str, object]) -> str:
        """Format nested result structure."""
        result_obj = data["result"]
        # Cast to dict after we know it exists
        result_dict = cast(dict[str, object], result_obj)
        records = result_dict.get("records", [])
        # Cast records to expected type after validation
        typed_records = cast(list[dict[str, object]], records) if records else []
        return self._format_records(typed_records) if typed_records else ""

    def _format_direct_records(self, data: dict[str, object]) -> str:
        """Format direct records structure."""
        records = data.get("records", [])
        # Cast records to expected type after validation
        typed_records = cast(list[dict[str, object]], records) if records else []
        return self._format_records(typed_records) if typed_records else ""

    def _format_records(self, records: list[dict[str, object]]) -> str:
        """Format records based on their content type."""
        if not records:
            return ""

        # Check if this is a process registry query
        if self._is_process_registry_query(records):
            return self._format_process_registry_records(records)

        # Format as conversation history/console messages
        return self._format_console_message_records(records)

    def _is_process_registry_query(self, records: list[dict[str, object]]) -> bool:
        """Check if records contain process_key field."""
        return any("process_key" in record for record in records)

    def _format_process_registry_records(self, records: list[dict[str, object]]) -> str:
        """Format process registry records as JSON for LLM consumption.

        Returns the full process registry entries as JSON string so the LLM has access
        to all metadata including parameters, return schemas, examples, and usage guidance.
        """
        import json

        # Return full records as JSON array, not just process_keys
        # This provides LLM with complete metadata for generating correct action plans
        return json.dumps(records, default=str)

    def _format_console_message_records(self, records: list[dict[str, object]]) -> str:
        """Format console message records."""
        formatted_records = [
            f"{record.get('message_type', 'message')}: {record.get('content', '')}"
            for record in records
        ]
        return " | ".join(formatted_records)
