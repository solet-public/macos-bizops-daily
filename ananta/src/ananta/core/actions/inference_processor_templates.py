"""Strict nested-argument merging for inference result processors."""

from __future__ import annotations

from typing import Any

JsonDict = dict[str, Any]


def merge_processor_template(base_template: JsonDict, processor_template: JsonDict) -> JsonDict:
    """Merge a processor template, replacing ``params.prompt`` but retaining its model."""
    merged = _deep_merge(base_template, processor_template)
    override_params = _get_prompt_params(processor_template)
    if override_params is None:
        return merged
    base_params = _get_params(base_template)
    merged_params = _get_params(merged)
    if base_params is not None and merged_params is not None:
        if "model" in base_params and "model" not in override_params:
            merged_params["model"] = base_params["model"]
        merged_params["prompt"] = override_params["prompt"]
    return merged


def _get_prompt_params(template: JsonDict) -> JsonDict | None:
    """Return a nested params mapping only when it declares a prompt override."""
    params = _get_params(template)
    return params if params is not None and "prompt" in params else None


def _get_params(template: JsonDict) -> JsonDict | None:
    """Return ``arguments.params`` when it is an object."""
    arguments = template.get("arguments")
    if not isinstance(arguments, dict):
        return None
    params = arguments.get("params")
    return params if isinstance(params, dict) else None


def _deep_merge(base: JsonDict, overrides: JsonDict) -> JsonDict:
    """Deep merge mappings, with override values taking precedence."""
    result = base.copy()
    for key, override_value in overrides.items():
        existing = result.get(key)
        result[key] = (
            _deep_merge(existing, override_value)
            if isinstance(existing, dict) and isinstance(override_value, dict)
            else override_value
        )
    return result
