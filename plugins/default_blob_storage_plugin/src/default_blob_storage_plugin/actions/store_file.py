from typing import Any, cast

from ananta.core.domain.status import create_error_response
from ananta.core.plugins.plugin_contracts import ActionResult
from ananta.interfaces.blob_storage_service_interface import BlobStorageServiceInterface


def store_file_action(
    file_service: BlobStorageServiceInterface, parameters: dict[str, Any]
) -> ActionResult:
    namespace = parameters.get("namespace")
    content = parameters.get("content")
    metadata = parameters.get("metadata", {})

    if not namespace:
        return cast(
            ActionResult,
            create_error_response("Namespace is required", "blob_storage.missing_namespace"),
        )

    if content is None:
        return cast(
            ActionResult,
            create_error_response("Blob content is required", "blob_storage.missing_content"),
        )

    if isinstance(content, str):
        str_content = content
        try:
            content = bytes.fromhex(str_content)
        except ValueError:
            # Convert string to bytes if not hex
            content = str_content.encode("utf-8")

    return file_service.store_blob(cast(str, namespace), cast(bytes, content), metadata)
