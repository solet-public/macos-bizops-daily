from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from ananta.constants import (
    CONTEXT_KEY_APP_HOME,
    CONTEXT_KEY_FLOW_ID,
    CONTEXT_KEY_PROCESS_KEY,
    CONTEXT_KEY_SESSION_ID,
    NOTES_MAX_LENGTH,
    TEMPLATE_VAR_FLOW_ID,
    TEMPLATE_VAR_NOTES,
    TEMPLATE_VAR_SESSION_ID,
)
from ananta.core.actions.action_submission_types import QueuedAction
from ananta.core.actions.inference_processor_templates import merge_processor_template
from ananta.core.contexts.normalization import normalize_flow_id, normalize_session_id
from ananta.core.domain.error_codes import ErrorCode
from ananta.core.plugins.plugin_contracts import ActionStatus
from ananta.error_handling import FrameworkError

logger = logging.getLogger(__name__)

# Type alias for JSON-like config/template data that is dynamically typed at runtime
JsonDict = dict[str, Any]

UNKNOWN_ARGUMENTS_ERROR_CODE = "action.unknown_arguments"


def validate_process_arguments(
    process_key: str,
    parameters: dict[str, object],
    arguments: dict[str, object],
) -> None:
    """Reject missing required or undeclared arguments for a registered process."""
    declared = list(parameters.keys())
    unknown = [name for name in arguments if name not in parameters]
    if unknown:
        raise FrameworkError(
            message=f"Unknown arguments: {unknown}. Declared arguments: {declared}",
            error_code=UNKNOWN_ARGUMENTS_ERROR_CODE,
            details={
                CONTEXT_KEY_PROCESS_KEY: process_key,
                "unknown_arguments": unknown,
                "declared_arguments": declared,
                "provided_arguments": list(arguments.keys()),
            },
        )

    required = [
        name
        for name, meta in parameters.items()
        if isinstance(meta, dict) and meta.get("required")
    ]
    missing = [name for name in required if name not in arguments]
    if missing:
        raise FrameworkError(
            message=f"Missing required arguments: {missing}",
            error_code="action.missing_required_arguments",
            details={
                CONTEXT_KEY_PROCESS_KEY: process_key,
                "missing_arguments": missing,
                "required_arguments": required,
                "provided_arguments": list(arguments.keys()),
            },
        )


class TemplateEngine(Protocol):
    """Protocol for template engine interface."""

    def resolve_templates(
        self, action_def: dict[str, object], context: dict[str, object]
    ) -> dict[str, object]:
        """Resolve templates in action definition with context."""
        ...


class StateService(Protocol):
    """Protocol for state service interface."""

    def generate_unique_string(self, length: int, encoding: str) -> dict[str, object]:
        """Generate a unique random string."""
        ...


class ActionEventRecorder(Protocol):
    """Protocol for action event recorder interface."""

    def store_action_event(self, action: dict[str, object]) -> str:
        """Store an action event and return its ID."""
        ...


