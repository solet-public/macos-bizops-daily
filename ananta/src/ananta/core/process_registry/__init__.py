"""
Process Registry Package

Manages the registration, introspection, and lifecycle of process definitions
that can be executed through the action system.

This package provides:
- Process registration and discovery
- Process schema validation
- Process introspection and metadata
- Registry building and management
- Process key resolution

Modules:
- manager: High-level registry management
- builder: Registry construction from plugins
- introspector: Process metadata and capability querying
- schema: Process definition schemas
- util: Registry utilities
- key_resolver: Process key resolution logic
- validation_manager: Process validation management
- registry_definition_manager: Registry definition management
- registry_manager: Core registry operations
"""

__all__ = [
    "manager",
    "builder",
    "introspector",
    "schema",
    "util",
    "key_resolver",
    "validation_manager",
    "registry_definition_manager",
    "registry_manager",
    # Builder collaborators (Step 9.A decomposition)
    "invocation_schema_generator",
    "kb_overlay_loader",
    "plugin_process_scanner",
    "plugin_registration_validator",
    "service_interface_metadata_generator",
    "service_interface_scanner",
]
