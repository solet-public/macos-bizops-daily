import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from ananta.constants import DEFAULT_BLOB_STORAGE_PLUGIN
from ananta.core.domain.enums import ErrorSeverity
from ananta.core.domain.types import ActionResult, ErrorDetail
from ananta.core.plugins.plugin_manager import PluginManager
from ananta.error_handling import FrameworkError
from ananta.interfaces.blob_storage_service_interface import BlobStorageServiceInterface
from ananta.interfaces.bootstrappable_service_interface import (
    BootstrappableServiceInterface,
)
from ananta.utils.naming import NamingError, build_external_id, parse_filename


# Protocol interface for PluginManager dependency
@runtime_checkable
class PluginManagerProtocol(Protocol):
    """Protocol interface for PluginManager methods used by BlobStorageService."""

    def get_plugin(self, plugin_name: str) -> object: ...


@runtime_checkable
class BlobStoragePluginProtocol(Protocol):
    """Protocol for blob storage plugin instances.

    Defines the attributes and methods accessed on blob storage plugins
    by the BlobStorageService wrapper.
    """

    def is_ready(self) -> bool:
        """Check if the plugin is ready for use."""
        ...

    def get_readiness_error(self) -> str | None:
        """Get the error message if not ready, None if ready."""
        ...

    def set_as_active_provider(self, interface_name: str) -> None:
        """Notify plugin it's the active provider for an interface."""
        ...

    def take_action(self, params: dict[str, object], state: dict[str, object]) -> dict[str, object]:
        """Execute an action on the plugin."""
        ...

    def store_blob_from_file(
        self,
        namespace: str,
        file_path: str,
        filename: str | None,
        mime_type: str | None,
        metadata: dict[str, object] | None,
        artifact_type: str | None,
    ) -> dict[str, object]:
        """Persist a file from disk as a blob, streaming via the provider."""
        ...

    def resolve_blob_path(self, blob_url: str) -> str | None:
        """Return a local filesystem path for the blob (provider-specific)."""
        ...


logger = logging.getLogger(__name__)


