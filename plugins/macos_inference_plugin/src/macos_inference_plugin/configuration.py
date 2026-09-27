"""Explicit, validated configuration for the Apple summary provider."""

from __future__ import annotations

import json
import math
from importlib.resources import files
from typing import Any

from ananta.services.context_management.config import ContextManagementConfig
from ananta.services.context_management.types import ContextIdSource, ContextMode
from jsonschema import validate


def default_config() -> dict[str, Any]:
    """Return a fresh shipped configuration for seed materialization."""
    return json.loads(files("macos_inference_plugin.resources").joinpath(
        "default_config.json"
    ).read_text(encoding="utf-8"))


def config_schema() -> dict[str, Any]:
    """Declare every required field; unsupported capabilities cannot be enabled."""
    defaults = default_config()
    types = {bool: "boolean", int: "integer", float: "number", str: "string"}
    properties: dict[str, Any] = {}
    for key, value in defaults.items():
        spec: dict[str, Any] = {"type": types.get(type(value), "null"), "default": value}
        if type(value) is int:
            spec["minimum"] = 1
        if type(value) is float:
            spec.update(minimum=0, maximum=2)
        properties[key] = spec
    for key in ("model", "context.mode", "context.id_source", "context.id_address_key",
                "context.warming_enabled", "context.supports_clear", "context.auto_compact",
                "context.model_context_tokens"):
        properties[key]["const"] = defaults[key]
    properties["max_tokens"]["maximum"] = 2048
    properties["context.discovery_min_similarity_threshold"]["maximum"] = 1
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "macOS summary inference",
        "type": "object", "properties": properties,
        "required": list(defaults), "additionalProperties": False,
    }


def validate_config(config: dict[str, Any]) -> None:
    """Reject incomplete or unsupported settings without merging defaults."""
    validate(config, config_schema())
    if any(isinstance(value, float) and not math.isfinite(value) for value in config.values()):
        raise ValueError("macOS inference configuration must contain finite numbers")
    if not (config["context.target_char_count"] < config["context.soft_max_char_count"]
            < config["context.max_char_count"]):
        raise ValueError("Context character limits must satisfy target < soft < hard")


def context_config(config: dict[str, Any]) -> ContextManagementConfig:
    """Translate the validated flat platform configuration."""
    validate_config(config)
    fields = {key.removeprefix("context."): value for key, value in config.items()
              if key.startswith("context.")}
    fields["context_mode"] = ContextMode(fields.pop("mode"))
    fields["context_id_source"] = ContextIdSource(fields.pop("id_source"))
    fields["context_id_address_key"] = fields.pop("id_address_key")
    return ContextManagementConfig(**fields)