class ActionFactory:
    def __init__(
        self,
        process_registry: dict[str, object] | None = None,
        template_engine: TemplateEngine | None = None,
        state_service: StateService | None = None,
        action_event_recorder: ActionEventRecorder | None = None,
        app_home: str = "",
    ) -> None:
        self.process_registry: dict[str, object] = process_registry or {}
        self.template_engine: TemplateEngine | None = template_engine
        self.state_service: StateService | None = state_service
        self.action_event_recorder: ActionEventRecorder | None = action_event_recorder
        self.app_home: str = app_home

        # Action queuing system for graceful degradation
        self.action_queue: list[QueuedAction] = []
        self.max_queue_size = 1000
        self.retry_interval = 5  # seconds

    def _validate_action_legacy(self, action_def: dict[str, object]) -> str:
        """Validate action using legacy path (no compiler) and return process key."""
        validated_process_key = self._resolve_process_key(action_def)
        self._validate_process_exists(validated_process_key)

        # Bridge-delivery actions (handoff 2026-05-10) route results and
        # errors through the bridge dispatcher, not through inference
        # templates.  Skip attachment of the corresponding processor
        # templates so the AQP failure path's
        # ``_process_error_processor_template`` short-circuits and the
        # shared error dispatcher's ``dispatch_execution_failure`` branch
        # fires (which routes by ``error_processor_kind`` to the bridge).
        rpk = action_def.get("result_processor_kind")
        rpk_value = getattr(rpk, "value", rpk)
        # A ``result_processor_kind`` of ``None`` is the terminal EDGE_SINK /
        # memory-tag-heartbeat shape (e.g. a cron dispatching
        # ``get_memories_by_tag``): there is no kind to route the result by,
        # so the action must stay terminal and ride the poller's
        # EDGE_SINK_SKIP branch (``result_processor_kind is None and
        # result_processor is None``, action_queue_poller.py). Stamping a
        # registry-default ``result_processor`` here anyway breaks that
        # both-None condition, dropping the action into result-contract
        # validation where it trips ``result_processor_kind_missing`` and
        # dies — the REL-12 platform-wide dead-cron-lane defect. Only attach a
        # result processor when a kind that consumes one is actually declared.
        if rpk_value is not None and rpk_value != "bridge_delivery":
            result_processor = self._get_result_processor_from_customizations(
                validated_process_key,
            )
            if result_processor:
                action_def["result_processor"] = result_processor
                action_def["processor_depth"] = 0
                action_def["processor_origin"] = "REGISTRY_DEFAULT"

        epk = action_def.get("error_processor_kind")
        epk_value = getattr(epk, "value", epk)
        if epk_value != "bridge_delivery":
            error_processor = self._get_error_processor_from_customizations(
                validated_process_key,
            )
            if error_processor and "error_processor" not in action_def:
                action_def["error_processor"] = error_processor

        self._require_error_processor_for_deterministic(action_def, validated_process_key)

        return validated_process_key

    def _require_error_processor_for_deterministic(
        self,
        action_def: dict[str, object],
        process_key: str,
    ) -> None:
        """Fail submission if a deterministic step lacks a process-level error handler.

        Per 2026-05-03 handoff Section 16: ``deterministic_continuation``
        steps need a configured error handler so contract violations have
        somewhere to route.  Inference steps and unannotated steps remain
        unconstrained.  When the action declares
        ``error_processor_kind = "bridge_delivery"`` the bridge dispatcher
        owns failure routing — no inference error handler is needed.
        """
        kind = action_def.get("result_processor_kind")
        kind_value = getattr(kind, "value", kind)
        if kind_value != "deterministic_continuation":
            return
        if action_def.get("error_processor"):
            return
        err_kind = action_def.get("error_processor_kind")
        err_kind_value = getattr(err_kind, "value", err_kind)
        if err_kind_value == "bridge_delivery":
            return
        raise FrameworkError(
            message=(
                "Deterministic-continuation action requires a process-level "
                "error handler; no error_processor_customizations declared "
                f"for {process_key!r}"
            ),
            error_code="action_factory.error_processor_required",
            details={
                "process_key": process_key,
                "result_processor_kind": kind_value,
            },
        )

    def _build_runtime_action(
        self,
        action_def: dict[str, object],
        validated_process_key: str,
        context: dict[str, object],
    ) -> dict[str, object]:
        """Build the runtime action dictionary from validated action definition."""
        arguments = action_def.get("arguments", {})

        function_name = self._extract_function_name_from_process_key(validated_process_key)
        unique_suffix = self._generate_unique_suffix()
        unique_action_name = f"{function_name}_{unique_suffix}"

        action: dict[str, object] = {
            "name": unique_action_name,
            "process_key": validated_process_key,
            "parameters": arguments,
            "notes": action_def["notes"],
            "action_status": ActionStatus.QUEUED.value,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        self._preserve_action_metadata(action, action_def)

        if context:
            action["plugin_context"] = context

        return action

    # Metadata keys copied from action_def to runtime action.
    # Includes session/flow/context routing, processor config, and the
    # ``parent_id`` link the recorder stores as ``core__action_events_id``
    # (parent provenance for child actions submitted by dispatchers).
    _METADATA_KEYS = (
        "session_id", "flow_id", "context_id",
        "result_processor", "result_processor_target",
        "error_processor", "processor_depth", "processor_origin",
        "result_processor_kind",
        "error_processor_kind",
        "job_result_ref",
        "parent_id",
    )

    def _preserve_action_metadata(
        self,
        action: dict[str, object],
        action_def: dict[str, object],
    ) -> None:
        """Preserve session, flow, context, and processor metadata in the action."""
        for key in self._METADATA_KEYS:
            if key in action_def:
                action[key] = action_def[key]

    def create_action(
        self, action_def: dict[str, object], context: dict[str, object] | None = None
    ) -> dict[str, object]:
        context = context or {}

        # Validate against live process registry and default processor customizations.
        validated_process_key = self._validate_action_legacy(action_def)

        # PHASE 2: Template substitution
        action_def = self._substitute_templates(action_def, context)

        # PHASE 3: Structure validation
        self._validate_action_structure(action_def)

        # PHASE 4: Build runtime action
        return self._build_runtime_action(action_def, validated_process_key, context)

    def _resolve_process_key(self, action_def: dict[str, object]) -> str:
        if "process_key" in action_def:
            process_key = action_def["process_key"]
            if not isinstance(process_key, str):
                raise FrameworkError(
                    message="process_key must be a string",
                    error_code=ErrorCode.ACTION_INVALID_DEFINITION,
                    details={"action_def": action_def},
                )
            corrected = self._try_correct_process_key(process_key, action_def)
            if corrected:
                action_def["process_key"] = corrected
                return corrected
            return process_key

        if "process" in action_def:
            p = action_def["process"]
            if not isinstance(p, dict):
                raise FrameworkError(
                    message="process must be a dictionary",
                    error_code=ErrorCode.ACTION_INVALID_DEFINITION,
                    details={"action_def": action_def},
                )

            provider_type_obj = p.get("provider_type", "plugin")
            provider_type = provider_type_obj if isinstance(provider_type_obj, str) else "plugin"

            provider_obj = p.get("provider")
            if not isinstance(provider_obj, str):
                raise FrameworkError(
                    message="process.provider must be a string",
                    error_code=ErrorCode.ACTION_INVALID_DEFINITION,
                    details={"action_def": action_def},
                )

            function_obj = p.get("function_name")
            if not isinstance(function_obj, str):
                raise FrameworkError(
                    message="process.function_name must be a string",
                    error_code=ErrorCode.ACTION_INVALID_DEFINITION,
                    details={"action_def": action_def},
                )

            process_key = f"{provider_type}::{provider_obj}::{function_obj}"

            corrected = self._try_correct_process_key(process_key, action_def)
            return corrected or process_key

        raise FrameworkError(
            message="Action definition missing process_key or process block",
            error_code=ErrorCode.ACTION_INVALID_DEFINITION,
            details={"action_def": action_def},
        )

    _PROVIDER_TYPES = ("plugin", "service_interface")

    def _try_correct_process_key(
        self,
        process_key: str,
        action_def: dict[str, object],
    ) -> str | None:
        """Try to correct a malformed process_key using registry lookup.

        Handles two model hallucination patterns:
        1. Wrong provider_type (3-part): ``service_interface::audio_processing_plugin::ffmpeg_aformat``
           → ``plugin::audio_processing_plugin::ffmpeg_aformat``
        2. Prepended extra segment (4-part): ``service_interface::plugin::audio_processing_plugin::ffmpeg_aformat``
           → ``plugin::audio_processing_plugin::ffmpeg_aformat``

        Corrects the action_def's process dict in-place when a match is found.
        """
        processes = self.process_registry.get("processes", {})
        if not isinstance(processes, dict) or process_key in processes:
            return None

        parts = process_key.split("::")
        candidates = self._build_correction_candidates(parts)

        for candidate in candidates:
            if candidate in processes:
                logger.warning(
                    "Auto-corrected process_key: '%s' -> '%s'",
                    process_key,
                    candidate,
                )
                self._update_process_dict(action_def, candidate)
                return candidate

        return None

    def _build_correction_candidates(self, parts: list[str]) -> list[str]:
        """Generate candidate process_keys from malformed key parts."""
        candidates: list[str] = []
        if len(parts) == 4:
            candidates.append(f"{parts[1]}::{parts[2]}::{parts[3]}")
        if len(parts) == 3:
            for alt_type in self._PROVIDER_TYPES:
                if alt_type != parts[0]:
                    candidates.append(f"{alt_type}::{parts[1]}::{parts[2]}")
        return candidates

    @staticmethod
    def _update_process_dict(action_def: dict[str, object], corrected_key: str) -> None:
        """Update action_def's process dict with corrected key components."""
        process_dict = action_def.get("process")
        if isinstance(process_dict, dict):
            corrected_parts = corrected_key.split("::")
            process_dict["provider_type"] = corrected_parts[0]
            process_dict["provider"] = corrected_parts[1]
            process_dict["function_name"] = corrected_parts[2]

    def _validate_process_exists(self, process_key: str) -> None:
        processes_obj = self.process_registry.get("processes", {})
        if not isinstance(processes_obj, dict):
            raise FrameworkError(
                message="Process registry 'processes' must be a dictionary",
                error_code=ErrorCode.ACTION_PROCESS_NOT_FOUND,
                details={CONTEXT_KEY_PROCESS_KEY: process_key},
            )

        if process_key not in processes_obj:
            available_processes = list(processes_obj.keys())[:5]
            raise FrameworkError(
                message=f"Process '{process_key}' not found in registry",
                error_code=ErrorCode.ACTION_PROCESS_NOT_FOUND,
                details={
                    CONTEXT_KEY_PROCESS_KEY: process_key,
                    "available_processes": available_processes,
                    "total_processes": len(processes_obj),
                },
            )

    def _get_process_def(self, process_key: str) -> dict[str, object]:
        """Get process definition from registry. Fail fast if not found.

        Args:
            process_key: The process key to look up

        Returns:
            The process definition dict

        Raises:
            FrameworkError: If process registry is malformed or process not found
        """
        processes_obj = self.process_registry.get("processes", {})
        if not isinstance(processes_obj, dict):
            raise FrameworkError(
                message="Process registry 'processes' must be a dictionary",
                error_code=ErrorCode.ACTION_PROCESS_NOT_FOUND,
                details={CONTEXT_KEY_PROCESS_KEY: process_key},
            )

        process_def = processes_obj.get(process_key)
        if not isinstance(process_def, dict):
            raise FrameworkError(
                message=f"Process '{process_key}' not found or malformed in registry",
                error_code=ErrorCode.ACTION_PROCESS_NOT_FOUND,
                details={CONTEXT_KEY_PROCESS_KEY: process_key},
            )

        return process_def

    def _validate_required_arguments(self, process_key: str, arguments: dict[str, object]) -> None:
        """Validate that all required arguments are provided. Fail fast.

        Uses only `parameters` from process definition - single source of truth.
        No fallback to invocation_schema.

        Note: Parameters that are injected at execution time (session_id, flow_id, state)
        should have required=False in registry metadata, not be excluded here.

        Args:
            process_key: The process key for error reporting
            arguments: The arguments provided in action definition

        Raises:
            FrameworkError: If parameters is missing/malformed or required args missing
        """
        process_def = self._get_process_def(process_key)

        parameters = process_def.get("parameters")
        if not isinstance(parameters, dict):
            raise FrameworkError(
                message="Process registry missing parameters",
                error_code="action.registry_malformed",
                details={CONTEXT_KEY_PROCESS_KEY: process_key},
            )

        validate_process_arguments(process_key, parameters, arguments)

    def _get_action_definition_template(self, process_key: str) -> JsonDict | None:
        """Get action_definition_template for a process from registry.

        This is used to provide base templates that result_processor_template
        can inherit from and override.

        Args:
            process_key: The process key to look up

        Returns:
            The action_definition_template dict if found, None otherwise
        """
        processes_obj = self.process_registry.get("processes", {})
        if not isinstance(processes_obj, dict):
            return None

        process_data = processes_obj.get(process_key)
        if not isinstance(process_data, dict):
            return None

        template = process_data.get("action_definition_template")
        if isinstance(template, dict):
            return dict(template)
        return None

    def _get_process_customizations(self, process_key: str) -> JsonDict | None:
        """Get result_processor_customizations for a process from registry."""
        processes_obj = self.process_registry.get("processes", {})
        if not isinstance(processes_obj, dict):
            return None

        process_data = processes_obj.get(process_key)
        if not isinstance(process_data, dict):
            return None

        customizations = process_data.get("result_processor_customizations")
        if isinstance(customizations, dict) and customizations:
            return customizations
        return None

    def _get_error_processor_customizations(
        self, process_key: str,
    ) -> JsonDict | None:
        """Get ``error_processor_customizations`` for a process from registry."""
        processes_obj = self.process_registry.get("processes", {})
        if not isinstance(processes_obj, dict):
            return None
        process_data = processes_obj.get(process_key)
        if not isinstance(process_data, dict):
            return None
        customizations = process_data.get("error_processor_customizations")
        if isinstance(customizations, dict) and customizations:
            return customizations
        return None

    def get_process_customizations(self, process_key: str) -> dict[str, object] | None:
        """Get result_processor_customizations for a process.

        Used by ActionQueuePoller for attachment extraction blob_fields lookup.
        """
        return self._get_process_customizations(process_key)

    def get_message_rendering(
        self,
        process_key: str,
        *,
        error_path: bool = False,
    ) -> dict[str, object] | None:
        """Get message_rendering contract from process customizations.

        Reads from ``result_processor_customizations`` by default, or from
        ``error_processor_customizations`` when ``error_path=True``.

        Returns the declarative rendering metadata that determines how an edge
        result (or error) appears in the prompt (context_layer, reasoning_slot,
        etc.). Returns None if no message_rendering is defined.
        """
        customization_key = (
            "error_processor_customizations"
            if error_path
            else "result_processor_customizations"
        )
        processes_obj = self.process_registry.get("processes", {})
        if not isinstance(processes_obj, dict):
            return None
        process_data = processes_obj.get(process_key)
        if not isinstance(process_data, dict):
            return None
        customizations = process_data.get(customization_key)
        if not isinstance(customizations, dict) or not customizations:
            return None
        rendering = customizations.get("message_rendering")
        if isinstance(rendering, dict) and rendering:
            return dict(rendering)
        return None

    def _get_inference_base_template(self) -> tuple[str, JsonDict | None]:
        """Get the inference process base template for result processing."""
        inference_process_key = "service_interface::inference_service::process_results"
        processes_obj = self.process_registry.get("processes", {})

        if not isinstance(processes_obj, dict):
            return inference_process_key, None

        inference_data = processes_obj.get(inference_process_key)
        if not isinstance(inference_data, dict):
            logger.error(f"ACTION_FACTORY: Inference process not found: {inference_process_key}")
            return inference_process_key, None

        base_template = inference_data.get("action_definition_template")
        if isinstance(base_template, dict):
            return inference_process_key, base_template
        logger.error(f"ACTION_FACTORY: No action_definition_template for {inference_process_key}")
        return inference_process_key, None

    def _get_process_error_base_template(self) -> tuple[str, JsonDict | None]:
        """Get the inference ``process_error`` base template.

        Mirrors :meth:`_get_inference_base_template` but for the error
        path; per handoff Section 16 the base is
        ``service_interface::inference_service::process_error``.
        """
        process_error_key = "service_interface::inference_service::process_error"
        processes_obj = self.process_registry.get("processes", {})
        if not isinstance(processes_obj, dict):
            return process_error_key, None
        process_data = processes_obj.get(process_error_key)
        if not isinstance(process_data, dict):
            logger.error(
                "ACTION_FACTORY: Inference process not found: %s",
                process_error_key,
            )
            return process_error_key, None
        base_template = process_data.get("action_definition_template")
        if isinstance(base_template, dict):
            return process_error_key, base_template
        logger.error(
            "ACTION_FACTORY: No action_definition_template for %s",
            process_error_key,
        )
        return process_error_key, None

    def _build_customization_user_content(self, customizations: JsonDict) -> JsonDict:
        """Build user content prompt from customizations.

        Produces the user message format validated in prompt engineering sets:
        boilerplate → output guidance → attachment guidance →
        presentation guidance → original request.

        NOTE: session_id is conveyed via the user message metadata trailer
        (e.g., {"namespace":"...","source":"...","session_id":"...","posted_at":"..."})
        so the model can read it from the persisted conversation message.
        """
        output_action_guidance = customizations.get("output_action_guidance", "")
        if output_action_guidance:
            output_instructions = [output_action_guidance]
        else:
            output_instructions = [
                "Determine the appropriate next action based on the observation and the original request.",
                "If the information requested is available, present it via post_message.",
                "If additional actions are needed to fulfill the request, take those actions.",
            ]

        instructions: list[str] = list(output_instructions)

        presentation_guidance = customizations.get("presentation_guidance", "")
        if presentation_guidance:
            instructions.append("")
            instructions.append(f"Response format: {presentation_guidance}")

        user_content: JsonDict = {
            "instructions": instructions,
            "flow_input": "<<<:service_interface::flow_service::get_flow_input_for_presentation()>>>",
        }

        # Alternate instructions for when the observation result set is empty.
        output_action_guidance_empty = customizations.get("output_action_guidance_empty", "")
        if output_action_guidance_empty:
            empty_instructions: list[str] = [str(output_action_guidance_empty)]
            if presentation_guidance:
                empty_instructions.append("")
                empty_instructions.append(f"Response format: {presentation_guidance}")
            user_content["instructions_when_observation_empty"] = empty_instructions

        return user_content

    def _get_result_processor_from_customizations(self, process_key: str) -> JsonDict | None:
        """Build result processor by merging customizations with inference action_definition_template."""
        customizations = self._get_process_customizations(process_key)
        if not customizations:
            return None

        inference_process_key, base_template = self._get_inference_base_template()
        if not base_template:
            return None

        import copy

        merged: JsonDict = {
            "process_key": inference_process_key,
            "name": "process_result_processor",
        }

        base_args = base_template.get("arguments", {})
        merged["arguments"] = copy.deepcopy(base_args) if isinstance(base_args, dict) else {}

        user_content = self._build_customization_user_content(customizations)

        args = merged["arguments"]
        params = args.get("params")
        if not isinstance(params, dict):
            raise FrameworkError(
                message="process_results template must declare an object arguments.params",
                error_code=ErrorCode.ACTION_INVALID_DEFINITION,
                details={"process_key": inference_process_key},
            )
        if "prompt" not in params:
            params["prompt"] = {}
        prompt = params["prompt"]
        if isinstance(prompt, dict):
            self._enrich_result_processor_prompt(prompt, user_content, customizations, process_key)

        return merged

    def _get_error_processor_from_customizations(
        self, process_key: str,
    ) -> JsonDict | None:
        """Build an error processor action from ``error_processor_customizations``.

        Mirrors :meth:`_get_result_processor_from_customizations`: merges
        the process's error-path customizations on top of the
        ``process_error`` base template.  Returns ``None`` when the
        process declares no error customizations (callers may then
        decide whether the absence is fatal — see
        :meth:`_require_error_processor_for_deterministic`).
        """
        customizations = self._get_error_processor_customizations(process_key)
        if not customizations:
            return None

        process_error_key, base_template = self._get_process_error_base_template()
        if not base_template:
            return None

        import copy

        merged: JsonDict = {
            "process_key": process_error_key,
            "name": "process_error_processor",
        }
        base_args = base_template.get("arguments", {})
        merged["arguments"] = copy.deepcopy(base_args) if isinstance(base_args, dict) else {}

        user_content = self._build_customization_user_content(customizations)
        args = merged["arguments"]
        params = args.get("params")
        if not isinstance(params, dict):
            raise FrameworkError(
                message="process_error template must declare an object arguments.params",
                error_code=ErrorCode.ACTION_INVALID_DEFINITION,
                details={"process_key": process_error_key},
            )
        if "prompt" not in params:
            params["prompt"] = {}
        prompt = params["prompt"]
        if isinstance(prompt, dict):
            self._enrich_result_processor_prompt(
                prompt, user_content, customizations, process_key,
            )

        return merged

    @staticmethod
    def _enrich_result_processor_prompt(
        prompt: JsonDict,
        user_content: JsonDict,
        customizations: JsonDict,
        process_key: str,
    ) -> None:
        """Enrich a result processor prompt with user content and observation metadata.

        Replaces the prompt's user section with customization-derived content,
        preserving the base template's output_schema. Enriches the observation
        section with action_label summary and process_key.

        Args:
            prompt: The prompt dict to mutate (from merged arguments).
            user_content: User content built from customizations.
            customizations: Process customizations for observation enrichment.
            process_key: Process key to attach to the observation.
        """
        # Preserve base template's output_schema before replacing user content
        base_output_schema = (
            prompt.get("user", {}).get("output_schema")
            if isinstance(prompt.get("user"), dict)
            else None
        )
        prompt["user"] = user_content
        if base_output_schema and "output_schema" not in user_content:
            user_content["output_schema"] = base_output_schema

        # Enrich observation with summary and process_key for rendering
        observation = prompt.get("observation", {})
        if isinstance(observation, dict):
            action_label = customizations.get("action_label", "")
            if isinstance(action_label, str) and action_label:
                observation["summary"] = action_label
            if process_key:
                observation["process_key"] = process_key
            prompt["observation"] = observation

        # Pass message_rendering through to the prompt pipeline (Section 15)
        message_rendering = customizations.get("message_rendering")
        if isinstance(message_rendering, dict) and message_rendering:
            prompt["message_rendering"] = dict(message_rendering)

    def _substitute_templates(
        self, action_def: dict[str, object], context: dict[str, object]
    ) -> dict[str, object]:
        # Preserve original name field to prevent corruption
        original_name = action_def.get("name")

        # OPTIMIZATION: Skip template resolution if no template patterns are detected
        action_def_str = str(action_def)
        has_templates = any(
            pattern in action_def_str for pattern in ["__", "<<", ">>", "{{", "}}", "@include"]
        )

        if not has_templates:
            return action_def

        if not self.template_engine:
            raise FrameworkError(
                message="Template engine not available for action template resolution",
                error_code=ErrorCode.SYSTEM_GENERIC,
                details={"action_def": action_def, "context": context},
            )

        # Inject required context keys for template resolution
        # process_key is extracted from action_def
        process_key = self._resolve_process_key(action_def)
        context[CONTEXT_KEY_PROCESS_KEY] = process_key
        context[CONTEXT_KEY_APP_HOME] = self.app_home

        resolved_action = self.template_engine.resolve_templates(action_def, context)

        # Restore original name if template engine corrupted it
        resolved_name = resolved_action.get("name")
        if original_name and (resolved_name != original_name):
            resolved_action["name"] = original_name

        return resolved_action

    def _validate_action_structure(self, action_def: dict[str, object]) -> None:
        # Generate default name if missing (using process_key or UUID)
        if "name" not in action_def:
            process_key = action_def.get("process_key")
            if process_key and isinstance(process_key, str):
                # Extract action name from process_key (e.g., "plugin::actr_memory_plugin::remember" -> "remember")
                parts = process_key.split("::")
                action_name = parts[-1] if parts else process_key
                generated_name = f"{action_name}_{uuid.uuid4().hex[:8]}"
            else:
                generated_name = f"action_{uuid.uuid4().hex[:8]}"
            action_def["name"] = generated_name

        # Handle notes field - provide default if missing
        notes_value = action_def.get("notes")
        if notes_value is None:
            # Provide default notes based on action name
            action_name = str(action_def.get("name", "action"))
            default_notes = f"Execute {action_name}"
            action_def["notes"] = default_notes
            notes_value = default_notes

        if not isinstance(notes_value, str):
            # Try to convert to string
            notes_str = str(notes_value)
            action_def["notes"] = notes_str
            notes_value = notes_str

        normalized_notes = notes_value.strip()
        if not normalized_notes:
            # Empty notes - use default
            action_name = str(action_def.get("name", "action"))
            default_notes = f"Execute {action_name}"
            action_def["notes"] = default_notes
            normalized_notes = default_notes

        if len(normalized_notes) > NOTES_MAX_LENGTH:
            # Truncate instead of failing
            truncated_notes = normalized_notes[:NOTES_MAX_LENGTH]
            action_def["notes"] = truncated_notes
            logger.error(
                f"ACTION_FACTORY: Notes truncated from {len(normalized_notes)} to {NOTES_MAX_LENGTH} chars for action '{action_def.get('name')}'"
            )
            normalized_notes = truncated_notes

        # Persist normalized notes back onto the action definition for downstream persistence
        action_def["notes"] = normalized_notes

        if not isinstance(action_def.get("parameters", {}), dict):
            raise FrameworkError(
                message="Action parameters must be a dictionary",
                error_code=ErrorCode.ACTION_INVALID_DEFINITION,
                details={"action_def": action_def},
            )

    def _extract_function_name_from_process_key(self, process_key: str) -> str:
        """Extract function name from process_key for consistent action naming.

        Args:
            process_key: Process key in format "provider_type::provider::function_name"

        Returns:
            str: Function name from the process key

        Examples:
            "service_interface::inference_service::process_results" -> "process_results"
            "service_interface::state_service::generate_unique_string" -> "generate_unique_string"
        """
        parts = process_key.split("::")
        if len(parts) >= 3:
            return parts[2]  # function_name is the third part
        raise ValueError(
            f"Malformed process_key '{process_key}': expected "
            f"'provider_type::provider::function_name' (3+ segments), "
            f"got {len(parts)} segment(s)"
        )

    def _generate_unique_suffix(self) -> str:
        """Generate unique suffix using the service interface for DRY compliance.

        Returns:
            str: Unique random string suffix (default 13 characters, base36 encoding)

        Raises:
            FrameworkError: If state_service is not available or string generation fails
        """
        if not self.state_service:
            raise FrameworkError(
                message="StateService not available for unique string generation",
                error_code=ErrorCode.SYSTEM_GENERIC,
                details={"context": "ActionFactory._generate_unique_suffix"},
            )

        result = self.state_service.generate_unique_string(length=13, encoding="base36")

        action_status = result.get("action_status")
        if action_status != "completed":
            raise FrameworkError(
                message="StateService failed to generate unique string for action suffix",
                error_code=ErrorCode.ACTION_EXECUTION_FAILED,
                details={"state_service_result": result},
            )

        data_obj = result.get("data")
        if not isinstance(data_obj, dict):
            raise FrameworkError(
                message="StateService returned invalid data format",
                error_code=ErrorCode.ACTION_EXECUTION_FAILED,
                details={"state_service_result": result},
            )

        random_string_obj = data_obj.get("random_string")
        if not isinstance(random_string_obj, str):
            raise FrameworkError(
                message="StateService returned invalid random_string type",
                error_code=ErrorCode.ACTION_EXECUTION_FAILED,
                details={"state_service_result": result},
            )

        return random_string_obj

    def submit_action_definition(
        self, action_definition: dict[str, object], context: dict[str, object] | None = None
    ) -> str:
        """Submit action definition with fail-fast semantics.

        Args:
            action_definition: The action definition to submit (must include flow_id)
            context: Optional context for template substitution

        Returns:
            str: The action_id of the submitted action

        Raises:
            FrameworkError: If services are not ready or submission fails
        """
        # Fail fast: services must be ready
        if not self._is_ready():
            raise FrameworkError(
                message="ActionFactory services not ready - cannot submit action",
                error_code="action_factory.services_not_ready",
                details={"action_name": action_definition.get("name", "unknown")},
            )

        # Submit immediately - let exceptions propagate
        return self._submit_immediate(action_definition, context)

    def _is_ready(self) -> bool:
        """Check if all required services are available for action submission."""
        return (
            self.action_event_recorder is not None
            and bool(self.process_registry)
            and self.template_engine is not None
            and self.state_service is not None
        )

    def _enforce_flow_id(self, action_definition: dict[str, object]) -> str:
        """Enforce that a valid flow_id is present in action_definition. Fail fast if missing.

        All actions require flow_id in action_definition. No context fallback.

        Args:
            action_definition: The action definition dict (must contain flow_id)

        Returns:
            The normalized flow_id

        Raises:
            FrameworkError: If flow_id is missing or invalid in action_definition
        """
        flow_id_raw = action_definition.get(CONTEXT_KEY_FLOW_ID)
        normalized = normalize_flow_id(flow_id_raw)
        if normalized:
            action_definition[CONTEXT_KEY_FLOW_ID] = normalized
            return normalized

        # No valid flow_id found - fail fast
        process_key = action_definition.get(CONTEXT_KEY_PROCESS_KEY, "unknown")
        raise FrameworkError(
            message="Action requires flow_id in action_definition - no context fallback",
            error_code="action.flow_id_required",
            details={
                CONTEXT_KEY_PROCESS_KEY: process_key,
                "action_definition_keys": list(action_definition.keys()),
            },
        )

    def _submit_immediate(
        self, action_definition: dict[str, object], context: dict[str, object] | None = None
    ) -> str:
        """Submit action immediately when all services are available."""
        context = context or {}

        # PHASE 0: Enforce flow_id BEFORE any processing (fail fast)
        # flow_id must be in action_definition - no context fallback
        flow_id = self._enforce_flow_id(action_definition)
        # Propagate flow_id to context for template resolution
        context[CONTEXT_KEY_FLOW_ID] = flow_id

        # Normalize session_id (optional, unlike flow_id) and propagate to context
        session_id_raw = action_definition.get(CONTEXT_KEY_SESSION_ID) or context.get(
            CONTEXT_KEY_SESSION_ID
        )
        normalized_session = normalize_session_id(session_id_raw)
        if normalized_session:
            action_definition[CONTEXT_KEY_SESSION_ID] = normalized_session
            context[CONTEXT_KEY_SESSION_ID] = normalized_session

        # Resolve process_key for required-arg validation
        process_key = self._resolve_process_key(action_definition)
        arguments = action_definition.get("arguments", {})
        if isinstance(arguments, dict):
            self._validate_required_arguments(process_key, arguments)

        # Apply existing template substitution + validation via create_action
        action = self.create_action(action_definition, context)

        # Store via ActionEventRecorder
        if not self.action_event_recorder:
            raise FrameworkError(
                message="ActionEventRecorder not available",
                error_code=ErrorCode.SYSTEM_GENERIC,
                details={"action": action},
            )

        action_id = self.action_event_recorder.store_action_event(action)

        return action_id

    def process_queued_actions(self) -> int:
        """Process queued actions when services become available.

        Returns:
            int: Number of actions successfully processed
        """
        if not self._is_ready() or not self.action_queue:
            return 0

        processed_count = 0
        failed_actions = []

        for queued_action in self.action_queue[:]:  # Copy to avoid modification during iteration
            try:
                if queued_action.should_retry:
                    self._submit_immediate(queued_action.action_definition, queued_action.context)
                    processed_count += 1
                    self.action_queue.remove(queued_action)
                else:
                    # Max retries exceeded
                    logger.error(f"Queued action {queued_action.id} exceeded max retries")
                    failed_actions.append(queued_action)
                    self.action_queue.remove(queued_action)

            except Exception as e:
                logger.error(f"Failed to process queued action {queued_action.id}: {e}")
                queued_action.increment_retry()
                if not queued_action.should_retry:
                    failed_actions.append(queued_action)
                    self.action_queue.remove(queued_action)

        if failed_actions:
            logger.error(f"Failed to process {len(failed_actions)} queued actions")

        return processed_count

    def _prepare_results_with_defaults(self, results: dict[str, object]) -> dict[str, object]:
        """Prepare results for template processing.

        Note: No longer injects default USER_INPUT. Callers should explicitly
        provide all required template variables.
        """
        return results

    def _merge_template_with_base(self, processor_template: dict[str, object]) -> dict[str, object]:
        """Merge processor_template with base action_definition_template if available."""
        process_key = processor_template.get("process_key")
        if not isinstance(process_key, str):
            return processor_template

        base_template = self._get_action_definition_template(process_key)
        if not base_template:
            return processor_template

        return merge_processor_template(base_template, processor_template)

    def _ensure_action_name(self, resolved_obj: dict[str, object]) -> None:
        """Ensure resolved action has a name field."""
        if "name" in resolved_obj:
            return

        unique_suffix = self._generate_unique_suffix()
        process_key = resolved_obj.get("process_key")

        if isinstance(process_key, str):
            function_name = self._extract_function_name_from_process_key(process_key)
            resolved_obj["name"] = f"result_processor_{function_name}_{unique_suffix}"
        else:
            resolved_obj["name"] = f"result_processor_action_{unique_suffix}"

    def _inject_session_context(
        self, resolved_obj: dict[str, object], results: dict[str, object]
    ) -> None:
        """Inject session_id, flow_id, and context_id from results into resolved action."""
        # Normalize IDs to prevent empty string propagation
        normalized_session = normalize_session_id(results.get(TEMPLATE_VAR_SESSION_ID))
        if normalized_session:
            resolved_obj[CONTEXT_KEY_SESSION_ID] = normalized_session

        normalized_flow = normalize_flow_id(results.get(TEMPLATE_VAR_FLOW_ID))
        if normalized_flow:
            resolved_obj[CONTEXT_KEY_FLOW_ID] = normalized_flow

        # Propagate context_id for platform context event correlation
        context_id = results.get("context_id")
        if context_id and isinstance(context_id, str):
            resolved_obj["context_id"] = context_id

    def _resolve_notes_field(
        self, resolved_obj: dict[str, object], results: dict[str, object]
    ) -> None:
        """Resolve and validate the notes field."""
        notes_value = resolved_obj.get("notes")
        if isinstance(notes_value, str) and notes_value.strip():
            resolved_obj["notes"] = notes_value.strip()[:NOTES_MAX_LENGTH]
            return

        for key in ("notes", TEMPLATE_VAR_NOTES):
            candidate = results.get(key)
            if isinstance(candidate, str) and candidate.strip():
                resolved_obj["notes"] = candidate.strip()[:NOTES_MAX_LENGTH]
                return

        raise FrameworkError(
            message="Result processor template must provide a notes field",
            error_code=ErrorCode.ACTION_INVALID_DEFINITION,
            details={"resolved_obj": resolved_obj},
        )

    def submit_result_with_template(
        self, results: dict[str, object], processor_template: dict[str, object]
    ) -> str:
        """Submit action with result processor template variable substitution."""

        if not self.action_event_recorder:
            raise FrameworkError(
                message="ActionEventRecorder not available for template submission",
                error_code=ErrorCode.SYSTEM_GENERIC,
                details={"processor_template": processor_template},
            )

        results_with_defaults = self._prepare_results_with_defaults(results)

        try:
            merged_template = self._merge_template_with_base(processor_template)
            resolved_obj = self._substitute_result_variables(merged_template, results_with_defaults)

            if not isinstance(resolved_obj, dict):
                raise FrameworkError(
                    message="Resolved template must be a dictionary",
                    error_code=ErrorCode.ACTION_INVALID_DEFINITION,
                    details={"resolved_obj": resolved_obj},
                )

            self._ensure_action_name(resolved_obj)
            self._inject_session_context(resolved_obj, results_with_defaults)
            self._resolve_notes_field(resolved_obj, results_with_defaults)

            # Build context with session_id, flow_id, and local_variables for template resolution
            # This ensures flow_id propagates and function templates can access result data
            submission_context: dict[str, object] = {}
            if TEMPLATE_VAR_SESSION_ID in results_with_defaults:
                submission_context[CONTEXT_KEY_SESSION_ID] = results_with_defaults[
                    TEMPLATE_VAR_SESSION_ID
                ]
            if TEMPLATE_VAR_FLOW_ID in results_with_defaults:
                submission_context[CONTEXT_KEY_FLOW_ID] = results_with_defaults[
                    TEMPLATE_VAR_FLOW_ID
                ]
            # Pass result data as local_variables for function template resolution
            # This allows <<<:...>>> templates to access RESULT, ACTION_STATUS, etc.
            submission_context["local_variables"] = results_with_defaults

            # submit_action_definition returns str action_id and raises on failure
            return self.submit_action_definition(resolved_obj, submission_context)

        except Exception as e:
            logger.error(f"Result processor template submission failed: {e}")
            raise FrameworkError(
                message=f"Result processor template submission failed: {e}",
                error_code=ErrorCode.ACTION_EXECUTION_FAILED,
                details={"template": processor_template, "results": results_with_defaults},
            ) from e

    def _get_required_result_variable(self, var_name: str, results: dict[str, object]) -> object:
        """Get a required variable from results, raising error if missing."""
        if var_name not in results:
            raise FrameworkError(
                message=f"Missing result variable: {var_name}",
                error_code=ErrorCode.ACTION_INVALID_DEFINITION,
                details={"variable": var_name, "available_results": list(results.keys())},
            )
        return results[var_name]

    def _substitute_embedded_variables(self, template: str, results: dict[str, object]) -> str:
        """Substitute embedded <<VARIABLE>> patterns within a string."""
        import json

        def substitute_match(match: re.Match[str]) -> str:
            var_name = match.group(1)
            value = self._get_required_result_variable(var_name, results)
            if isinstance(value, dict | list):
                return json.dumps(value)
            return str(value)

        return re.sub(r"<<(\w+)>>", substitute_match, template)

    def _substitute_result_variables(self, template: object, results: dict[str, object]) -> object:
        """Safe recursive substitution for <<VARIABLE>> patterns."""
        if isinstance(template, dict):
            # Don't substitute inside nested result_processor fields
            return {
                key: template[key]
                if key == "result_processor"
                else self._substitute_result_variables(value, results)
                for key, value in template.items()
            }

        if isinstance(template, list):
            return [self._substitute_result_variables(item, results) for item in template]

        if isinstance(template, str):
            match = re.match(r"^<<(\w+)>>$", template)
            if match:
                return self._get_required_result_variable(match.group(1), results)
            if "<<" in template and ">>" in template:
                return self._substitute_embedded_variables(template, results)

        return template

    def update_process_registry(self, process_registry: dict[str, object]) -> None:
        self.process_registry = process_registry
