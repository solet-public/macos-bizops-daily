"""Service-interface process scanning + registration.

Extracted from `ProcessRegistryBuilder` during the Step 9.A decomposition
(`workbench/2026-05-25_plugin_god_class_remediation.md` §9.1).

Responsibility: recursively scan for `*/interfaces/public.py` modules in
both framework services and plugin directories, discover methods
decorated with `@service_interface_process`, and register them as
`service_interface::<provider>::<function>` entries on the registry.

Depends on `InvocationSchemaGenerator` for per-entry JSON Schema and the
service-interface metadata extractors.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ananta.core.process_registry.constants import SYSTEM_PROMPT_PROCESS_KEYS
from ananta.core.process_registry.invocation_schema_generator import (
    InvocationSchemaGenerator,
)
from ananta.core.services.service_interface_decorator import (
    ServiceInterfaceActionMetadata,
    is_service_interface_process_enabled,
)

logger = logging.getLogger(__name__)


class ServiceInterfaceScanner:
    """Scan service-interface modules and register their decorated processes."""

    def __init__(self, schema_generator: InvocationSchemaGenerator) -> None:
        self._schema_generator = schema_generator

    def scan(self, registry: dict[str, object]) -> int:
        """Scan and register service interface processes from decorated methods.

        Recursively scans for */interfaces/public.py modules in both framework services
        and plugin directories. Discovers methods decorated with @service_interface_process.
        The decorator provides complete metadata for automatic registry registration.

        Returns:
            Number of registered service interface processes
        """
        count = 0

        # Type narrow processes to dict
        processes = registry["processes"]
        if not isinstance(processes, dict):
            raise TypeError("Registry processes must be a dict")

        # Define search paths
        framework_services_dir = Path(__file__).parent.parent.parent / "services"
        plugins_base_dir = (
            Path(__file__).parent.parent.parent.parent.parent.parent / "ananta_plugins"
        )

        logger.debug(
            f"Scanning for service interface processes in framework services: {framework_services_dir}"
        )
        logger.debug(f"Scanning for service interface processes in plugins: {plugins_base_dir}")

        # Scan framework services
        if framework_services_dir.exists():
            count += self._scan_service_interfaces_in_directory(
                framework_services_dir, "ananta.services", processes
            )

        # Scan plugin directories
        if plugins_base_dir.exists():
            for plugin_dir in plugins_base_dir.iterdir():
                if not plugin_dir.is_dir() or plugin_dir.name.startswith("."):
                    continue

                # Look for src/{plugin_name}/interfaces/public.py
                plugin_src_dir = plugin_dir / "src" / plugin_dir.name
                if plugin_src_dir.exists():
                    count += self._scan_service_interfaces_in_directory(
                        plugin_src_dir, f"{plugin_dir.name}", processes
                    )

        logger.debug(f"Total service interface processes registered: {count}")
        return count

    def _scan_service_interfaces_in_directory(
        self, search_dir: Path, module_prefix: str, processes: dict[str, object]
    ) -> int:
        """Recursively scan directory for */interfaces/public.py modules.

        Args:
            search_dir: Directory to search for service interfaces
            module_prefix: Python module path prefix
            processes: Process registry dict to populate

        Returns:
            Number of processes registered from this directory
        """
        count = 0

        for interfaces_public in search_dir.rglob("interfaces/public.py"):
            full_module_name = self._build_module_name(search_dir, interfaces_public, module_prefix)
            count += self._scan_single_module(full_module_name, processes)

        return count

    def _build_module_name(
        self, search_dir: Path, interfaces_public: Path, module_prefix: str
    ) -> str:
        """Build full module name from interface path.

        Args:
            search_dir: Base search directory
            interfaces_public: Path to interfaces/public.py file
            module_prefix: Python module path prefix

        Returns:
            Full module name string
        """
        relative_path = interfaces_public.relative_to(search_dir)
        module_parts = list(relative_path.parts[:-1])  # Remove 'public.py'
        module_parts.append("public")

        if module_prefix == "ananta.services":
            return f"{module_prefix}.{'.'.join(module_parts)}"
        return f"{module_prefix}.interfaces.public"

    def _scan_single_module(self, full_module_name: str, processes: dict[str, object]) -> int:
        """Scan a single module for service interface processes.

        Args:
            full_module_name: Full Python module name to import
            processes: Process registry dict to populate

        Returns:
            Number of processes registered from this module
        """
        try:
            module = importlib.import_module(full_module_name)
            return self._scan_module_classes(module, full_module_name, processes)
        except Exception as e:
            logger.error(f"Error scanning module {full_module_name}: {e}", exc_info=True)
            return 0

    def _scan_module_classes(
        self, module: object, full_module_name: str, processes: dict[str, object]
    ) -> int:
        """Scan all classes in a module for service interface methods.

        Args:
            module: Imported Python module
            full_module_name: Full module name for filtering
            processes: Process registry dict to populate

        Returns:
            Number of processes registered from this module
        """
        count = 0

        for _class_name, class_obj in inspect.getmembers(module, inspect.isclass):
            if class_obj.__module__ != full_module_name:
                continue

            count += self._scan_class_methods(class_obj, processes)

        return count

    def _scan_class_methods(self, class_obj: type, processes: dict[str, object]) -> int:
        """Scan all methods in a class for service interface decorators.

        Args:
            class_obj: Class to scan
            processes: Process registry dict to populate

        Returns:
            Number of processes registered from this class
        """
        count = 0

        for _method_name, method_obj in inspect.getmembers(class_obj, inspect.isfunction):
            if not hasattr(method_obj, "_service_interface_metadata"):
                continue

            # Cast required: method_obj has dynamic decorator attribute
            method_any: Any = method_obj
            metadata: ServiceInterfaceActionMetadata = method_any._service_interface_metadata

            # Skip disabled service interface processes (e.g., io_interface_service)
            if not is_service_interface_process_enabled(metadata):
                logger.debug(
                    f"Skipping disabled service interface process: "
                    f"{metadata.provider}::{metadata.function_name}"
                )
                continue

            process_key = f"service_interface::{metadata.provider}::{metadata.function_name}"
            process_entry = self._build_service_interface_process_entry(process_key, metadata)

            processes[process_key] = process_entry
            count += 1
            logger.debug(f"Registered service interface process: {process_key}")

        return count

    def _build_service_interface_process_entry(
        self, process_key: str, metadata: ServiceInterfaceActionMetadata
    ) -> dict[str, object]:
        """Build a complete process registry entry from service interface metadata.

        Args:
            process_key: The process key for registration
            metadata: ServiceInterfaceActionMetadata from decorator

        Returns:
            Complete process entry dict for registry
        """
        parameters_dict = self._extract_parameters_dict(metadata)
        process_entry = self._build_base_process_entry(process_key, metadata, parameters_dict)
        self._add_optional_process_fields(process_entry, metadata)
        self._add_generated_metadata(process_entry, process_key, metadata, parameters_dict)
        return process_entry

    def _extract_parameters_dict(
        self, metadata: ServiceInterfaceActionMetadata
    ) -> dict[str, object]:
        """Extract parameters dictionary from metadata.

        Args:
            metadata: ServiceInterfaceActionMetadata instance

        Returns:
            Dictionary of parameter name to parameter dict
        """
        parameters_dict: dict[str, object] = {}
        for param_name, param_meta in metadata.parameters.items():
            parameters_dict[param_name] = param_meta.to_dict()
        return parameters_dict

    def _build_base_process_entry(
        self,
        process_key: str,
        metadata: ServiceInterfaceActionMetadata,
        parameters_dict: dict[str, object],
    ) -> dict[str, object]:
        """Build the base process entry with required fields.

        Args:
            process_key: The process key for registration
            metadata: ServiceInterfaceActionMetadata from decorator
            parameters_dict: Pre-extracted parameters dictionary

        Returns:
            Base process entry dict
        """
        return {
            "name": metadata.name,
            "display_name": metadata.display_name,
            "description": metadata.description,
            "embedding_description": metadata.embedding_description,
            "is_discoverable": metadata.is_discoverable,
            "provider_type": "service_interface",
            "provider": metadata.provider,
            "function_name": metadata.function_name,
            "process_key": process_key,
            "is_async": False,
            "estimated_duration": "< 1s",
            "version": metadata.version,
            "parameters": parameters_dict,
            "parameter_schema": parameters_dict,
            "output": metadata.return_value_schema.to_dict(),
            "is_inference_capable": metadata.is_inference_capable,
            "is_enabled": metadata.is_enabled,
            "is_long_running": metadata.is_long_running,
            "work_count_impact": metadata.work_count_impact,
            "include_in_system_prompt": process_key in SYSTEM_PROMPT_PROCESS_KEYS,
        }

    # Field-name -> (metadata-attribute-name, transform-callable) dispatch
    # table for ``_add_optional_process_fields``. Each entry says: "if
    # metadata.<attr> is set, write process_entry[<field-name>] =
    # transform(metadata.<attr>)". The transforms keep the per-field
    # logic isolated; the dispatcher loop stays linear (CC A) regardless
    # of how many optional fields exist. Adding a new optional field is
    # a one-line addition to this table.
    _OPTIONAL_FIELD_DISPATCH: tuple[
        tuple[str, str, Callable[[Any], object]], ...
    ] = (
        ("processor_policy_category", "processor_policy_category",
         lambda v: v.value),
        ("chaining_guidance", "chaining_guidance", list),
        ("action_definition_template", "action_definition_template",
         lambda v: v),
        ("result_processor_customizations",
         "result_processor_customizations", lambda v: v.to_dict()),
        ("error_processor_customizations",
         "error_processor_customizations", lambda v: v.to_dict()),
        ("requires_result_processor", "requires_result_processor",
         lambda v: v),
        ("requires_call_context", "requires_call_context",
         lambda _v: True),
    )

    def _add_optional_process_fields(
        self, process_entry: dict[str, object], metadata: ServiceInterfaceActionMetadata,
    ) -> None:
        """Add optional fields to ``process_entry`` based on ``metadata``.

        Dispatches through :attr:`_OPTIONAL_FIELD_DISPATCH` — one entry
        per optional field. Each entry's predicate is "metadata.<attr> is
        truthy AND, if dynamic, hasattr(metadata, <attr>)". The transform
        callable converts the raw metadata value into the registry-shape
        value. Adding a new optional field is a one-line table edit; the
        dispatcher itself has cyclomatic complexity A regardless of field
        count.
        """
        for field_name, attr_name, transform in self._OPTIONAL_FIELD_DISPATCH:
            raw = getattr(metadata, attr_name, None)
            if not raw:
                continue
            process_entry[field_name] = transform(raw)

    def _add_generated_metadata(
        self,
        process_entry: dict[str, object],
        process_key: str,
        metadata: ServiceInterfaceActionMetadata,
        parameters_dict: dict[str, object],
    ) -> None:
        """Add generated metadata structures to process entry.

        Args:
            process_entry: Process entry dict to modify in place
            process_key: The process key for registration
            metadata: ServiceInterfaceActionMetadata from decorator
            parameters_dict: Pre-extracted parameters dictionary
        """
        process_entry["input_contract"] = (
            self._schema_generator.generate_input_contract_from_metadata(metadata)
        )
        process_entry["action_blueprint"] = (
            self._schema_generator.generate_action_blueprint_from_metadata(process_key, metadata)
        )
        process_entry["planning_docs"] = self._schema_generator.extract_planning_docs_from_metadata(
            process_key, metadata
        )
        process_entry["error_handling_docs"] = (
            self._schema_generator.extract_error_handling_docs_from_metadata(process_key, metadata)
        )
        process_entry["response_handling_docs"] = (
            self._schema_generator.extract_response_handling_docs_from_metadata(
                process_key, metadata
            )
        )
        process_entry["invocation_schema"] = self._schema_generator.generate(
            process_key=process_key,
            parameters_dict=parameters_dict,
        )
