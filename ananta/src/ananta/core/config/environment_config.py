"""
Centralized environment configuration utility for Ananta platform.
Provides typed access to environment variables with consistent parsing.
"""

import os


class EnvironmentConfig:
    """
    Centralized access to environment variables used throughout the Ananta platform.
    Provides typed getters and consistent boolean parsing.
    """

    # Boolean parsing values
    TRUE_VALUES = ("true", "1", "yes", "on")
    FALSE_VALUES = ("false", "0", "no", "off")

    @staticmethod
    def get_bool(key: str, default: bool = False) -> bool:
        """
        Get a boolean environment variable.

        Args:
            key: Environment variable name
            default: Default value if not set

        Returns:
            Boolean value (true/1/yes/on = True, false/0/no/off = False)
        """
        value = os.environ.get(key, str(default)).lower()
        return value in EnvironmentConfig.TRUE_VALUES

    @staticmethod
    def get_string(key: str, default: str | None = None) -> str | None:
        """
        Get a string environment variable.

        Args:
            key: Environment variable name
            default: Default value if not set

        Returns:
            String value or default
        """
        return os.environ.get(key, default)

    @staticmethod
    def get_int(key: str, default: int = 0) -> int:
        """
        Get an integer environment variable.

        Args:
            key: Environment variable name
            default: Default value if not set or invalid

        Returns:
            Integer value or default
        """
        value = os.environ.get(key)
        if value is None:
            return default
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def get_list(key: str, separator: str = ",", default: list[str] | None = None) -> list[str]:
        """
        Get a list from an environment variable (comma-separated by default).

        Args:
            key: Environment variable name
            separator: String separator (default: comma)
            default: Default list if not set

        Returns:
            List of strings or default
        """
        value = os.environ.get(key)
        if value is None:
            return default or []
        return [item.strip() for item in value.split(separator) if item.strip()]

    # Application configuration
    @staticmethod
    def app_home() -> str | None:
        """Get APP_HOME environment variable."""
        return EnvironmentConfig.get_string("APP_HOME")

    @staticmethod
    def solet_name() -> str:
        """Get the solet name (single source of truth).

        This is THE canonical way to get the solet identity.
        Used for: PostgreSQL schema name, logging, identity systems.

        The SOLET_NAME environment variable MUST be set. A silent
        default would route schema-qualified queries to a stale or
        non-existent schema and surface as inscrutable downstream
        errors; fast-fail with a discoverable message instead.
        """
        from ananta.constants import SOLET_NAME_ENV_VAR

        value = os.environ.get(SOLET_NAME_ENV_VAR)
        if value:
            return value

        legacy = os.environ.get("HOMUNCULUS_NAME")
        if legacy:
            raise RuntimeError(
                f"un-migrated launcher: legacy HOMUNCULUS_NAME={legacy!r} is set "
                f"but {SOLET_NAME_ENV_VAR} is not. The P2 rename (2026-08-13) has "
                "no compatibility alias. Run deployment/scripts/migrate_to_solet.py "
                "(or the seed-update runbook's migration step) to rewrite the "
                "LaunchAgent/launcher environment, then relaunch."
            )

        app_home = os.environ.get("APP_HOME", "<unset>")
        msg = (
            f"{SOLET_NAME_ENV_VAR} environment variable is required but not set. "
            f"Set {SOLET_NAME_ENV_VAR} to the name of this solet before "
            f"invoking the solet. Hint: this process's APP_HOME is {app_home!r} — use the "
            f"basename of the solet directory (e.g. 'example' if APP_HOME is "
            f"'~/Workspace/example'). See knowledge_bases/ananta_platform/18_cli_conventions/ "
            f"for the convention."
        )
        raise RuntimeError(msg)

    @staticmethod
    def is_debug() -> bool:
        """Check if debug mode is enabled."""
        return EnvironmentConfig.get_bool("ANANTA_DEBUG", False)

    # Logging configuration
    @staticmethod
    def log_outputs() -> list[str]:
        """Get configured log outputs."""
        return EnvironmentConfig.get_list("ANANTA_LOG_OUTPUTS", default=["console"])

    # Feature flags - Action Processing
    @staticmethod
    def use_action_processor() -> bool:
        """Check if action processor is enabled."""
        return EnvironmentConfig.get_bool("ANANTA_USE_ACTION_PROCESSOR", False)

    @staticmethod
    def use_session_manager() -> bool:
        """Check if session manager is enabled."""
        return EnvironmentConfig.get_bool("ANANTA_USE_SESSION_MANAGER", False)

    @staticmethod
    def use_service_coordinator() -> bool:
        """Check if service coordinator is enabled."""
        return EnvironmentConfig.get_bool("ANANTA_USE_SERVICE_COORDINATOR", False)

    @staticmethod
    def use_event_coordinator() -> bool:
        """Check if event coordinator is enabled."""
        return EnvironmentConfig.get_bool("ANANTA_USE_EVENT_COORDINATOR", False)

    @staticmethod
    def use_action_queue_manager() -> bool:
        """Check if action queue manager is enabled."""
        return EnvironmentConfig.get_bool("ANANTA_USE_ACTION_QUEUE_MANAGER", False)

    @staticmethod
    def use_flow_manager() -> bool:
        """Check if flow manager is enabled."""
        return EnvironmentConfig.get_bool("ANANTA_USE_FLOW_MANAGER", False)

    @staticmethod
    def use_action_event_recorder() -> bool:
        """Check if action event recorder is enabled."""
        return EnvironmentConfig.get_bool("ANANTA_USE_ACTION_EVENT_RECORDER", True)

    @staticmethod
    def use_metadata_registry() -> bool:
        """Check if metadata registry is enabled."""
        return EnvironmentConfig.get_bool("ANANTA_USE_METADATA_REGISTRY", True)

    @staticmethod
    def use_new_template_engine() -> bool:
        """Check if new template engine is enabled."""
        return EnvironmentConfig.get_bool("ANANTA_USE_NEW_TEMPLATE_ENGINE", True)

    @staticmethod
    def use_system_platform_manager() -> bool:
        """Check if system platform manager is enabled."""
        return EnvironmentConfig.get_bool("ANANTA_USE_SYSTEM_PLATFORM_MANAGER", True)

    @staticmethod
    def use_process_registry_manager() -> bool:
        """Check if process registry manager is enabled."""
        return EnvironmentConfig.get_bool("ANANTA_USE_PROCESS_REGISTRY_MANAGER", True)

    @staticmethod
    def use_event_processor() -> bool:
        """Check if event processor is enabled."""
        return EnvironmentConfig.get_bool("ANANTA_USE_EVENT_PROCESSOR", False)

    @staticmethod
    def use_event_orchestrator() -> bool:
        """Check if event orchestrator is enabled."""
        return EnvironmentConfig.get_bool("ANANTA_USE_EVENT_ORCHESTRATOR", True)

    @staticmethod
    def enable_all_orchestration_features() -> bool:
        """Check if all orchestration features should be enabled."""
        return EnvironmentConfig.get_bool("ANANTA_ENABLE_ALL_ORCHESTRATION_FEATURES", False)

    # New metadata system flags
    @staticmethod
    def use_new_metadata_system() -> bool:
        """Check if new metadata system is enabled."""
        return EnvironmentConfig.get_bool("ANANTA_USE_NEW_METADATA_SYSTEM", True)


