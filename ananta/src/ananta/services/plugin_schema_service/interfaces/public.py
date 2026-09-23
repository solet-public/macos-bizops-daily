"""Plugin Schema Service Public API.

AI-discoverable plugin-schema lifecycle operations with @service_interface_process
decorators. All methods in this interface are indexed for process discovery
and callable through the channel as ``service_interface::plugin_schema_service::*``.

The verbs reconcile a plugin's declared persistent shape (a ``SchemaDefinition``,
serialized via ``ananta.services.plugin_schema_service.serialization.to_json``)
against the live PostgreSQL state. Install / update are additive at the table
level; only ``uninstall`` (logical, data-preserving) and ``purge`` (destructive)
ever remove tables.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ananta.core.actions.action_metadata import (
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.services.service_interface_decorator import service_interface_process

PROVIDER = "plugin_schema_service"

_DECLARED_SCHEMA_PARAM = ParameterMetadata(
    description=(
        "Canonical JSON shape of a SchemaDefinition (mirror of the dataclass "
        "fields; ColumnType serialized by .name). Use "
        "ananta.services.plugin_schema_service.serialization.to_json to produce "
        "the dict from a SchemaDefinition."
    ),
    required=True,
    type=ParameterType.OBJECT,
)
_NAMESPACE_PARAM = ParameterMetadata(
    description=(
        "Plugin namespace (e.g. 'audio_processing_plugin', 'core'). Tables "
        "for this namespace are stored as <namespace>__<table>; ownership "
        "rows in platform__plugin_schema_ownership are keyed by it."
    ),
    required=True,
    type=ParameterType.STRING,
)


def _result_schema(extra: dict[str, ParameterMetadata] | None = None) -> ReturnValueSchema:
    """Common return shape: {status, namespace, tables, ...verb-specific extras}."""
    properties: dict[str, ParameterMetadata] = {
        "status": ParameterMetadata(
            type=ParameterType.STRING,
            description="Outcome — e.g. 'installed', 'no_op', 'updated', 'reactivated', 'uninstalled', 'purged', 'adopted'.",
        ),
        "namespace": ParameterMetadata(
            type=ParameterType.STRING, description="Plugin namespace operated on."
        ),
        "tables": ParameterMetadata(
            type=ParameterType.LIST,
            description="Table names in this namespace (declared or owned, depending on verb).",
        ),
    }
    if extra:
        properties.update(extra)
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="Lifecycle outcome describing what changed.",
        properties=properties,
    )


class PluginSchemaServicePublicAPI(ABC):
    """AI-discoverable plugin-schema lifecycle operations.

    Access via: ``service_interface::plugin_schema_service::{verb}``
    """

    @service_interface_process(
        name="install_plugin_schema",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={
            "plugin_namespace": _NAMESPACE_PARAM,
            "declared_schema_json": _DECLARED_SCHEMA_PARAM,
        },
        return_value_schema=_result_schema(),
    )
    @abstractmethod
    def install_plugin_schema(
        self, plugin_namespace: str, declared_schema_json: dict[str, Any]
    ) -> dict[str, Any]:
        """Idempotent install / adoption / additive update for a plugin's declared schema.

        Tables not on disk → CREATE. Tables on disk that match declared shape
        → adopt (legacy-type normalization + index reconciliation). Identical
        re-install → no-op. Previously-uninstalled namespace → reactivate. Tables
        in the declaration that already exist with a different shape → diff-and-update
        (additive: add columns, swap indexes; never drops tables — for that, use
        ``uninstall`` (logical) or ``purge`` (destructive)).
        """
        pass

    @service_interface_process(
        name="update_plugin_schema",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={
            "plugin_namespace": _NAMESPACE_PARAM,
            "declared_schema_json": _DECLARED_SCHEMA_PARAM,
        },
        return_value_schema=_result_schema(),
    )
    @abstractmethod
    def update_plugin_schema(
        self, plugin_namespace: str, declared_schema_json: dict[str, Any]
    ) -> dict[str, Any]:
        """Diff declared shape against ownership snapshot and apply DDL in one transaction.

        Refuses table removal — operator must ``uninstall_plugin_schema`` (logical,
        preserves data) or ``purge_plugin_schema`` (destructive). Refuses column
        type changes, ``with_history`` toggles, ``id_prefix`` changes (out of v1 scope).
        """
        pass

    @service_interface_process(
        name="uninstall_plugin_schema",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={"plugin_namespace": _NAMESPACE_PARAM},
        return_value_schema=_result_schema(
            extra={
                "data_preserved": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="Always true — uninstall is logical; tables/data remain intact.",
                ),
            }
        ),
    )
    @abstractmethod
    def uninstall_plugin_schema(self, plugin_namespace: str) -> dict[str, Any]:
        """Logical uninstall — marks ownership inactive; tables and data remain intact.

        Reversible via ``install_plugin_schema`` (which reactivates the namespace and
        applies any new diff). To actually drop the data, follow with ``purge_plugin_schema``.
        """
        pass

    @service_interface_process(
        name="purge_plugin_schema",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={
            "plugin_namespace": _NAMESPACE_PARAM,
            "force": ParameterMetadata(
                description="Required to purge an active namespace. Without it, "
                "the verb refuses against active. Always destructive.",
                required=False,
                type=ParameterType.BOOLEAN,
                default=False,
            ),
        },
        return_value_schema=_result_schema(
            extra={
                "force": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="Whether the operator passed force=True.",
                ),
            }
        ),
    )
    @abstractmethod
    def purge_plugin_schema(
        self, plugin_namespace: str, force: bool = False
    ) -> dict[str, Any]:
        """Destructive purge — drops platform-owned tables and ownership rows.

        Refuses against an ``active`` namespace unless ``force=True``. Refuses
        to touch any table not recorded in ownership for this namespace.
        """
        pass

    @service_interface_process(
        name="get_installed_schema",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={"plugin_namespace": _NAMESPACE_PARAM},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Per-table snapshot + status for the namespace.",
            properties={
                "namespace": ParameterMetadata(
                    type=ParameterType.STRING, description="Plugin namespace queried."
                ),
                "tables": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Map of table_name → {status, schema_snapshot, installed_at, updated_at, uninstalled_at}.",
                ),
            },
        ),
    )
    @abstractmethod
    def get_installed_schema(self, plugin_namespace: str) -> dict[str, Any]:
        """Read-only introspection — returns recorded SchemaDefinition snapshot and per-table status."""
        pass