class BlobStorageService(BootstrappableServiceInterface, BlobStorageServiceInterface):
    def __init__(
        self,
        plugin_manager: PluginManager | None = None,
        blob_storage_plugin_name: str | None = None,
        app_home: str = "",
    ):
        self.app_home = app_home
        self._blob_storage_plugin_name = blob_storage_plugin_name or DEFAULT_BLOB_STORAGE_PLUGIN
        self._blob_storage_plugin: BlobStoragePluginProtocol | None = None  # Cached plugin instance

        # Initialize via BootstrappableServiceInterface pattern
        super().__init__(plugin_manager)

    def _init_bootstrap(self) -> None:
        # Bootstrap mode uses in-memory storage
        self.memory_blobs: dict[str, dict[str, object]] = {}

    def _init_plugin(self) -> None:
        # Deferred validation - do not call _validate_blob_storage_plugin() during transition
        # Plugin validation will occur lazily during first _execute_blob_storage_action() call
        pass

    def _capture_bootstrap_state(self) -> dict[str, object]:
        return {"blobs": dict(self.memory_blobs)}

    def _restore_bootstrap_data(self, data: dict[str, object]) -> None:
        # Bootstrap blobs should be migrated to plugin storage via operation replay
        # No additional restoration needed
        pass

    def _get_plugin_instance(self) -> BlobStoragePluginProtocol:
        """Get blob storage plugin instance (cached after first call).

        Returns:
            The blob storage plugin instance

        Raises:
            FrameworkError: If plugin not found or plugin_manager not initialized
        """
        # Return cached instance if available
        if self._blob_storage_plugin is not None:
            return self._blob_storage_plugin

        # Validate plugin_manager is available
        if self.plugin_manager is None:
            raise FrameworkError(
                message="BlobStorageService plugin_manager is not initialized",
                error_code="blob_storage_service.plugin_manager_not_initialized",
                severity=ErrorSeverity.ERROR,
            )

        # Get and cache plugin instance
        plugin_manager = cast(PluginManagerProtocol, self.plugin_manager)
        plugin = plugin_manager.get_plugin(self._blob_storage_plugin_name)

        if plugin is None:
            raise FrameworkError(
                message=f"Blob storage plugin '{self._blob_storage_plugin_name}' not found",
                error_code="blob_storage_service.plugin_not_found",
                severity=ErrorSeverity.ERROR,
            )

        # Cast to protocol type for type safety
        self._blob_storage_plugin = cast(BlobStoragePluginProtocol, plugin)

        # CRITICAL: Notify plugin it's an active interface provider
        if hasattr(self._blob_storage_plugin, "set_as_active_provider"):
            self._blob_storage_plugin.set_as_active_provider("BlobStorageServiceInterface")

        return self._blob_storage_plugin

    def _ensure_ready(self) -> BlobStoragePluginProtocol:
        """Ensure blob storage plugin is available and ready.

        Returns:
            The blob storage plugin instance

        Raises:
            FrameworkError: If plugin not ready
        """
        plugin = self._get_plugin_instance()

        # Check plugin readiness via protocol methods
        if not plugin.is_ready():
            error_msg = plugin.readiness_error or "Plugin not ready"
            raise FrameworkError(
                message=f"Blob storage plugin not ready: {error_msg}",
                error_code="blob_storage_service.plugin_not_ready",
                severity=ErrorSeverity.ERROR,
            )

        return plugin


    def _execute_blob_storage_action(
        self, action_name: str, parameters: dict[str, object]
    ) -> ActionResult:
        if self.bootstrap_mode:
            raise FrameworkError(
                message=f"Cannot execute plugin action '{action_name}' in bootstrap mode",
                error_code="blob_storage_service.bootstrap_mode_plugin_call",
                details={"action_name": action_name, "parameters": parameters},
                severity=ErrorSeverity.ERROR,
            )

        plugin = self._ensure_ready()
        action_params = {"action": {"name": action_name}, **parameters}

        try:
            result = plugin.take_action(
                params=action_params,
                state={},
            )
            return self._convert_plugin_result(result)

        except Exception as e:
            return self._build_action_error(action_name, parameters, e)

    def _convert_plugin_result(self, result: dict[str, object]) -> ActionResult:
        """Convert plugin result to ActionResult format with type narrowing.

        Args:
            result: Raw result from plugin

        Returns:
            Properly typed ActionResult
        """
        return {
            "action_status": self._extract_str(result, "action_status", "completed"),
            "data": self._extract_dict(result, "data"),
            "actions": self._extract_list(result, "actions"),
            "error": self._extract_error_detail(result.get("error")),
            "timestamp": self._extract_timestamp(result),
        }

    def _extract_str(self, result: dict[str, object], key: str, default: str) -> str:
        """Extract string value with type narrowing."""
        value = result.get(key, default)
        return value if isinstance(value, str) else default

    def _extract_dict(self, result: dict[str, object], key: str) -> dict[str, object]:
        """Extract dict value with type narrowing."""
        value = result.get(key, {})
        return value if isinstance(value, dict) else {}

    def _extract_list(self, result: dict[str, object], key: str) -> list[dict[str, object]]:
        """Extract list of dicts with type narrowing."""
        value = result.get(key, [])
        if not isinstance(value, list):
            return []
        # Filter to only dict items for type safety
        return [item for item in value if isinstance(item, dict)]

    def _extract_timestamp(self, result: dict[str, object]) -> str:
        """Extract or generate timestamp."""
        value = result.get("timestamp", datetime.now(UTC).isoformat())
        return value if isinstance(value, str) else datetime.now(UTC).isoformat()

    def _extract_error_detail(self, error_value: object) -> ErrorDetail | None:
        """Extract and validate ErrorDetail from result.

        Args:
            error_value: Raw error value from result

        Returns:
            Validated ErrorDetail or None
        """
        if error_value is None:
            return None
        if not isinstance(error_value, dict):
            return None

        required_keys = ["type", "code", "message", "details", "severity", "timestamp"]
        if all(key in error_value for key in required_keys):
            return cast(ErrorDetail, error_value)
        return None

    def _build_action_error(
        self, action_name: str, parameters: dict[str, object], error: Exception
    ) -> ActionResult:
        """Build error ActionResult for failed action.

        Args:
            action_name: Name of the failed action
            parameters: Action parameters
            error: The exception that occurred

        Returns:
            Error ActionResult
        """
        error_result = FrameworkError(
            message=f"Blob storage action '{action_name}' failed: {str(error)}",
            error_code="blob_storage_service.action_failed",
            details={
                "action_name": action_name,
                "parameters": parameters,
                "plugin": self._blob_storage_plugin_name,
            },
            original_error=error,
            severity=ErrorSeverity.ERROR,
        )

        return {
            "action_status": "error",
            "data": {},
            "actions": [],
            "error": cast(ErrorDetail, error_result.to_dict()),
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def _validate_content(self, content: bytes) -> None:
        if len(content) == 0:
            raise FrameworkError(
                message="Blob content cannot be empty",
                error_code="blob_storage_service.empty_content",
                details={},
                severity=ErrorSeverity.ERROR,
            )

    def _validate_blob_id(self, blob_id: str) -> None:
        if not blob_id:
            raise FrameworkError(
                message="Blob ID must be a non-empty string",
                error_code="blob_storage_service.invalid_blob_id",
                details={"provided_blob_id": blob_id},
                severity=ErrorSeverity.ERROR,
            )

    def _enrich_metadata(
        self, metadata: dict[str, object], content: bytes, namespace: str
    ) -> dict[str, object]:
        enriched = {**metadata}

        # Add standard fields
        enriched["size"] = len(content)
        enriched["created_at"] = datetime.now(UTC).isoformat()
        enriched["updated_at"] = enriched["created_at"]
        enriched["namespace"] = namespace  # Add namespace for data isolation

        # Generate preview for text-like content
        if not enriched.get("preview"):
            try:
                # Try to decode as text for preview
                text_content = content.decode("utf-8", errors="ignore")
                # Remove NUL bytes which are invalid in PostgreSQL TEXT columns
                preview = (
                    text_content[:50].replace("\n", " ").replace("\r", " ").replace("\x00", "")
                )
                enriched["preview"] = preview
            except Exception:
                # For binary files, use size info
                enriched["preview"] = f"Binary file ({len(content)} bytes)"

        return enriched

    def store_blob(
        self, namespace: str, content: bytes | str, metadata: dict[str, object]
    ) -> ActionResult:
        # Handle base64-encoded content (when called via action system)
        if isinstance(content, str):
            import base64

            try:
                content = base64.b64decode(content)
            except Exception as e:
                raise FrameworkError(
                    message=f"Failed to decode base64 content: {str(e)}",
                    error_code="blob_storage_service.invalid_base64",
                    details={"error": str(e)},
                    severity=ErrorSeverity.ERROR,
                ) from e

        self._validate_content(content)

        enriched_metadata = self._enrich_metadata(metadata, content, namespace)

        if self.bootstrap_mode:
            # In bootstrap mode, we still need to generate an ID since there's no state service
            import uuid

            blob_id = f"blob_{uuid.uuid4().hex[:8]}"
            self.memory_blobs[blob_id] = {
                "content": content,
                "metadata": enriched_metadata,
                "namespace": namespace,  # Track namespace in bootstrap mode
            }
            return {
                "action_status": "completed",
                "data": {"blob_id": blob_id},
                "actions": [],
                "error": None,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        else:
            # Plugin mode: Let the provider handle ID generation via state service
            return self._execute_blob_storage_action(
                "store_file",
                {"namespace": namespace, "content": content, "metadata": enriched_metadata},
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
        """Persist a file from disk as a blob without reading its bytes through the proxy.

        Delegates to the active plugin's ``store_blob_from_file`` directly, so
        large files stream via the provider (e.g. ``s3.upload_file`` multipart)
        instead of being read into proxy-process memory.
        """
        if self.bootstrap_mode:
            raise FrameworkError(
                message="store_blob_from_file is not available in bootstrap mode",
                error_code="blob_storage_service.bootstrap_mode_plugin_call",
                details={"file_path": file_path},
                severity=ErrorSeverity.ERROR,
            )
        # Path validation happens in the plugin/provider so the proxy never reads bytes.
        plugin = self._ensure_ready()
        result = plugin.store_blob_from_file(
            namespace, file_path, filename, mime_type, metadata, artifact_type
        )
        return self._convert_plugin_result(result)

    @staticmethod
    def _validate_ingestion_path(file_path: str) -> Path:
        path = Path(file_path)
        if not path.is_absolute():
            raise FrameworkError(
                message=f"file_path must be absolute, got {file_path!r}",
                error_code="blob_storage.file_path_not_absolute",
                details={"file_path": file_path},
                severity=ErrorSeverity.ERROR,
            )
        if not path.exists():
            raise FrameworkError(
                message=f"file_path does not exist: {file_path}",
                error_code="blob_storage.file_not_found",
                details={"file_path": file_path},
                severity=ErrorSeverity.ERROR,
            )
        if not path.is_file():
            raise FrameworkError(
                message=f"file_path is not a regular file: {file_path}",
                error_code="blob_storage.file_not_regular",
                details={"file_path": file_path},
                severity=ErrorSeverity.ERROR,
            )
        return path

    @staticmethod
    def _resolve_ingestion_mime_type(
        file_path: str, resolved_filename: str, mime_type: str | None
    ) -> str:
        import mimetypes

        if mime_type:
            return mime_type
        guessed = mimetypes.guess_type(resolved_filename)[0]
        if guessed:
            return guessed
        raise FrameworkError(
            message=(
                f"mime_type could not be inferred for {file_path!r}; "
                "supply mime_type explicitly"
            ),
            error_code="blob_storage.mime_type_unknown",
            details={"file_path": file_path, "filename": resolved_filename},
            severity=ErrorSeverity.ERROR,
        )

    @staticmethod
    def _build_ingestion_metadata(
        caller_metadata: dict[str, object] | None,
        path: Path,
        filename: str,
        resolved_mime_type: str,
        byte_count: int,
        artifact_type: str | None,
    ) -> dict[str, object]:
        merged: dict[str, object] = dict(caller_metadata or {})
        merged.setdefault("filename", filename)
        merged.setdefault("original_name", filename)
        merged.setdefault("mime_type", resolved_mime_type)
        merged.setdefault("source_path", str(path))
        merged.setdefault("byte_count", byte_count)
        if artifact_type is not None:
            merged.setdefault("artifact_type", artifact_type)
        elif "/" in resolved_mime_type:
            merged.setdefault("artifact_type", resolved_mime_type.split("/", 1)[0])
        return merged

    def retrieve_blob(self, blob_id: str) -> ActionResult:
        """Retrieve blob by internal blob_id.

        This is the internal method used by plugins that already have the blob_id.
        For LLM-facing retrieval by filename, use retrieve_blob_by_name().
        """
        self._validate_blob_id(blob_id)

        if self.bootstrap_mode:
            # Retrieve from memory for bootstrap (no namespace restriction)
            blob_data = self.memory_blobs.get(blob_id)
            if blob_data:
                metadata = cast(dict[str, object], blob_data.get("metadata", {}))
                return {
                    "action_status": "completed",
                    "data": {
                        "content": blob_data["content"],
                        "metadata": metadata,
                        "blob_id": blob_id,
                        "namespace": blob_data.get("namespace", ""),
                        "filename": metadata.get("filename")
                        or metadata.get("original_name")
                        or blob_id,
                    },
                    "actions": [],
                    "error": None,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            else:
                return {
                    "action_status": "error",
                    "data": {},
                    "actions": [],
                    "error": cast(
                        ErrorDetail,
                        {
                            "type": "FrameworkError",
                            "code": "blob_storage_service.blob_not_found",
                            "message": f"Blob {blob_id} not found",
                            "details": {"blob_id": blob_id},
                            "severity": "error",
                            "timestamp": datetime.now(UTC).isoformat(),
                        },
                    ),
                    "timestamp": datetime.now(UTC).isoformat(),
                }
        else:
            # Pass empty namespace to trigger cross-namespace search
            return self._execute_blob_storage_action(
                "retrieve_file", {"namespace": "", "blob_id": blob_id}
            )

    def retrieve_blob_by_name(self, filename: str) -> ActionResult:
        """Retrieve blob by filename - the LLM-facing interface.

        Resolves the filename to the internal blob_id using name-based lookup,
        then retrieves the content and metadata.
        """
        if not filename:
            return {
                "action_status": "error",
                "data": {},
                "actions": [],
                "error": cast(
                    ErrorDetail,
                    {
                        "type": "FrameworkError",
                        "code": "blob_storage_service.invalid_filename",
                        "message": "Filename must be a non-empty string",
                        "details": {"filename": filename},
                        "severity": "error",
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                ),
                "timestamp": datetime.now(UTC).isoformat(),
            }

        if self.bootstrap_mode:
            # In bootstrap mode, search memory blobs by filename/original_name
            for blob_id, blob_data in self.memory_blobs.items():
                metadata = cast(dict[str, object], blob_data.get("metadata", {}))
                stored_filename = metadata.get("filename") or metadata.get("original_name")
                if stored_filename == filename:
                    return {
                        "action_status": "completed",
                        "data": {
                            "content": blob_data["content"],
                            "metadata": metadata,
                            "blob_id": blob_id,
                            "namespace": blob_data.get("namespace", ""),
                            "filename": stored_filename or blob_id,
                        },
                        "actions": [],
                        "error": None,
                        "timestamp": datetime.now(UTC).isoformat(),
                    }
            return {
                "action_status": "error",
                "data": {},
                "actions": [],
                "error": cast(
                    ErrorDetail,
                    {
                        "type": "FrameworkError",
                        "code": "blob_storage_service.blob_not_found",
                        "message": f"File '{filename}' not found in blob storage",
                        "details": {"filename": filename},
                        "severity": "error",
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                ),
                "timestamp": datetime.now(UTC).isoformat(),
            }
        else:
            # Resolve filename to blob_id using name-based lookup
            blob_metadata = self.get_by_name(filename)
            if not blob_metadata:
                return {
                    "action_status": "error",
                    "data": {},
                    "actions": [],
                    "error": cast(
                        ErrorDetail,
                        {
                            "type": "FrameworkError",
                            "code": "blob_storage_service.blob_not_found",
                            "message": f"File '{filename}' not found in blob storage",
                            "details": {"filename": filename},
                            "severity": "error",
                            "timestamp": datetime.now(UTC).isoformat(),
                        },
                    ),
                    "timestamp": datetime.now(UTC).isoformat(),
                }

            blob_id = str(blob_metadata.get("blob_id", ""))
            # Delegate to internal retrieve_blob
            return self._execute_blob_storage_action(
                "retrieve_file", {"namespace": "", "blob_id": blob_id}
            )

    def delete_blob(self, namespace: str, blob_id: str) -> ActionResult:
        self._validate_blob_id(blob_id)

        return self._execute_blob_storage_action(
            "delete_file", {"namespace": namespace, "blob_id": blob_id}
        )

    def search_blobs(self, namespace: str, metadata_filters: dict[str, object]) -> ActionResult:
        return self._execute_blob_storage_action(
            "search_files", {"namespace": namespace, "filters": metadata_filters}
        )

    def get_blob_metadata(self, namespace: str, blob_id: str) -> ActionResult:
        self._validate_blob_id(blob_id)

        return self._execute_blob_storage_action(
            "get_metadata", {"namespace": namespace, "blob_id": blob_id}
        )

    def update_blob_metadata(
        self, namespace: str, blob_id: str, metadata: dict[str, object]
    ) -> ActionResult:
        self._validate_blob_id(blob_id)

        return self._execute_blob_storage_action(
            "update_metadata", {"namespace": namespace, "blob_id": blob_id, "metadata": metadata}
        )

    def resolve_blob_path(self, blob_url: str) -> str | None:
        """Resolve a blob URL or raw blob_id to a filesystem path via the active plugin.

        The proxy no longer hardcodes the default plugin's blobs directory.
        Cloud-backed providers (e.g. S3) lazily materialize the blob into a
        task-local cache and return that path; filesystem providers return
        their on-disk path directly.
        """
        if self.bootstrap_mode:
            return None
        plugin = self._ensure_ready()
        return plugin.resolve_blob_path(blob_url)  # type: ignore[attr-defined]

    def file_command(self, command: str) -> ActionResult:
        """Execute Unix-style file commands on blob storage.

        Utility method that operates across all namespaces.
        Delegates to the blob storage plugin.
        """
        return self._execute_blob_storage_action("file_command", {"command": command})

    def get_by_name(self, name: str, namespace: str | None = None) -> dict[str, object] | None:
        """Look up blob metadata by name.

        Resolves a human-readable name to blob metadata by normalizing
        the name to external_id and querying the metadata store.

        Args:
            name: Display name or filename (e.g., "speech" or "speech.wav")
            namespace: Optional namespace to restrict search

        Returns:
            Blob metadata dict if found, None otherwise.
            The dict includes: blob_id, name, external_id, original_name, media_type, etc.
        """
        # Parse filename to get base name (strip extension if present)
        base_name, _ = parse_filename(name)

        # Normalize to external_id
        try:
            external_id = build_external_id(base_name)
        except NamingError:
            # Invalid name - cannot resolve
            logger.warning(f"Cannot normalize name '{name}' to external_id")
            return None

        # Search for blob with matching external_id
        filters: dict[str, object] = {"external_id": external_id}
        if namespace:
            filters["namespace"] = namespace

        result = self._execute_blob_storage_action("search_files", {"filters": filters})

        if result.get("action_status") != "completed":
            return None

        data = result.get("data", {})
        files = data.get("files", [])
        if not isinstance(files, list) or not files:
            return None

        # Return first match (external_id should be unique)
        first_match = files[0]
        return first_match if isinstance(first_match, dict) else None

    def resolve_blob_url(self, blob_url: str, namespace: str | None = None) -> str | None:
        """Resolve blob:// URL to filesystem path, supporting name-based resolution.

        Supports two URL formats:
        - blob://<blob_id> - Direct blob ID lookup (legacy)
        - blob://<filename> - Name-based lookup via external_id

        Args:
            blob_url: URL in blob:// format
            namespace: Optional namespace for name-based resolution

        Returns:
            Filesystem path if resolved, None if not found
        """
        if not blob_url.startswith("blob://"):
            return blob_url  # Return as-is if not a blob URL

        identifier = blob_url.replace("blob://", "")

        # First, try direct blob ID resolution (legacy path)
        direct_path = self.resolve_blob_path(blob_url)
        if direct_path:
            return direct_path

        # If direct resolution failed, try name-based resolution
        blob_metadata = self.get_by_name(identifier, namespace)
        if blob_metadata:
            blob_id = blob_metadata.get("blob_id")
            if blob_id and isinstance(blob_id, str):
                return self.resolve_blob_path(f"blob://{blob_id}")

        logger.warning(f"Could not resolve blob URL: {blob_url}")
        return None
