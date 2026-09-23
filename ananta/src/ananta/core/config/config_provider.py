import json
import logging
from typing import TypeVar

from ananta.core.config.config_types import PluginOperationalConfig
from ananta.core.domain.enums import ErrorSeverity
from ananta.core.domain.error_codes import ErrorCode
from ananta.error_handling import (
    ConfigurationError,
    InvalidConfigurationFormatError,
    MissingConfigurationError,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ConfigProvider:
    def __init__(self, plugin_name: str, config: dict[str, object]) -> None:
        self.plugin_name = plugin_name
        self.config = config or {}

    def get(self, param_name: str, default: T | None = None) -> object | T:
        if not param_name:
            raise ConfigurationError(
                message="Parameter name cannot be empty",
                error_code=ErrorCode.VALIDATION_GENERIC,
                details={"plugin_name": self.plugin_name},
                severity=ErrorSeverity.ERROR,
            )

        return self.config.get(param_name, default)

    def get_int(self, param_name: str, default: int | None = None) -> int:
        value = self.get(param_name, default)
        if value is None:
            return 0

        if isinstance(value, int):
            return value

        if isinstance(value, str | float):
            try:
                return int(value)
            except (ValueError, TypeError) as e:
                raise InvalidConfigurationFormatError(
                    message=f"Invalid integer value for {param_name}",
                    plugin_name=self.plugin_name,
                    param_name=param_name,
                    expected_type="int",
                    received_type=type(value).__name__,
                    original_error=e,
                    severity=ErrorSeverity.ERROR,
                ) from e

        raise InvalidConfigurationFormatError(
            message=f"Cannot convert {type(value).__name__} to integer",
            plugin_name=self.plugin_name,
            param_name=param_name,
            expected_type="int",
            received_type=type(value).__name__,
            severity=ErrorSeverity.ERROR,
        )

    def get_float(self, param_name: str, default: float | None = None) -> float:
        value = self.get(param_name, default)
        if value is None:
            return 0.0

        if isinstance(value, float):
            return value

        if isinstance(value, str | int):
            try:
                return float(value)
            except (ValueError, TypeError) as e:
                raise InvalidConfigurationFormatError(
                    message=f"Invalid float value for {param_name}",
                    plugin_name=self.plugin_name,
                    param_name=param_name,
                    expected_type="float",
                    received_type=type(value).__name__,
                    original_error=e,
                    severity=ErrorSeverity.ERROR,
                ) from e

        raise InvalidConfigurationFormatError(
            message=f"Cannot convert {type(value).__name__} to float",
            plugin_name=self.plugin_name,
            param_name=param_name,
            expected_type="float",
            received_type=type(value).__name__,
            severity=ErrorSeverity.ERROR,
        )

    def get_bool(self, param_name: str, default: bool | None = None) -> bool:
        value = self.get(param_name, default)
        if value is None:
            return False

        if isinstance(value, bool):
            return value

        if isinstance(value, str):
            return value.lower() in ("true", "yes", "1", "y", "t")

        if isinstance(value, int | float):
            return bool(value)

        return bool(value)

    def get_dict(
        self, param_name: str, default: dict[str, object] | None = None
    ) -> dict[str, object]:
        value = self.get(param_name, default)
        if value is None:
            return {}

        if isinstance(value, dict):
            # Ensure all keys are strings and values are objects
            result: dict[str, object] = {}
            for key, val in value.items():
                if not isinstance(key, str):
                    raise InvalidConfigurationFormatError(
                        message=f"Dictionary key must be a string, got {type(key).__name__}",
                        plugin_name=self.plugin_name,
                        param_name=param_name,
                        expected_type="dict[str, object]",
                        received_type=f"dict[{type(key).__name__}, object]",
                        severity=ErrorSeverity.ERROR,
                    )
                result[key] = val
            return result

        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if not isinstance(parsed, dict):
                    raise InvalidConfigurationFormatError(
                        message="JSON value is not a dictionary",
                        plugin_name=self.plugin_name,
                        param_name=param_name,
                        expected_type="dict",
                        received_type=type(parsed).__name__,
                        severity=ErrorSeverity.ERROR,
                    )
                # Ensure all keys are strings
                result = {}
                for key, val in parsed.items():
                    if not isinstance(key, str):
                        raise InvalidConfigurationFormatError(
                            message=f"Dictionary key must be a string, got {type(key).__name__}",
                            plugin_name=self.plugin_name,
                            param_name=param_name,
                            expected_type="dict[str, object]",
                            received_type=f"dict[{type(key).__name__}, object]",
                            severity=ErrorSeverity.ERROR,
                        )
                    result[key] = val
                return result
            except json.JSONDecodeError as e:
                raise InvalidConfigurationFormatError(
                    message=f"Invalid JSON value for {param_name}",
                    plugin_name=self.plugin_name,
                    param_name=param_name,
                    expected_type="dict",
                    received_type="str (invalid JSON)",
                    original_error=e,
                    severity=ErrorSeverity.ERROR,
                ) from e

        raise InvalidConfigurationFormatError(
            message=f"Cannot convert {type(value).__name__} to dictionary",
            plugin_name=self.plugin_name,
            param_name=param_name,
            expected_type="dict",
            received_type=type(value).__name__,
            severity=ErrorSeverity.ERROR,
        )

    def _parse_json_list(self, value: str) -> list[object]:
        """Parse a JSON array string into a list."""
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return list(parsed)
        return [parsed]

    def _parse_csv_list(self, value: str) -> list[object]:
        """Parse a comma-separated string into a list."""
        return [item.strip() for item in value.split(",") if item.strip()]

    def _convert_string_to_list(self, value: str) -> list[object]:
        """Convert a string value to a list, trying JSON first then CSV."""
        if not value.strip().startswith("["):
            return self._parse_csv_list(value)

        try:
            return self._parse_json_list(value)
        except json.JSONDecodeError:
            return self._parse_csv_list(value)

    def get_list(self, param_name: str, default: list[object] | None = None) -> list[object]:
        value = self.get(param_name, default)
        if value is None:
            return []

        if isinstance(value, list):
            return list(value)

        if isinstance(value, str):
            return self._convert_string_to_list(value)

        return [value]

    def require(self, param_name: str) -> object:
        value = self.get(param_name)
        if value is None:
            raise MissingConfigurationError(
                message=f"Required parameter {param_name} not found",
                plugin_name=self.plugin_name,
                param_name=param_name,
                severity=ErrorSeverity.ERROR,
            )
        return value

    def has(self, param_name: str) -> bool:
        return self.get(param_name) is not None

    def get_log_level(self, default: str = "info") -> str:
        log_level = self.get("log_level", default)
        if isinstance(log_level, str):
            return log_level.lower()
        return str(log_level).lower()

    def get_log_format(
        self, default: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    ) -> str:
        value = self.get("log_format", default)
        if isinstance(value, str):
            return value
        return str(value)

    def get_log_max_size(self, default: int = 524288000) -> int:
        return self.get_int("log_max_size", default)

    def get_log_backup_count(self, default: int = 5) -> int:
        return self.get_int("log_backup_count", default)

    def get_log_outputs(self, default: list[str] | None = None) -> list[str]:
        outputs_str = self.get("log_outputs")
        if outputs_str:
            if isinstance(outputs_str, str):
                return [output.strip() for output in outputs_str.split(",") if output.strip()]
            elif isinstance(outputs_str, list):
                # Ensure all items are strings
                result: list[str] = []
                for item in outputs_str:
                    if isinstance(item, str):
                        result.append(item)
                    else:
                        result.append(str(item))
                return result

        if default is not None:
            return default

        return ["file"]

    def is_enabled(self, default: bool = True) -> bool:
        return self.get_bool("enabled", default)

    def get_timeout(self, default: int = 30) -> int:
        return self.get_int("timeout", default)

    def get_retry_count(self, default: int = 3) -> int:
        return self.get_int("retry_count", default)

    def get_debug_level(self, default: str = "info") -> str:
        debug_level = self.get("debug_level")
        if debug_level is not None:
            if isinstance(debug_level, str):
                return debug_level.lower()
            return str(debug_level).lower()
        return default

    def to_operational_config(self) -> PluginOperationalConfig:
        name_value = self.get("name", self.plugin_name)
        version_value = self.get("version", "0.1.0")

        # Ensure name and version are strings
        name = name_value if isinstance(name_value, str) else str(name_value)
        version = version_value if isinstance(version_value, str) else str(version_value)

        return {
            "name": name,
            "version": version,
            "enabled": self.is_enabled(),
            "log_level": self.get_log_level(),
            "log_format": self.get_log_format(),
            "log_max_size": self.get_log_max_size(),
            "log_backup_count": self.get_log_backup_count(),
        }
