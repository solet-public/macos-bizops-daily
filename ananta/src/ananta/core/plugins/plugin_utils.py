from typing import cast

from ananta.core.config.config_types import PluginOperationalConfig


def extract_actions_from_data(data: dict[str, object]) -> list[dict[str, object]]:
    if "actions" in data and isinstance(data["actions"], list):
        return cast(list[dict[str, object]], data["actions"])
    return []


def validate_error_code_format(error_code: str) -> bool:
    parts = error_code.split(".")
    if len(parts) != 2:
        return False
    if not all(parts):
        return False
    return True


def validate_operational_config_format(config: dict[str, object]) -> bool:
    action_level_keys = {
        "model",
        "api_url",
        "api_key",
        "parameters",
        "process",
        "actions",
        "prompts",
        "temperature",
        "max_tokens",
    }

    config_keys = set(config.keys())

    if config_keys.intersection(action_level_keys):
        return False

    return True


def extract_operational_settings(config: dict[str, object]) -> PluginOperationalConfig:
    operational_config = cast(PluginOperationalConfig, {})

    if "name" in config:
        operational_config["name"] = cast(str, config["name"])
    if "version" in config:
        operational_config["version"] = cast(str, config["version"])
    if "enabled" in config:
        operational_config["enabled"] = cast(bool, config["enabled"])
    if "log_level" in config:
        operational_config["log_level"] = cast(str, config["log_level"])
    if "log_format" in config:
        operational_config["log_format"] = cast(str, config["log_format"])
    if "log_max_size" in config:
        operational_config["log_max_size"] = cast(int, config["log_max_size"])
    if "log_backup_count" in config:
        operational_config["log_backup_count"] = cast(int, config["log_backup_count"])

    return operational_config


def merge_operational_configs(
    base_config: PluginOperationalConfig, override_config: PluginOperationalConfig
) -> PluginOperationalConfig:
    merged = cast(PluginOperationalConfig, dict(base_config))
    merged.update(override_config)
    return merged


def get_default_operational_config(plugin_name: str) -> PluginOperationalConfig:
    return {
        "name": plugin_name,
        "version": "0.1.0",
        "enabled": True,
        "log_level": "info",
        "log_format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        "log_max_size": 524288000,
        "log_backup_count": 5,
    }
