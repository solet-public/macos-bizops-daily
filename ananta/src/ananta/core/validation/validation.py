from ananta.core.services.action_validation_service import ActionValidationService


def validate_action(action: dict[str, object], definition: dict[str, object] | None) -> None:
    """Validate action structure and parameters using dedicated service."""
    validation_service = ActionValidationService()
    validation_service.validate_action_structure(action, definition)


_REQUIRED_FIELDS = ["action_status", "data", "actions", "error", "timestamp"]
_VALID_STATUSES = ["queued_for_processing", "processing", "completed", "error"]


def validate_action_response(response: dict[str, object]) -> dict[str, object] | None:
    """Validate action response structure."""
    error = _check_missing_fields(response)
    if error:
        return error

    error = _check_data_type(response)
    if error:
        return error

    error = _check_actions_type(response)
    if error:
        return error

    error = _check_action_status(response)
    if error:
        return error

    error = _check_error_field(response)
    if error:
        return error

    return None


def _check_missing_fields(response: dict[str, object]) -> dict[str, object] | None:
    """Check for missing required fields."""
    missing_fields = [f for f in _REQUIRED_FIELDS if f not in response]
    if missing_fields:
        return {
            "missing_fields": missing_fields,
            "received_fields": list(response.keys()),
            "message": "Response missing required fields",
        }
    return None


def _check_data_type(response: dict[str, object]) -> dict[str, object] | None:
    """Check that data field is a dictionary."""
    if not isinstance(response["data"], dict):
        return {
            "field": "data",
            "expected_type": "dict",
            "received_type": type(response["data"]).__name__,
            "message": "Response field 'data' must be a dictionary",
        }
    return None


def _check_actions_type(response: dict[str, object]) -> dict[str, object] | None:
    """Check that actions field is a list."""
    if not isinstance(response["actions"], list):
        return {
            "field": "actions",
            "expected_type": "list",
            "received_type": type(response["actions"]).__name__,
            "message": "Response field 'actions' must be a list",
        }
    return None


def _check_action_status(response: dict[str, object]) -> dict[str, object] | None:
    """Check that action_status has a valid value."""
    if response["action_status"] not in _VALID_STATUSES:
        return {
            "field": "action_status",
            "received_value": response["action_status"],
            "valid_values": _VALID_STATUSES,
            "message": "Response field 'action_status' has invalid value",
        }
    return None


def _check_error_field(response: dict[str, object]) -> dict[str, object] | None:
    """Check error field validity."""
    if response["action_status"] == "error" and response["error"] is None:
        return {
            "field": "error",
            "action_status": response["action_status"],
            "message": "Response with error status must include error details",
        }

    if response["error"] is not None and not isinstance(response["error"], dict):
        return {
            "field": "error",
            "expected_type": "dict or None",
            "received_type": type(response["error"]).__name__,
            "message": "Response field 'error' must be a dictionary or None",
        }
    return None
