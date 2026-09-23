"""Claude.ai web export ZIP plugin (M9).

Thin adapter around the vendor parser at
``ananta.llm.session_ledger.vendor.claude_ai_export``. Implements
``PushedSourceMixin`` because operator-side delivery is via the
``ingest_export`` EDGE verb (the retired HTTP route was replaced per
the unified URL-walker design v3 §3+§4).

The ``ingest_export`` verb body lives in :mod:`.ingest_verb`; it stores
the ZIP via ``blob_storage_service``, registers a source row + import
batch via ``session_ledger_service.register_claude_ai_export_source``,
and kicks the importer. From there ``parse_chunk`` walks the archive
per the standard push path.

See ``ananta/src/ananta/llm/session_ledger/vendor/claude_ai_export.py``
for the full schema reference, ROOT_SENTINEL handling, ``extract_text``
fallback chain, and the M9 probe outcomes (attachment representation +
cross-vendor UUID).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ananta.core.actions.action_metadata import (
    ContextHandling,
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.domain.enums import ActionStatus, ProcessorPolicyCategory
from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.edge_process_provider import (
    EdgeProcessDefinition,
    EdgeProcessProvider,
)
from ananta.interfaces.llm_session_source_interface import (
    LLMSessionSourceInterface,
    PushedSourceMixin,
)
from ananta.llm.session_ledger.types import (
    IngestMode,
    IngestSourceKind,
    NormalizedSessionEvent,
    RawSessionEvent,
    SessionSourceDescriptor,
    SourceVendor,
)
from ananta.llm.session_ledger.vendor import claude_ai_export as vendor

from claude_ai_export_session_source_plugin.ingest_verb import (
    IngestExportError,
    perform_ingest,
)

logger = logging.getLogger(__name__)

_INGEST_RESULT_TYPE = "claude_ai_export_ingest_result"


def _ingest_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="Source + batch identifiers for the ingested Claude.ai export.",
        properties={
            "source_id": ParameterMetadata(
                type=ParameterType.STRING,
                description="The session_ledger__source row id.",
            ),
            "batch_id": ParameterMetadata(
                type=ParameterType.STRING,
                description="The session_ledger__import_batch row id (running).",
            ),
            "blob_id": ParameterMetadata(
                type=ParameterType.STRING,
                description="The blob_storage_service blob_id holding the ZIP bytes.",
            ),
        },
    )


def _optional_str(params: dict[str, Any], key: str) -> str | None:
    """Return ``params[key]`` if it is a non-empty string, else ``None``."""
    value = params.get(key)
    if isinstance(value, str) and value:
        return value
    return None


class ClaudeAiExportSessionSourcePlugin(
    PluginBase,
    LLMSessionSourceInterface,
    PushedSourceMixin,
    EdgeProcessProvider,
):
    """Adapter: vendor parser → importer per-event pipeline.

    Also hosts the ``ingest_export`` EDGE verb that replaces the
    retired HTTP upload route.
    """

    def __init__(self) -> None:
        super().__init__()
        self.name = "claude_ai_export_session_source_plugin"

    def prepare_for_readiness(self) -> None:
        self.set_ready()

    # ------------------------------------------------------------------
    # EdgeProcessProvider — ingest_export verb declaration
    # ------------------------------------------------------------------

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {
            "ingest_export": EdgeProcessDefinition(
                name="ingest_export",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type=_INGEST_RESULT_TYPE,
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
        }

    @platform_process(
        name="ingest_export",
        is_discoverable=True,
        context_handling=ContextHandling.NONE,
        parameters={
            "file_path": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "Absolute path to a Claude.ai export ZIP on the solet-side "
                    "filesystem. Use when coordinator + solet share a "
                    "filesystem (LOCAL profile). XOR with content_bytes — exactly "
                    "one must be supplied."
                ),
                required=False,
            ),
            "content_bytes": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "Base64-encoded ZIP bytes inline. Use when coordinator and "
                    "solet do NOT share a filesystem (cloud-solet path). "
                    "XOR with file_path. Hard cap: 100 MiB decoded."
                ),
                required=False,
            ),
            "filename": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "Logical filename for blob metadata + account_label seed. "
                    "Required when content_bytes is supplied; optional when "
                    "file_path is supplied (defaults to basename of the path)."
                ),
                required=False,
            ),
            "account_label": ParameterMetadata(
                type=ParameterType.STRING,
                description=(
                    "Operator-supplied label for the source row. Optional. "
                    "Defaults to filename when omitted."
                ),
                required=False,
            ),
        },
        output_type="object",
        output_description=(
            "Source + batch identifiers for the ingested Claude.ai export: "
            "{source_id, batch_id, blob_id}."
        ),
        return_value_schema=_ingest_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type=_INGEST_RESULT_TYPE,
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    def ingest_export(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Ingest a Claude.ai export ZIP via file_path OR base64 content_bytes."""
        del state
        file_path = _optional_str(params, "file_path")
        content_bytes = _optional_str(params, "content_bytes")
        filename = _optional_str(params, "filename")
        account_label = _optional_str(params, "account_label")
        try:
            payload = perform_ingest(
                blob_storage_service=self._require_blob_storage_service(),
                session_ledger_service=self._require_ledger_service(),
                file_path=file_path,
                content_bytes=content_bytes,
                filename=filename,
                account_label=account_label or filename,
            )
        except IngestExportError as exc:
            return {
                "action_status": ActionStatus.FAILED.value,
                "data": {},
                "actions": [],
                "error": {"code": exc.code, "message": exc.detail},
                "timestamp": datetime.now(UTC).isoformat(),
            }
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": payload,
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def _require_ledger_service(self) -> object:
        if self.orchestrator_ref is None:
            raise RuntimeError(f"{self.name}: orchestrator_ref unavailable")
        ledger = self.orchestrator_ref.get_service("session_ledger_service")
        if ledger is None:
            raise RuntimeError(
                f"{self.name}: session_ledger_service unavailable"
            )
        return ledger

    def _require_blob_storage_service(self) -> object:
        if self.orchestrator_ref is None:
            raise RuntimeError(f"{self.name}: orchestrator_ref unavailable")
        blob = self.orchestrator_ref.get_service("blob_storage_service")
        if blob is None:
            raise RuntimeError(
                f"{self.name}: blob_storage_service unavailable"
            )
        return blob

    def describe(self) -> SessionSourceDescriptor:
        return SessionSourceDescriptor(
            source_kind=IngestSourceKind.CLAUDE_AI_EXPORT,
            vendor=SourceVendor.CLAUDE_AI,
            supported_modes=(IngestMode.PUSHED,),
        )

    def normalize(self, raw: RawSessionEvent) -> NormalizedSessionEvent:
        """Convert RawSessionEvent payload back to a NormalizedSessionEvent.

        The push-path importer (``SessionLedgerImporter.dispatch_pushed``)
        accepts the chunk-text from the upload route, calls
        ``parse_chunk`` → RawSessionEvent → normalize → persist. M9
        treats the chunk-text as the blob_id; the route handler stores
        the ZIP bytes into blob storage and passes the blob_id through.
        """
        from ananta.llm.session_ledger.types import EventType, MessageRole  # noqa: PLC0415

        payload = raw.payload
        kind = payload.get("kind")
        if kind != vendor.PAYLOAD_KIND_MESSAGE:
            raise ValueError(
                f"claude_ai_export: unexpected payload kind {kind!r}; "
                f"only {vendor.PAYLOAD_KIND_MESSAGE!r} is emitted by this source",
            )
        sender = payload.get("sender")
        role = MessageRole.USER
        if sender == "assistant":
            role = MessageRole.ASSISTANT
        content_text = payload.get("content_text")
        text = content_text if isinstance(content_text, str) else ""
        return NormalizedSessionEvent(
            external_session_id=raw.external_session_id,
            event_type=EventType.MESSAGE,
            role=role,
            content_text=text,
            content_json=None,
            event_at=raw.event_at,
            vendor_event_id=raw.vendor_event_id,
            vendor_parent_event_id=raw.vendor_parent_event_id,
            attachment_blob_upload=None,
            attachment_mime_type=None,
            attachment_filename=None,
        )

    def parse_chunk(self, chunk_text: str) -> Iterator[RawSessionEvent]:
        """Parse the chunk-text (= blob_id of a stored ZIP) into RawSessionEvents.

        The route handler stores the ZIP into blob storage and hands
        ``blob_id`` to ``ingest_raw_chunk`` as ``chunk_text``. The plugin
        materializes the blob path from the service's blob_storage
        binding (caller-supplied at registration). For the smoke + the
        upload-handler integration, the chunk_text IS the literal ZIP
        file path on disk (the route handler keeps a stable path during
        the persistence flow per spec §10.10.1).
        """
        zip_path = Path(chunk_text)
        for payload in vendor.parse_export_zip(zip_path):
            for msg in payload.messages:
                yield vendor.to_raw_event(msg, payload.session_ref.external_session_id)


__all__ = ["ClaudeAiExportSessionSourcePlugin"]
