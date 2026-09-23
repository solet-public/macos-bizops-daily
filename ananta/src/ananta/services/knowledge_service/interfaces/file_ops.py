"""Knowledge-base file CRUD service-interface verbs (W5.Q decomposition).

Browse, read, edit, create, and delete files within a knowledge base
directory (living KBs). For git-backed KBs the edit/create/delete
verbs commit to the managed branch. Lifted byte-for-byte from the
W5.Q-pre-decomposition ``KnowledgeServiceAPI``.
"""

from abc import ABC, abstractmethod
from typing import Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.services.service_interface_decorator import service_interface_process


class KnowledgeFileOpsAPI(ABC):
    """Knowledge-base file CRUD verbs — browse / read_file / edit_file / create_file / delete_file."""

    @service_interface_process(
        name="browse",
        is_discoverable=True,
        provider="knowledge_service",
        parameters={
            "name": ParameterMetadata(
                description="Knowledge base name",
                required=True,
                type=ParameterType.STRING,
            ),
            "path": ParameterMetadata(
                description="Relative path within the KB (empty string for root)",
                required=False,
                type=ParameterType.STRING,
                default="",
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Directory listing", type=ParameterType.OBJECT
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        )
    )
    @abstractmethod
    def browse(self, name: str, path: str = "") -> dict[str, Any]: ...

    @service_interface_process(
        name="read_file",
        is_discoverable=True,
        provider="knowledge_service",
        parameters={
            "name": ParameterMetadata(
                description="Knowledge base name",
                required=True,
                type=ParameterType.STRING,
            ),
            "path": ParameterMetadata(
                description="Relative file path within the knowledge base",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="File contents", type=ParameterType.OBJECT
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        )
    )
    @abstractmethod
    def read_file(self, name: str, path: str) -> dict[str, Any]: ...

    @service_interface_process(
        name="edit_file",
        is_discoverable=True,
        provider="knowledge_service",
        parameters={
            "name": ParameterMetadata(
                description="Knowledge base name",
                required=True,
                type=ParameterType.STRING,
            ),
            "path": ParameterMetadata(
                description="Relative file path within the knowledge base",
                required=True,
                type=ParameterType.STRING,
            ),
            "content": ParameterMetadata(
                description="New file content",
                required=True,
                type=ParameterType.STRING,
            ),
            "expected_content_hash": ParameterMetadata(
                description=(
                    "Optional optimistic-concurrency precondition: the "
                    "content_sha256 that read_file returned for the version you "
                    "are editing. When supplied it must match the file's current "
                    "hash or the edit fails loud (re-read and reapply). Required "
                    "by knowledge bases whose write posture forbids blind overwrites."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Edit result", type=ParameterType.OBJECT
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        )
    )
    @abstractmethod
    def edit_file(
        self, name: str, path: str, content: str,
        expected_content_hash: str | None = None,
    ) -> dict[str, Any]: ...

    @service_interface_process(
        name="create_file",
        is_discoverable=True,
        provider="knowledge_service",
        parameters={
            "name": ParameterMetadata(
                description="Knowledge base name",
                required=True,
                type=ParameterType.STRING,
            ),
            "path": ParameterMetadata(
                description="Relative file path to create",
                required=True,
                type=ParameterType.STRING,
            ),
            "content": ParameterMetadata(
                description="File content",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Create result", type=ParameterType.OBJECT
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        )
    )
    @abstractmethod
    def create_file(self, name: str, path: str, content: str) -> dict[str, Any]: ...

    @service_interface_process(
        name="delete_file",
        is_discoverable=True,
        provider="knowledge_service",
        parameters={
            "name": ParameterMetadata(
                description="Knowledge base name",
                required=True,
                type=ParameterType.STRING,
            ),
            "path": ParameterMetadata(
                description="Relative file path to delete",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Delete result", type=ParameterType.OBJECT
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        )
    )
    @abstractmethod
    def delete_file(self, name: str, path: str) -> dict[str, Any]: ...

    @service_interface_process(
        name="archive_file",
        is_discoverable=True,
        provider="knowledge_service",
        parameters={
            "name": ParameterMetadata(
                description="Knowledge base name",
                required=True,
                type=ParameterType.STRING,
            ),
            "path": ParameterMetadata(
                description="Relative file path of the document to archive",
                required=True,
                type=ParameterType.STRING,
            ),
            "superseded_by": ParameterMetadata(
                description=(
                    "Optional relative path (or identifier) of the successor "
                    "document; when given, the archived doc's §4 block gains "
                    "Superseded_by and Status: superseded."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Archive result", type=ParameterType.OBJECT
        ),
        result_processor_customizations=MergeResultProcessorCustomizations(
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        )
    )
    @abstractmethod
    def archive_file(
        self, name: str, path: str, superseded_by: str | None = None,
    ) -> dict[str, Any]: ...
