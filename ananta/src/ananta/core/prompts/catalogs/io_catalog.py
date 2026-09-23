"""IO process catalog helpers — schema navigation, formatting, and guards.

Pure functions for inspecting output schemas, formatting IO argument
summaries, and determining IO routing.  No plugin or service dependencies.
"""

from __future__ import annotations

from typing import Any


def format_io_args_summary(process_data: dict[str, object]) -> str:
    """Format a compact arguments summary from invocation_schema.

    Produces: {"session_id":"string","message":"string","attachments":"string[]?"}
    """
    args_schema = navigate_to_args_schema(process_data)
    if args_schema is None:
        return ""

    properties = args_schema.get("properties")
    if not isinstance(properties, dict):
        return ""

    required_raw = args_schema.get("required")
    required_set = {str(r) for r in required_raw} if isinstance(required_raw, list) else set()

    parts = [
        format_arg_type(name, prop, name in required_set)
        for name, prop in properties.items()
    ]
    return "{" + ",".join(parts) + "}" if parts else ""


def navigate_to_args_schema(
    process_data: dict[str, object],
) -> dict[str, Any] | None:
    """Navigate invocation_schema envelope to the inner arguments schema."""
    schema = process_data.get("invocation_schema")
    if not isinstance(schema, dict):
        return None
    outer_props = schema.get("properties")
    if not isinstance(outer_props, dict):
        return None
    args_schema = outer_props.get("arguments")
    return args_schema if isinstance(args_schema, dict) else None


def format_arg_type(name: str, prop: Any, is_required: bool) -> str:
    """Format a single argument as '"name":"type?"' for compact summary."""
    type_str = "object"
    if isinstance(prop, dict):
        prop_type = prop.get("type", "object")
        if prop_type == "array":
            items = prop.get("items", {})
            item_type = (
                items.get("type", "object") if isinstance(items, dict) else "object"
            )
            type_str = f"{item_type}[]"
        else:
            type_str = str(prop_type)
    optional = "" if is_required else "?"
    return f'"{name}":"{type_str}{optional}"'


def schema_allows_plugin(output_schema: dict[str, Any] | None) -> bool:
    """Check if the output schema allows plugin provider_type."""
    if not output_schema:
        return False

    properties = output_schema.get("properties")
    if not isinstance(properties, dict):
        return False

    actions = properties.get("actions")
    if not isinstance(actions, dict):
        return False

    items = actions.get("items")
    if not isinstance(items, dict):
        return False

    process = (items.get("properties") or {}).get("process")
    if not isinstance(process, dict):
        return False

    provider_type = (process.get("properties") or {}).get("provider_type")
    if not isinstance(provider_type, dict):
        return False

    enum_values = provider_type.get("enum", [])
    return "plugin" in enum_values


def extract_schema_process_props(
    output_schema: dict[str, Any] | None,
) -> dict[str, Any]:
    """Navigate to the process properties dict inside an action schema."""
    if not output_schema:
        return {}
    actions = output_schema.get("properties", {}).get("actions", {})
    items = actions.get("items", {}) if isinstance(actions, dict) else {}
    process = (items.get("properties", {}) or {}).get("process", {})
    pprops = (process.get("properties") or {}) if isinstance(process, dict) else {}
    return pprops if isinstance(pprops, dict) else {}


def schema_allows_post_message(
    output_schema: dict[str, Any] | None, io_namespace: str | None,
) -> bool:
    """Check if the output schema allows post_message for the given IO namespace.

    Returns True when the schema's function_name enum includes
    ``post_message`` AND either the provider enum includes the IO
    namespace's provider or the check is purely functional (any IO
    provider allowed).
    """
    if not output_schema:
        return False
    props = extract_schema_process_props(output_schema)
    fn_enum = (props.get("function_name") or {}).get("enum", [])
    if "post_message" not in fn_enum:
        return False
    if io_namespace is None:
        return True
    provider_enum = (props.get("provider") or {}).get("enum", [])
    # Extract the provider from the IO namespace
    # e.g. "agent_messaging_plugin" from "service::agent_messaging_plugin::post_message"
    parts = io_namespace.split("::")
    io_provider = parts[1] if len(parts) >= 2 else io_namespace
    return io_provider in provider_enum


def parse_process_key(process_key: str) -> tuple[str, str, str]:
    """Split a full process key into (provider_type, provider, function_name)."""
    parts = process_key.split("::")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    return "", "", process_key


def parse_plugin_process_key(pkey: str) -> tuple[str, str] | None:
    """Parse a plugin process key into (plugin_name, function_name).

    Returns None if not a plugin key.
    """
    parts = pkey.split("::")
    if len(parts) == 3 and parts[0] == "plugin":
        return parts[1], parts[2]
    return None


def strip_section(content: str, header: str) -> str:
    """Remove a named section (## header ... next ## or EOF) from content."""
    lines = content.split("\n")
    result: list[str] = []
    skipping = False
    for line in lines:
        if line.strip().startswith(f"## {header}"):
            skipping = True
            continue
        if skipping and line.strip().startswith("## "):
            skipping = False
        if not skipping:
            result.append(line)
    return "\n".join(result)


def replace_or_append_section(content: str, header: str, new_block: str) -> str:
    """Replace a named section or append it at the end."""
    stripped = strip_section(content, header)
    return stripped.rstrip() + "\n\n" + new_block
