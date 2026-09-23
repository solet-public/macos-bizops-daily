"""FormatStage - Formats system and user prompts from action parameters.

Replaces the duplicate formatting logic in:
- config.py PluginConfig.get_formatted_prompt()
- prompt_manager.py PromptManager.get_formatted_prompt()
- helpers/prompt_builder.py PromptBuilder.build_chat_prompt()
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ananta.core.prompts.context import PromptContext

logger = logging.getLogger(__name__)

# Keys to skip when processing remaining user fields
# - output_schema: injected as the API decode contract, not in message content
# - flow_input: original_input is injected as separate USER message by APIStage;
#   remaining flow_input fields are not rendered to the LLM
_SKIP_KEYS = frozenset({"output_schema", "flow_input", "session_id", "instructions_when_observation_empty"})

# Fields to remove from action_result before showing to LLM.
# Only strip internal blob identifiers — NOT "content", which is legitimate
# article text in knowledge base search/read results.
_ACTION_RESULT_HIDDEN_FIELDS = frozenset({
    "blob_id", "blob_namespace", "output_blobs",
    "audio_blob_key", "image_blob_id", "output_blob_id",
})

# Internal fields to remove from action_result (implementation details).
# Stripped at top level and inside `data` dict when present.
_ACTION_RESULT_INTERNAL_FIELDS = frozenset({
    "success", "status", "timestamp", "match_type", "suppress_observation",
    "_completed_arguments",
})

# Fields to strip from each process in discovery results
# NOTE: invocation_schema is KEPT - the model needs it to understand process arguments.
# Previously stripped because it was "enforced via output_schema oneOf" - that approach
# was removed as it caused LM Studio hangs. The model now reads schemas from messages.
# See: knowledge_base/2026-02-02_inference_and_discord_troubleshooting.md
_PROCESS_HIDDEN_FIELDS: frozenset[str] = frozenset()


def _format_list_or_str(value: Any) -> str:
    """Format a value that may be a list or string."""
    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    return str(value)


def _scrub_blob_fields(obj: Any) -> Any:
    """Recursively remove internal blob fields from nested structures."""
    if isinstance(obj, dict):
        return {
            k: _scrub_blob_fields(v)
            for k, v in obj.items()
            if k not in _ACTION_RESULT_HIDDEN_FIELDS
        }
    if isinstance(obj, list):
        return [_scrub_blob_fields(item) for item in obj]
    return obj


def _filter_process_fields(processes: list[Any]) -> list[dict[str, Any]]:
    """Filter process fields for discovery results shown to LLM.

    The LLM reads process schemas from message content to understand arguments.
    All fields including invocation_schema are kept so the model can determine
    correct process invocation.

    NOTE: Previously stripped invocation_schema because it was "enforced via
    output_schema oneOf" - that approach caused LM Studio hangs and was removed.
    See: knowledge_base/2026-02-02_inference_and_discord_troubleshooting.md
    """
    result = []
    for proc in processes:
        if not isinstance(proc, dict):
            continue
        filtered = {
            k: v for k, v in proc.items()
            if k not in _PROCESS_HIDDEN_FIELDS
        }
        result.append(filtered)
    return result


def _data_is_flat(data: dict[str, Any]) -> bool:
    """Check if data contains only simple scalar values (no lists or dicts).

    Used to decide whether to flatten the ``data`` wrapper into the parent dict.
    Simple results like ``{"message_id": "msg-..."}`` are flattened for compact
    observation text.  Complex results like ``{"results": [...], "count": N}``
    are kept nested so the model sees a structured ``data`` block.
    """
    return all(not isinstance(v, list | dict) for v in data.values())


def _sanitize_action_result(action_result: dict[str, Any]) -> dict[str, Any]:
    """Remove internal and redundant fields from action_result before showing to LLM.

    - Removes blob-related internal fields (blob_id, namespace, etc.)
    - Removes internal timing/status fields (timestamp, match_type, etc.)
    - Conditionally flattens simple ``data`` wrappers (e.g. delivery results)
    - Preserves complex ``data`` dicts (e.g. search results with arrays)
    - Filters process fields in discovery results (keeps invocation_schema for model to read)
    """
    scrubbed = _scrub_blob_fields(action_result)
    result = _strip_internal_fields(scrubbed)
    result = _handle_data_wrapper(result)
    if "processes" in result and isinstance(result["processes"], list):
        result["processes"] = _filter_process_fields(result["processes"])
    return result


def _strip_internal_fields(scrubbed: dict[str, Any]) -> dict[str, Any]:
    """Remove internal timing/status fields from the top level."""
    return {
        k: v for k, v in scrubbed.items()
        if k not in _ACTION_RESULT_INTERNAL_FIELDS
    }


def _handle_data_wrapper(result: dict[str, Any]) -> dict[str, Any]:
    """Flatten simple ``data`` wrappers, preserve complex ones.

    Template wraps <<RESULT>> in a "data" key.  For simple results (only scalar
    values), flatten into the parent dict.  For complex results (arrays/nested
    dicts), keep the "data" wrapper but strip internal fields from it.
    """
    if "data" not in result or not isinstance(result["data"], dict):
        return result
    data = {k: v for k, v in result["data"].items() if k not in _ACTION_RESULT_INTERNAL_FIELDS}
    if _data_is_flat(data):
        result.pop("data")
        for k, v in data.items():
            if k not in result:
                result[k] = v
    else:
        result["data"] = data
    return result


def _format_dict_value(value: Any) -> str:
    """Format a value for inclusion in prompt, handling dicts specially."""
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _format_context_dict(context: dict[str, Any]) -> str:
    """Format a context dictionary as key-value pairs."""
    parts = [f"{key}: {_format_dict_value(value)}" for key, value in context.items()]
    return "\n".join(parts) if parts else ""


def _format_output_requirements(output: dict[str, Any]) -> str | None:
    """Extract and format output requirements from output dict."""
    formatting_reqs = output.get("formatting_requirements")
    if isinstance(formatting_reqs, list) and formatting_reqs:
        return "Output formatting requirements:\n" + "\n".join(
            f"- {req}" for req in formatting_reqs
        )
    return None


class FormatStage:
    """Formats system and user prompts from resolved action parameters.

    Handles two prompt formats:
    1. Legacy: {task: [...], context: {...}, output: {...}}
    2. New: {instructions: [...], question: "...", environment: {...}}

    Extracts output_schema for the API decode contract (not included in message content).
    """

    name = "format"

    def __init__(self, prompts_dir: Path) -> None:
        """Initialize with prompts directory for system.json loading."""
        self._prompts_dir = prompts_dir
        self._system_cache: dict[str, Any] | None = None

    def execute(self, ctx: PromptContext) -> PromptContext:
        """Format prompts from resolved action parameters."""
        prompt_info = ctx.resolved_action_params.get("prompt", {})
        if not isinstance(prompt_info, dict):
            prompt_info = {}

        self._extract_system_prompt(ctx, prompt_info)
        self._extract_observation(ctx, prompt_info)
        self._extract_user_prompt(ctx, prompt_info)

        return ctx

    def _extract_system_prompt(
        self, ctx: PromptContext, prompt_info: dict[str, Any],
    ) -> None:
        """Extract and format system prompt (priority: action > global)."""
        if "system" in prompt_info:
            ctx.system_prompt = self._format_system(prompt_info["system"])
            ctx.add_decision(self.name, "System prompt: from action definition")
        else:
            ctx.system_prompt = self._load_global_system()
            ctx.add_decision(self.name, "System prompt: from system.json")
        ctx.add_decision(self.name, f"System prompt: {len(ctx.system_prompt)} chars")

    def _extract_observation(
        self, ctx: PromptContext, prompt_info: dict[str, Any],
    ) -> None:
        """Extract tool observation (action result / error context → assistant message)."""
        if "observation" not in prompt_info:
            return

        observation = prompt_info["observation"]
        if self._should_suppress_observation(observation):
            if isinstance(observation, dict):
                ctx.raw_observation_dict = observation
            ctx.add_decision(self.name, "Tool observation: suppressed by action_result flag")
        else:
            ctx.tool_observation = self._format_observation(observation)
            if isinstance(observation, dict):
                ctx.raw_observation_dict = observation
                self._detect_observation_emptiness(ctx, observation)
            ctx.add_decision(self.name, f"Tool observation: {len(ctx.tool_observation)} chars")
        # Extract source_memory_id from action result for ID-based focus dedup
        if isinstance(observation, dict):
            action_result = observation.get("action_result", observation)
            if isinstance(action_result, dict):
                ctx.observation_source_memory_id = str(
                    action_result.get("source_memory_id", ""),
                )

    @staticmethod
    def _detect_observation_emptiness(
        ctx: PromptContext, observation: dict[str, Any],
    ) -> None:
        """Set ``observation_is_empty`` when action_result.data is an empty collection.

        Handles two shapes:
        - ``data: []``  (top-level empty list)
        - ``data: {"memories": [], ...}``  (dict where every list value is empty)
        """
        action_result = observation.get("action_result")
        if not isinstance(action_result, dict):
            return
        data = action_result.get("data")
        if isinstance(data, list) and len(data) == 0:
            ctx.observation_is_empty = True
        elif isinstance(data, dict):
            collections = [v for v in data.values() if isinstance(v, list)]
            if collections and all(len(c) == 0 for c in collections):
                ctx.observation_is_empty = True

    def _should_suppress_observation(self, observation: Any) -> bool:
        """Check if the EDGE result requested observation suppression.

        suppress_observation may be at the top level of action_result (flat template)
        or nested inside action_result.data (nested template with data: <<RESULT>>).
        """
        action_result = (
            observation.get("action_result") if isinstance(observation, dict) else None
        )
        if not isinstance(action_result, dict):
            return False
        if action_result.get("suppress_observation"):
            return True
        data = action_result.get("data")
        return isinstance(data, dict) and bool(data.get("suppress_observation"))

    def _extract_user_prompt(
        self, ctx: PromptContext, prompt_info: dict[str, Any],
    ) -> None:
        """Extract and format user prompt and output schema."""
        user_part = prompt_info.get("user", {})
        ctx.user_prompt = self._format_user(user_part)
        ctx.add_decision(self.name, f"User prompt: {len(ctx.user_prompt)} chars")

        # Build alternate instruction for when the observation is empty.
        if isinstance(user_part, dict):
            empty_instructions = user_part.get("instructions_when_observation_empty")
            if isinstance(empty_instructions, list) and empty_instructions:
                modified = {**user_part, "instructions": empty_instructions}
                ctx.user_prompt_when_observation_empty = self._format_user(modified)

        if isinstance(user_part, dict) and "output_schema" in user_part:
            ctx.output_schema = user_part["output_schema"]
            ctx.add_decision(self.name, "Output schema: from action definition")

    def _load_global_system(self) -> str:
        """Load and format global system.json."""
        if self._system_cache is None:
            path = self._prompts_dir / "system.json"
            with open(path, encoding="utf-8") as f:
                data = json.load(f)

            if not isinstance(data, dict) or "prompt" not in data:
                raise ValueError(f"Invalid system.json structure at {path}")

            self._system_cache = data

        system_dict = self._system_cache.get("prompt", {}).get("system", {})
        return self._format_system(system_dict)

    def _format_system(self, system: str | dict[str, Any]) -> str:
        """Format system prompt from string or dictionary."""
        if isinstance(system, str):
            return system.strip()

        parts: list[str] = []
        self._append_system_context(parts, system)
        self._append_system_rules(parts, system)
        self._append_system_output(parts, system)
        return "\n\n".join(parts)

    def _append_system_context(self, parts: list[str], system: dict[str, Any]) -> None:
        """Append context section to parts if present."""
        if "context" in system:
            parts.append(_format_list_or_str(system["context"]))

    def _append_system_rules(self, parts: list[str], system: dict[str, Any]) -> None:
        """Append rules section to parts if present."""
        if "rules" not in system:
            return
        rules = system["rules"]
        if isinstance(rules, list):
            parts.append("Rules:\n" + "\n".join(f"- {rule}" for rule in rules))
        else:
            parts.append(f"Rules: {rules}")

    def _append_system_output(self, parts: list[str], system: dict[str, Any]) -> None:
        """Append output requirements to parts if present."""
        if "output" in system and isinstance(system["output"], dict):
            formatted = _format_output_requirements(system["output"])
            if formatted:
                parts.append(formatted)

    def _format_observation(self, observation: str | dict[str, Any]) -> str:
        """Format tool observation (action result / error context) for assistant message.

        Renders observation dict as factual flat text:
        - summary value (no key prefix)
        - process: <process_key>
        - result: <sanitized action_result JSON>
        - error fields (action_context, etc.) rendered as-is
        """
        if isinstance(observation, str):
            return observation.strip()

        parts: list[str] = []

        # Summary first, without "summary:" prefix
        summary = observation.get("summary")
        if isinstance(summary, str) and summary:
            parts.append(summary)

        # Process key
        process_key = observation.get("process_key")
        if isinstance(process_key, str) and process_key:
            parts.append(f"process: {process_key}")

        # Action result rendered as "result:" (not "action_result:")
        action_result = observation.get("action_result")
        formatted_result = self._format_action_result(action_result, process_key)
        if formatted_result is not None:
            parts.append(formatted_result)

        # Remaining fields (error context, etc.) — skip already-handled keys
        _handled = frozenset({"summary", "process_key", "action_result", "result_type", "_completed_arguments"})
        for key, value in observation.items():
            if key in _handled:
                continue
            parts.append(f"{key}: {_format_dict_value(value)}")

        return "\n".join(parts)

    def _format_action_result(
        self,
        action_result: Any,
        process_key: Any,
    ) -> str | None:
        if not isinstance(action_result, dict):
            if action_result is not None:
                return f"result: {_format_dict_value(action_result)}"
            return None

        sanitized = _sanitize_action_result(action_result)
        # Strip plan content from create_extended_plan results — already
        # present as a focused memory in the conversation, and the escaped
        # ARGS JSON inside the content causes deep nesting that stalls LM Studio.
        if (
            isinstance(process_key, str)
            and "create_extended_plan" in process_key
            and "content" in sanitized
        ):
            sanitized.pop("content")
        return f"result: {_format_dict_value(sanitized)}"

    def _format_user(self, user: str | dict[str, Any]) -> str:
        """Format user prompt from string or dictionary."""
        if isinstance(user, str):
            return user.strip()

        parts: list[str] = []
        processed_keys: set[str] = set()

        self._append_user_instructions(parts, processed_keys, user)
        self._append_user_task(parts, processed_keys, user)
        self._append_user_context(parts, processed_keys, user)
        self._append_user_output(parts, processed_keys, user)
        self._append_remaining_fields(parts, processed_keys, user)
        self._append_original_request(parts, user)

        return "\n".join(parts) if parts else json.dumps(user)

    def _append_user_instructions(
        self, parts: list[str], processed: set[str], user: dict[str, Any]
    ) -> None:
        """Append instructions section (new format)."""
        if "instructions" in user:
            parts.append(_format_list_or_str(user["instructions"]))
            processed.add("instructions")

    def _append_user_task(
        self, parts: list[str], processed: set[str], user: dict[str, Any]
    ) -> None:
        """Append task section (legacy format)."""
        if "task" in user:
            parts.append(_format_list_or_str(user["task"]))
            processed.add("task")

    def _append_user_context(
        self, parts: list[str], processed: set[str], user: dict[str, Any]
    ) -> None:
        """Append context section (legacy format)."""
        if "context" not in user:
            return
        context = user["context"]
        if isinstance(context, dict):
            formatted = _format_context_dict(context)
            if formatted:
                parts.append(f"\nContext:\n{formatted}")
        else:
            parts.append(f"\nContext: {context}")
        processed.add("context")

    def _append_user_output(
        self, parts: list[str], processed: set[str], user: dict[str, Any]
    ) -> None:
        """Append output section (legacy format)."""
        if "output" not in user:
            return
        output = user["output"]
        if isinstance(output, dict):
            formatted = _format_output_requirements(output)
            if formatted:
                parts.append(f"\n{formatted}")
        else:
            parts.append(f"\nOutput: {output}")
        processed.add("output")

    def _append_remaining_fields(
        self, parts: list[str], processed: set[str], user: dict[str, Any]
    ) -> None:
        """Append any remaining fields not specially handled."""
        for key, value in user.items():
            if key in processed or key in _SKIP_KEYS:
                continue
            # Special handling for action_result: remove internal blob fields
            if key == "action_result" and isinstance(value, dict):
                sanitized = _sanitize_action_result(value)
                parts.append(f"\n{key}: {_format_dict_value(sanitized)}")
                continue
            parts.append(f"\n{key}: {_format_dict_value(value)}")

    _ORIGINAL_REQUEST_MAX_CHARS = 200

    def _append_original_request(
        self, parts: list[str], user: dict[str, Any]
    ) -> None:
        """Append original request from resolved flow_input as final line.

        Extracts original_input from the resolved flow_input dict and appends
        it as 'Original request: <text>'. This gives the model a convenient
        reference to the user's original request without scrolling back.

        Long requests (multi-section creative briefs) are truncated to the
        first line.  The full request is already in conversation history;
        repeating it here inflates the prompt and causes the model to try
        to address every constraint individually.
        """
        flow_input = user.get("flow_input")
        if not isinstance(flow_input, dict):
            return
        original_input = flow_input.get("original_input")
        if isinstance(original_input, str) and original_input.strip():
            text = original_input.strip()
            if len(text) > self._ORIGINAL_REQUEST_MAX_CHARS:
                # Truncate to the first line (before any ## heading or double newline)
                first_line = text.split("\n")[0].strip()
                text = first_line
            parts.append(f"\nOriginal request: {text}")
