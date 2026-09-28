from typing import Any

from ananta.error_handling import PluginError

from .errors import InferenceErrorCode


def validate_model_parameters(model_info: Any) -> dict[str, Any]:
    """Validate model parameters structure.

    Note: 'name' field is optional here - _validate_model_config will inject
    the configured model name if not provided. Empty dict {} is valid and will
    get the configured model name and defaults injected.
    """
    # Reject None but allow empty dict (will get defaults injected)
    if model_info is None:
        raise PluginError(
            message="Model information is required (use empty {} for defaults)",
            error_code=InferenceErrorCode.PARAMETER_ERROR,
            details={"provided_value": "None"},
        )

    if not isinstance(model_info, dict):
        raise PluginError(
            message=f"Model configuration must be a dictionary, got {type(model_info).__name__}",
            error_code=InferenceErrorCode.PARAMETER_ERROR,
            details={"provided_type": type(model_info).__name__},
        )

    # Note: 'name' field is NOT required here - _validate_model_config will
    # inject the configured model name if omitted. Reject only "default" or
    # "None" as ambiguous (explicit mismatch handled in _validate_model_config).
    name = model_info.get("name")
    if name in ("default", "None"):
        raise PluginError(
            message=(
                f"model.name '{name}' is ambiguous - "
                "omit for configured model or specify exact name"
            ),
            error_code=InferenceErrorCode.PARAMETER_ERROR,
            details={"provided_name": name},
        )

    return model_info


def validate_prompt_parameters(prompt_info: Any) -> dict[str, Any]:
    if not prompt_info:
        raise PluginError(
            message="Empty prompt provided",
            error_code=InferenceErrorCode.PARAMETER_ERROR,
            details={"prompt": str(prompt_info)},
        )

    if isinstance(prompt_info, str):
        if not prompt_info.strip():
            raise PluginError(
                message="Empty string prompt provided",
                error_code=InferenceErrorCode.PARAMETER_ERROR,
            )
        return {"user": prompt_info.strip()}

    if isinstance(prompt_info, dict):
        if "user" not in prompt_info and "system" not in prompt_info:
            raise PluginError(
                message="Prompt dictionary must contain 'user' or 'system' key",
                error_code=InferenceErrorCode.PARAMETER_ERROR,
                details={"prompt_keys": list(prompt_info.keys())},
            )
        return prompt_info

    raise PluginError(
        message=f"Unsupported prompt type: {type(prompt_info).__name__}",
        error_code=InferenceErrorCode.PARAMETER_ERROR,
        details={"prompt_type": type(prompt_info).__name__},
    )


def validate_provider(provider_name: str, available_providers: dict[str, Any]) -> None:
    if not provider_name:
        raise PluginError(
            message="Provider name cannot be empty",
            error_code=InferenceErrorCode.PARAMETER_ERROR,
        )

    if provider_name not in available_providers:
        raise PluginError(
            message=f"Unsupported model provider: {provider_name}",
            error_code=InferenceErrorCode.UNSUPPORTED_MODEL,
            details={
                "requested_provider": provider_name,
                "available_providers": list(available_providers.keys()),
            },
        )


def validate_action_parameters(
    action_params: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate action parameters for inference requests.

    FAIL-FAST: Requires 'model' and 'prompt' in action_params directly.
    No fallback to plugin_config - action templates must include these fields.

    Note: model_info can be empty dict {} which is valid - _validate_model_config
    will inject the configured model name and defaults. Only None is rejected.
    """
    model_info = action_params.get("model")
    prompt_info = action_params.get("prompt")

    # Check for None explicitly - empty dict {} is valid for model_info
    if model_info is None or prompt_info is None:
        raise PluginError(
            message="Missing 'model' or 'prompt' in action parameters",
            error_code=InferenceErrorCode.PARAMETER_ERROR,
            details={
                "model_present": model_info is not None,
                "prompt_present": prompt_info is not None,
                "action_params_keys": list(action_params.keys()),
            },
        )

    validated_model = validate_model_parameters(model_info)
    validated_prompt = validate_prompt_parameters(prompt_info)

    return validated_model, validated_prompt


def validate_provider_response(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise PluginError(
            message=f"Provider response must be a dictionary, got {type(response).__name__}",
            error_code=InferenceErrorCode.INVALID_RESPONSE,
            details={"response_type": type(response).__name__},
        )

    if "error" in response and response["error"] is not None:
        return response

    if "content" not in response and "data" not in response:
        raise PluginError(
            message="Provider response missing required 'content' or 'data' fields",
            error_code=InferenceErrorCode.INVALID_RESPONSE,
            details={"response_keys": list(response.keys())},
        )

    return response


def validate_provider_options(
    temperature: float | None = None,
    max_tokens: int | None = None,
    extra_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    options = {}

    if temperature is not None:
        if not isinstance(temperature, int | float):  # type: ignore[reportUnnecessaryIsInstance]
            raise PluginError(
                message=f"Temperature must be a number, got {type(temperature).__name__}",
                error_code=InferenceErrorCode.PARAMETER_ERROR,
                details={"temperature": temperature},
            )

        if temperature < 0 or temperature > 2:
            raise PluginError(
                message=f"Temperature should be between 0 and 2, got {temperature}",
                error_code=InferenceErrorCode.PARAMETER_ERROR,
                details={"temperature": temperature},
            )

        options["temperature"] = float(temperature)

    if max_tokens is not None:
        if not isinstance(max_tokens, int):  # type: ignore[reportUnnecessaryIsInstance]
            raise PluginError(
                message=f"max_tokens must be an integer, got {type(max_tokens).__name__}",
                error_code=InferenceErrorCode.PARAMETER_ERROR,
                details={"max_tokens": max_tokens},
            )

        if max_tokens <= 0:
            raise PluginError(
                message=f"max_tokens must be positive, got {max_tokens}",
                error_code=InferenceErrorCode.PARAMETER_ERROR,
                details={"max_tokens": max_tokens},
            )

        options["max_tokens"] = max_tokens

    if extra_options and isinstance(extra_options, dict):  # type: ignore[reportUnnecessaryIsInstance]
        options.update(extra_options)

    return options
