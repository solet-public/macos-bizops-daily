import logging
from datetime import UTC, datetime
from typing import TypeVar

from ananta.config.schema_factory import get_standardized_schema
from ananta.constants import FRAMEWORK_NAMESPACE, STATE_VERSION
from ananta.core.actions.action_factory import ActionFactory
from ananta.core.domain.enums import ActionStatus, ErrorSeverity
from ananta.core.domain.error_codes import ErrorCode
from ananta.core.domain.status import is_status_match
from ananta.core.domain.types import ActionResult
from ananta.error_handling import (
    AnantaError,
    FrameworkError,
    log_exception,
)
from ananta.interfaces.state_management_interface import StateManagementInterface

logger = logging.getLogger(__name__)

StateDict = dict[str, object]
T = TypeVar("T", bound=StateDict)


class StateManager[T: StateDict]:
    STATE_VERSION = STATE_VERSION

    def __init__(self, state_service: StateManagementInterface) -> None:
        self.state_service = state_service
        self._pending_injections: list[dict[str, object]] = []
        self._current_state: T | None = None
        self._action_factory: ActionFactory | None = None
        self._in_memory_state: T | None = None
        self._schema_initialized: bool = False
        # FAIL-FAST: Always use in-memory state to avoid circular dependencies

    def set_action_factory(self, action_factory: "ActionFactory") -> None:
        """Set the action factory for state management operations."""
        self._action_factory = action_factory

    def _ensure_schema_created(self) -> None:
        """Lazy schema initialization to avoid circular dependency."""
        if self._schema_initialized:
            return

        try:
            # Check if we can safely call state_service (avoid circular dependency)
            if hasattr(self.state_service, "bootstrap_mode") and self.state_service.bootstrap_mode:
                return

            # Use standardized schema from SchemaFactory which includes proper system fields
            orchestrator_schema = get_standardized_schema("orchestrator_state")

            # Convert TableSchema objects to dictionary format expected by state service
            tables_dict: dict[str, object] = {}
            for table_name, table_schema in orchestrator_schema.tables.items():
                column_definitions: dict[str, object] = {}
                for col_name, col_def in table_schema.columns.items():
                    # Use the ColumnDefinition.to_sql() method to generate complete SQL with constraints
                    full_sql_definition = col_def.to_sql(col_name)

                    # Extract just the type and constraints part (without column name)
                    parts = full_sql_definition.split(" ", 1)
                    if len(parts) > 1:
                        column_sql: str = parts[1]  # Everything after column name
                    else:
                        column_sql = col_def.type.value  # Fallback to just type

                    column_definitions[col_name] = column_sql

                # Build table definition with id_prefix and columns
                tables_dict[table_name] = {
                    "id_prefix": table_schema.id_prefix,
                    "columns": column_definitions,
                }

            schema: dict[str, object] = {"tables": tables_dict}

            result = self.state_service.create_schema(FRAMEWORK_NAMESPACE, schema)
            if is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
                self._schema_initialized = True
            else:
                logger.error(f"Failed to create orchestrator state schema: {result}")

        except Exception as e:
            logger.error(f"Error ensuring orchestrator state schema: {e}")

    def load_sync(self) -> T:
        # FAIL-FAST: Orchestrator StateManager ALWAYS uses in-memory state to avoid circular dependencies
        if self._in_memory_state is None:
            # Create initial state - mypy knows this satisfies StateDict constraints
            initial_state: StateDict = {"_version": self.STATE_VERSION, "actions": []}
            # We know T is bound to StateDict, so this is safe
            self._in_memory_state = initial_state  # type: ignore[assignment]
            logger.debug("Created new in-memory orchestrator state")
        self._current_state = self._in_memory_state
        # At this point _in_memory_state is not None, so we can safely return it
        assert self._in_memory_state is not None
        return self._in_memory_state

    async def load(self) -> T:
        await self._process_pending_injections()

        # FAIL-FAST: Orchestrator StateManager ALWAYS uses in-memory state to avoid circular dependencies
        if self._in_memory_state is None:
            # Create initial state - mypy knows this satisfies StateDict constraints
            initial_state: StateDict = {"_version": self.STATE_VERSION, "actions": []}
            # We know T is bound to StateDict, so this is safe
            self._in_memory_state = initial_state  # type: ignore[assignment]
            logger.debug("Created new in-memory orchestrator state")
        self._current_state = self._in_memory_state
        # At this point _in_memory_state is not None, so we can safely return it
        assert self._in_memory_state is not None
        return self._in_memory_state

    async def save(self, state: T) -> None:
        # FAIL-FAST: Orchestrator StateManager uses ONLY in-memory state to avoid circular dependencies
        self._in_memory_state = state
        self._current_state = state
        return

    async def add_action(self, action: dict[str, object]) -> T:
        # Validate action name with type narrowing
        action_name = action.get("name", "")
        if not isinstance(action_name, str):
            raise FrameworkError(
                message="Action name must be a string",
                error_code=ErrorCode.ACTION_MISSING_NAME,
                severity=ErrorSeverity.ERROR,
            )
        self._validate_action_name(action_name)

        try:
            state = await self.load()
            timestamp = datetime.now(UTC).isoformat()

            action["action_status"] = ActionStatus.QUEUED.value
            action["timestamp"] = timestamp

            # Type narrow the actions list
            actions_obj = state.get("actions", [])
            if not isinstance(actions_obj, list):
                raise FrameworkError(
                    message="State 'actions' must be a list",
                    error_code=ErrorCode.SYSTEM_GENERIC,
                    severity=ErrorSeverity.ERROR,
                )
            # We've verified it's a list, now trust the type
            actions: list[object] = actions_obj
            actions.append(action)
            state["actions"] = actions

            logger.debug(f"Added action: {action.get('name', 'unknown')}")
            await self.save(state)
            return state
        except AnantaError:
            logger.error(
                f"Error occurred while adding action: {action.get('name', 'unknown')}",
                exc_info=True,
            )
            raise
        except Exception as e:
            error = FrameworkError(
                message=f"Failed to add action {action.get('name', 'unknown')}",
                error_code=ErrorCode.ACTION_EXECUTION_FAILED,
                details={"action": action},
                original_error=e,
                severity=ErrorSeverity.ERROR,
            )
            log_exception(error, logger)
            raise error from None

    async def update_action_result(self, action_name: str, result: dict[str, object]) -> T:
        self._validate_action_name(action_name)
        self._validate_action_result(result, action_name)

        try:
            state = await self.load()
            logger.debug(f"Setting result for action: {action_name}")

            # Type narrow the actions list
            actions_obj = state.get("actions", [])
            if not isinstance(actions_obj, list):
                raise FrameworkError(
                    message="State 'actions' must be a list",
                    error_code=ErrorCode.SYSTEM_GENERIC,
                    severity=ErrorSeverity.ERROR,
                )
            # We've verified it's a list, now trust the type
            actions: list[object] = actions_obj
            timestamp = datetime.now(UTC).isoformat()

            for action_obj in actions:
                if not isinstance(action_obj, dict):
                    continue
                action = action_obj
                if action.get("name") == action_name:
                    action.update(result)
                    action["timestamp"] = timestamp
                    break

            await self.save(state)
            return state
        except AnantaError:
            logger.error(
                f"Error occurred while setting result for action: {action_name}", exc_info=True
            )
            raise
        except Exception as e:
            error = FrameworkError(
                message=f"Failed to set result for action {action_name}",
                error_code=ErrorCode.ACTION_EXECUTION_FAILED,
                details={"action_name": action_name},
                original_error=e,
                severity=ErrorSeverity.ERROR,
            )
            log_exception(error, logger)
            raise error from None

    async def update_action_error(self, action_name: str, error_data: dict[str, object]) -> T:
        self._validate_action_name(action_name)

        try:
            state = await self.load()
            logger.debug(f"Setting error for action: {action_name}")

            state["last_error"] = error_data

            # Type narrow the actions list
            actions_obj = state.get("actions", [])
            if not isinstance(actions_obj, list):
                raise FrameworkError(
                    message="State 'actions' must be a list",
                    error_code=ErrorCode.SYSTEM_GENERIC,
                    severity=ErrorSeverity.ERROR,
                )
            # We've verified it's a list, now trust the type
            actions: list[object] = actions_obj
            timestamp = datetime.now(UTC).isoformat()

            for action_obj in actions:
                if not isinstance(action_obj, dict):
                    continue
                action = action_obj
                if action.get("name") == action_name:
                    action["error"] = error_data
                    action["action_status"] = ActionStatus.ERROR.value
                    action["timestamp"] = timestamp
                    break

            await self.save(state)
            return state
        except AnantaError:
            logger.error(
                f"Error occurred while setting error for action: {action_name}", exc_info=True
            )
            raise
        except Exception as e:
            error = FrameworkError(
                message=f"Failed to set error for action {action_name}",
                error_code=ErrorCode.ACTION_EXECUTION_FAILED,
                details={"action_name": action_name},
                original_error=e,
                severity=ErrorSeverity.ERROR,
            )
            log_exception(error, logger)
            raise error from None

    def _extract_state_data_from_result(self, result: ActionResult) -> dict[str, object]:
        """Extract state_data from plugin state query result."""
        if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            return {}

        data = result.get("data")
        if not isinstance(data, dict):
            return {}

        result_obj = data.get("result")
        if not isinstance(result_obj, dict):
            return {}

        records = result_obj.get("records")
        if not isinstance(records, list) or not records:
            return {}

        first_record = records[0]
        if not isinstance(first_record, dict):
            return {}

        state_data = first_record.get("state_data", {})
        return state_data if isinstance(state_data, dict) else {}

    async def get_plugin_state(self, plugin_name: str) -> dict[str, object]:
        if not plugin_name:
            raise FrameworkError(
                message="Plugin name cannot be empty",
                error_code=ErrorCode.VALIDATION_GENERIC,
                severity=ErrorSeverity.ERROR,
            )

        try:
            result = self.state_service.read_state(
                namespace=plugin_name,
                query={
                    "table": "plugin_state",
                    "filters": {},
                    "limit": 1,
                    "order_by": "updated_at DESC",
                },
            )
            return self._extract_state_data_from_result(result)

        except AnantaError:
            logger.error(f"Error retrieving plugin state for: {plugin_name}", exc_info=True)
            raise
        except Exception as e:
            error = FrameworkError(
                message=f"Failed to retrieve plugin state for {plugin_name}",
                error_code=ErrorCode.SYSTEM_GENERIC,
                details={"plugin_name": plugin_name},
                original_error=e,
                severity=ErrorSeverity.ERROR,
            )
            log_exception(error, logger)
            raise error from None

    def _validate_plugin_state_inputs(self, plugin_name: str, data: dict[str, object]) -> None:
        """Validate plugin name and data parameters."""
        if not plugin_name:
            raise FrameworkError(
                message="Plugin name cannot be empty",
                error_code=ErrorCode.VALIDATION_GENERIC,
                severity=ErrorSeverity.ERROR,
            )

        if not hasattr(data, "keys") or not callable(data.keys):
            raise FrameworkError(
                message=f"Plugin state data must be a dictionary, got {type(data).__name__}",
                error_code=ErrorCode.VALIDATION_GENERIC,
                details={
                    "provided_type": type(data).__name__,
                    "expected_type": "dict",
                    "plugin_name": plugin_name,
                },
                severity=ErrorSeverity.ERROR,
            )

    def _ensure_plugin_schema(self, plugin_name: str) -> None:
        """Ensure schema exists for plugin with standardized fields."""
        plugin_state_columns: dict[str, object] = {"state_data": "TEXT"}
        plugin_table_def: dict[str, object] = {
            "id_prefix": "ps",  # plugin_state prefix
            "columns": plugin_state_columns,
        }
        plugin_tables: dict[str, object] = {"plugin_state": plugin_table_def}
        schema: dict[str, object] = {"tables": plugin_tables}
        self.state_service.create_schema(plugin_name, schema)

    def _check_plugin_state_exists(self, plugin_name: str) -> bool:
        """Check if plugin state record exists."""
        existing_result = self.state_service.read_state(
            namespace=plugin_name, query={"table": "plugin_state", "filters": {}, "limit": 1}
        )

        if not is_status_match(existing_result.get("action_status"), ActionStatus.COMPLETED):
            return False

        data_obj = existing_result.get("data")
        if not isinstance(data_obj, dict):
            return False

        result_obj = data_obj.get("result")
        if not isinstance(result_obj, dict):
            return False

        records = result_obj.get("records")
        return isinstance(records, list) and len(records) > 0

    def _write_or_update_plugin_state(
        self, plugin_name: str, current_state: dict[str, object], has_records: bool
    ) -> ActionResult:
        """Write or update plugin state record."""
        if has_records:
            return self.state_service.update_state(
                namespace=plugin_name,
                query={"table": "plugin_state", "filters": {}},
                updates={"state_data": current_state},
            )
        return self.state_service.write_state(
            namespace=plugin_name,
            data={"table": "plugin_state", "record": {"state_data": current_state}},
        )

    async def update_plugin_state(self, plugin_name: str, data: dict[str, object]) -> T:
        self._validate_plugin_state_inputs(plugin_name, data)

        try:
            self._ensure_plugin_schema(plugin_name)

            current_state = await self.get_plugin_state(plugin_name)
            current_state.update(data)

            has_records = self._check_plugin_state_exists(plugin_name)
            result = self._write_or_update_plugin_state(plugin_name, current_state, has_records)

            if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
                raise FrameworkError(
                    message=f"Failed to update plugin state: {result}",
                    error_code=ErrorCode.SYSTEM_GENERIC,
                    details={"plugin_name": plugin_name, "result": result},
                    severity=ErrorSeverity.ERROR,
                )

            return await self.load()

        except AnantaError:
            logger.error(f"Error updating plugin state for: {plugin_name}", exc_info=True)
            raise
        except Exception as e:
            error = FrameworkError(
                message=f"Failed to update plugin state for {plugin_name}",
                error_code=ErrorCode.SYSTEM_GENERIC,
                details={"plugin_name": plugin_name},
                original_error=e,
                severity=ErrorSeverity.ERROR,
            )
            log_exception(error, logger)
            raise error from None

    def _validate_action_name(self, action_name: str) -> None:
        if not action_name:
            raise FrameworkError(
                message="Action name cannot be empty",
                error_code=ErrorCode.ACTION_MISSING_NAME,
                severity=ErrorSeverity.ERROR,
            )

    def _validate_action_result(self, result: dict[str, object], action_name: str) -> None:
        if not hasattr(result, "keys") or not callable(result.keys):
            raise FrameworkError(
                message=f"Action result must be a dictionary, got {type(result).__name__}",
                error_code=ErrorCode.ACTION_INVALID_RESPONSE_FORMAT,
                details={
                    "provided_type": type(result).__name__,
                    "expected_type": "dict",
                    "action": action_name,
                },
                severity=ErrorSeverity.ERROR,
            )

    async def _process_pending_injections(self) -> None:
        if not self._pending_injections:
            return

        actions_to_inject: list[dict[str, object]] = self._pending_injections.copy()
        self._pending_injections.clear()

        if actions_to_inject:
            logger.debug(f"Processing {len(actions_to_inject)} pending action injections")

            try:
                state = await self.load()
                # Type narrow the actions list
                actions_obj = state.get("actions", [])
                if not isinstance(actions_obj, list):
                    raise FrameworkError(
                        message="State 'actions' must be a list",
                        error_code=ErrorCode.SYSTEM_GENERIC,
                        severity=ErrorSeverity.ERROR,
                    )
                # We've verified it's a list, now trust the type
                actions: list[object] = actions_obj

                for action in actions_to_inject:
                    actions.append(action)
                    action_name_obj = action.get("name")
                    _action_name_str = (
                        str(action_name_obj) if action_name_obj is not None else "unknown"
                    )

                state["actions"] = actions
                await self.save(state)

                logger.debug(f"Successfully processed {len(actions_to_inject)} injected actions")

            except Exception as e:
                logger.error(f"Error processing pending injections: {e}")
                self._pending_injections.extend(actions_to_inject)
                raise

    def inject_external_action(self, action: dict[str, object]) -> None:
        self._pending_injections.append(action)

    def _compute_process_key(self, process_obj: dict[str, object]) -> str:
        provider_type_obj = process_obj.get("provider_type", "plugin")
        provider_type = str(provider_type_obj) if provider_type_obj is not None else "plugin"

        provider_obj = process_obj.get("provider")
        function_name_obj = process_obj.get("function_name")

        if not provider_obj:
            raise FrameworkError(
                message="Process object missing 'provider' field",
                error_code=ErrorCode.VALIDATION_GENERIC,
                details={"process": process_obj},
                severity=ErrorSeverity.ERROR,
            )

        if not function_name_obj:
            raise FrameworkError(
                message="Process object missing 'function' field",
                error_code=ErrorCode.VALIDATION_GENERIC,
                details={"process": process_obj},
                severity=ErrorSeverity.ERROR,
            )

        provider = str(provider_obj)
        function_name = str(function_name_obj)
        return f"{provider_type}::{provider}::{function_name}"
