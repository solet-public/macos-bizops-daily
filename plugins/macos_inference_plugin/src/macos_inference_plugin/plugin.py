"""Standalone Apple summary provider; frontier orchestration stays in the platform."""

from __future__ import annotations

import logging
from typing import Any

from ananta.core.domain.types import ActionResult
from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces import InferenceRequest, InferenceServiceUnavailableError
from ananta.interfaces.context_management_contract import ContextManagementContract
from ananta.interfaces.inference_errors import InferenceValidationError
from ananta.services.context_management.compaction_types import CompactionRequest, WarmingRequest
from ananta.services.context_management.config import ContextManagementConfig
from ananta.services.inference_service.interfaces.provider import InferenceDefaults, InferenceProvider

from .configuration import config_schema, context_config, default_config, validate_config
from .providers.apple_fm_provider import AppleFMProvider

logger = logging.getLogger(__name__)

# Every unavailable reason the provider reports. The first four are apple_fm_sdk
# 0.2.1's SystemLanguageModelUnavailableReason members; SDK_UNAVAILABLE is the
# provider's own "SDK not installed or loadable". Each means the system model is
# absent here, which rul_73886083, rul_18bd93a3 and rul_cc1afc13 make a warning:
# the plugin stays ready and degraded, so the roster accepts it and the warning
# stays visible. PROBE_FAILED (the SDK raised) is a real provider error.
DEGRADED_REASONS = frozenset({
    "APPLE_INTELLIGENCE_NOT_ENABLED", "DEVICE_NOT_ELIGIBLE", "MODEL_NOT_READY", "UNKNOWN",
    "SDK_UNAVAILABLE",
})


class Plugin(PluginBase, InferenceProvider, ContextManagementContract):
    """Independent summary provider with per-request SDK sessions and no stored state."""

    def __init__(self) -> None:
        super().__init__()
        self.name = "macos_inference_plugin"
        self.provider: AppleFMProvider | None = None
        self._settings: dict[str, Any] | None = None

    def prepare_for_readiness(self) -> None:
        """Validate wiring/configuration; model absence is a readiness warning later."""
        if self.config_provider is None:
            manager = getattr(self.orchestrator_ref, "config_manager", None)
            if manager is None:
                raise RuntimeError("macos_inference_plugin requires an injected config provider")
            self.config_provider = manager.get_plugin_config_provider(self.name)
        if self.config_provider is None:
            raise RuntimeError("macos_inference_plugin configuration is missing")
        settings: dict[str, Any] = dict(self.config_provider.config)
        validate_config(settings)
        self._settings = settings
        self.provider = AppleFMProvider(timeout_seconds=settings["timeout_seconds"])
        # Registration must succeed even on a VM without the Apple model.
        # Runtime requests always probe the current model before generating.
        self.set_ready()

    def _provider(self) -> AppleFMProvider:
        if self.provider is None:
            raise InferenceServiceUnavailableError("macos_inference_plugin is not initialized")
        return self.provider

    def _config(self) -> dict[str, Any]:
        if self._settings is None:
            raise RuntimeError("macos_inference_plugin configuration is not initialized")
        return self._settings

    def start_post_registration_work(self) -> None:
        """Observe model readiness without failing registration or starting a daemon."""
        result = self.validate_availability()
        error = result.get("error")
        if error:
            logger.warning("Apple summary capability unavailable: %s", error)

    def is_ready(self) -> bool:
        """Recheck an unavailable model so the service can recover after repair."""
        if self.provider is not None and (
            self.readiness_error is not None or self.readiness_warning is not None
        ):
            self.validate_availability()
        return super().is_ready()

    def validate_availability(self) -> ActionResult:
        """Probe on demand so repair/unavailability are reversible without restart."""
        result = self._provider().validate_availability()
        data = result.get("data", {})
        if data.get("available"):
            self.set_ready()
            return result
        error = result.get("error") or {}
        message = str(error.get("message", "Apple model unavailable"))
        if data.get("reason") in DEGRADED_REASONS:
            self.set_ready(warning=message)
        else:
            self.set_error(message)
        return result

    def generate_completion(self, request: InferenceRequest) -> ActionResult:
        """Generate prose or an explicitly supplied guided summary schema."""
        if request.use_structured_output and request.response_schema is None:
            raise InferenceValidationError(
                "Apple summaries require an explicit schema; action generation is unsupported"
            )
        return self._provider().generate_completion(request)

    def get_model_info(self) -> ActionResult:
        return self._provider().get_model_info()

    def get_configured_model_name(self) -> str:
        return str(self._config()["model"])

    def get_inference_defaults(self) -> InferenceDefaults:
        config = self._config()
        return InferenceDefaults(
            temperature=config["temperature"], max_tokens=config["max_tokens"],
            action_vertex_temperature=config["temperature"],
            action_vertex_max_tokens=config["max_tokens"],
        )

    def propose_name(self, params: dict[str, Any], state: dict[str, Any]) -> ActionResult:
        del params, state
        raise InferenceValidationError(
            "Naming is outside the Apple summary contract; use the frontier session"
        )

    def get_context_management_config(self) -> ContextManagementConfig:
        return context_config(self._config())

    def generate_compaction_summary(self, request: CompactionRequest) -> str:
        """Summarize bounded caller-supplied context; oversized input fails explicitly."""
        source = "\n".join(
            f"{message.get('role', 'unknown')}: {message.get('content', '')}"
            for message in request.messages_to_summarize
        )
        prompt = (
            f"Summarize the following conversation in at most {request.summary_budget_chars} "
            f"characters. Preserve decisions and key facts.\n"
            f"Previous summary: {request.existing_summary or ''}\nConversation:\n{source}"
        )
        result = self.generate_completion(InferenceRequest(
            prompt, temperature=request.temperature, max_tokens=request.max_tokens,
            use_structured_output=False, hide_from_context=True,
        ))
        payload = result.get("data", {}).get("result")
        if not isinstance(payload, dict) or not isinstance(payload.get("completion"), str):
            raise InferenceValidationError("Apple summary result has no completion text")
        return payload["completion"]

    def warm_cache(self, request: WarmingRequest) -> bool:
        """Unsupported: no reusable session/cache survives an SDK request."""
        del request
        return False

    def get_default_config(self) -> dict[str, object]:
        return default_config()

    def get_config_schema(self) -> dict[str, object]:
        return config_schema()

    @property
    def service_interfaces(self) -> tuple[type, ...]:
        return (InferenceProvider,)

    @property
    def supported_interface_versions(self) -> dict[type, str]:
        return {InferenceProvider: "1.0.0"}
