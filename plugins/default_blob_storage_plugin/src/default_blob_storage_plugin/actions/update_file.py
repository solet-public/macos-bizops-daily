from typing import Any, cast

from ananta.core.domain.status import create_error_response
from ananta.core.plugins.plugin_contracts import ActionResult
from ananta.interfaces.blob_storage_service_interface import BlobStorageServiceInterface


def update_file_action(
    file_service: BlobStorageServiceInterface, parameters: dict[str, Any]
) -> ActionResult:
    namespace = parameters.get("namespace")
    blob_id = parameters.get("blob_id")
    parameters.get("metadata", {})

    if not namespace:
        return cast(
            ActionResult,
            create_error_response("Namespace is required", "blob_storage.missing_namespace"),
        )

    if not blob_id:
        return cast(
            ActionResult,
            create_error_response("Blob ID is required", "blob_storage.missing_blob_id"),
        )

    return file_service.get_blob_metadata(cast(str, namespace), cast(str, blob_id))
