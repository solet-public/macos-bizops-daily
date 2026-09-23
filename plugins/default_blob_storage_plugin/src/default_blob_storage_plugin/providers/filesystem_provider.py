"""Filesystem-backed blob storage provider."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ananta.core.plugins.plugin_contracts import ActionResult
from ananta.interfaces.blob_storage_service_interface import BlobStorageServiceInterface
from ananta.interfaces.state_service_protocol import StateServiceProtocol

from ..config import get_blobs_directory
from ..constants import DEFAULT_MAX_FILE_SIZE
from ..errors import (
    BlobStorageErrorCode,
    BlobValidationError,
    create_error_response,
    create_error_response_from_exception,
    create_success_response,
)
from ..validation import (
    normalize_metadata,
    validate_blob_id_format,
    validate_file_content,
    validate_file_metadata,
    validate_search_filters,
)
from . import metadata_prep, search_filters, state_ops


class FilesystemProvider(BlobStorageServiceInterface):
    """Filesystem-backed implementation of BlobStorageServiceInterface."""

    def __init__(
        self,
        app_home: str,
        config: dict[str, Any],
        plugin_name: str = "default_blob_storage_plugin",
    ) -> None:
        self.app_home: str = app_home
        self.config: dict[str, Any] = config
        self.plugin_name: str = plugin_name
        self.blobs_dir: Path = get_blobs_directory(app_home)
        self.max_file_size: int = config.get("max_file_size", DEFAULT_MAX_FILE_SIZE)
        self.state_service: StateServiceProtocol | None = None
        self.blobs_dir.mkdir(parents=True, exist_ok=True)

    def set_state_service(self, state_service: StateServiceProtocol) -> None:
        self.state_service = state_service

    def resolve_blob_path(self, blob_url: str) -> str | None:
        """Resolve blob:// URL to filesystem path."""
        if not blob_url.startswith("blob://"):
            return None
        full_path = self.blobs_dir / blob_url[7:]
        return str(full_path) if full_path.exists() else None

    def file_command(
        self,
        command: str,  # pyright: ignore[reportUnusedParameter]  # noqa: ARG002
    ) -> ActionResult:
        """Execute Unix-style file commands - implemented at plugin level."""
        raise NotImplementedError(
            "file_command is a user-facing command implemented at the plugin level, "
            "not at the provider level. Use DefaultBlobStoragePlugin.file_command() instead."
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_blob_path(self, blob_id: str) -> Path:
        return self.blobs_dir / blob_id

    def _extract_job_context(self, metadata: dict[str, Any]) -> str:
        tags = metadata.get("tags", "")
        if isinstance(tags, str) and "job_id:" in tags:
            for part in tags.split(","):
                if part.strip().startswith("job_id:"):
                    return part.strip().split(":", 1)[1]
        return "NO_JOB_ID"

    def _write_blob_file(
        self, content: bytes, generated_id: str, log_prefix: str
    ) -> tuple[Path, Exception | None]:
        logger = logging.getLogger(__name__)
        blob_path = self._get_blob_path(generated_id)
        logger.debug("%s BLOB_PATH: %s", log_prefix, blob_path)
        logger.debug("%s BLOB_WRITE: Writing %d bytes to %s", log_prefix, len(content), blob_path)
        try:
            with open(blob_path, "wb") as f:
                f.write(content)
            logger.debug("%s BLOB_WRITE: SUCCESS", log_prefix)
            return blob_path, None
        except Exception as blob_error:
            logger.error("%s BLOB_WRITE: FAILED - Rolling back metadata", log_prefix)
            return blob_path, blob_error

    def _cleanup_orphaned_files(self) -> None:
        """Remove blob files without corresponding metadata records."""
        try:
            if not self.blobs_dir.exists() or not self.state_service:
                return
            filename_to_path = {f.name: f for f in self.blobs_dir.iterdir() if f.is_file()}
            metadata_ids = state_ops.get_metadata_blob_ids(self.state_service, self.plugin_name)
            for blob_id in set(filename_to_path.keys()) - metadata_ids:
                filename_to_path[blob_id].unlink(missing_ok=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # BlobStorageServiceInterface implementation
    # ------------------------------------------------------------------

    def store_blob(
        self, namespace: str, content: bytes, metadata: dict[str, Any]
    ) -> ActionResult:
        """Store blob with metadata using state service for ID generation."""
        log_prefix = f"[BLOB_STORE {self._extract_job_context(metadata)}]"
        logger = logging.getLogger(__name__)

        try:
            content_size = len(content)
            logger.debug(
                "%s ENTRY: content_size=%d, metadata_keys=%s, tags=%s",
                log_prefix, content_size, list(metadata.keys()), metadata.get("tags", "NONE"),
            )
            validate_file_content(content, self.max_file_size)
            validate_file_metadata(metadata)

            if not self.state_service:
                logger.error("%s ERROR: State service not available", log_prefix)
                return create_error_response(
                    BlobStorageErrorCode.STORAGE_ERROR.value,
                    "State service not available for metadata storage",
                )

            schema_metadata, normalized_metadata = metadata_prep.prepare_metadata_for_storage(
                metadata, content_size, log_prefix
            )
            generated_id, error_response = state_ops.store_metadata_with_conflict_check(
                self.state_service, self.plugin_name, namespace, schema_metadata, log_prefix
            )
            if error_response:
                return error_response
            assert generated_id is not None

            blob_path, blob_error = self._write_blob_file(content, generated_id, log_prefix)
            if blob_error:
                self.state_service.delete_records(
                    namespace=self.plugin_name,
                    query={"table": "metadata", "filters": {"id": generated_id}},
                )
                raise blob_error

            update_error = state_ops.update_blob_id_in_metadata(
                self.state_service, self.plugin_name, generated_id, blob_path, log_prefix
            )
            if update_error:
                return update_error

            logger.debug("%s SUCCESS: Stored with state-generated ID=%s", log_prefix, generated_id)
            relative_path = (
                f"data/plugin_data/default_blob_storage_plugin/blobs/{generated_id}"
            )
            return create_success_response(
                {"blob_id": generated_id, "file_path": relative_path, "metadata": normalized_metadata}
            )

        except BlobValidationError as e:
            logger.error("%s VALIDATION_ERROR: %s - %s", log_prefix, e.error_code, e.message)
            return create_error_response(e.error_code, e.message)
        except Exception as e:
            import traceback
            logger.error("%s EXCEPTION: %s - %s", log_prefix, type(e).__name__, e)
            logger.error("%s TRACEBACK: %s", log_prefix, traceback.format_exc())
            return create_error_response_from_exception(e, "store_file")

    def retrieve_blob(self, blob_id: str) -> ActionResult:
        try:
            validate_blob_id_format(blob_id)

            if not self.state_service:
                return create_error_response(
                    BlobStorageErrorCode.STORAGE_ERROR.value,
                    "State service not available for metadata retrieval",
                )

            metadata = state_ops.find_metadata_by_blob_id(
                self.state_service, self.plugin_name, blob_id
            )
            actual_namespace = metadata.get("plugin_namespace", "") if metadata else ""

            if not metadata:
                return create_error_response(
                    BlobStorageErrorCode.BLOB_NOT_FOUND.value, f"Blob not found: {blob_id}"
                )

            blob_path = self._get_blob_path(blob_id)
            if not blob_path.exists():
                return create_error_response(
                    BlobStorageErrorCode.BLOB_NOT_FOUND.value, f"Blob not found: {blob_id}"
                )

            with open(blob_path, "rb") as f:
                content = f.read()

            filename = metadata.get("filename") or metadata.get("original_name") or blob_id
            return create_success_response({
                "blob_id": blob_id,
                "content": content.hex(),
                "metadata": metadata,
                "namespace": actual_namespace,
                "filename": filename,
            })

        except BlobValidationError as e:
            return create_error_response(e.error_code, e.message)
        except Exception as e:
            return create_error_response_from_exception(e, "retrieve_blob")

    def update_blob_metadata(
        self, namespace: str, blob_id: str, metadata: dict[str, Any]
    ) -> ActionResult:
        try:
            validate_blob_id_format(blob_id)
            validate_file_metadata(metadata)

            if not self.state_service:
                return create_error_response(
                    BlobStorageErrorCode.STORAGE_ERROR.value,
                    "State service not available for metadata update",
                )

            existing_metadata = state_ops.get_metadata_from_state(
                self.state_service, self.plugin_name, namespace, blob_id
            )
            if not existing_metadata:
                return create_error_response(
                    BlobStorageErrorCode.BLOB_NOT_FOUND.value, f"Blob not found: {blob_id}"
                )

            updated_metadata = {**existing_metadata, **metadata}
            normalized_metadata = normalize_metadata(updated_metadata)
            filtered_metadata = metadata_prep.filter_state_managed_fields(normalized_metadata)

            update_result = self.state_service.update_state(
                namespace=self.plugin_name,
                query={
                    "table": "metadata",
                    "filters": {"blob_id": blob_id, "plugin_namespace": namespace},
                },
                updates=filtered_metadata,
            )

            if update_result.get("action_status") != "completed":
                return create_error_response(
                    BlobStorageErrorCode.METADATA_STORAGE_ERROR.value,
                    f"Failed to update metadata: {update_result.get('error', 'Unknown error')}",
                )

            return create_success_response({"blob_id": blob_id, "metadata": normalized_metadata})

        except BlobValidationError as e:
            return create_error_response(e.error_code, e.message)
        except Exception as e:
            return create_error_response_from_exception(e, "update_blob_metadata")

    def delete_blob(self, namespace: str, blob_id: str) -> ActionResult:
        try:
            validate_blob_id_format(blob_id)

            if not self.state_service:
                return create_error_response(
                    BlobStorageErrorCode.STORAGE_ERROR.value,
                    "State service not available for metadata deletion",
                )

            metadata = state_ops.get_metadata_from_state(
                self.state_service, self.plugin_name, namespace, blob_id
            )
            if not metadata:
                return create_error_response(
                    BlobStorageErrorCode.BLOB_NOT_FOUND.value, f"Blob not found: {blob_id}"
                )

            blob_path = self._get_blob_path(blob_id)
            if not blob_path.exists():
                return create_error_response(
                    BlobStorageErrorCode.BLOB_NOT_FOUND.value,
                    f"Blob not found at path: {blob_path}",
                )

            blob_path.unlink(missing_ok=True)

            delete_result = self.state_service.delete_records(
                namespace=self.plugin_name,
                query={
                    "table": "metadata",
                    "filters": {"blob_id": blob_id, "plugin_namespace": namespace},
                },
            )
            if delete_result.get("action_status") != "completed":
                pass  # Blob already deleted; metadata orphan is non-fatal

            return create_success_response({"blob_id": blob_id, "deleted": True})

        except BlobValidationError as e:
            return create_error_response(e.error_code, e.message)
        except Exception as e:
            return create_error_response_from_exception(e, "delete_blob")

    def search_blobs(self, namespace: str, metadata_filters: dict[str, Any]) -> ActionResult:
        try:
            validate_search_filters(metadata_filters)

            if not self.state_service:
                return create_error_response(
                    BlobStorageErrorCode.STORAGE_ERROR.value,
                    "State service not available for metadata search",
                )

            records = state_ops.fetch_namespace_records(
                self.state_service, self.plugin_name, namespace
            )
            all_files = search_filters.collect_matching_files(records, metadata_filters)

            limit = metadata_filters.get("limit", 100)
            offset = metadata_filters.get("offset", 0)
            return create_success_response({
                "files": all_files[offset : offset + limit],
                "total_count": len(all_files),
                "limit": limit,
                "offset": offset,
            })

        except BlobValidationError as e:
            return create_error_response(e.error_code, e.message)
        except Exception as e:
            return create_error_response_from_exception(e, "search_files")

    def get_blob_metadata(self, namespace: str, blob_id: str) -> ActionResult:
        try:
            validate_blob_id_format(blob_id)

            if not self.state_service:
                return create_error_response(
                    BlobStorageErrorCode.STORAGE_ERROR.value,
                    "State service not available for metadata retrieval",
                )

            metadata = state_ops.get_metadata_from_state(
                self.state_service, self.plugin_name, namespace, blob_id
            )
            if not metadata:
                return create_error_response(
                    BlobStorageErrorCode.BLOB_NOT_FOUND.value,
                    f"Blob metadata not found: {blob_id}",
                )

            resolved_path = self.resolve_blob_path(f"blob://{blob_id}")
            if resolved_path is None:
                return create_error_response(
                    BlobStorageErrorCode.BLOB_NOT_FOUND.value,
                    f"Blob metadata exists for {blob_id} but its file is missing on disk",
                )

            return create_success_response(
                {"blob_id": blob_id, "metadata": metadata, "resolved_path": resolved_path}
            )

        except BlobValidationError as e:
            return create_error_response(e.error_code, e.message)
        except Exception as e:
            return create_error_response_from_exception(e, "get_blob_metadata")

    def update_metadata(
        self, namespace: str, blob_id: str, metadata: dict[str, Any]
    ) -> ActionResult:
        return self.update_blob_metadata(namespace, blob_id, metadata)

    def get_metadata(self, namespace: str, blob_id: str) -> ActionResult:
        """Backward compatibility alias for get_blob_metadata."""
        return self.get_blob_metadata(namespace, blob_id)
