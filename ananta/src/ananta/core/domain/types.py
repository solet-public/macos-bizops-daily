from typing import TypedDict


class ActionProcess(TypedDict):
    plugin: str
    function: str


class ActionDefinitionFile(TypedDict):
    name: str
    description: str
    process: ActionProcess
    parameters: dict[str, object]
    properties: dict[str, object] | None


class ActionObject(TypedDict, total=False):
    name: str
    parameters: dict[str, object]
    notes: str
    action_status: str
    timestamp: str
    error: dict[str, object] | None  # ErrorDetail
    result: dict[str, object] | None
    data: dict[str, object] | None
    source: str | None


class StateRoot(TypedDict, total=False):
    _version: int
    actions: list[ActionObject]
    last_error: dict[str, object] | None  # ErrorDetail
    chat_history: list[object] | None


class PluginConfig(TypedDict, total=False):
    name: str
    version: str
    enabled: bool
    log_level: str
    timeout: int | None
    retry_count: int | None


class ModelConfig(TypedDict, total=False):
    name: str
    api_url: str
    api_key_env: str
    description: str | None
    temperature: float | None


class PromptConfig(TypedDict, total=False):
    compose: list[str] | None
    action_instructions: dict[str, object] | None
    system: str | None
    user: str | None


class ActionParameter(TypedDict):
    name: str
    value: object


class ActionDefinition(TypedDict):
    name: str
    parameters: dict[str, object]


class ErrorDetail(TypedDict, total=True):
    type: str
    code: str
    message: str
    details: dict[str, object]
    severity: str
    timestamp: str


class ActionResult(TypedDict, total=False):
    action_status: str
    data: dict[str, object]
    actions: list[dict[str, object]]
    error: ErrorDetail | None
    timestamp: str
