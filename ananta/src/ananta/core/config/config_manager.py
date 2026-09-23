import json
import logging
import os
from pathlib import Path

from ananta.constants import ENV_PREFIX
from ananta.core.config.config_provider import ConfigProvider
from ananta.core.config.environment_config import EnvironmentConfig
from ananta.core.domain.enums import ErrorSeverity
from ananta.core.domain.error_codes import ErrorCode
from ananta.error_handling import (
    ConfigurationError,
    FrameworkError,
)
from ananta.utils import create_directory

logger = logging.getLogger(__name__)


class ConfigManager:
    def __init__(self, APP_HOME: str, plugin_cli_args: dict[str, dict[str, object]] | None = None):
        if not APP_HOME:
            raise ValueError("APP_HOME must be provided")

        self.APP_HOME = os.path.abspath(APP_HOME)
        self.config_dir = Path(self.APP_HOME) / "config"
        self.plugins_config_dir = self.config_dir / "plugins"
        self._plugin_configs: dict[str, dict[str, object]] = {}
        self._plugin_cli_args = plugin_cli_args or {}
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return

        logger.debug(f"Initializing config structure for: {self.APP_HOME}")

        try:
            self._create_directories()
            self._load_plugin_configs()
            self._initialized = True
        except Exception as e:
            logger.error(f"Error initializing config: {e}")
            raise FrameworkError(
                message=f"Failed to initialize configuration: {e}",
                error_code=ErrorCode.CONFIGURATION_ERROR,
                details={"APP_HOME": self.APP_HOME},
                original_error=e,
                severity=ErrorSeverity.CRITICAL,
            ) from e

    def _create_directories(self) -> None:
        """Create config directories. Fails if creation is not possible."""

        try:
            create_directory(self.config_dir)
            create_directory(self.plugins_config_dir)
        except Exception as e:
            logger.error(f"Error creating config directories: {e}")
            raise

    def _load_plugin_configs(self) -> None:
        try:
            if not self.plugins_config_dir.exists():
                return

            for config_file in self.plugins_config_dir.glob("*.json"):
                try:
                    plugin_name = config_file.stem
                    with open(config_file, encoding="utf-8") as f:
                        plugin_config = json.load(f)
                        self._validate_plugin_config(plugin_config, plugin_name)
                        self._plugin_configs[plugin_name] = plugin_config
                except json.JSONDecodeError as e:
                    logger.error(f"Invalid JSON in config file {config_file.name}: {e}")
                except Exception as e:
                    logger.error(f"Error loading config for {config_file.name}: {e}")
        except Exception as e:
            logger.error(f"Error loading plugin configs: {e}")

    def _validate_plugin_config(self, config: dict[str, object], plugin_name: str) -> None:
        if "name" in config and not isinstance(config["name"], str):
            raise ConfigurationError(
                message=f"Plugin name in config for {plugin_name} must be a string",
                error_code=ErrorCode.CONFIGURATION_ERROR,
                details={"plugin_name": plugin_name},
                severity=ErrorSeverity.ERROR,
            )

        if "version" in config and not isinstance(config["version"], str):
            raise ConfigurationError(
                message=f"Plugin version in config for {plugin_name} must be a string",
                error_code=ErrorCode.CONFIGURATION_ERROR,
                details={"plugin_name": plugin_name},
                severity=ErrorSeverity.ERROR,
            )

        if "enabled" in config and not isinstance(config["enabled"], bool):
            raise ConfigurationError(
                message=f"Plugin enabled flag in config for {plugin_name} must be a boolean",
                error_code=ErrorCode.CONFIGURATION_ERROR,
                details={"plugin_name": plugin_name},
                severity=ErrorSeverity.ERROR,
            )

    def _get_plugin_env_vars(self, plugin_name: str) -> dict[str, object]:
        prefix = f"{ENV_PREFIX}{plugin_name.upper()}_"
        env_config: dict[str, object] = {}

        for key, value in os.environ.items():
            if key.startswith(prefix):
                param_name = key[len(prefix) :].lower()
                env_config[param_name] = value

        return env_config

    def get_plugin_config(
        self, plugin_name: str, default_config: dict[str, object] | None = None
    ) -> dict[str, object]:
        """Return the merged runtime config for a plugin.

        Merge order (lowest → highest priority):

        1. ``default_config`` — yaml-derived defaults from
           ``plugin.yaml``'s ``config:`` block (per the 2026-05-30
           plugin-config-defaults unification). The loader at
           :mod:`ananta.core.config.plugin_yaml_loader` produces this;
           ``plugin_initializer.initialize_all_plugins`` passes it in.
        2. ``self._plugin_configs[plugin_name]`` — operator deviations
           from disk at ``<APP_HOME>/config/plugins/<plugin_name>.json``.
        3. Per-plugin env vars (``ANANTA_<PLUGIN_NAME>_*``).
        4. CLI args (highest — operator's invocation-time override).

        Replaces the prior create-on-empty + write-to-disk behavior
        that silently snapshotted yaml defaults into the override file
        as a side effect, producing three drift surfaces in a row. After
        this change yaml is authoritative for declared defaults; the
        override file holds only operator deviations. See
        ``workbench/2026-05-30_plugin_config_defaults_unification.md``
        §8.2 (Q1.2 + Q1.3 — eager merge in the ConfigManager layer).

        Args:
            plugin_name: Plugin to read config for.
            default_config: Yaml-derived defaults from
                ``load_plugin_yaml_defaults``. Empty dict / None means
                no yaml defaults (the merge starts from the override
                file).

        Returns:
            Merged dict suitable for ``plugin.initialize(config)``.

        Raises:
            FrameworkError: when config initialization fails AND no
                ``default_config`` was supplied. (With defaults the
                early-return path still serves a degraded boot.)
        """
        if not self._initialized:
            try:
                self.initialize()
            except Exception as e:
                logger.error(f"Failed to initialize config for plugin {plugin_name}: {e}")

                if default_config is not None:
                    logger.error(f"Using provided default config for {plugin_name}")
                    return dict(default_config)

                from ananta.error_handling import FrameworkError

                raise FrameworkError(
                    message=f"Failed to load configuration for plugin {plugin_name}",
                    error_code="config.initialization_failed",
                    details={"plugin_name": plugin_name, "original_error": str(e)},
                ) from e

        config: dict[str, object] = {}
        if default_config:
            config.update(default_config)
        config.update(self._plugin_configs.get(plugin_name, {}))
        config.update(self._get_plugin_env_vars(plugin_name))
        config.update(self._plugin_cli_args.get(plugin_name, {}))
        return config

    def get_plugin_config_provider(
        self, plugin_name: str, default_config: dict[str, object] | None = None
    ) -> ConfigProvider:
        config = self.get_plugin_config(plugin_name, default_config)
        return ConfigProvider(plugin_name, config)

    def save_plugin_config(self, plugin_name: str, config: dict[str, object]) -> bool:
        if not self._initialized:
            try:
                self.initialize()
            except Exception as e:
                logger.error(f"Failed to initialize config when saving plugin {plugin_name}: {e}")
                return False

        try:
            self._validate_plugin_config(config, plugin_name)
            config_file = self.plugins_config_dir / f"{plugin_name}.json"

            with open(config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4)

            self._plugin_configs[plugin_name] = config
            logger.debug(f"Saved config for plugin: {plugin_name}")
            return True
        except Exception as e:
            logger.error(f"Error saving config for plugin {plugin_name}: {e}")
            return False

    def get_all_plugin_names(self) -> list[str]:
        if not self._initialized:
            try:
                self.initialize()
            except Exception as e:
                logger.error(f"Failed to initialize config when getting plugin names: {e}")
                return []

        plugin_names = set(self._plugin_configs.keys()) | set(self._plugin_cli_args.keys())
        return sorted(plugin_names)

    def update_plugin_cli_args(self, plugin_cli_args: dict[str, dict[str, object]]) -> None:
        self._plugin_cli_args = plugin_cli_args

    def get_core_config(self) -> dict[str, object]:
        return {
            "debug": EnvironmentConfig.is_debug(),
            "APP_HOME": self.APP_HOME,
            "data_directory": os.path.join(self.APP_HOME, "data"),
            "state_file": os.path.join(self.APP_HOME, "data", "state.json"),
            "config_directory": str(self.config_dir),
            "plugins_config_directory": str(self.plugins_config_dir),
            "logs_directory": os.path.join(self.APP_HOME, "logs"),
            "prompts_directory": os.path.join(self.APP_HOME, "config", "prompts"),
        }


