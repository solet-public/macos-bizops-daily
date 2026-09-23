import logging

from ananta.services.state_service import StateService

logger = logging.getLogger(__name__)


class ActionSuggestionService:
    """
    Service for managing action suggestions and similarity matching.

    ARCHITECTURAL ROLE: Supporting service that extracts action suggestion logic
    from ActionValidator while maintaining validation pipeline integrity.

    This service handles:
    - Finding similar actions based on name patterns
    - Calculating action name similarity scores
    - Handling missing action scenarios with suggestions
    - Database queries for action suggestion generation
    """

    def __init__(self, state_service: StateService | None = None):
        """Initialize ActionSuggestionService."""
        self.state_service = state_service

    def find_similar_actions(self, action_name: str) -> list[str]:
        """
        Find similar action names from the action definitions registry.

        EXTRACTED FROM: ActionValidator._find_similar_actions() - B(10) complexity

        Args:
            action_name: Name of the action to find similarities for

        Returns:
            List of similar action names (max 5)
        """
        if not self.state_service:
            return []

        try:
            existing_actions = self._fetch_existing_action_names()
            return self._filter_similar_actions(action_name, existing_actions)
        except Exception as e:
            logger.error(f"Error finding similar actions: {e}")
            return []

    def _fetch_existing_action_names(self) -> list[str]:
        """Fetch action names from state service."""
        if not self.state_service:
            return []

        result = self.state_service.read_state(
            namespace="core", query={"table": "action_definitions", "filters": {}, "limit": 100}
        )
        records = self._extract_records_from_result(result)
        return [record.get("name", "") if isinstance(record, dict) else "" for record in records]

    def _extract_records_from_result(self, result: object) -> list[object]:
        """Extract records list from nested state service result."""
        if not isinstance(result, dict) or result.get("action_status") != "completed":
            return []

        data = result.get("data")
        if not isinstance(data, dict):
            return []

        result_data = data.get("result")
        if not isinstance(result_data, dict):
            return []

        records = result_data.get("records")
        if not isinstance(records, list):
            return []

        return records

    def _filter_similar_actions(self, action_name: str, existing_actions: list[str]) -> list[str]:
        """Filter existing actions to find similar ones."""
        similar_actions: list[str] = []
        action_lower = action_name.lower()

        for existing_action in existing_actions:
            if self._is_similar_action(action_lower, existing_action.lower()):
                similar_actions.append(existing_action)

        return similar_actions[:5]

    def _is_similar_action(self, action_lower: str, existing_lower: str) -> bool:
        """Check if two action names are similar."""
        return (
            action_lower in existing_lower
            or existing_lower in action_lower
            or self.calculate_similarity(action_lower, existing_lower) > 0.6
        )

    def calculate_similarity(self, str1: str, str2: str) -> float:
        """
        Calculate similarity score between two action names.

        EXTRACTED FROM: ActionValidator._calculate_similarity() - A(4) complexity

        Args:
            str1: First action name
            str2: Second action name

        Returns:
            Similarity score between 0.0 and 1.0
        """
        if not str1 or not str2:
            return 0.0

        # Simple Jaccard similarity for words
        words1 = set(str1.split("_"))
        words2 = set(str2.split("_"))

        intersection = len(words1.intersection(words2))
        union = len(words1.union(words2))

        return intersection / union if union > 0 else 0.0

    def is_potentially_correctable_action(self, action_name: str) -> bool:
        """
        Check if action name matches patterns that suggest it might be correctable.

        EXTRACTED FROM: ActionValidator._handle_missing_action() logic

        Args:
            action_name: Action name to check

        Returns:
            True if action appears correctable, False otherwise
        """
        correctable_patterns = ["list_", "describe_", "query_", "get_", "check_"]
        return any(action_name.startswith(pattern) for pattern in correctable_patterns)
