from abc import abstractmethod
from typing import TypedDict

from ananta.core.providers.base_provider import BaseProvider


class ToolSchema(TypedDict):
    name: str
    description: str
    parameters: dict[str, object]
    provider: str
    source: str


class ToolExecutionResult(TypedDict):
    success: bool
    result: object
    error: str | None
    tool_name: str
    provider: str
    execution_time_ms: int


class BaseToolProvider(BaseProvider):
    @abstractmethod
    async def execute_tool(
        self, _tool_name: str, parameters: dict[str, object]
    ) -> ToolExecutionResult: ...

    @abstractmethod
    def get_tool_schema(self, _tool_name: str) -> dict[str, object]: ...

    def validate_config(self, config: dict[str, object]) -> bool:
        required_fields = ["provider_type"]
        return all(field in config for field in required_fields)
