"""Metadata-dict generators for plugin process entries.

Extracted from `ProcessRegistryBuilder` during the Step 9.A decomposition
(`workbench/2026-05-25_plugin_god_class_remediation.md` §9.1).

The class name preserves the design doc's wording. It owns the
plugin-side `input_contract` and `action_blueprint` generators called by
`PluginProcessScanner` when building each plugin registry entry from
`ActionMetadata`:

  - `generate_input_contract(action)`
  - `generate_action_blueprint(process_key, action)`
  - `_build_default_arguments(action)` (private helper)

A second cluster of `_generate_service_interface_*` /
`_extract_*_from_service_interface` / parameters-JSON-schema methods
sat alongside these in the pre-decomposition builder but had no live
callers anywhere in the codebase; they were removed in the Step 9.A
follow-up dead-code pass (§2 of this skill's continuation dispatch).
"""

from __future__ import annotations

from ananta.core.actions.action_metadata import ActionMetadata


class ServiceInterfaceMetadataGenerator:
    """Generate per-entry metadata dicts for plugin processes.

    Stateless. The orchestrator constructs one instance and shares it
    with `PluginProcessScanner`.
    """

    def generate_input_contract(self, action: ActionMetadata) -> dict[str, object]:
        """Generate input_contract from ActionMetadata.

        Structure:
            pass
        {
            "parameters": {param_name: param_spec},
            "context_requirements": [list of required context keys],
            "result_shape": {return value specification}
        }

        Args:
            action: ActionMetadata with parameter definitions

        Returns:
            Input contract dict with parameters, context requirements, and result shape
        """
        # Build parameters dict separately for type safety
        parameters_dict: dict[str, object] = {}
        for param_name, param_meta in action.parameters.items():
            param_dict = param_meta.to_dict()
            parameters_dict[param_name] = param_dict

        contract: dict[str, object] = {
            "parameters": parameters_dict,
            "context_requirements": [],
            "result_shape": {},
        }

        # Extract context requirements (analyze parameters for context references)
        # For now, basic implementation - can be enhanced with placeholder analysis
        if action.parameters:
            contract["context_requirements"] = ["session_id", "user_input"]

        # Build result_shape from return_value_schema
        if action.return_value_schema:
            contract["result_shape"] = action.return_value_schema.to_dict()

        return contract

    def generate_action_blueprint(
        self, process_key: str, action: ActionMetadata
    ) -> dict[str, object]:
        """Generate action_blueprint with default argument skeleton.

        Structure:
            pass
        {
            "process_key": str,
            "arguments": {defaults with placeholders},
            "context_overrides": {context bindings},
            "metadata": {capabilities, category, etc.},
            "post_processing": {follow-up hooks}
        }

        Args:
            process_key: Full process key
            action: ActionMetadata with parameter definitions

        Returns:
            Action blueprint dict with complete default structure
        """
        # Build default arguments from parameters
        default_args = self._build_default_arguments(action)

        blueprint: dict[str, object] = {
            "process_key": process_key,
            "arguments": default_args,
            "context_overrides": {},
            "metadata": {
                "is_inference_capable": action.is_inference_capable,
                "is_async": action.is_async,
                "estimated_duration": action.estimated_duration or "< 1s",
                "version": action.version,
            },
            "post_processing": {},
        }

        return blueprint

    def _build_default_arguments(self, action: ActionMetadata) -> dict[str, object]:
        """Build default argument skeleton with placeholder mappings.

        Logic:
            pass
        - Required params with no default → placeholder like "<<PARAM_NAME>>"
        - Optional params → use default value from ParameterMetadata
        - Special handling for common patterns (prompt → <<USER_INPUT>>)

        Args:
            action: ActionMetadata with parameter definitions

        Returns:
            Default arguments dict with placeholders and defaults
        """
        args: dict[str, object] = {}

        for param_name, param_meta in action.parameters.items():
            if param_meta.default is not None:
                # Use provided default
                args[param_name] = param_meta.default
            elif param_meta.required:
                # Required param needs placeholder
                # Use AI hints to determine appropriate placeholder
                if param_name == "prompt" or "prompt" in param_name.lower():
                    args[param_name] = "<<USER_INPUT>>"
                elif param_name == "message":
                    args[param_name] = "<<MESSAGE>>"
                else:
                    # Generic placeholder
                    args[param_name] = f"<<{param_name.upper()}>>"
            else:
                # Optional param without default - omit from blueprint
                pass

        return args
