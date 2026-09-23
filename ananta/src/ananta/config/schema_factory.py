from collections.abc import Callable

from ananta.config.core_schemas import CoreSchemaDefinitions
from ananta.types.schema_standardizer import SchemaStandardizer
from ananta.types.schema_types import SchemaDefinition


class SchemaFactory:
    def __init__(self) -> None:
        self._standardizer: SchemaStandardizer = SchemaStandardizer()

    def get_job_schema(self) -> SchemaDefinition:
        raw_schema = CoreSchemaDefinitions.get_job_schema()
        return self._standardizer.standardize_schema(raw_schema)

    def get_job_payload_schema(self) -> SchemaDefinition:
        raw_schema = CoreSchemaDefinitions.get_job_payload_schema()
        return self._standardizer.standardize_schema(raw_schema)

    def get_process_registry_schema(self) -> SchemaDefinition:
        raw_schema = CoreSchemaDefinitions.get_process_registry_schema()
        return self._standardizer.standardize_schema(raw_schema)

    def get_key_value_store_schema(self) -> SchemaDefinition:
        raw_schema = CoreSchemaDefinitions.get_key_value_store_schema()
        return self._standardizer.standardize_schema(raw_schema)

    def get_logs_schema(self) -> SchemaDefinition:
        raw_schema = CoreSchemaDefinitions.get_logs_schema()
        return self._standardizer.standardize_schema(raw_schema)

    def get_sessions_schema(self) -> SchemaDefinition:
        raw_schema = CoreSchemaDefinitions.get_sessions_schema()
        return self._standardizer.standardize_schema(raw_schema)

    def get_flows_schema(self) -> SchemaDefinition:
        raw_schema = CoreSchemaDefinitions.get_flows_schema()
        return self._standardizer.standardize_schema(raw_schema)

    def get_action_events_schema(self) -> SchemaDefinition:
        raw_schema = CoreSchemaDefinitions.get_action_events_schema()
        return self._standardizer.standardize_schema(raw_schema)

    def get_orchestrator_state_schema(self) -> SchemaDefinition:
        raw_schema = CoreSchemaDefinitions.get_orchestrator_state_schema()
        return self._standardizer.standardize_schema(raw_schema)

    def get_workflow_patterns_schema(self) -> SchemaDefinition:
        raw_schema = CoreSchemaDefinitions.get_workflow_patterns_schema()
        return self._standardizer.standardize_schema(raw_schema)

    def get_process_chains_schema(self) -> SchemaDefinition:
        raw_schema = CoreSchemaDefinitions.get_process_chains_schema()
        return self._standardizer.standardize_schema(raw_schema)

    def get_event_bus_events_schema(self) -> SchemaDefinition:
        raw_schema = CoreSchemaDefinitions.get_event_bus_events_schema()
        return self._standardizer.standardize_schema(raw_schema)

    def get_usage_stats_schema(self) -> SchemaDefinition:
        raw_schema = CoreSchemaDefinitions.get_usage_stats_schema()
        return self._standardizer.standardize_schema(raw_schema)

    def get_action_results_schema(self) -> SchemaDefinition:
        raw_schema = CoreSchemaDefinitions.get_action_results_schema()
        return self._standardizer.standardize_schema(raw_schema)

    def get_all_core_schemas(self) -> list[SchemaDefinition]:
        # Delegate to CoreSchemaDefinitions to ensure all schemas are included
        return [
            self._standardizer.standardize_schema(schema)
            for schema in CoreSchemaDefinitions.get_all_core_schemas()
        ]


# Singleton instance for global use
_schema_factory = SchemaFactory()


def get_standardized_core_schemas() -> list[SchemaDefinition]:
    return _schema_factory.get_all_core_schemas()


def get_standardized_schema(schema_name: str) -> SchemaDefinition:
    method_name = f"get_{schema_name}_schema"
    if hasattr(_schema_factory, method_name):
        method: Callable[[], SchemaDefinition] = getattr(_schema_factory, method_name)
        return method()
    else:
        raise ValueError(f"Unknown schema: {schema_name}")
