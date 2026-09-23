from typing import Any, cast

from ananta.core.domain.status import create_error_response
from ananta.core.plugins.plugin_contracts import ActionResult
from ananta.interfaces.blob_storage_service_interface import BlobStorageServiceInterface


def search_files_action(
    file_service: BlobStorageServiceInterface, parameters: dict[str, Any]
) -> ActionResult:
    namespace = parameters.get("namespace")
    filters = parameters.get("filters", {})

    if not namespace:
        return cast(
            ActionResult,
            create_error_response("Namespace is required", "blob_storage.missing_namespace"),
        )

    return file_service.search_blobs(cast(str, namespace), filters)