class FeatureFlags:
    """
    Convenience wrapper for feature flag checks.
    Checks enable_all first, then individual flags.
    """

    @staticmethod
    def use_action_processor() -> bool:
        """Check if action processor is enabled (respects enable_all)."""
        if EnvironmentConfig.enable_all_orchestration_features():
            return True
        return EnvironmentConfig.use_action_processor()

    @staticmethod
    def use_session_manager() -> bool:
        """Check if session manager is enabled (respects enable_all)."""
        if EnvironmentConfig.enable_all_orchestration_features():
            return True
        return EnvironmentConfig.use_session_manager()

    @staticmethod
    def use_service_coordinator() -> bool:
        """Check if service coordinator is enabled (respects enable_all)."""
        if EnvironmentConfig.enable_all_orchestration_features():
            return True
        return EnvironmentConfig.use_service_coordinator()

    @staticmethod
    def use_event_coordinator() -> bool:
        """Check if event coordinator is enabled (respects enable_all)."""
        if EnvironmentConfig.enable_all_orchestration_features():
            return True
        return EnvironmentConfig.use_event_coordinator()

    @staticmethod
    def use_action_queue_manager() -> bool:
        """Check if action queue manager is enabled (respects enable_all)."""
        if EnvironmentConfig.enable_all_orchestration_features():
            return True
        return EnvironmentConfig.use_action_queue_manager()

    @staticmethod
    def use_flow_manager() -> bool:
        """Check if flow manager is enabled (respects enable_all)."""
        if EnvironmentConfig.enable_all_orchestration_features():
            return True
        return EnvironmentConfig.use_flow_manager()

    @staticmethod
    def use_action_event_recorder() -> bool:
        """Check if action event recorder is enabled (respects enable_all)."""
        if EnvironmentConfig.enable_all_orchestration_features():
            return True
        return EnvironmentConfig.use_action_event_recorder()

    @staticmethod
    def use_metadata_registry() -> bool:
        """Check if metadata registry is enabled (respects enable_all)."""
        if EnvironmentConfig.enable_all_orchestration_features():
            return True
        return EnvironmentConfig.use_metadata_registry()

    @staticmethod
    def use_new_template_engine() -> bool:
        """Check if new template engine is enabled (respects enable_all)."""
        if EnvironmentConfig.enable_all_orchestration_features():
            return True
        return EnvironmentConfig.use_new_template_engine()

    @staticmethod
    def use_system_platform_manager() -> bool:
        """Check if system platform manager is enabled (respects enable_all)."""
        if EnvironmentConfig.enable_all_orchestration_features():
            return True
        return EnvironmentConfig.use_system_platform_manager()

    @staticmethod
    def use_process_registry_manager() -> bool:
        """Check if process registry manager is enabled (respects enable_all)."""
        if EnvironmentConfig.enable_all_orchestration_features():
            return True
        return EnvironmentConfig.use_process_registry_manager()

    @staticmethod
    def use_event_processor() -> bool:
        """Check if event processor is enabled (respects enable_all)."""
        if EnvironmentConfig.enable_all_orchestration_features():
            return True
        return EnvironmentConfig.use_event_processor()

    @staticmethod
    def use_event_orchestrator() -> bool:
        """Check if event orchestrator is enabled (respects enable_all)."""
        if EnvironmentConfig.enable_all_orchestration_features():
            return True
        return EnvironmentConfig.use_event_orchestrator()