_config_instance: ConfigManager | None = None


def initialize_config(
    APP_HOME: str, plugin_cli_args: dict[str, dict[str, object]] | None = None
) -> ConfigManager:
    if not APP_HOME:
        raise ValueError("APP_HOME must be provided to initialize_config")

    logger.debug(f"Initializing config with APP_HOME: {APP_HOME}")

    try:
        config_manager = ConfigManager(APP_HOME, plugin_cli_args)
        config_manager.initialize()
        set_config_instance(config_manager)
        return config_manager
    except Exception as e:
        logger.error(f"Failed to initialize config: {e}")
        raise FrameworkError(
            message=f"Config initialization failed: {e}",
            error_code=ErrorCode.CONFIGURATION_ERROR,
            details={"APP_HOME": APP_HOME},
            original_error=e,
            severity=ErrorSeverity.CRITICAL,
        ) from e


def get_config() -> ConfigManager:
    global _config_instance
    if _config_instance is None:
        raise FrameworkError(
            message="Config not initialized. Call initialize_config() before getting the config.",
            error_code=ErrorCode.CONFIGURATION_ERROR,
            severity=ErrorSeverity.CRITICAL,
        )
    return _config_instance


def set_config_instance(config_manager: ConfigManager) -> None:
    global _config_instance
    _config_instance = config_manager


def reset_config(
    APP_HOME: str, plugin_cli_args: dict[str, dict[str, object]] | None = None
) -> ConfigManager:
    if not APP_HOME:
        raise ValueError("APP_HOME must be provided to reset_config")

    config_manager = initialize_config(APP_HOME, plugin_cli_args)
    return config_manager
