import logging
from pathlib import Path
from typing import Any, cast

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.config.config_provider import ConfigProvider
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.core.plugins.plugin_base import PluginBase
from ananta.core.plugins.plugin_contracts import ActionResult
from ananta.interfaces.blob_storage_service_interface import BlobStorageServiceInterface
from ananta.interfaces.edge_process_provider import EdgeProcessDefinition, EdgeProcessProvider
from ananta.interfaces.state_aware_plugin import StateAwarePlugin
from ananta.interfaces.state_service_protocol import StateServiceProtocol
from ananta.logging_setup import configure_plugin_logging
from ananta.types.schema_types import SchemaDefinition
from ananta.utils.naming import resolve_file_by_name

from . import file_commands, ingestion
from .config import get_default_config, validate_config
from .constants import MIME_PRIMARY_CLASS_SEPARATOR
from .errors import (
    create_error_response,
    create_error_response_from_exception,
    create_success_response,
)
from .providers.filesystem_provider import FilesystemProvider
from .schema import get_blob_storage_schema


class DefaultBlobStoragePlugin(
    PluginBase, BlobStorageServiceInterface, StateAwarePlugin, EdgeProcessProvider
):
    """Filesystem-based blob storage plugin.

    Implements BlobStorageServiceInterface for standardized blob storage operations.
    Uses StateAwarePlugin for schema management via SchemaManager.
    """

    service_interfaces: tuple[type, ...] = (BlobStorageServiceInterface,)
    supported_interface_versions: dict[type, str] = {
        BlobStorageServiceInterface: BlobStorageServiceInterface.INTERFACE_VERSION
    }

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.config = config or get_default_config()
        self.name = "default_blob_storage_plugin"
        self.logger = logging.getLogger(self.name)
        self.config_provider: ConfigProvider | None = None

        try:
            validate_config(self.config)
        except ValueError as e:
            raise ValueError(f"Plugin configuration error: {str(e)}") from e

        self.provider: BlobStorageServiceInterface | None = None
        self.state_service: StateServiceProtocol | None = None
        self.action_factory: Any | None = None  # Will be injected by ActionProcessor

    def prepare_for_readiness(self) -> None:
        """Initialize plugin. Fail-fast if dependencies unavailable.

        Uses orchestrator.get_service() to request state_service.
        """
        if not self.orchestrator_ref:
            raise RuntimeError(f"{self.name}: orchestrator_ref not injected")

        APP_HOME = getattr(self.orchestrator_ref, "APP_HOME", None)
        if not APP_HOME:
            raise RuntimeError(
                f"{self.name}: Application directory not configured - plugin cannot initialize"
            )

        # Initialize configuration and logging
        self.config_provider = ConfigProvider(self.name, self.config)
        self.logger = configure_plugin_logging(APP_HOME, self.name, self.config_provider)
        self.logger.debug(f"Initializing {self.name}")

        # Create the FilesystemProvider
        self.provider = FilesystemProvider(APP_HOME, self.config, self.name)
        self.logger.debug(f"FilesystemProvider created for {self.name}")

        # Request state_service via new service binding architecture
        state_service = self.orchestrator_ref.get_service("state_service")
        if not state_service:
            raise RuntimeError(
                f"{self.name}: state_service not available - check service_bindings.json"
            )
        # get_service returns object - cast to StateServiceProtocol after validation
        self.set_state_service(cast(StateServiceProtocol, state_service))

        # Schema is created via StateAwarePlugin.get_schema_definitions() by SchemaManager
        self.logger.debug(f"{self.name} ready")

    # -------------------------------------------------------------------------
    # StateAwarePlugin interface
    # -------------------------------------------------------------------------

    def get_schema_definitions(self) -> list[SchemaDefinition]:
        """Return schema definitions for blob storage metadata table."""
        return [get_blob_storage_schema()]

    def get_config_schema(self) -> dict[str, object]:
        """Declare configuration schema for the blob storage plugin.

        Returns JSON Schema for setup flow to generate UI/prompts.
        """
        return {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "title": "Default Blob Storage Plugin",
            "description": "Filesystem-based blob storage with metadata tracking",
            "type": "object",
            "properties": {
                "max_file_size": {
                    "type": "integer",
                    "title": "Maximum File Size",
                    "description": "Maximum file size in bytes (default: 100MB)",
                    "default": 104857600,
                    "minimum": 1024,
                    "maximum": 1073741824,
                    "x-group": "advanced",
                    "x-order": 1,
                },
                "cleanup_on_startup": {
                    "type": "boolean",
                    "title": "Cleanup on Startup",
                    "description": "Remove orphaned blobs without metadata entries on startup",
                    "default": True,
                    "x-group": "advanced",
                    "x-order": 2,
                },
                "enable_compression": {
                    "type": "boolean",
                    "title": "Enable Compression",
                    "description": "Automatically compress stored blobs to save disk space",
                    "default": False,
                    "x-group": "advanced",
                    "x-order": 3,
                },
                "enable_deduplication": {
                    "type": "boolean",
                    "title": "Enable Deduplication",
                    "description": "Detect and deduplicate identical blobs based on content hash",
                    "default": False,
                    "x-group": "advanced",
                    "x-order": 4,
                },
            },
        }

    def set_state_service(self, state_service: StateServiceProtocol) -> None:
        self.state_service = state_service
        # If provider already exists, inject the state service
        set_state_service_fn = getattr(self.provider, "set_state_service", None)
        if self.provider and set_state_service_fn is not None:
            set_state_service_fn(state_service)
        # CRITICAL FIX: Defer schema creation to prevent circular dependency during initialization
        # Schema will be created lazily when first metadata operation is performed
        self.logger.debug(f"State service injected into {self.name}")

    async def initialize(self) -> None:  # type: ignore[override]
        try:
            self.logger.debug(f"FILE_PLUGIN_INIT_START: {self.name} beginning initialization")

            # Get APP_HOME from orchestrator reference (set by framework)
            self.logger.debug(
                f"FILE_PLUGIN_ORCHESTRATOR_CHECK: {self.name} checking orchestrator reference"
            )
            if not self.orchestrator_ref:
                raise RuntimeError(
                    "Orchestrator reference not available - framework initialization failed"
                )

            self.logger.debug(
                f"FILE_PLUGIN_ORCHESTRATOR_OK: {self.name} orchestrator reference available"
            )
            app_home = getattr(self.orchestrator_ref, "APP_HOME", None)
            if not app_home:
                self.logger.critical(
                    "FATAL: Application directory not configured. "
                    "Blob storage plugin cannot proceed."
                )
                self.logger.critical("This indicates a critical system initialization failure.")
                raise SystemExit(
                    "FATAL: Application directory not configured - "
                    "system terminating to prevent data corruption"
                )
            self.logger.debug(f"FILE_PLUGIN_APP_HOME: {self.name} using APP_HOME: {app_home}")

            self.logger.debug(
                f"FILE_PLUGIN_PROVIDER_CREATE: {self.name} creating FilesystemProvider"
            )
            self.provider = FilesystemProvider(app_home, self.config, self.name)
            self.logger.debug(
                f"FILE_PLUGIN_PROVIDER_CREATED: {self.name} "
                f"FilesystemProvider created successfully"
            )

            # State service injection handled by framework service injection phase
            self.logger.debug(
                f"FILE_PLUGIN_STATE_CHECK: {self.name} checking state service availability"
            )
            if self.state_service and hasattr(self.provider, "set_state_service"):
                self.logger.debug(
                    f"FILE_PLUGIN_STATE_INJECT: {self.name} "
                    f"injecting state service into provider"
                )
                self.provider.set_state_service(self.state_service)
                self.logger.debug(
                    f"FILE_PLUGIN_STATE_INJECTED: {self.name} "
                    f"state service injected successfully"
                )
                # Schema is created via StateAwarePlugin.get_schema_definitions() by SchemaManager
            else:
                self.logger.error(
                    f"{self.name} state service not "
                    f"available or provider lacks set_state_service method"
                )

            self.logger.debug(
                f"FILE_PLUGIN_INIT_SUCCESS: {self.name} initialization completed successfully"
            )

        except Exception as e:
            self.logger.error(f"{self.name} initialization failed: {str(e)}")
            raise RuntimeError(f"Failed to initialize blob storage plugin: {str(e)}") from e

    def take_action(
        self, params: dict[str, Any], state: dict[str, Any]
    ) -> dict[str, Any]:
        """Public action-dispatch surface declared by ``BlobStoragePluginProtocol``.

        The ``BlobStorageService`` proxy (``ananta/src/ananta/services/blob_storage_service/__init__.py``)
        calls ``plugin.take_action(params=..., state={})`` for every action
        it routes through the plugin (store_file, retrieve_file, etc.). The
        §9.H decomposition (commit ``aba7c7dc``) inlined this dispatch into
        ``_execute_action`` and removed the public hook, which broke every
        proxy callsite — every >4KB event in the LLM session ledger silently
        dropped because ``blob_storage_service.store_blob`` raises
        ``'DefaultBlobStoragePlugin' object has no attribute 'take_action'``.

        Restored to match the sibling pattern in
        ``s3_blob_storage_plugin/plugin.py:150-169``. ``_execute_action``
        (the framework hook on ``PluginBase``) now delegates here.
        """
        _ = state  # action handlers carry their own state; not used at this layer
        if "action" in params and isinstance(params["action"], dict):
            action_name = params["action"].get("name", "unknown")
            parameters = {k: v for k, v in params.items() if k != "action"}
        else:
            action_name = params.get("name", "unknown")
            parameters = params.get("parameters", {})
        result = self._run_action(action_name, parameters)
        return cast(dict[str, Any], result)

    def _execute_action(  # type: ignore[override]
        self,
        action_params: dict[str, Any],
        state: dict[str, Any],
        APP_HOME: str,  # noqa: ARG002 — framework signature
        config: dict[str, Any],  # noqa: ARG002 — framework signature
    ) -> ActionResult:
        """Framework-required hook on ``PluginBase``; delegates to :meth:`take_action`."""
        return cast(ActionResult, self.take_action(action_params, state))

    def _run_action(self, action_name: str, parameters: dict[str, Any]) -> ActionResult:
        """Execute an action by dispatching to the provider.

        Provider methods handle their own validation, so we just route the call.
        Type casts are used to satisfy static type checking - actual runtime
        validation is delegated to provider methods.

        Namespace requirements:
        - REQUIRED for store operations (need to know where to put the file)
        - OPTIONAL for other operations (blob_id is globally unique)
        """
        if not self.provider:
            return create_error_response(
                "blob_storage.not_initialized",
                "Plugin not initialized - provider not available",
            )

        # Cross-namespace operations - don't require namespace parameter
        if action_name == "file_command":
            command = cast(str, parameters.get("command", "ls"))
            return self.file_command(command)

        # Namespace is optional - empty string means "all namespaces" or "lookup from blob_id"
        namespace = cast(str, parameters.get("namespace", ""))

        # Only store operations require namespace (need to know where to put the file)
        store_operations = {"store_blob", "store_file"}
        if action_name in store_operations and not namespace:
            return create_error_response(
                "blob_storage.missing_namespace",
                "Namespace parameter is required for store operations",
            )

        try:
            return self._dispatch_action(action_name, namespace, parameters)
        except Exception as e:
            return create_error_response_from_exception(e, action_name)

    def _dispatch_action(
        self, action_name: str, namespace: str, parameters: dict[str, Any]
    ) -> ActionResult:
        """Dispatch action to appropriate provider method."""
        # Action name aliases mapped to canonical names
        action_aliases: dict[str, str] = {
            "store_file": "store_blob",
            "retrieve_file": "retrieve_blob",
            "update_file": "update_metadata",
            "update_blob": "update_metadata",
            "delete_file": "delete_blob",
            "search_files": "search_blobs",
            "get_metadata": "get_blob_metadata",
        }

        canonical_name = action_aliases.get(action_name, action_name)
        provider = self.provider
        assert provider is not None  # Checked above

        if canonical_name == "store_blob":
            return provider.store_blob(
                namespace,
                cast(bytes, parameters.get("content")),
                parameters.get("metadata", {}),
            )
        if canonical_name == "retrieve_blob":
            return provider.retrieve_blob(cast(str, parameters.get("blob_id")))
        if canonical_name == "update_metadata":
            return provider.update_blob_metadata(
                namespace,
                cast(str, parameters.get("blob_id")),
                parameters.get("metadata", {}),
            )
        if canonical_name == "delete_blob":
            return provider.delete_blob(namespace, cast(str, parameters.get("blob_id")))
        if canonical_name == "search_blobs":
            return provider.search_blobs(namespace, parameters.get("filters", {}))
        if canonical_name == "get_blob_metadata":
            return provider.get_blob_metadata(namespace, cast(str, parameters.get("blob_id")))

        return create_error_response(
            "blob_storage.unsupported_action",
            f"Unsupported action: {action_name}",
        )

    # BlobStorageServiceInterface direct method implementations
    def store_blob(
        self, namespace: str, content: bytes, metadata: dict[str, object]
    ) -> ActionResult:
        """Store blob with metadata - direct interface implementation."""
        return self._run_action(
            "store_file",
            {
                "namespace": namespace,
                "content": content,
                "metadata": metadata,
            },
        )

    def store_blob_from_file(
        self,
        namespace: str,
        file_path: str,
        filename: str | None = None,
        mime_type: str | None = None,
        metadata: dict[str, object] | None = None,
        artifact_type: str | None = None,
    ) -> ActionResult:
        """Ingest a file from disk as a blob.

        Reads bytes from ``file_path`` (which must be absolute), derives metadata
        (filename, mime_type, source_path, byte_count, artifact_type), merges any
        caller-supplied ``metadata``, then delegates to the same internal store
        path used by ``store_blob``. The agent never holds the file content.
        """
        validation_error = ingestion.validate_path(file_path)
        if validation_error is not None:
            return validation_error

        content_result = ingestion.read_content(file_path)
        if isinstance(content_result, dict):
            return content_result
        content = content_result

        path_obj = Path(file_path)
        resolved_filename = filename if filename is not None else path_obj.name
        mime_resolution = ingestion.resolve_mime_type(mime_type, resolved_filename)
        if isinstance(mime_resolution, dict):
            return mime_resolution
        resolved_mime = mime_resolution
        resolved_artifact_type = (
            artifact_type
            if artifact_type is not None
            else resolved_mime.split(MIME_PRIMARY_CLASS_SEPARATOR, 1)[0]
        )
        merged_metadata = ingestion.build_metadata(
            metadata or {},
            resolved_filename,
            resolved_mime,
            file_path,
            len(content),
            resolved_artifact_type,
        )
        return self.store_blob(namespace, content, merged_metadata)

    def retrieve_blob(self, blob_id: str) -> ActionResult:
        """Retrieve blob - direct interface implementation.

        Searches across all namespaces to find the blob.
        """
        return self._run_action(
            "retrieve_file",
            {
                "namespace": "",  # Empty namespace triggers cross-namespace search
                "blob_id": blob_id,
            },
        )

    def delete_blob(self, namespace: str, blob_id: str) -> ActionResult:
        """Delete blob - direct interface implementation."""
        return self._run_action(
            "delete_file",
            {
                "namespace": namespace,
                "blob_id": blob_id,
            },
        )

    def search_blobs(self, namespace: str, metadata_filters: dict[str, object]) -> ActionResult:
        """Search blobs - direct interface implementation."""
        return self._run_action(
            "search_files",
            {
                "namespace": namespace,
                "filters": metadata_filters,
            },
        )

    def get_blob_metadata(self, namespace: str, blob_id: str) -> ActionResult:
        """Get blob metadata - direct interface implementation."""
        return self._run_action(
            "get_metadata",
            {
                "namespace": namespace,
                "blob_id": blob_id,
            },
        )

    def update_blob_metadata(
        self, namespace: str, blob_id: str, metadata: dict[str, object]
    ) -> ActionResult:
        """Update blob metadata - direct interface implementation."""
        return self._run_action(
            "update_metadata",
            {
                "namespace": namespace,
                "blob_id": blob_id,
                "metadata": metadata,
            },
        )

    def resolve_blob_path(self, blob_url: str) -> str | None:
        """Resolve blob:// URL to filesystem path."""
        if not blob_url.startswith("blob://"):
            return None
        # Remove blob:// prefix
        relative_path = blob_url[7:]
        blobs_dir = getattr(self.provider, "blobs_dir", None) if self.provider else None
        if blobs_dir is not None:
            full_path = blobs_dir / relative_path
            return str(full_path) if full_path.exists() else None
        return None

    def file_command(self, command: str) -> ActionResult:
        """Execute Unix-style file commands on blob storage.

        Interface method implementation - operates across all namespaces.
        """
        return file_commands.execute_file_command(command, self.provider)

    # ========================================
    # Platform Process Actions (Plugin-specific)
    # ========================================

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/store_blob.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="store_blob",
        parameters={
            "namespace": ParameterMetadata(
                description="Namespace for data isolation (typically calling plugin/service name)",
                required=True,
                type=ParameterType.STRING,
            ),
            "content": ParameterMetadata(
                description="Binary content to store (base64-encoded or raw bytes)",
                required=True,
                type=ParameterType.STRING,
            ),
            "metadata": ParameterMetadata(
                description="Metadata key-value pairs for searchability and organization",
                required=False,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Storage result with blob_id",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or error",
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Result data containing blob_id",
                ),
            },
            usage_patterns=[
                "Store binary files (audio, images, documents)",
                "Save generated content to blob storage",
                "Persist large binary data with metadata",
            ],
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    def store_blob_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Store blob via platform process interface."""
        namespace = params.get("namespace", "")
        content = params.get("content", b"")
        metadata = params.get("metadata", {})
        return self.store_blob(namespace, content, metadata)

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/store_blob_from_file.json — the builder merges them at
    # startup, overwriting any values set here in the decorator.
    @platform_process(
        name="store_blob_from_file",
        parameters={
            "namespace": ParameterMetadata(
                description="Namespace for data isolation (typically calling plugin/service name)",
                required=True,
                type=ParameterType.STRING,
            ),
            "file_path": ParameterMetadata(
                description="Absolute path to the file to ingest as a blob",
                required=True,
                type=ParameterType.STRING,
            ),
            "filename": ParameterMetadata(
                description="Display filename for blob metadata; defaults to basename of file_path",
                required=False,
                type=ParameterType.STRING,
            ),
            "mime_type": ParameterMetadata(
                description="MIME type for the blob; if omitted, inferred from extension",
                required=False,
                type=ParameterType.STRING,
            ),
            "metadata": ParameterMetadata(
                description="Additional metadata key-value pairs (merged with auto-derived fields)",
                required=False,
                type=ParameterType.OBJECT,
            ),
            "artifact_type": ParameterMetadata(
                description="Semantic type tag (e.g. 'audio', 'image'); defaults to MIME primary class",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Storage result with blob_id",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or error",
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Result data containing blob_id, namespace, metadata",
                ),
            },
            usage_patterns=[
                "Ingest a finished render (FLAC, M4A, MP4) from disk",
                "Move large binary artifacts into blob storage without round-tripping bytes",
                "Stage cover art (JPEG, PNG) for downstream upload steps",
            ],
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False
        )
    )
    def store_blob_from_file_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Ingest a file from disk as a blob via platform process interface."""
        namespace = cast(str, params.get("namespace", ""))
        file_path = cast(str, params.get("file_path", ""))
        filename = cast(str | None, params.get("filename"))
        mime_type = cast(str | None, params.get("mime_type"))
        metadata = cast(dict[str, object], params.get("metadata", {}))
        artifact_type = cast(str | None, params.get("artifact_type"))
        return self.store_blob_from_file(
            namespace, file_path, filename, mime_type, metadata, artifact_type
        )

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/get_blob.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="get_blob",
        parameters={
            "name": ParameterMetadata(
                description=(
                    "File identifier - can be filename (tremolo.wav), name (tremolo), or blob_id (bmd-xxx)"
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Attachment info ready for post_message",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or error",
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Attachment with blob_id, namespace, media_type, filename, size_bytes",
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            blob_fields={
                "blob_id": "blob_id",
                "namespace": "namespace",
                "artifact_type": "artifact_type",
                "filename": "filename",
            }
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    def get_blob_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> ActionResult:
        """Get blob by name and return attachment-ready structure."""
        name = params.get("name", "")
        if not name:
            return create_error_response("get_blob.missing_name", "name parameter is required")

        # Resolve name to blob_id and namespace
        resolution = resolve_file_by_name(self, name)
        if not resolution:
            return create_error_response(
                "get_blob.not_found",
                f"File not found: '{name}'. Use file_command('ls') to list available files.",
            )

        blob_id, namespace = resolution

        # Get metadata to build complete attachment
        metadata_result = self.get_blob_metadata(namespace, blob_id)
        if metadata_result.get("action_status") != "completed":
            return metadata_result

        data = cast(dict[str, Any], metadata_result.get("data", {}))
        metadata = cast(dict[str, Any], data.get("metadata", {}))
        mime_type = str(metadata.get("mime_type", "application/octet-stream"))
        filename = str(metadata.get("filename") or metadata.get("original_name") or name)
        size_bytes = int(metadata.get("size", 0))

        # Derive artifact_type from mime_type
        artifact_type = mime_type.split("/")[0] if "/" in mime_type else "file"

        # Return flat fields - blob_fields only supports top-level keys
        return create_success_response({
            "blob_id": blob_id,
            "namespace": namespace,
            "artifact_type": artifact_type,
            "media_type": mime_type,
            "filename": filename,
            "size_bytes": size_bytes,
        })

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/delete_blob.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="delete_blob",
        parameters={
            "namespace": ParameterMetadata(
                description="Namespace for data isolation",
                required=True,
                type=ParameterType.STRING,
            ),
            "blob_id": ParameterMetadata(
                description="Unique blob identifier to delete",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Deletion confirmation",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or error",
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Deletion result",
                ),
            },
            usage_patterns=[
                "Delete temporary files",
                "Clean up old blobs",
                "Remove obsolete content",
            ],
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False
        )
    )
    def delete_blob_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Delete blob via platform process interface."""
        namespace = params.get("namespace", "")
        blob_id = params.get("blob_id", "")
        return self.delete_blob(namespace, blob_id)

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/search_blobs.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="search_blobs",
        parameters={
            "namespace": ParameterMetadata(
                description="Namespace for data isolation",
                required=True,
                type=ParameterType.STRING,
            ),
            "metadata_filters": ParameterMetadata(
                description="Filter criteria for metadata search (key-value pairs)",
                required=True,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Search results with matching blobs",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or error",
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Search results with list of matching blobs",
                ),
            },
            usage_patterns=[
                "Find blobs by metadata criteria",
                "Search for files with specific properties",
                "Filter blobs by tags or attributes",
            ],
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    def search_blobs_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Search blobs via platform process interface."""
        namespace = params.get("namespace", "")
        metadata_filters = params.get("metadata_filters", {})
        return self.search_blobs(namespace, metadata_filters)

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/get_blob_metadata.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="get_blob_metadata",
        parameters={
            "namespace": ParameterMetadata(
                description="Namespace for data isolation",
                required=True,
                type=ParameterType.STRING,
            ),
            "blob_id": ParameterMetadata(
                description="Unique blob identifier",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Blob metadata without content",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or error",
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Blob metadata",
                ),
            },
            usage_patterns=[
                "Check blob properties without downloading",
                "Inspect metadata before retrieval",
                "Verify blob existence and attributes",
            ],
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    def get_blob_metadata_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Get blob metadata via platform process interface."""
        namespace = params.get("namespace", "")
        blob_id = params.get("blob_id", "")
        return self.get_blob_metadata(namespace, blob_id)

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/update_blob_metadata.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="update_blob_metadata",
        parameters={
            "namespace": ParameterMetadata(
                description="Namespace for data isolation",
                required=True,
                type=ParameterType.STRING,
            ),
            "blob_id": ParameterMetadata(
                description="Unique blob identifier to update",
                required=True,
                type=ParameterType.STRING,
            ),
            "metadata": ParameterMetadata(
                description="New metadata key-value pairs to merge with existing metadata",
                required=True,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Update confirmation with new metadata",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or error",
                ),
                "data": ParameterMetadata(
                    type=ParameterType.OBJECT,
                    description="Updated blob metadata",
                ),
            },
            usage_patterns=[
                "Add tags or labels to existing blobs",
                "Update description or classification",
                "Modify custom attributes",
            ],
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    def update_blob_metadata_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Update blob metadata via platform process interface."""
        namespace = params.get("namespace", "")
        blob_id = params.get("blob_id", "")
        metadata = params.get("metadata", {})
        return self.update_blob_metadata(namespace, blob_id, metadata)

    # =========================================================================
    # File Command - Unix-style file listing for blob storage
    # =========================================================================

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/file_command.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="file_command",
        parameters={
            "command": ParameterMetadata(
                description="Command to execute (e.g., 'ls', 'ls -l', 'file ID', 'find PATTERN')",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Command output formatted as text",
            properties={
                "action_status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status: completed or error",
                ),
                "output": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Formatted command output",
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True
        )
    )
    def file_command_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> ActionResult:
        """Execute file command via platform process interface."""
        command = params.get("command", "ls")
        return self.file_command(command)

    # =========================================================================
    # EdgeProcessProvider Implementation
    # =========================================================================

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        """Return all edge process definitions for blob storage plugin.

        Returns:
            Dictionary mapping process names to their EdgeProcessDefinition.
        """
        return {
            "store_blob": EdgeProcessDefinition(
                name="store_blob",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True
                ),
            ),
            "store_blob_from_file": EdgeProcessDefinition(
                name="store_blob_from_file",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False
                ),
            ),
            "get_blob": EdgeProcessDefinition(
                name="get_blob",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True
                ),
            ),
            "delete_blob": EdgeProcessDefinition(
                name="delete_blob",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True
                ),
            ),
            "search_blobs": EdgeProcessDefinition(
                name="search_blobs",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True
                ),
            ),
            "get_blob_metadata": EdgeProcessDefinition(
                name="get_blob_metadata",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True
                ),
            ),
            "update_blob_metadata": EdgeProcessDefinition(
                name="update_blob_metadata",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True
                ),
            ),
            "file_command": EdgeProcessDefinition(
                name="file_command",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True
                ),
            ),
        }
