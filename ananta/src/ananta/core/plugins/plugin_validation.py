import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ananta.core.actions.action_validator import ValidationResult

logger = logging.getLogger(__name__)

DEFAULT_VALIDATION_PRIORITY = 100


class ValidationPhase(Enum):
    PRE_STRUCTURE = "pre_structure"
    POST_STRUCTURE = "post_structure"
    PARAMETER = "parameter"
    POST_PARAMETER = "post_parameter"
    FINAL = "final"


@dataclass
class PluginValidationHook:
    plugin_name: str
    action_name: str
    validator_function: Callable[[dict[str, object]], tuple[bool, str | None]]
    priority: int = DEFAULT_VALIDATION_PRIORITY
    validation_phase: ValidationPhase = ValidationPhase.PARAMETER
    description: str = ""


class PluginValidationRegistry:
    def __init__(self) -> None:
        self._validators: dict[str, list[PluginValidationHook]] = {}
        self._global_validators: list[PluginValidationHook] = []

    def register_validator(self, hook: PluginValidationHook) -> None:
        if hook.action_name == "*":
            self._global_validators.append(hook)
        else:
            if hook.action_name not in self._validators:
                self._validators[hook.action_name] = []
            self._validators[hook.action_name].append(hook)

    def get_validators(
        self, action_name: str, phase: ValidationPhase
    ) -> list[PluginValidationHook]:
        validators = []

        validators.extend([v for v in self._global_validators if v.validation_phase == phase])

        action_validators = self._validators.get(action_name, [])
        validators.extend([v for v in action_validators if v.validation_phase == phase])

        return sorted(validators, key=lambda x: x.priority)

    def validate_with_plugins(
        self,
        action_request: dict[str, object],
        phase: ValidationPhase,
        source_context: dict[str, object],
    ) -> "ValidationResult":
        from ananta.core.actions.action_validator import ValidationDecision, ValidationResult

        action_name_obj = action_request.get("name", "unknown")
        action_name = str(action_name_obj) if action_name_obj else "unknown"
        validators = self.get_validators(action_name, phase)

        if not validators:
            return ValidationResult(decision=ValidationDecision.PROCEED, success=True)

        for validator in validators:
            try:
                arguments_obj = action_request.get("arguments", {})
                arguments = arguments_obj if isinstance(arguments_obj, dict) else {}
                is_valid, error_message = validator.validator_function(arguments)

                if not is_valid:
                    logger.error(
                        f"Plugin validation failed [{validator.plugin_name}]: {error_message}"
                    )
                    return ValidationResult(
                        decision=ValidationDecision.REJECT,
                        success=False,
                        error_message=f"Plugin validation failed [{validator.plugin_name}]: {error_message}",
                        original_context=source_context,
                    )
                else:
                    pass  # Validation passed

            except Exception as e:
                logger.error(f"Plugin validator error [{validator.plugin_name}]: {str(e)}")
                return ValidationResult(
                    decision=ValidationDecision.REJECT,
                    success=False,
                    error_message=f"Plugin validator error [{validator.plugin_name}]: {str(e)}",
                    original_context=source_context,
                )

        return ValidationResult(decision=ValidationDecision.PROCEED, success=True)

    def get_validation_summary(self) -> dict[str, object]:
        total_validators = len(self._global_validators)
        action_validators = sum(len(validators) for validators in self._validators.values())
        total_validators += action_validators

        return {
            "total_validators": total_validators,
            "global_validators": len(self._global_validators),
            "action_specific_validators": action_validators,
            "registered_actions": list(self._validators.keys()),
            "plugins_with_validators": list(
                set(
                    [v.plugin_name for v in self._global_validators]
                    + [
                        v.plugin_name
                        for validators in self._validators.values()
                        for v in validators
                    ]
                )
            ),
        }
