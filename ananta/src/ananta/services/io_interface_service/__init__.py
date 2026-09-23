"""IO Interface Service - Routes messages to IO interfaces by session namespace.

This service provides session-aware message routing to IO interface plugins.
The model addresses IO plugins directly (``plugin::<namespace>::post_message``).
The system prompt defines ``POST_MESSAGE`` as the set of available IO targets.

The IOInterfaceRegistry maps namespace → plugin for dispatch.
"""

from __future__ import annotations

import logging
from typing import Any

from ananta.core.domain.enums import ActionStatus
from ananta.core.state.async_job_manager import AsyncJobManager
from ananta.interfaces.io_capabilities import IOCapability

from .interfaces.public import IOInterfaceServiceAPI
from .registry import IOInterfaceRegistry

logger = logging.getLogger(__name__)


class IOInterfaceService(IOInterfaceServiceAPI):
    """Routes messages to IO interfaces based on session namespace.

    This service acts as a dispatch facade, enabling session-aware routing
    without hardcoding plugin names. It looks up the session's namespace
    to determine which IO interface plugin should receive the message.
    """

    def __init__(
        self,
        registry: IOInterfaceRegistry,
        state_service: Any,
        app_home: str,
        async_job_manager: AsyncJobManager | None = None,
    ) -> None:
        """Initialize IOInterfaceService.

        Args:
            registry: Registry of IO interface plugins by namespace
            state_service: State service for session lookups
            app_home: Application home directory path
        """
        self.registry = registry
        self.state_service = state_service
        self.app_home = app_home
        self.async_job_manager = async_job_manager

    def _get_session(self, session_id: str) -> dict[str, Any] | None:
        """Look up session by ID.

        Args:
            session_id: Session ID to look up

        Returns:
            Session dict if found, None otherwise
        """
        result = self.state_service.read_state(
            namespace="core",
            query={"table": "sessions", "filters": {"id": session_id}},
        )

        # Validate ActionResult structure
        if not isinstance(result, dict) or "action_status" not in result:
            logger.error(f"Session lookup returned invalid ActionResult: {type(result)}")
            return None

        if result.get("action_status") != ActionStatus.COMPLETED.value:
            logger.error(f"Session lookup failed: {result.get('error')}")
            return None

        # Extract records from ActionResult
        # State plugin returns: {"data": {"records": [...], "count": N, ...}}
        data = result.get("data")
        if not isinstance(data, dict):
            logger.error(f"Session lookup returned invalid data structure: {type(data)}")
            return None

        records = data.get("records")
        if not isinstance(records, list):
            logger.error(f"Session lookup missing 'records' in data: {data.keys()}")
            return None

        if len(records) == 0:
            return None  # No matching session found

        first_record = records[0]
        if not isinstance(first_record, dict):
            logger.error(f"Session record is not a dict: {type(first_record)}")
            return None
        return first_record

    def post_message(
        self,
        session_id: str,
        message: str,
        attachments: list[dict[str, object]] | None = None,
        job_result_ref: str | None = None,
    ) -> dict[str, Any]:
        """Route message to appropriate IO interface based on session namespace.

        When job_result_ref is provided and no explicit attachments, this method
        uses capability-aware routing: plugins with FILE_UPLOAD capability get
        attachments, others get text-based blob references.

        Args:
            session_id: Session ID determining which interface to route to
            message: Message content to send
            attachments: Explicit attachments to include (bypasses capability check)
            job_result_ref: Job ID to derive attachments from (uses capability check)

        Returns:
            ActionResult dict from the underlying IO interface plugin
        """
        # If job_result_ref is provided without explicit attachments,
        # use deliver_artifact for capability-aware routing
        if job_result_ref and not attachments:
            return self.deliver_artifact(session_id, job_result_ref, message)

        # Otherwise, proceed with direct attachment delivery
        # 1. Look up session
        session = self._get_session(session_id)
        if not session:
            logger.error(f"Session not found: {session_id}")
            return {
                "action_status": ActionStatus.ERROR.value,
                "error": f"Session not found: {session_id}",
            }

        # 2. Extract routing info (namespace)
        namespace = session.get("namespace")
        if not namespace:
            logger.error(f"Session {session_id} has no namespace")
            return {
                "action_status": ActionStatus.ERROR.value,
                "error": f"Session {session_id} has no namespace",
            }

        # 3. Resolve plugin by namespace
        logger.debug(f"Session {session_id} namespace={namespace}")
        plugin = self.registry.resolve(namespace)
        if not plugin:
            logger.error(
                f"No IO plugin registered for namespace: {namespace}, session_id={session_id}"
            )
            return {
                "action_status": ActionStatus.ERROR.value,
                "error": f"No IO plugin registered for namespace: {namespace}",
            }

        # 4. Prepare explicit attachments if provided
        try:
            normalized_attachments = self._prepare_attachments(attachments, job_result_ref=None)
        except Exception as exc:  # pragma: no cover
            logger.error("Attachment preparation failed: %s", exc, exc_info=True)
            return {
                "action_status": ActionStatus.ERROR.value,
                "error": f"Attachment preparation failed: {exc}",
            }

        payload: dict[str, object] = {"message": message}
        if normalized_attachments:
            payload["attachments"] = normalized_attachments

        try:
            result = plugin.post_message(
                params=payload,
                state={"session_id": session_id},
            )
            return result
        except Exception as e:
            logger.error(f"Error posting message via {namespace}: {e}", exc_info=True)
            return {
                "action_status": ActionStatus.ERROR.value,
                "error": f"Error posting message via {namespace}: {e}",
            }

    def deliver_artifact(
        self,
        session_id: str,
        job_result_ref: str,
        message: str | None = None,
    ) -> dict[str, Any]:
        """Capability-aware artifact delivery.

        Checks if the target IO interface supports file/image upload and delivers
        appropriately - either as a file attachment or as a text reference.

        Args:
            session_id: Session ID determining which interface to route to
            job_result_ref: Reference to async job containing artifact data
            message: Optional accompanying text message

        Returns:
            ActionResult dict with delivery_mode indicating how it was delivered
        """
        # 1. Look up session
        session = self._get_session(session_id)
        if not session:
            logger.error(f"Session not found for artifact delivery: {session_id}")
            return {
                "action_status": ActionStatus.ERROR.value,
                "error": f"Session not found: {session_id}",
            }

        # 2. Get namespace and plugin
        namespace = session.get("namespace")
        if not namespace:
            logger.error(f"Session {session_id} has no namespace")
            return {
                "action_status": ActionStatus.ERROR.value,
                "error": f"Session {session_id} has no namespace",
            }

        plugin = self.registry.resolve(namespace)
        if not plugin:
            logger.error(f"No IO plugin for namespace: {namespace}")
            return {
                "action_status": ActionStatus.ERROR.value,
                "error": f"No IO plugin registered for namespace: {namespace}",
            }

        # 3. Check capabilities
        capabilities = plugin.get_supported_capabilities()
        supports_file_upload = (
            IOCapability.FILE_UPLOAD in capabilities or IOCapability.IMAGE_UPLOAD in capabilities
        )

        try:
            if supports_file_upload:
                # 4a. Full attachment delivery - prepare attachments directly
                logger.debug("Delivering artifact via file upload to %s", namespace)
                attachments = self._prepare_attachments(None, job_result_ref=job_result_ref)

                payload: dict[str, object] = {"message": message or ""}
                if attachments:
                    payload["attachments"] = attachments

                result = plugin.post_message(
                    params=payload,
                    state={"session_id": session_id},
                )
                result["delivery_mode"] = "file_upload"
                return result
            else:
                # 4b. Text fallback with blob references
                logger.debug(f"Delivering artifact via text reference to {namespace}")
                blob_refs = self._extract_blob_refs_from_job(job_result_ref)
                fallback_msg = self._format_text_fallback(message, blob_refs)
                result = self.post_message(session_id, fallback_msg)
                result["delivery_mode"] = "text_reference"
                return result

        except Exception as e:
            # Attachment preparation failed — deliver message without attachments
            # rather than failing the entire post_message action
            logger.warning(
                "Artifact preparation failed for job %s, delivering message only: %s",
                job_result_ref, e,
            )
            result = plugin.post_message(
                params={"message": message or ""},
                state={"session_id": session_id},
            )
            result["delivery_mode"] = "message_only"
            return result

    def _extract_blob_refs_from_job(self, job_id: str) -> list[str]:
        """Extract blob references from a job's artifacts."""
        if not self.async_job_manager:
            return []

        try:
            payload = self._get_job_payload(job_id)
            if not payload:
                return []

            blob_refs: list[str] = []
            self._collect_blob_ids_from_list(payload.get("artifacts"), blob_refs)
            self._collect_blob_ids_from_list(payload.get("images"), blob_refs)
            return blob_refs
        except Exception as e:
            logger.error(f"Failed to extract blob refs from job {job_id}: {e}")
            return []

    def _get_job_payload(self, job_id: str) -> dict[str, object] | None:
        """Get payload dict from job result."""
        assert self.async_job_manager is not None, "async_job_manager required for _get_job_payload"
        payload_result = self.async_job_manager.get_job_payload(job_id, "result")
        if payload_result.get("action_status") != ActionStatus.COMPLETED.value:
            return None

        data = payload_result.get("data") or {}
        if not isinstance(data, dict):
            return None

        payload = data.get("payload") or {}
        return payload if isinstance(payload, dict) else None

    def _collect_blob_ids_from_list(self, items: object, blob_refs: list[str]) -> None:
        """Collect blob_id values from a list of dicts into blob_refs."""
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, dict):
                blob_id = item.get("blob_id")
                if isinstance(blob_id, str) and blob_id:
                    blob_refs.append(blob_id)

    def _format_text_fallback(self, message: str | None, blob_refs: list[str]) -> str:
        """Format a text message with blob references for text-only interfaces."""
        parts: list[str] = []

        if message:
            parts.append(message)

        if blob_refs:
            parts.append("\n\nGenerated artifacts:")
            for i, blob_ref in enumerate(blob_refs, 1):
                # Ensure blob:// prefix
                if not blob_ref.startswith("blob://"):
                    blob_ref = f"blob://{blob_ref}"
                parts.append(f"  {i}. {blob_ref}")

        return "\n".join(parts) if parts else "Artifact delivery completed."

    def _prepare_attachments(
        self,
        attachments: list[dict[str, object]] | None,
        job_result_ref: str | None,
    ) -> list[dict[str, object]]:
        """Validate explicit attachments or derive them from async job payload."""
        if attachments is not None:
            return [self._validate_attachment_structure(item) for item in attachments]

        if not job_result_ref:
            return []

        derived = self._build_attachments_from_job(job_result_ref)
        if not derived:
            # No artifacts in job - log warning but don't fail, message will be sent without attachments
            logger.error(
                f"No artifacts found for job {job_result_ref}, sending message without attachments"
            )
            return []
        return derived

    def _build_attachments_from_job(self, job_id: str) -> list[dict[str, object]]:
        """Fetch async job payload and convert artifacts/images to attachment dicts."""
        if not self.async_job_manager:
            raise RuntimeError("AsyncJobManager not configured for IOInterfaceService")

        payload = self._fetch_job_payload(job_id)

        namespace = self._resolve_job_namespace(job_id)
        attachments = self._extract_attachments_from_payload(payload, namespace)

        logger.debug(f"Extracted {len(attachments)} attachments from job {job_id}")

        return [self._validate_attachment_structure(item, namespace) for item in attachments]

    def _fetch_job_payload(self, job_id: str) -> dict[str, object]:
        """Fetch and validate job payload from async job manager."""
        # async_job_manager is guaranteed non-None by caller (_build_attachments_from_job)
        assert self.async_job_manager is not None
        payload_result = self.async_job_manager.get_job_payload(job_id, "result")
        logger.debug(
            f"Job payload result for {job_id}: action_status={payload_result.get('action_status')}"
        )

        if payload_result.get("action_status") != ActionStatus.COMPLETED.value:
            logger.error(f"Unable to load job payload for {job_id}: {payload_result.get('error')}")
            raise ValueError(f"Unable to load job payload for {job_id}")

        data = payload_result.get("data") or {}
        if not isinstance(data, dict):
            raise ValueError(f"Job payload for {job_id} missing data")

        payload = data.get("payload") or {}
        if not isinstance(payload, dict):
            raise ValueError(f"Job payload for {job_id} malformed")

        return payload

    def _log_payload_contents(self, job_id: str, payload: dict[str, object]) -> None:
        """Log payload structure for debugging."""
        logger.debug(f"Job {job_id} payload keys: {list(payload.keys()) if payload else 'empty'}")
        artifacts = payload.get("artifacts")
        if isinstance(artifacts, list):
            logger.debug(f"Job {job_id} has {len(artifacts)} artifacts")
        images = payload.get("images")
        if isinstance(images, list):
            logger.debug(f"Job {job_id} has {len(images)} images")

    def _resolve_job_namespace(self, job_id: str) -> str:
        """Resolve namespace (plugin name) responsible for the async job."""
        if not self.async_job_manager:
            return "unknown"

        job_result = self.async_job_manager.get_job(job_id)
        if job_result.get("action_status") != ActionStatus.COMPLETED.value:
            return "unknown"

        data = job_result.get("data") or {}
        job = data.get("job") if isinstance(data, dict) else None
        if not isinstance(job, dict):
            return "unknown"

        provider_name = job.get("provider_name")
        if isinstance(provider_name, str) and provider_name:
            namespace = provider_name.split(".", 1)[0]
            if namespace:
                return namespace

        return "unknown"

    def _extract_attachments_from_payload(
        self, payload: dict[str, object], namespace: str
    ) -> list[dict[str, object]]:
        """Extract standardized attachment dicts from stored job payload."""
        artifacts = payload.get("artifacts")
        attachments: list[dict[str, object]] = []

        if isinstance(artifacts, list) and artifacts:
            for artifact in artifacts:
                if isinstance(artifact, dict):
                    normalized = dict(artifact)
                    normalized.setdefault("namespace", namespace)
                    attachments.append(normalized)
            if attachments:
                return attachments

        images = payload.get("images")
        if isinstance(images, list) and images:
            attachments.extend(self._convert_images_to_attachments(images, payload, namespace))

        return attachments

    def _convert_images_to_attachments(
        self,
        images: list[dict[str, object]],
        payload: dict[str, object],
        namespace: str,
    ) -> list[dict[str, object]]:
        """Convert legacy image payloads into attachment dicts."""
        description = self._extract_image_description(payload)
        tags = ["image_generation", namespace]

        attachments: list[dict[str, object]] = []
        for idx, image in enumerate(images):
            attachment = self._convert_single_image(image, idx, namespace, description, tags)
            if attachment:
                attachments.append(attachment)

        return attachments

    def _extract_image_description(self, payload: dict[str, object]) -> str:
        """Extract description from payload prompt."""
        prompt = payload.get("prompt")
        if isinstance(prompt, str) and prompt:
            return f"Prompt: {prompt[:200]}"
        return "Generated image"

    def _convert_single_image(
        self,
        image: object,
        idx: int,
        namespace: str,
        description: str,
        tags: list[str],
    ) -> dict[str, object] | None:
        """Convert a single image dict to attachment format."""
        if not isinstance(image, dict):
            return None

        blob_id = image.get("blob_id")
        if not isinstance(blob_id, str) or not blob_id:
            return None

        return {
            "namespace": namespace,
            "artifact_type": "image",
            "blob_id": blob_id,
            "media_type": self._infer_media_type(image),
            "size_bytes": self._coerce_int(image.get("file_size")),
            "caption": self._get_image_caption(image, idx),
            "description": description,
            "tags": tags,
            "additional_metadata": self._extract_image_metadata(image),
        }

    def _get_image_caption(self, image: dict[str, object], idx: int) -> str:
        """Get caption from image or generate default."""
        caption = image.get("caption")
        if isinstance(caption, str) and caption:
            return caption
        return f"Image {idx + 1}"

    def _extract_image_metadata(self, image: dict[str, object]) -> dict[str, object]:
        """Extract non-None metadata fields from image."""
        metadata = {
            "image_blob_key": image.get("image_blob_key"),
            "quality_score": image.get("quality_score"),
            "quality_passed": image.get("quality_passed"),
            "original_filename": image.get("original_filename"),
        }
        return {k: v for k, v in metadata.items() if v is not None}

    def _infer_media_type(self, image: dict[str, object]) -> str:
        """Derive media type from legacy image payload."""
        mime_type = image.get("mime_type")
        if isinstance(mime_type, str) and mime_type:
            return mime_type

        fmt = image.get("format")
        if isinstance(fmt, str) and fmt:
            fmt = fmt.lower()
            if "/" in fmt:
                return fmt
            return f"image/{fmt}"

        return "application/octet-stream"

    def _coerce_int(self, value: object) -> int:
        """Best-effort conversion to integer bytes."""
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return 0

    def _validate_attachment_structure(
        self, attachment: dict[str, object], namespace_fallback: str | None = None
    ) -> dict[str, object]:
        """Ensure attachment dict adheres to canonical schema."""
        normalized = dict(attachment)
        self._validate_attachment_namespace(normalized, namespace_fallback)
        self._validate_attachment_required_fields(normalized)
        self._validate_attachment_size_bytes(normalized)
        self._normalize_attachment_tags(normalized)
        addl_metadata = self._normalize_attachment_metadata(normalized)
        normalized["blob_id"] = self._ensure_blob_url(normalized.get("blob_id"), addl_metadata)

        return normalized

    def _validate_attachment_namespace(
        self, normalized: dict[str, object], namespace_fallback: str | None
    ) -> None:
        """Validate and set namespace field."""
        namespace = normalized.get("namespace") or namespace_fallback
        if not isinstance(namespace, str) or not namespace:
            raise ValueError("Attachment missing namespace")
        normalized["namespace"] = namespace

    def _validate_attachment_required_fields(self, normalized: dict[str, object]) -> None:
        """Validate required string fields."""
        for key in ("artifact_type", "media_type"):
            if key not in normalized or not isinstance(normalized[key], str) or not normalized[key]:
                raise ValueError(f"Attachment missing required field '{key}'")

    def _validate_attachment_size_bytes(self, normalized: dict[str, object]) -> None:
        """Validate and normalize size_bytes field."""
        size_bytes = normalized.get("size_bytes")
        # Coerce size_bytes to int (handles string numbers, floats, etc.)
        normalized["size_bytes"] = self._coerce_int(size_bytes)

    def _normalize_attachment_tags(self, normalized: dict[str, object]) -> None:
        """Normalize tags field to list of strings."""
        tags = normalized.get("tags")
        if tags is None:
            normalized["tags"] = []
        elif not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError("Attachment tags must be a list of strings")

    def _normalize_attachment_metadata(self, normalized: dict[str, object]) -> dict[str, object]:
        """Normalize additional_metadata field, auto-populating required fields.

        Consumer conversion (prepare_consumer_attachments) requires 'name' and
        'external_id' in additional_metadata.  When these are absent — common for
        attachments derived from async-job artifacts — derive them from the
        attachment's own blob_id and filename so the consumer pipeline never fails.
        """
        addl_metadata = normalized.get("additional_metadata")
        if addl_metadata is None:
            addl_metadata = {}
        elif not isinstance(addl_metadata, dict):
            raise ValueError("additional_metadata must be an object")

        # Derive external_id from raw blob_id (before blob:// normalization)
        if "external_id" not in addl_metadata:
            blob_id = normalized.get("blob_id")
            if isinstance(blob_id, str) and blob_id:
                addl_metadata["external_id"] = blob_id.removeprefix("blob://")

        # Derive name from filename
        if "name" not in addl_metadata:
            filename = normalized.get("filename")
            if isinstance(filename, str) and filename:
                addl_metadata["name"] = filename

        normalized["additional_metadata"] = addl_metadata
        return addl_metadata

    def _ensure_blob_url(
        self,
        blob_identifier: object,
        metadata: dict[str, object] | None,
    ) -> str:
        """Normalize blob identifiers so plugins always receive blob:// URLs."""
        if self._is_blob_url(blob_identifier):
            return blob_identifier  # type: ignore[return-value]

        candidate = self._find_blob_url_in_metadata(metadata)
        if not candidate:
            candidate = self._build_blob_url(blob_identifier)

        if not candidate:
            raise ValueError("Attachment missing valid blob reference")

        return candidate

    def _is_blob_url(self, value: object) -> bool:
        """Check if value is already a blob:// URL."""
        return isinstance(value, str) and value.startswith("blob://")

    def _find_blob_url_in_metadata(self, metadata: dict[str, object] | None) -> str | None:
        """Extract blob URL from metadata if present."""
        if not isinstance(metadata, dict):
            return None

        blob_url = metadata.get("image_blob_key") or metadata.get("blob_url")
        if self._is_blob_url(blob_url):
            return blob_url  # type: ignore[return-value]
        return None

    def _build_blob_url(self, blob_identifier: object) -> str | None:
        """Build blob:// URL from identifier."""
        if isinstance(blob_identifier, str) and blob_identifier:
            return f"blob://{blob_identifier}"
        return None


__all__ = ["IOInterfaceService", "IOInterfaceRegistry", "IOInterfaceServiceAPI"]
