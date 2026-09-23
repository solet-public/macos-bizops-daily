from typing import Literal, TypedDict, TypeVar

T = TypeVar("T")
K = TypeVar("K")

LogLevel = Literal["debug", "info", "warning", "error", "critical"]


class PluginOperationalConfig(TypedDict, total=False):
    name: str
    version: str
    enabled: bool
    log_level: LogLevel | str
    log_format: str
    log_max_size: int
    log_backup_count: int


class CoreFrameworkConfig(TypedDict):
    debug: bool
    APP_HOME: str
    data_directory: str
    state_file: str
    config_directory: str
    plugins_config_directory: str
    logs_directory: str
    prompts_directory: str
