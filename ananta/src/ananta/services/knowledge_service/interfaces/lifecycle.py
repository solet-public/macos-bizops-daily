"""Knowledge-base lifecycle service-interface verbs (W5.Q decomposition).

Install, uninstall, update, list, activate, and deactivate the KB install
records that drive the knowledge_service's directory-backed reference
library. Lifted byte-for-byte from the W5.Q-pre-decomposition
``KnowledgeServiceAPI`` god class (996 LOC, 19 verbs); see the W5.Q design
memo at ``workbench/2026-06-13_w5q_knowledge_service_api_decomposition_design.md``
for the split rationale.
"""

from abc import ABC, abstractmethod
from typing import Any

from ananta.core.actions.action_metadata import (
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.services.service_interface_decorator import service_interface_process


class KnowledgeLifecycleAPI(ABC):
    """Knowledge-base lifecycle verbs — install / uninstall / update / list / activate / deactivate."""

    @service_interface_process(
        name="install",
        is_discoverable=True,
        provider="knowledge_service",
        parameters={
            "name": ParameterMetadata(
                description="Knowledge base directory name",
                required=True,
                type=ParameterType.STRING,
            ),
            "source": ParameterMetadata(
                description="Optional git URL to clone from (no credentials needed for public repos)",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Install result with chunk count and metadata",
            type=ParameterType.OBJECT,
            properties={
                "name": ParameterMetadata(type=ParameterType.STRING, description="KB name"),
                "chunk_count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of indexed chunks"
                ),
                "source_type": ParameterMetadata(
                    type=ParameterType.STRING, description="local, symlink, or git"
                ),
            },
        ),
    )
    @abstractmethod
    def install(self, name: str, source: str | None = None) -> dict[str, Any]: ...

    @service_interface_process(
        name="ingest",
        is_discoverable=True,
        provider="knowledge_service",
        parameters={
            "name": ParameterMetadata(
                description=(
                    "Knowledge base directory name, or the literal 'all' to ingest "
                    "every knowledge base under the knowledge_base root"
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Idempotent ingest result: which KBs were reindexed, skipped, or failed",
            type=ParameterType.OBJECT,
            properties={
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="'success', or 'partial' when one or more KBs failed in 'all' mode",
                ),
                "mode": ParameterMetadata(
                    type=ParameterType.STRING, description="'single' or 'all'"
                ),
                "ingested": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="KB names that were reindexed (were stale or not yet installed)",
                ),
                "unchanged": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="KB names skipped because their indexed content is current",
                ),
                "failed": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Per-KB failures as {name, error} ('all' mode only; single mode raises)",
                ),
                "total_chunks": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Total chunks indexed across the reingested KBs",
                ),
            },
        ),
    )
    @abstractmethod
    def ingest(self, name: str) -> dict[str, Any]: ...

    @service_interface_process(
        name="uninstall",
        is_discoverable=True,
        provider="knowledge_service",
        parameters={
            "name": ParameterMetadata(
                description="Knowledge base name to uninstall",
                required=True,
                type=ParameterType.STRING,
            ),
            "remove_files": ParameterMetadata(
                description="Also delete git-cloned directory (never local/symlinked)",
                required=False,
                type=ParameterType.BOOLEAN,
                default=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Uninstall result",
            type=ParameterType.OBJECT,
            properties={
                "name": ParameterMetadata(type=ParameterType.STRING, description="KB name"),
                "chunks_archived": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of chunks archived"
                ),
            },
        ),
    )
    @abstractmethod
    def uninstall(self, name: str, remove_files: bool = False) -> dict[str, Any]: ...

    @service_interface_process(
        name="update",
        is_discoverable=True,
        provider="knowledge_service",
        parameters={
            "name": ParameterMetadata(
                description="Knowledge base name to update",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Update result with changed file count",
            type=ParameterType.OBJECT,
            properties={
                "name": ParameterMetadata(type=ParameterType.STRING, description="KB name"),
                "files_changed": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of files reindexed"
                ),
            },
        ),
    )
    @abstractmethod
    def update(self, name: str) -> dict[str, Any]: ...

    @service_interface_process(
        name="list_installed",
        is_discoverable=True,
        provider="knowledge_service",
        parameters={
            "active_only": ParameterMetadata(
                description="If true, only return active (non-deactivated) KBs",
                required=False,
                type=ParameterType.BOOLEAN,
                default=False,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="List of installed knowledge bases",
            type=ParameterType.OBJECT,
            properties={
                "installs": ParameterMetadata(
                    type=ParameterType.LIST, description="List of KB install records"
                ),
                "count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Total count"
                ),
            },
        ),
    )
    @abstractmethod
    def list_installed(self, active_only: bool = False) -> dict[str, Any]: ...

    @service_interface_process(
        name="activate",
        is_discoverable=True,
        provider="knowledge_service",
        parameters={
            "name": ParameterMetadata(
                description="Knowledge base name to activate",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Activation result", type=ParameterType.OBJECT
        ),
    )
    @abstractmethod
    def activate(self, name: str) -> dict[str, Any]: ...

    @service_interface_process(
        name="deactivate",
        is_discoverable=True,
        provider="knowledge_service",
        parameters={
            "name": ParameterMetadata(
                description="Knowledge base name to deactivate",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Deactivation result", type=ParameterType.OBJECT
        ),
    )
    @abstractmethod
    def deactivate(self, name: str) -> dict[str, Any]: ...
