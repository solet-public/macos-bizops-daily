import asyncio
import logging

from ananta.core.plugins.plugin_contracts import ErrorCode, ErrorSeverity
from ananta.core.plugins.plugin_manager import PluginManager
from ananta.core.process_registry.builder import build_process_registry
from ananta.core.process_registry.constants import SYSTEM_PROMPT_PROCESS_KEYS
from ananta.core.process_registry.util import ProcessRegistryUtil
from ananta.core.state.state_manager import StateManager
from ananta.error_handling import FrameworkError
from ananta.services.discovery_service import DiscoveryService
from ananta.services.schema_manager import SchemaManager
from ananta.services.state_service import StateService
from ananta.types.column_types import ColumnType
from ananta.types.schema_types import (
    ColumnDefinition,
    SchemaDefinition,
    TableSchema,
)

from ..interfaces.process_registry_manager_interface import IProcessRegistryManager

logger = logging.getLogger(__name__)


class ProcessRegistryManager(IProcessRegistryManager):
    def __init__(
        self,
        plugin_manager: PluginManager,
        state_service: StateService,
        state_manager: StateManager[dict[str, object]],
    ):
        self.plugin_manager = plugin_manager
        self.state_service = state_service
        self.process_registry_util = ProcessRegistryUtil(state_service)
        self.state_manager = state_manager
        self.discovery_service: DiscoveryService | None = None
        self.schema_manager: SchemaManager | None = None
        self._process_registry: dict[str, object] | None = None

    def set_discovery_service(self, discovery_service: DiscoveryService) -> None:
        self.discovery_service = discovery_service
        # CRITICAL FIX: If registry was already built, populate discovery service now
        # This handles the case where discovery_service is set AFTER build_and_populate_registry()
        if self._process_registry is not None:
            registry_processes = self._process_registry.get("processes")
            if isinstance(registry_processes, dict) and len(registry_processes) > 0:
                logger.debug(
                    f"ProcessRegistryManager: Populating discovery_service with {len(registry_processes)} existing processes"
                )
                for process_key, process_data in registry_processes.items():
                    if isinstance(process_data, dict):
                        try:
                            discovery_service.store_process(process_key, process_data)
                        except Exception as e:
                            logger.error(
                                f"ProcessRegistryManager: Failed to store process {process_key} in discovery: {e}"
                            )

    def set_schema_manager(self, schema_manager: SchemaManager) -> None:
        self.schema_manager = schema_manager

    def _validate_and_extract_processes(self, raw_registry: object) -> dict[str, object]:
        """Validate registry structure and extract processes dict."""
        if not isinstance(raw_registry, dict):
            raise TypeError("build_process_registry must return a dict")

        processes_obj = raw_registry.get("processes")
        if not isinstance(processes_obj, dict):
            raise TypeError("Registry must contain a 'processes' dict")

        return processes_obj

    def _log_registry_stats(self, registry_processes: dict[str, object]) -> None:
        """Log statistics about the built registry."""
        service_interface_count = sum(
            1 for k in registry_processes.keys() if k.startswith("service_interface::")
        )
        plugin_processes = len(registry_processes) - service_interface_count
        total_processes = len(registry_processes)

        logger.debug(
            f"ProcessRegistryManager: Built registry with {total_processes} total processes "
            f"({plugin_processes} plugin, {service_interface_count} service interface)"
        )

    def _populate_discovery_service(self, registry_processes: dict[str, object]) -> None:
        """Populate discovery service with process definitions."""
        if self.discovery_service is None:
            logger.error(
                "ProcessRegistryManager: No discovery service available - processes will not be searchable"
            )
            return

        # Clear existing process vectors before loading new ones
        # This ensures embedding_description changes take effect on restart
        # (vectors have unique constraint on external_id that prevents updates)
        if hasattr(self.discovery_service, "clear_process_vectors"):
            self.discovery_service.clear_process_vectors()

        for process_key, process_data in registry_processes.items():
            if not isinstance(process_data, dict):
                logger.error(f"Skipping non-dict process data for {process_key}")
                continue
            try:
                self.discovery_service.store_process(process_key, process_data)
            except Exception as e:
                logger.error(
                    f"ProcessRegistryManager: Failed to store process {process_key}: {e}",
                    exc_info=True,
                )

    def build_and_populate_registry(self, *, populate_discovery: bool = True) -> None:
        logger.debug("ProcessRegistryManager.build_and_populate_registry() called")
        try:
            raw_registry = build_process_registry(self.plugin_manager)
            processes_obj = self._validate_and_extract_processes(raw_registry)

            logger.debug(
                f"ProcessRegistryManager: build_process_registry returned registry with {len(processes_obj)} processes"
            )

            self._process_registry = {"processes": processes_obj}
            self._log_registry_stats(processes_obj)
            if populate_discovery:
                self._populate_discovery_service(processes_obj)

        except Exception as e:
            raise FrameworkError(
                message="Process registry building failed - system cannot operate without process registry",
                error_code=ErrorCode.SYSTEM_GENERIC,
                details={"error": str(e)},
                severity=ErrorSeverity.CRITICAL,
            ) from e

    def populate_discovery_after_registration(self) -> None:
        """Build process vectors only after the router has activated this process."""
        if self._process_registry is None:
            raise RuntimeError("Process registry is unavailable for deferred discovery population")
        processes_obj = self._process_registry.get("processes")
        if not isinstance(processes_obj, dict):
            raise TypeError("Process registry has invalid processes data")
        self._populate_discovery_service(processes_obj)

    async def persist_registry(self) -> None:
        try:
            self._do_process_registry_persistence()
        except Exception as e:
            raise FrameworkError(
                message="Process registry persistence failed - system cannot operate without persisted registry",
                error_code=ErrorCode.SYSTEM_GENERIC,
                details={"error": str(e)},
                severity=ErrorSeverity.CRITICAL,
            ) from e

    def register_dynamic_processes(self, processes: dict[str, object]) -> None:
        """Add service processes to runtime registry"""
        if self._process_registry is None:
            return

        # Type narrow to get the processes dict
        registry_processes = self._process_registry.get("processes")
        if not isinstance(registry_processes, dict):
            logger.error("Registry processes is not a dict")
            return

        # Type narrow each process before adding
        for key, value in processes.items():
            if isinstance(value, dict):
                registry_processes[key] = value
            else:
                logger.error(f"Skipping non-dict process data for {key}")

        # Update discovery service if available
        if self.discovery_service:
            for process_key, process_data in processes.items():
                if isinstance(process_data, dict):
                    self.discovery_service.store_process(process_key, process_data)
                else:
                    logger.error(f"Skipping non-dict process data for {process_key}")

        len(registry_processes)

    def unregister_dynamic_processes(self, process_keys: list[str]) -> None:
        """Remove service processes from runtime registry"""
        if self._process_registry is None:
            return

        # Type narrow to get the processes dict
        registry_processes = self._process_registry.get("processes")
        if not isinstance(registry_processes, dict):
            logger.error("Registry processes is not a dict")
            return

        removed_count = 0
        for key in process_keys:
            if key in registry_processes:
                del registry_processes[key]
                removed_count += 1

        # Update discovery service if available
        if self.discovery_service:
            remove_method = getattr(self.discovery_service, "remove_process", None)
            if callable(remove_method):
                for process_key in process_keys:
                    remove_method(process_key)

        len(registry_processes)

    def get_dynamic_process_count(self) -> int:
        """Get count of dynamic processes in registry"""
        if self._process_registry is None:
            return 0

        # Type narrow to get the processes dict
        registry_processes = self._process_registry.get("processes")
        if not isinstance(registry_processes, dict):
            return 0

        dynamic_count = 0
        for process_data in registry_processes.values():
            if isinstance(process_data, dict) and process_data.get("is_dynamic", False):
                dynamic_count += 1

        return dynamic_count

    def list_dynamic_processes(self) -> dict[str, dict[str, object]]:
        """List all dynamic processes currently registered"""
        if self._process_registry is None:
            return {}

        # Type narrow to get the processes dict
        registry_processes = self._process_registry.get("processes")
        if not isinstance(registry_processes, dict):
            return {}

        dynamic_processes: dict[str, dict[str, object]] = {}
        for process_key, process_data in registry_processes.items():
            if isinstance(process_data, dict) and process_data.get("is_dynamic", False):
                dynamic_processes[process_key] = process_data

        return dynamic_processes

    def _determine_record_namespace(self, process_data: dict[str, object]) -> str:
        """Determine the appropriate namespace for a process record."""
        if process_data.get("provider_type") == "plugin":
            provider = process_data.get("provider", "core")
            if isinstance(provider, str):
                return provider
            return "core"
        return "core"

    def _convert_inference_capable_to_int(self, value: object) -> int:
        """Convert is_inference_capable to int for database storage."""
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        return 0

    def _serialize_json_field(self, value: object, default: str = "{}") -> str:
        """Serialize a field to JSON string for database storage."""
        import json

        if value:
            return json.dumps(value)
        return default

    def _serialize_parameter_schema(self, schema_data: object, process_key: str) -> str:
        """Validate and serialize parameter_schema to JSON."""
        import json

        if not isinstance(schema_data, dict):
            raise TypeError(
                f"parameter_schema must be a dict (JSON-ready), got {type(schema_data)} for {process_key}"
            )
        return json.dumps(schema_data, ensure_ascii=False)

    def _build_json_fields(self, process_data: dict[str, object]) -> dict[str, str]:
        """Build all JSON-serialized fields for the record."""
        return {
            "input_contract": self._serialize_json_field(process_data.get("input_contract", {})),
            "action_blueprint": self._serialize_json_field(
                process_data.get("action_blueprint", {})
            ),
            "planning_docs": self._serialize_json_field(process_data.get("planning_docs", {})),
            "error_handling_docs": self._serialize_json_field(
                process_data.get("error_handling_docs", {})
            ),
            "response_handling_docs": self._serialize_json_field(
                process_data.get("response_handling_docs", {})
            ),
            "chaining_guidance": self._serialize_json_field(
                process_data.get("chaining_guidance", []), "[]"
            ),
            "invocation_schema": self._serialize_json_field(
                process_data.get("invocation_schema", {})
            ),
            "action_definition_template": self._serialize_json_field(
                process_data.get("action_definition_template", {})
            ),
            "result_processor_customizations": self._serialize_json_field(
                process_data.get("result_processor_customizations", {})
            ),
            "error_processor_customizations": self._serialize_json_field(
                process_data.get("error_processor_customizations", {})
            ),
        }

    def _build_record_data(
        self, process_key: str, process_data: dict[str, object], record_namespace: str
    ) -> dict[str, object]:
        """Build the record data structure for persistence with dual-write support."""
        is_inference_capable_int = self._convert_inference_capable_to_int(
            process_data.get("is_inference_capable", False)
        )

        json_fields = self._build_json_fields(process_data)
        parameter_schema_json = self._serialize_parameter_schema(
            process_data.get("parameter_schema"), process_key
        )
        external_id = f"proc_{process_key.replace('::', '_')}"

        record_data: dict[str, object] = {
            "external_id": external_id,
            "provider_type": process_data.get("provider_type", "plugin"),
            "provider": process_data.get("provider", ""),
            "function_name": process_data.get("function_name", ""),
            "process_key": process_key,
            "name": process_data.get("name"),
            "display_name": process_data.get("display_name", ""),
            "description": process_data.get(
                "description",
                f"Process {process_data.get('function_name', '')} from {process_data.get('provider', '')}",
            ),
            "embedding_description": process_data.get("embedding_description", ""),
            "is_discoverable": bool(process_data.get("is_discoverable", True)),
            "include_in_system_prompt": process_key in SYSTEM_PROMPT_PROCESS_KEYS,
            "parameter_schema": parameter_schema_json,
            "process_template": str(process_data.get("process_template", "{}")),
            "is_inference_capable": bool(is_inference_capable_int),
            "is_enabled": True,
            "is_long_running": bool(process_data.get("is_long_running")),
            "work_count_impact": process_data["work_count_impact"],
            "namespace": record_namespace,
        }

        record_data.update(json_fields)
        return record_data

    def _persist_single_process(self, process_key: str, process_data: dict[str, object]) -> bool:
        """Persist a single process to the database, return success status."""
        try:
            record_namespace = self._determine_record_namespace(process_data)
            record_data = self._build_record_data(process_key, process_data, record_namespace)

            success = self.process_registry_util.write_single_record(record_data)

            if success:
                pass
            else:
                pass

            return success

        except Exception:
            return False

    def _do_process_registry_persistence(self) -> None:
        try:
            if not self._process_registry:
                return

            # Type narrow to get the processes dict
            processes_obj = self._process_registry.get("processes")
            if not isinstance(processes_obj, dict):
                logger.error("Registry processes is not a dict")
                return

            persisted_count = 0
            error_count = 0

            for process_key, process_data in processes_obj.items():
                if isinstance(process_data, dict):
                    if self._persist_single_process(process_key, process_data):
                        persisted_count += 1
                    else:
                        error_count += 1
                else:
                    logger.error(f"Skipping non-dict process data for {process_key}")
                    error_count += 1

        except Exception as e:
            # Safely get process count for error details
            process_count = 0
            if self._process_registry:
                processes_obj = self._process_registry.get("processes")
                if isinstance(processes_obj, dict):
                    process_count = len(processes_obj)

            raise FrameworkError(
                message=f"Process registry persistence failed: {str(e)}",
                error_code="process_registry.persist_failed",
                details={
                    "total_processes": process_count,
                    "registry_available": True,
                    "state_service_available": True,
                },
                original_error=e,
                severity=ErrorSeverity.ERROR,
            ) from e

    async def load_into_discovery_service(self) -> None:
        if not self.discovery_service:
            raise FrameworkError(
                message="Discovery service not available for process loading",
                error_code=ErrorCode.SYSTEM_GENERIC,
                details={"discovery_service_available": False},
                severity=ErrorSeverity.CRITICAL,
            )

        try:
            await self._initialize_discovery_service_tables()

            # Clear existing process vectors before loading new ones
            # This ensures embedding_description changes take effect on restart
            # (vectors have unique constraint on external_id that prevents updates)
            if hasattr(self.discovery_service, "clear_process_vectors"):
                self.discovery_service.clear_process_vectors()

            plugin_registry = await asyncio.get_event_loop().run_in_executor(
                None, build_process_registry, self.plugin_manager
            )

            plugin_processes_obj = plugin_registry.get("processes", {})
            if not isinstance(plugin_processes_obj, dict):
                raise TypeError("Registry must contain a 'processes' dict")

            for process_key, process_data in plugin_processes_obj.items():
                if not isinstance(process_data, dict):
                    logger.error(f"Skipping non-dict process data for {process_key}")
                    continue
                self.discovery_service.store_process(process_key, process_data)

            # Service interface processes are already included in plugin_processes_obj
            # from build_process_registry() - no need to add them separately
            self.discovery_service.get_process_count()
            len(plugin_processes_obj)

            await asyncio.get_event_loop().run_in_executor(
                None, self.discovery_service.rebuild_index
            )

        except Exception as e:
            raise FrameworkError(
                message="Process loading failed - discovery service cannot operate without processes",
                error_code=ErrorCode.SYSTEM_GENERIC,
                details={"error": str(e)},
                severity=ErrorSeverity.CRITICAL,
            ) from e

    async def _initialize_discovery_service_tables(self) -> None:
        if not self.schema_manager:
            raise FrameworkError(
                message="Schema manager not available for discovery service table initialization",
                error_code=ErrorCode.SYSTEM_GENERIC,
                details={"schema_manager_available": False},
                severity=ErrorSeverity.CRITICAL,
            )

        discovery_schema = SchemaDefinition(
            namespace="core",
            version="1.0.0",
            description="Simplified discovery service for usage tracking and intelligent disambiguation",
            tables={
                "usage_stats": TableSchema(
                    table_name="usage_stats",
                    columns={
                        "process_key": ColumnDefinition(type=ColumnType.TEXT, primary_key=True),
                        "total_executions": ColumnDefinition(type=ColumnType.INTEGER, default=0),
                        "last_used": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
                    },
                ),
                "plugin_popularity": TableSchema(
                    table_name="plugin_popularity",
                    columns={
                        "plugin_name": ColumnDefinition(type=ColumnType.TEXT, primary_key=True),
                        "execution_count": ColumnDefinition(type=ColumnType.INTEGER, default=0),
                        "last_updated": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
                    },
                ),
            },
        )

        self.schema_manager.initialize_schemas([discovery_schema])

    async def save_registry_to_state(self, state: dict[str, object]) -> None:
        """Save the process registry to state. Fails if registry not initialized."""
        if not hasattr(self, "_process_registry") or not self._process_registry:
            raise RuntimeError("Process registry not initialized. Cannot save to state.")
        state["process_registry"] = self._process_registry
        await self.state_manager.save(state)

    def apply_knowledge_base_updates(
        self, updates: dict[str, dict[str, object]],
    ) -> dict[str, object]:
        """Apply knowledge base text field updates to live registry entries.

        Args:
            updates: Mapping of process_key -> dict of fields to merge.

        Returns:
            Summary dict with keys: updated_count, process_keys, errors.
        """
        if self._process_registry is None:
            raise FrameworkError(
                message="Cannot apply updates: process registry not initialized",
                error_code="process_registry.not_initialized",
                details={},
                severity=ErrorSeverity.ERROR,
            )

        registry_processes = self._process_registry.get("processes")
        if not isinstance(registry_processes, dict):
            raise FrameworkError(
                message="Cannot apply updates: registry has no 'processes' dict",
                error_code="process_registry.invalid_structure",
                details={},
                severity=ErrorSeverity.ERROR,
            )

        updated_keys: list[str] = []
        errors: list[str] = []

        for process_key, fields in updates.items():
            if process_key not in registry_processes:
                errors.append(f"Process key not found in registry: {process_key}")
                continue

            entry = registry_processes[process_key]
            if not isinstance(entry, dict):
                errors.append(f"Registry entry is not a dict: {process_key}")
                continue

            # Merge fields into live registry entry
            for field_name, field_value in fields.items():
                entry[field_name] = field_value

            # Persist updated entry
            if self._persist_single_process(process_key, entry):
                updated_keys.append(process_key)
            else:
                errors.append(f"Failed to persist: {process_key}")

        # Full discovery rebuild after all updates
        if updated_keys:
            self.full_discovery_rebuild()

        return {
            "updated_count": len(updated_keys),
            "process_keys": updated_keys,
            "errors": errors,
        }

    def full_discovery_rebuild(self) -> None:
        """Clear all discovery vectors and in-memory process map, re-store from registry."""
        if not self.discovery_service:
            logger.error("Cannot rebuild discovery: no discovery service available")
            return
        self.discovery_service.clear_process_vectors()
        self.discovery_service.rebuild_index(process_registry=self._process_registry)

    def get_registry_data(self) -> dict[str, object] | None:
        return self._process_registry
