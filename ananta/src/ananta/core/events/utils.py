"""Event processing utilities for consistent action handling."""

import logging

logger = logging.getLogger(__name__)


def generate_action_name(
    action_data: dict[str, object], context: str = "unknown", modify_dict: bool = False
) -> str:
    """
    Generate a proper action name from action data.

    Extracts function name from process_key if name is missing.
    Follows process_key format: provider_type::provider::function_name

    Args:
        action_data: Action dictionary potentially containing 'name' or 'process_key'
        context: Context string for DEBUG logging (e.g., "legacy_conversion", "console_submission")
        modify_dict: If True, sets the generated name in action_data['name'] for EventProcessor

    Returns:
        str: Generated action name or "unknown" if no valid name can be determined
    """
    action_name = action_data.get("name")

    # Type narrow action_name to str if it exists and is a string
    if action_name is not None and isinstance(action_name, str) and action_name:
        return action_name

    # Try to generate from process_key
    if "process_key" in action_data:
        process_key = action_data["process_key"]
        # Type narrow process_key to str
        if isinstance(process_key, str):
            # Extract function name from process_key format: provider_type::provider::function_name
            generated_name = process_key.split("::")[-1] if "::" in process_key else process_key

            # Optionally set the name field in the action_dict for EventProcessor
            if modify_dict:
                action_data["name"] = generated_name

            return generated_name

    return "unknown"
