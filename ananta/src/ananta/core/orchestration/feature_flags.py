import logging
from collections.abc import Callable

from ananta.core.config.environment_config import FeatureFlags

logger = logging.getLogger(__name__)


class OrchestrationFeatureFlags:
    @staticmethod
    def use_plugin_lifecycle_manager() -> bool:
        return False

    @staticmethod
    def use_action_processor() -> bool:
        enabled = FeatureFlags.use_action_processor()
        return enabled

    @staticmethod
    def use_session_manager() -> bool:
        enabled = FeatureFlags.use_session_manager()
        return enabled

    @staticmethod
    def use_service_coordinator() -> bool:
        enabled = FeatureFlags.use_service_coordinator()
        return enabled

    @staticmethod
    def use_event_coordinator() -> bool:
        enabled = FeatureFlags.use_event_coordinator()
        return enabled

    @staticmethod
    def use_action_queue_manager() -> bool:
        enabled = FeatureFlags.use_action_queue_manager()
        return enabled

    @staticmethod
    def use_flow_manager() -> bool:
        enabled = FeatureFlags.use_flow_manager()
        return enabled

    @staticmethod
    def use_action_event_recorder() -> bool:
        enabled = FeatureFlags.use_action_event_recorder()
        return enabled

    @staticmethod
    def use_metadata_registry() -> bool:
        enabled = FeatureFlags.use_metadata_registry()
        return enabled

    @staticmethod
    def use_new_template_engine() -> bool:
        enabled = FeatureFlags.use_new_template_engine()
        return enabled

    @staticmethod
    def use_system_platform_manager() -> bool:
        enabled = FeatureFlags.use_system_platform_manager()
        return enabled

    @staticmethod
    def use_process_registry_manager() -> bool:
        enabled = FeatureFlags.use_process_registry_manager()
        return enabled

    @staticmethod
    def use_event_processor() -> bool:
        enabled = FeatureFlags.use_event_processor()
        return enabled

    @staticmethod
    def use_event_orchestrator() -> bool:
        enabled = FeatureFlags.use_event_orchestrator()
        return enabled

    @staticmethod
    def enable_all_orchestration_features() -> bool:
        from ananta.core.config.environment_config import EnvironmentConfig

        enabled = EnvironmentConfig.enable_all_orchestration_features()
        return enabled

    @staticmethod
    def is_legacy_mode() -> bool:
        return not any(
            [
                OrchestrationFeatureFlags.use_plugin_lifecycle_manager(),
                OrchestrationFeatureFlags.use_action_processor(),
                OrchestrationFeatureFlags.use_session_manager(),
                OrchestrationFeatureFlags.use_service_coordinator(),
                OrchestrationFeatureFlags.use_event_coordinator(),
                OrchestrationFeatureFlags.use_action_queue_manager(),
                OrchestrationFeatureFlags.use_flow_manager(),
                OrchestrationFeatureFlags.use_action_event_recorder(),
            ]
        )

    @staticmethod
    def _get_component_flag_mapping() -> list[tuple[str, Callable[[], bool]]]:
        return [
            ("PluginLifecycleManager", OrchestrationFeatureFlags.use_plugin_lifecycle_manager),
            ("ActionProcessor", OrchestrationFeatureFlags.use_action_processor),
            ("SessionManager", OrchestrationFeatureFlags.use_session_manager),
            ("ServiceCoordinator", OrchestrationFeatureFlags.use_service_coordinator),
            ("EventCoordinator", OrchestrationFeatureFlags.use_event_coordinator),
            ("ActionQueueManager", OrchestrationFeatureFlags.use_action_queue_manager),
            ("FlowManager", OrchestrationFeatureFlags.use_flow_manager),
            ("ActionEventRecorder", OrchestrationFeatureFlags.use_action_event_recorder),
            ("SystemPlatformManager", OrchestrationFeatureFlags.use_system_platform_manager),
            ("ProcessRegistryManager", OrchestrationFeatureFlags.use_process_registry_manager),
            ("EventProcessor", OrchestrationFeatureFlags.use_event_processor),
            ("EventOrchestrator", OrchestrationFeatureFlags.use_event_orchestrator),
        ]

    @staticmethod
    def get_enabled_components() -> list[str]:
        component_mapping = OrchestrationFeatureFlags._get_component_flag_mapping()
        return [
            component_name for component_name, flag_method in component_mapping if flag_method()
        ]
