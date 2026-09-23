"""Shared helpers for IO interface plugins that drive conversational chat flows."""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ananta.core.plugins.plugin_base import PluginBase
    from ananta.core.services.compilation_context_builder import CompilationContextBuilder


def load_start_action_definition(
    app_home: str,
    start_param: str | dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve the configured start action into an action dictionary."""
    if not start_param:
        msg = "start parameter is required in start_interface configuration"
        raise ValueError(msg)

    if isinstance(start_param, str):
        return _load_start_action_from_file(app_home, start_param)

    return start_param


def prepare_start_action_definition(
    plugin: PluginBase,
    start_action: dict[str, Any],
    user_input: str,
    session_id: str,
    flow_id: str,
    compilation_context_builder: CompilationContextBuilder,
) -> dict[str, Any]:
    """Create a per-request action definition following the console action pattern."""
    action: dict[str, Any] = copy.deepcopy(start_action)
    template_engine = _resolve_template_engine(plugin)

    # FAIL-FAST: Template engine is required for proper operation
    if not template_engine:
        raise RuntimeError(
            "Template engine not available. This indicates a startup sequence issue - "
            "action_preparation_service may not be initialized on the orchestrator."
        )

    context = compilation_context_builder.build_context(session_id=session_id, flow_id=flow_id)
    context.setdefault("runtime_args", {})
    context["runtime_args"]["user_input"] = user_input

    # Debug logging for template resolution
    runtime_args = context.get("runtime_args", {})
    logger.debug(
        f"TEMPLATE_RESOLUTION: runtime_args keys = {list(runtime_args.keys()) if isinstance(runtime_args, dict) else 'not a dict'}"
    )
    if isinstance(runtime_args, dict):
        pass

    resolved = template_engine.resolve_templates(action, context)
    if not isinstance(resolved, dict):
        raise TypeError(f"resolve_templates returned {type(resolved)}, expected dict")
    action = resolved
    logger.debug("TEMPLATE_RESOLUTION: Templates resolved successfully")

    if "arguments" not in action:
        action["arguments"] = {}

    action["arguments"]["session_id"] = session_id
    action["arguments"]["user_input"] = user_input
    action["session_id"] = session_id
    action["flow_id"] = flow_id

    return action


def inject_default_result_processor(
    action_definition: dict[str, Any],
    *,
    io_namespace: str | None = None,
) -> None:
    """Ensure @ command actions route their output through the active IO plugin.

    Args:
        action_definition: The action definition to inject a result processor into.
        io_namespace: The IO plugin namespace (e.g., 'discord_plugin'). When provided,
            builds a plugin-addressed process key (plugin::<namespace>::post_message).
            Required for proper IO routing in the plugin-addressed model.
    """
    if "result_processor" in action_definition:
        return

    # ``bridge_delivery`` means the bridge dispatcher (not a result-
    # processor template) owns delivery; do not inject a post_message
    # template here.
    if action_definition.get("result_processor_kind") == "bridge_delivery":
        return

    process_key = _build_io_post_message_key(io_namespace)

    action_definition["result_processor"] = json.dumps(
        {
            "process_key": process_key,
            "arguments": {"message": "<<RESULT>>"},
        }
    )


def _build_io_post_message_key(io_namespace: str | None) -> str:
    """Build the post_message process key for the active IO plugin.

    Args:
        io_namespace: The IO plugin namespace. If None, raises.

    Returns:
        Process key string (e.g., 'plugin::discord_plugin::post_message').

    Raises:
        RuntimeError: If io_namespace is not provided.
    """
    if not io_namespace:
        raise RuntimeError(
            "io_namespace is required for inject_default_result_processor. "
            "The calling IO plugin must pass its own namespace."
        )
    return f"plugin::{io_namespace}::post_message"


def _load_start_action_from_file(app_home: str, filename: str) -> dict[str, Any]:
    """Load start action JSON from APP_HOME-relative path."""
    loaded_action = _load_action_file(app_home, filename)

    if not loaded_action:
        msg = f"Failed to load start action file: {filename}"
        raise FileNotFoundError(msg)

    if isinstance(loaded_action, list):
        if len(loaded_action) != 1:
            msg = f"Start action file must contain exactly one action, found {len(loaded_action)}"
            raise ValueError(msg)
        return loaded_action[0]

    return loaded_action


def _load_action_file(app_home: str, filename: str) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Load an action definition file relative to APP_HOME.

    Args:
        app_home: Application home directory
        filename: Path to file relative to APP_HOME (e.g., 'config/prompts/start.json')

    Returns:
        Loaded action definition(s) or None if file not found
    """
    # Security: Reject path traversal attempts
    if ".." in filename:
        logger.error(f"Path traversal not allowed in action file path: {filename}")
        return None

    action_file = Path(app_home) / filename

    if not action_file.exists():
        return None

    with action_file.open() as handler:
        loaded = json.load(handler)
        # Type narrowing: json.load can return Any, so we validate the expected types
        if isinstance(loaded, dict) or isinstance(loaded, list):
            return loaded
        return None


def _resolve_template_engine(plugin: PluginBase) -> Any | None:
    """Fetch the orchestrator's template engine when available."""
    orchestrator = plugin.orchestrator_ref
    if not orchestrator:
        logger.error("TEMPLATE_ENGINE_RESOLVE: orchestrator is None")
        return None
    action_prep_service = getattr(orchestrator, "action_preparation_service", None)
    if not action_prep_service:
        logger.error("TEMPLATE_ENGINE_RESOLVE: action_preparation_service is None on orchestrator")
        return None
    template_engine = getattr(action_prep_service, "template_engine", None)
    if not template_engine:
        logger.error(
            "TEMPLATE_ENGINE_RESOLVE: template_engine is None on action_preparation_service"
        )
    else:
        pass
    return template_engine


def get_process_results_template(orchestrator: object) -> dict[str, Any]:
    """Get process_results action_definition_template from the process registry."""
    get_registry = getattr(orchestrator, "get_process_registry", None)
    if get_registry is None:
        msg = "Orchestrator does not expose get_process_registry"
        raise RuntimeError(msg)
    registry: dict[str, object] = get_registry()
    processes = registry.get("processes", {})
    if not isinstance(processes, dict):
        msg = "processes is not a dict in registry"
        raise RuntimeError(msg)
    entry = processes.get("service_interface::inference_service::process_results")
    if not isinstance(entry, dict):
        msg = "process_results process not found in registry"
        raise RuntimeError(msg)
    template = entry.get("action_definition_template")
    if not isinstance(template, dict):
        msg = "process_results has no action_definition_template"
        raise RuntimeError(msg)
    return template


def build_initial_vertex_action(
    session_id: str,
    flow_id: str,
    orchestrator: object,
) -> dict[str, Any]:
    """Build the initial vertex action from the process_results template.

    Deep-copies the process_results template and removes the observation key
    so the action is treated as a non-callback (triggers INPUT event storage).
    Instructions are emptied — the prompt pipeline + NS06 guidance shape the final prompt.

    Args:
        session_id: Session identifier
        flow_id: Flow identifier
        orchestrator: The EventOrchestrator (used to resolve process registry at call time).

    Returns:
        Action definition dict for direct process_results submission.
    """
    process_results_template = get_process_results_template(orchestrator)
    action_def: dict[str, Any] = copy.deepcopy(process_results_template)
    action_def["name"] = "initial_vertex"
    action_def["session_id"] = session_id
    action_def["flow_id"] = flow_id

    arguments = action_def.setdefault("arguments", {})
    params = arguments.setdefault("params", {})
    prompt = params.setdefault("prompt", {})

    # CRITICAL: Remove observation key entirely so _is_processor_callback() returns False.
    # This ensures the generic non-callback INPUT storage path fires.
    # An empty dict would still be detected as a callback — the key must be absent.
    prompt.pop("observation", None)

    user = prompt.setdefault("user", {})
    user["instructions"] = []

    return action_def


__all__ = [
    "build_initial_vertex_action",
    "inject_default_result_processor",
    "load_start_action_definition",
    "prepare_start_action_definition",
]
