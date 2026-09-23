"""Claude Code Cloud session-source plugin.

Implements ``LLMSessionSourceInterface + PushedSourceMixin`` over the
``vendor.claude_code`` parser (reused as-is per design v3 §2.3a — the
``/v1/session_ingress/session/<id>`` loglines are byte-equivalent to the
local rollout JSONL shape), plus one ``@platform_process`` verb
``backfill_from_cloud`` that walks the operator's cloud session surface
and dispatches each session envelope through the existing push-mode
importer seam.

Design v3 §2.3a bearer-cell. Phase A discovery
``workbench/2026-06-13_claude_code_web_phase_a_discovery.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
from ananta.core.actions.action_metadata import (
    ContextHandling,
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.config.config_provider import ConfigProvider
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
    EventType,
    IngestMode,
    IngestSourceKind,
    MessageRole,
    NormalizedSessionEvent,
    RawSessionEvent,
    SessionSourceDescriptor,
    SourceVendor,
)
from ananta.llm.session_ledger.vendor import claude_code as vendor
from ananta.llm.session_ledger.vendor.claude_code import (
    PAYLOAD_KIND_MESSAGE,
    PAYLOAD_KIND_SYSTEM,
    PAYLOAD_KIND_TOOL_CALL,
    PAYLOAD_KIND_TOOL_RESULT,
)

from claude_code_cloud_session_source_plugin.walker import (
    ClaudeCodeCloudCredentials,
    ClaudeCodeCloudWalkerError,
    WalkerConfig,
    WalkerReport,
    fetch_and_dispatch_concurrent,
    list_all_session_summaries,
    load_credentials,
)

logger = logging.getLogger(__name__)

_PLUGIN_NAME = "claude_code_cloud_session_source_plugin"
_VERB_NAME = "backfill_from_cloud"
_RESULT_TYPE = "claude_code_cloud_backfill_result"


_ACCOUNT_LABEL_PARAM = ParameterMetadata(
    type=ParameterType.STRING,
    description=(
        "Optional operator-supplied label persisted on the "
        "session_ledger__source row (defaults to 'claude_code_cloud')."
    ),
)
_FORCE_PARAM = ParameterMetadata(
    type=ParameterType.BOOLEAN,
    description=(
        "When True, re-fetch sessions already present in the ledger. "
        "Default False — already-ingested session IDs are skipped via "
        "SET DIFFERENCE against list_sessions(vendor='claude_code')."
    ),
)


def _return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="Backfill summary returned from one walker invocation.",
        properties={
            "status": ParameterMetadata(
                type=ParameterType.STRING,
                description="'completed' | 'auth_expired' | 'rate_limited' | 'failed'.",
            ),
            "listed_count": ParameterMetadata(
                type=ParameterType.INTEGER,
                description="Number of cloud sessions seen on the list endpoint.",
            ),
            "fetched_count": ParameterMetadata(
                type=ParameterType.INTEGER,
                description="Number of sessions fetched and ingested this run.",
            ),
            "skipped_count": ParameterMetadata(
                type=ParameterType.INTEGER,
                description="Number of sessions skipped via SET DIFFERENCE.",
            ),
            "errored_count": ParameterMetadata(
                type=ParameterType.INTEGER,
                description="Number of sessions that errored (per-session fetch or parse).",
            ),
            "errors": ParameterMetadata(
                type=ParameterType.STRING,
                description="Per-session error tokens (joined newline-separated).",
            ),
        },
    )


class ClaudeCodeCloudSessionSourcePlugin(
    PluginBase,
    LLMSessionSourceInterface,
    PushedSourceMixin,
    EdgeProcessProvider,
):
    """Claude Code Cloud bearer-cell walker (PushedSourceMixin).

    The plugin's two contracts are decoupled:

    * **PushedSourceMixin** (``describe`` + ``normalize`` + ``parse_chunk``):
      the importer's ``dispatch_pushed`` seam routes each session envelope
      through ``vendor.claude_code`` (reused as-is) → ``RawSessionEvent``
      → normalize → persist.
    * **EdgeProcessProvider** (``backfill_from_cloud`` verb): the
      operator-fireable backfill primitive — list + SET DIFFERENCE +
      parallel fetch + dispatch. Returns a structured summary envelope.
    """

    name: str = _PLUGIN_NAME

    def __init__(self) -> None:
        super().__init__()
        self.name = _PLUGIN_NAME

    # ------------------------------------------------------------------
    # PluginBase lifecycle
    # ------------------------------------------------------------------

    def prepare_for_readiness(self) -> None:
        self.set_ready()

    def initialize(self, config: dict[str, object]) -> None:
        self.config_provider = ConfigProvider(self.name, config)

    # ------------------------------------------------------------------
    # LLMSessionSourceInterface
    # ------------------------------------------------------------------

    def describe(self) -> SessionSourceDescriptor:
        return SessionSourceDescriptor(
            source_kind=IngestSourceKind.CLAUDE_CODE_CLOUD,
            vendor=SourceVendor.CLAUDE_CODE,
            supported_modes=(IngestMode.PUSHED,),
        )

    def normalize(self, raw: RawSessionEvent) -> NormalizedSessionEvent:
        payload = raw.payload
        kind = _payload_require_str(payload, "kind")
        if kind == PAYLOAD_KIND_MESSAGE:
            return _normalize_message(raw, payload)
        if kind == PAYLOAD_KIND_TOOL_CALL:
            return _normalize_tool_call(raw, payload)
        if kind == PAYLOAD_KIND_TOOL_RESULT:
            return _normalize_tool_result(raw, payload)
        if kind == PAYLOAD_KIND_SYSTEM:
            return _normalize_system(raw, payload)
        raise ValueError(f"claude_code_cloud: unknown payload kind {kind!r}")

    # ------------------------------------------------------------------
    # PushedSourceMixin
    # ------------------------------------------------------------------

    def parse_chunk(self, chunk_text: str) -> Iterator[RawSessionEvent]:
        envelope = _decode_envelope(chunk_text)
        external_session_id = _normalize_cloud_session_id(
            envelope["external_session_id"],
        )
        events = envelope["events"]
        yield from _yield_events_from_cloud_envelope(
            external_session_id=external_session_id, events=events,
        )

    # ------------------------------------------------------------------
    # EdgeProcessProvider
    # ------------------------------------------------------------------

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {
            _VERB_NAME: EdgeProcessDefinition(
                name=_VERB_NAME,
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type=_RESULT_TYPE,
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
        }

    # ------------------------------------------------------------------
    # @platform_process verb
    # ------------------------------------------------------------------

    @platform_process(
        name="backfill_from_cloud",
        is_discoverable=True,
        context_handling=ContextHandling.NONE,
        parameters={
            "account_label": _ACCOUNT_LABEL_PARAM,
            "force": _FORCE_PARAM,
        },
        output_type="object",
        output_description=(
            "Backfill summary — status + listed_count + fetched_count + "
            "skipped_count + errored_count + errors."
        ),
        return_value_schema=_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_long_running=True,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type=_RESULT_TYPE,
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    def backfill_from_cloud(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        del state
        _ = params.get("account_label")
        force = bool(params.get("force") or False)
        try:
            walker_config = self._build_walker_config()
            creds = load_credentials(
                keychain_service=walker_config.keychain_service,
                envelope_path=walker_config.keychain_envelope_path,
            )
        except ClaudeCodeCloudWalkerError as exc:
            return _error(exc.code, str(exc))
        try:
            report = asyncio.run(
                self._run_backfill(
                    walker_config=walker_config,
                    creds=creds,
                    force=force,
                ),
            )
        except ClaudeCodeCloudWalkerError as exc:
            return _error(exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001 — return structured failure
            logger.exception("claude_code_cloud backfill crashed")
            return _error("failed", str(exc))
        return _success(report)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _run_backfill(
        self,
        *,
        walker_config: WalkerConfig,
        creds: ClaudeCodeCloudCredentials,
        force: bool,
    ) -> WalkerReport:
        report = WalkerReport()
        importer = self._require_importer()
        async with httpx.AsyncClient(
            timeout=walker_config.fetch_timeout_seconds,
        ) as client:
            summaries = await list_all_session_summaries(
                client=client, config=walker_config, creds=creds,
            )
            report.listed_count = len(summaries)
            to_fetch, skipped = self._filter_already_ingested(
                summaries=summaries, force=force,
            )
            report.skipped_count = skipped

            def _dispatch(envelope: str) -> None:
                importer.dispatch_pushed(
                    source_kind=IngestSourceKind.CLAUDE_CODE_CLOUD,
                    chunk_text=envelope,
                )

            await fetch_and_dispatch_concurrent(
                summaries=to_fetch,
                config=walker_config,
                creds=creds,
                dispatch_callable=_dispatch,
                client=client,
                report=report,
            )
        return report

    def _filter_already_ingested(
        self,
        *,
        summaries: list[dict[str, Any]],
        force: bool,
    ) -> tuple[list[dict[str, Any]], int]:
        if force:
            return summaries, 0
        ingested = self._ingested_external_session_ids()
        to_fetch: list[dict[str, Any]] = []
        skipped = 0
        for summary in summaries:
            session_id = summary.get("id")
            if isinstance(session_id, str) and (
                _normalize_cloud_session_id(session_id) in ingested
            ):
                skipped += 1
                continue
            to_fetch.append(summary)
        return to_fetch, skipped

    def _ingested_external_session_ids(self) -> set[str]:
        """Set of bare-suffix external_session_ids already in the ledger under
        source_kind='claude_code_cloud'.

        Paginates ``list_sessions`` because the verb caps at 200 rows per call
        and the operator's cloud corpus regularly exceeds that (446+ rows
        when sub-agent threads are counted). Walks pages by decrementing
        ``first_event_until`` to the oldest seen ``first_event_at``; stops when
        a page returns fewer rows than the requested limit. The bare-suffix
        normalization here matches the listing-side normalization in
        ``_filter_already_ingested`` so SET DIFFERENCE works correctly.
        """
        ledger = self._lookup_ledger_service()
        if ledger is None:
            return set()
        list_sessions = getattr(ledger, "list_sessions", None)
        if not callable(list_sessions):
            return set()
        ids: set[str] = set()
        page_limit = 200
        cursor_until: str | None = None
        seen_cursors: set[str] = set()
        while True:
            rows = _list_sessions_page(list_sessions, page_limit, cursor_until)
            if not rows:
                break
            page_ids, oldest = _scan_session_page(rows)
            ids |= page_ids
            if len(rows) < page_limit or oldest is None or oldest in seen_cursors:
                break
            seen_cursors.add(oldest)
            cursor_until = oldest
        return ids

    def _require_importer(self) -> Any:
        ledger = self._lookup_ledger_service()
        if ledger is None:
            raise ClaudeCodeCloudWalkerError(
                "importer_missing",
                "session_ledger_service unavailable; plugin cannot dispatch.",
            )
        importer = getattr(ledger, "importer", None)
        if importer is None:
            raise ClaudeCodeCloudWalkerError(
                "importer_missing",
                "session_ledger_service has no 'importer' attribute.",
            )
        return importer

    def _lookup_ledger_service(self) -> Any:
        if self.orchestrator_ref is None:
            return None
        return self.orchestrator_ref.get_service("session_ledger_service")

    def _build_walker_config(self) -> WalkerConfig:
        provider = self.config_provider
        if provider is None:
            raise ClaudeCodeCloudWalkerError(
                "config_missing",
                "plugin config_provider not bound; cannot build walker config.",
            )
        keychain_service = _require_str_config(
            provider.get("keychain_service"), "keychain_service",
        )
        keychain_envelope_path = _require_str_config(
            provider.get("keychain_envelope_path"), "keychain_envelope_path",
        )
        api_base_url = _require_str_config(
            provider.get("api_base_url"), "api_base_url",
        )
        anthropic_version = _require_str_config(
            provider.get("anthropic_version"), "anthropic_version",
        )
        anthropic_beta = _require_str_config(
            provider.get("anthropic_beta"), "anthropic_beta",
        )
        list_page_limit = _require_positive_int_config(
            provider.get("list_page_limit"), "list_page_limit",
        )
        events_page_limit = _require_positive_int_config(
            provider.get("events_page_limit"), "events_page_limit",
        )
        walker_concurrency = _require_positive_int_config(
            provider.get("walker_concurrency"), "walker_concurrency",
        )
        fetch_timeout_seconds = _require_positive_int_config(
            provider.get("fetch_timeout_seconds"), "fetch_timeout_seconds",
        )
        backoff = _require_backoff_config(
            provider.get("rate_limit_backoff_seconds"),
        )
        return WalkerConfig(
            keychain_service=keychain_service,
            keychain_envelope_path=keychain_envelope_path,
            api_base_url=api_base_url,
            anthropic_version=anthropic_version,
            anthropic_beta=anthropic_beta,
            list_page_limit=list_page_limit,
            events_page_limit=events_page_limit,
            walker_concurrency=walker_concurrency,
            fetch_timeout_seconds=fetch_timeout_seconds,
            rate_limit_backoff_seconds=backoff,
        )


# ───────────────────────────────────────────────────────────────────────────
# Chunk decoding
# ───────────────────────────────────────────────────────────────────────────


_CONVERSATIONAL_EVENT_TYPES = frozenset({"user", "assistant", "system"})
_PLUMBING_EVENT_TYPES = frozenset({"result", "control_request", "control_response"})

# Cloud session IDs surface in two prefix conventions for the same conversation:
# 'cse_X' on the listing API + the operator-side env var
# (``CLAUDE_CODE_REMOTE_SESSION_ID``); 'session_X' on the transcript URL path
# and inside per-event ``payload.session_id``. Anthropic documents the
# substitution at https://code.claude.com/docs/en/claude-code-on-the-web —
# ``${CLAUDE_CODE_REMOTE_SESSION_ID/#cse_/session_}``. Stripping both prefixes
# collapses events that arrive through either namespace into a single ledger
# session keyed by the bare suffix. Local Claude Code JSONL uses UUID-shape
# IDs (no prefix), so the bare suffix is disjoint from any local-source key.
_CLOUD_SESSION_ID_PREFIXES: tuple[str, ...] = ("cse_", "session_")


def _normalize_cloud_session_id(raw: str) -> str:
    for prefix in _CLOUD_SESSION_ID_PREFIXES:
        if raw.startswith(prefix):
            return raw[len(prefix):]
    return raw


def _decode_envelope(chunk_text: str) -> dict[str, Any]:
    try:
        envelope = json.loads(chunk_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"claude_code_cloud: chunk is not valid JSON: {exc}",
        ) from exc
    if not isinstance(envelope, dict):
        raise ValueError(
            "claude_code_cloud: chunk envelope is not a JSON object",
        )
    ext = envelope.get("external_session_id")
    if not isinstance(ext, str) or not ext:
        raise ValueError(
            "claude_code_cloud: chunk envelope missing string 'external_session_id'",
        )
    events = envelope.get("events")
    if not isinstance(events, list):
        raise ValueError(
            "claude_code_cloud: chunk envelope missing list 'events'",
        )
    return envelope


def _yield_events_from_cloud_envelope(
    *,
    external_session_id: str,
    events: list[Any],
) -> Iterator[RawSessionEvent]:
    """Yield raw events from one cloud-session envelope.

    Each cloud event has shape
    ``{event_id, event_type, created_at, payload: {...}, ...}``. The
    ``payload`` dict carries the local-Claude-Code JSONL line shape with
    minor key renames (``session_id`` vs ``sessionId``,
    ``parent_tool_use_id`` vs ``parentUuid``). Conversational event types
    (``user``/``assistant``/``system``) round-trip through
    ``vendor.claude_code.parse_line_data`` after a thin adapter renames
    the keys and surfaces ``created_at`` as ``timestamp`` when the payload
    does not carry one. Plumbing events (``result``, ``control_*``) are
    managed-agents-API observability markers without local-JSONL
    counterparts and are silently skipped.
    """
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("event_type")
        if event_type in _PLUMBING_EVENT_TYPES:
            continue
        if event_type not in _CONVERSATIONAL_EVENT_TYPES:
            logger.debug(
                "claude_code_cloud: skipping unknown event_type %r for session %s",
                event_type, external_session_id,
            )
            continue
        adapted = _adapt_cloud_event_to_local_shape(
            event=event, external_session_id=external_session_id,
        )
        yield from vendor.parse_line_data(adapted)


def _adapt_cloud_event_to_local_shape(
    *,
    event: dict[str, Any],
    external_session_id: str,
) -> dict[str, Any]:
    """Rename + surface fields so ``vendor.claude_code.parse_line_data`` accepts the payload."""
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise ValueError(
            "claude_code_cloud: event missing dict 'payload'",
        )
    adapted: dict[str, Any] = dict(payload)
    _surface_session_id(adapted, external_session_id)
    _surface_timestamp(adapted, event)
    _surface_parent_uuid(adapted)
    return adapted


def _surface_session_id(adapted: dict[str, Any], external_session_id: str) -> None:
    if "sessionId" in adapted:
        adapted["sessionId"] = _normalize_cloud_session_id(adapted["sessionId"])
        return
    inner = adapted.get("session_id")
    adapted["sessionId"] = (
        _normalize_cloud_session_id(inner)
        if isinstance(inner, str) and inner
        else external_session_id
    )


def _surface_timestamp(adapted: dict[str, Any], event: dict[str, Any]) -> None:
    if "timestamp" in adapted:
        return
    created_at = event.get("created_at")
    if isinstance(created_at, str) and created_at:
        adapted["timestamp"] = created_at


def _surface_parent_uuid(adapted: dict[str, Any]) -> None:
    if "parentUuid" in adapted:
        return
    parent = adapted.get("parent_tool_use_id")
    if isinstance(parent, str) and parent:
        adapted["parentUuid"] = parent


# ───────────────────────────────────────────────────────────────────────────
# Row helpers
# ───────────────────────────────────────────────────────────────────────────


def _row_external_session_id(row: Any) -> str | None:
    if isinstance(row, dict):
        ext = row.get("external_session_id")
        if isinstance(ext, str) and ext:
            return ext
        return None
    ext = getattr(row, "external_session_id", None)
    if isinstance(ext, str) and ext:
        return ext
    return None


def _row_first_event_at(row: Any) -> str | None:
    """``first_event_at`` from a session row (dict or attr form), or None."""
    if isinstance(row, dict):
        value = row.get("first_event_at")
    else:
        value = getattr(row, "first_event_at", None)
    return value if isinstance(value, str) else None


def _scan_session_page(rows: list[Any]) -> tuple[set[str], str | None]:
    """External_session_ids on this page plus the oldest ``first_event_at`` seen.

    The oldest value is the next page's ``first_event_until`` cursor under the
    ``first_event_at_desc`` ordering the caller requests.
    """
    ids: set[str] = set()
    oldest: str | None = None
    for row in rows:
        ext_id = _row_external_session_id(row)
        if ext_id:
            ids.add(ext_id)
        fea = _row_first_event_at(row)
        if fea is not None and (oldest is None or fea < oldest):
            oldest = fea
    return ids, oldest


def _list_sessions_page(
    list_sessions: Any, page_limit: int, cursor_until: str | None,
) -> list[Any]:
    """One ``list_sessions`` call for the claude_code_cloud source, normalized
    to a list of rows (empty when the envelope carries no session list)."""
    kwargs: dict[str, Any] = {
        "source_kind": IngestSourceKind.CLAUDE_CODE_CLOUD.value,
        "limit": page_limit,
        "order_by": "first_event_at_desc",
    }
    if cursor_until is not None:
        kwargs["first_event_until"] = cursor_until
    envelope = list_sessions(**kwargs)
    rows = envelope.get("sessions") if isinstance(envelope, dict) else envelope
    return rows if isinstance(rows, list) else []


# ───────────────────────────────────────────────────────────────────────────
# Config validators
# ───────────────────────────────────────────────────────────────────────────


def _require_str_config(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value:
        raise ClaudeCodeCloudWalkerError(
            "config_missing", f"config {key!r} missing or non-string",
        )
    return value


def _require_positive_int_config(value: Any, key: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise ClaudeCodeCloudWalkerError(
            "config_missing", f"config {key!r} must be positive int",
        )
    return value


def _require_backoff_config(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ClaudeCodeCloudWalkerError(
            "config_missing",
            "config 'rate_limit_backoff_seconds' must be non-empty list of ints",
        )
    for item in value:
        if not isinstance(item, int) or item < 0:
            raise ClaudeCodeCloudWalkerError(
                "config_missing",
                "config 'rate_limit_backoff_seconds' entries must be non-negative ints",
            )
    return tuple(value)


# ───────────────────────────────────────────────────────────────────────────
# Envelope helpers
# ───────────────────────────────────────────────────────────────────────────


def _success(report: WalkerReport) -> dict[str, Any]:
    return {
        "action_status": ActionStatus.COMPLETED.value,
        "data": {
            "status": "completed",
            "listed_count": report.listed_count,
            "fetched_count": report.fetched_count,
            "skipped_count": report.skipped_count,
            "errored_count": report.errored_count,
            "errors": "\n".join(report.errors),
        },
        "actions": [],
        "error": None,
        "timestamp": datetime.now(UTC).isoformat(),
    }


def _error(code: str, message: str) -> dict[str, Any]:
    return {
        "action_status": ActionStatus.ERROR.value,
        "data": {
            "status": code,
            "listed_count": 0,
            "fetched_count": 0,
            "skipped_count": 0,
            "errored_count": 0,
            "errors": message,
        },
        "actions": [],
        "error": {"code": code, "message": message, "details": {}},
        "timestamp": datetime.now(UTC).isoformat(),
    }


# ───────────────────────────────────────────────────────────────────────────
# Normalize helpers (lifted from claude_code_filesystem_session_source_plugin
# — the payload shape is identical because both feed through
# vendor.claude_code.parse_line_data, which stamps the same payload kinds)
# ───────────────────────────────────────────────────────────────────────────


def _normalize_message(
    raw: RawSessionEvent, payload: dict[str, object],
) -> NormalizedSessionEvent:
    role_str = _payload_require_str(payload, "role")
    return NormalizedSessionEvent(
        external_session_id=raw.external_session_id,
        event_type=EventType.MESSAGE,
        role=_map_message_role(role_str),
        content_text=_payload_optional_str(payload.get("text")),
        content_json=None,
        event_at=raw.event_at,
        vendor_event_id=raw.vendor_event_id,
        vendor_parent_event_id=raw.vendor_parent_event_id,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
    )


def _normalize_tool_call(
    raw: RawSessionEvent, payload: dict[str, object],
) -> NormalizedSessionEvent:
    tool_name = _payload_require_str(payload, "tool_name")
    tool_use_id = _payload_require_str(payload, "tool_use_id")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        raise ValueError(
            "claude_code_cloud: tool_call payload missing dict 'tool_input'",
        )
    return NormalizedSessionEvent(
        external_session_id=raw.external_session_id,
        event_type=EventType.TOOL_CALL,
        role=None,
        content_text=None,
        content_json={
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
            "input": tool_input,
        },
        event_at=raw.event_at,
        vendor_event_id=raw.vendor_event_id,
        vendor_parent_event_id=raw.vendor_parent_event_id,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
    )


def _normalize_tool_result(
    raw: RawSessionEvent, payload: dict[str, object],
) -> NormalizedSessionEvent:
    return NormalizedSessionEvent(
        external_session_id=raw.external_session_id,
        event_type=EventType.TOOL_RESULT,
        role=MessageRole.TOOL,
        content_text=_payload_optional_str(payload.get("text")) or "",
        content_json=None,
        event_at=raw.event_at,
        vendor_event_id=raw.vendor_event_id,
        vendor_parent_event_id=raw.vendor_parent_event_id,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
    )


def _normalize_system(
    raw: RawSessionEvent, payload: dict[str, object],
) -> NormalizedSessionEvent:
    subtype = _payload_optional_str(payload.get("subtype"))
    return NormalizedSessionEvent(
        external_session_id=raw.external_session_id,
        event_type=EventType.SYSTEM,
        role=MessageRole.SYSTEM,
        content_text=_payload_optional_str(payload.get("text")) or "",
        content_json={"subtype": subtype} if subtype else None,
        event_at=raw.event_at,
        vendor_event_id=raw.vendor_event_id,
        vendor_parent_event_id=raw.vendor_parent_event_id,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
    )


def _map_message_role(role: str) -> MessageRole:
    if role == "user":
        return MessageRole.USER
    if role == "assistant":
        return MessageRole.ASSISTANT
    if role == "system":
        return MessageRole.SYSTEM
    raise ValueError(
        f"claude_code_cloud: cannot map message role {role!r} to MessageRole",
    )


def _payload_require_str(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"claude_code_cloud: payload missing non-empty {field!r}",
        )
    return value


def _payload_optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return None


__all__ = ["ClaudeCodeCloudSessionSourcePlugin"]
