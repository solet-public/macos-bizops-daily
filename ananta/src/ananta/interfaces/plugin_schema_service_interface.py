"""Plugin Schema Service Interface — install/update/uninstall/purge a plugin's schema.

Plugins declare their persistent shape as ``SchemaDefinition`` via
``SchemaProvider``. The platform reconciles the declared shape against the
live database and applies the diff. There are no Alembic migrations — the
unit of change is always a plugin snapshot.

Install and uninstall are reversible without data loss. Uninstall is logical
(marks ownership inactive, leaves tables intact). Only ``purge`` ever drops
a table.

Plugins implementing this interface should:
1. Define ``service_interfaces`` returning a tuple containing
   ``PluginSchemaServiceInterface``.
2. Define ``supported_interface_versions`` mapping the interface to its version.
3. Provide a transactional apply path for the DDL ops they emit.
4. Maintain an ownership table that records what the platform installed.

See: ``knowledge_bases/ananta_platform/15_metadata_driven_ddl/``.
"""

from abc import ABC, abstractmethod
from typing import Any, ClassVar


class PluginSchemaServiceInterface(ABC):
    """Plugin schema lifecycle: install / update / uninstall / purge."""

    INTERFACE_VERSION: ClassVar[str] = "1.0.0"

    @abstractmethod
    def install_plugin_schema(
        self, plugin_namespace: str, declared_schema_json: dict[str, Any]
    ) -> dict[str, Any]:
        """Idempotent install for a namespace.

        - Unknown namespace, no live tables: create everything, record ownership.
        - Unknown namespace, live tables that match declared shape: adopt.
        - Unknown namespace, live tables that diverge: fail-fast with recovery options.
        - Known namespace, identical shape recorded: no-op (bump ``updated_at``).
        - Known namespace, different shape recorded: delegate to update.
        - Previously uninstalled (status=``inactive``): reactivate, then update if differs.
        """
        ...

    @abstractmethod
    def update_plugin_schema(
        self, plugin_namespace: str, declared_schema_json: dict[str, Any]
    ) -> dict[str, Any]:
        """Diff declared-new against ownership-recorded-current and apply.

        Refuses to drop tables — operator must ``uninstall_plugin_schema`` (preserves
        data) or ``purge_plugin_schema`` (destructive) for table removal. Other
        non-additive changes (column type changes, check-constraint mutations,
        ``with_history`` toggles, ``id_prefix`` changes) raise ``NotImplementedError``.
        """
        ...

    @abstractmethod
    def uninstall_plugin_schema(self, plugin_namespace: str) -> dict[str, Any]:
        """Logical uninstall — marks ownership inactive; tables and data remain.

        Reversible via ``install_plugin_schema``. To actually drop the data,
        operator follows up with ``purge_plugin_schema``.
        """
        ...

    @abstractmethod
    def purge_plugin_schema(
        self, plugin_namespace: str, force: bool = False
    ) -> dict[str, Any]:
        """Destructive purge — drops platform-owned tables and ownership rows.

        Refuses against an ``active`` namespace unless ``force=True``. Refuses
        to touch any table not in the ownership table for this namespace.
        """
        ...

    @abstractmethod
    def get_installed_schema(self, plugin_namespace: str) -> dict[str, Any]:
        """Read-only introspection — returns the recorded schema snapshot and per-table status."""
        ...
