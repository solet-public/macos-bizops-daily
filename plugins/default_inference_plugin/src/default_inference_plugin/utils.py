import json
import logging
import re
from typing import Any

from ananta.core.plugins.plugin_contracts import ErrorCode
from ananta.error_handling import ExternalError, PluginError

logger = logging.getLogger(__name__)


def clean_json_response(response_str: str) -> dict[str, Any] | list[Any] | str:
    if not response_str:
        return ""

    # Try direct parsing first
    direct_result = _try_parse_json(response_str)
    if direct_result is not None:
        return direct_result

    # Clean markdown and extract JSON
    cleaned = re.sub(r"```(?:json)?", "", response_str, flags=re.IGNORECASE).strip()
    json_str = _extract_json_substring(cleaned)

    if json_str is None:
        return response_str

    extracted_result = _try_parse_json(json_str)
    if extracted_result is not None:
        return extracted_result

    logger.error("JSON parsing failed; returning raw string.")
    return json_str


def _try_parse_json(text: str) -> dict[str, Any] | list[Any] | None:
    """Try to parse JSON, returning None on failure."""
    try:
        result: dict[str, Any] | list[Any] = json.loads(text)
        return result
    except json.JSONDecodeError:
        return None


def _extract_json_substring(cleaned: str) -> str | None:
    """Extract JSON object or array substring from text."""
    first_brace = cleaned.find("{")
    first_bracket = cleaned.find("[")
    last_brace = cleaned.rfind("}")
    last_bracket = cleaned.rfind("]")

    is_object = first_brace != -1 and last_brace != -1
    is_array = first_bracket != -1 and last_bracket != -1

    if not is_object and not is_array:
        return None

    if is_object and (not is_array or first_brace < first_bracket):
        return cleaned[first_brace : last_brace + 1]
    return cleaned[first_bracket : last_bracket + 1]


def extract_json_from_content(content: str) -> dict[str, Any] | None:
    if not content:
        return None

    json_pattern = r"({[\s\S]*})"
    matches = re.search(json_pattern, content)

    if not matches:
        return None

    json_str = matches.group(1)
    try:
        result: dict[str, Any] = json.loads(json_str)
        return result
    except json.JSONDecodeError:
        return None


def normalize_model_response(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        if "status" in response and ("data" in response or "error" in response):
            return response

        if "error" in response:
            return {"status": "error", "error": response["error"], "data": None}

        return {"status": "success", "data": response, "actions": []}
    elif isinstance(response, str):
        try:
            parsed = json.loads(response)
            return normalize_model_response(parsed)
        except json.JSONDecodeError:
            return {"status": "success", "data": {"result": response}, "actions": []}
    else:
        return {"status": "success", "data": {"result": response}, "actions": []}


def serialize_state_for_prompt(state: dict[str, Any]) -> str:
    if not state:
        return ""
    return f"\n\nCurrent State:\n```json\n{json.dumps(state)}\n```"


def validate_json_file(file_path: str) -> dict[str, Any]:
    try:
        with open(file_path) as f:
            result: dict[str, Any] = json.load(f)
            return result
    except FileNotFoundError as e:
        raise ExternalError(
            f"File not found: {file_path}",
            error_code=ErrorCode.FILE_NOT_FOUND,
            details={"file_path": file_path},
            service_name="filesystem",
        ) from e
    except PermissionError as e:
        raise ExternalError(
            f"Permission denied for file: {file_path}",
            error_code=ErrorCode.PERMISSION_ERROR,
            details={"file_path": file_path},
            service_name="filesystem",
        ) from e
    except json.JSONDecodeError as e:
        raise PluginError(
            f"Invalid JSON in file {file_path}: {str(e)}",
            error_code=ErrorCode.JSON_PARSE_ERROR,
            details={"file_path": file_path, "error": str(e)},
            plugin_name="default_inference_plugin",
        ) from e
    except Exception as e:
        raise ExternalError(
            f"Error reading file {file_path}: {str(e)}",
            error_code=ErrorCode.FILE_ACCESS_ERROR,
            details={"file_path": file_path, "error": str(e)},
            service_name="filesystem",
        ) from e


def parse_json_string(json_str: str, source: str = "input") -> dict[str, Any]:
    if not json_str:
        raise PluginError(
            "Empty JSON string provided",
            error_code=ErrorCode.VALIDATION_ERROR,
            details={"source": source},
            plugin_name="default_inference_plugin",
        )

    try:
        result: dict[str, Any] = json.loads(json_str)
        return result
    except json.JSONDecodeError as e:
        raise PluginError(
            f"Invalid JSON: {str(e)}",
            error_code=ErrorCode.JSON_PARSE_ERROR,
            details={"source": source, "error": str(e)},
            plugin_name="default_inference_plugin",
        ) from e
