"""Platform-call surface backing the merged bridge plugin's HTTP routes.

A thin orchestration façade around the platform services that the
former ``claude_code_channel_plugin`` reached through ``orchestrator_ref``.
Every public method returns a plain ``dict`` ready for JSON serialization
on the happy path and raises :class:`BridgeError` (carrying a ``.code``
attribute drawn from the ``bridge.*`` namespace) on failure so the
``http_routes`` layer can map the token to an HTTP status uniformly.

Ported from ``claude_code_channel_plugin.plugin`` during the bridge-
consolidation work — see
``workbench/2026-05-16_codex_mcp_channel_and_inter_agent_outstanding_work.md``
sub-phase 2f for the pickup contract.  All references to ``self.<service>``
were replaced with constructor-injected handles so this class has no
implicit coupling to a plugin instance.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from ananta.core.actions.action_factory import UNKNOWN_ARGUMENTS_ERROR_CODE
from ananta.core.domain.enums import ActionStatus
from ananta.core.result_processing import ErrorProcessorKind, ResultProcessorKind

from .constants import PLUGIN_NAME
from .process_exposure import (
    ProcessExportPolicy,
    filter_discovery_payload,
    is_valid_process_key_shape,
)
from .role_binding_store import list_roles_for_agent_instance

if TYPE_CHECKING:
    from collections.abc import Callable

    from .bridge_sessions import BridgeSessionManager
    from .models import BridgeSessionState
    from .peer_registry import PeerRegistry

logger = logging.getLogger(__name__)


# Bridge-namespace error tokens.  Mapped to HTTP status codes by the
# http_routes layer; surfaced verbatim to MCP clients on the error path.
ERR_BRIDGE_DISABLED: Final[str] = "bridge.process_export_disabled"
ERR_INVALID_PROCESS_KEY: Final[str] = "bridge.invalid_process_key"
ERR_PROCESS_NOT_ALLOWED: Final[str] = "bridge.process_not_allowed"
ERR_NO_BRIDGE: Final[str] = "bridge.no_active_bridge"
ERR_DISCOVERY_UNAVAILABLE: Final[str] = "bridge.discovery_unavailable"
ERR_STATE_UNAVAILABLE: Final[str] = "bridge.state_service_unavailable"
ERR_BLOB_UNAVAILABLE: Final[str] = "bridge.blob_storage_unavailable"
ERR_BLOB_NOT_FOUND: Final[str] = "bridge.blob_not_found"
ERR_PROCESS_CALL_FAILED: Final[str] = "bridge.process_call_failed"
ERR_ACTION_RESULT_NOT_FOUND: Final[str] = "bridge.action_result_not_found"
ERR_DEPENDENCIES_NOT_READY: Final[str] = "bridge.dependencies_not_ready"
ERR_ATTACHMENT_MISSING: Final[str] = "bridge.attachment_missing"

# Process keys for the bridge-delivery EDGE_SINK pair.  Kept here (not in
# constants.py) because the dispatcher contract (process_call's async
# completion path) is local to the bridge surface.
_DELIVER_RESULT_PROCESS_KEY: Final[str] = (
    f"plugin::{PLUGIN_NAME}::deliver_result"
)
_DELIVER_ERROR_PROCESS_KEY: Final[str] = (
    f"plugin::{PLUGIN_NAME}::deliver_error"
)

_PROCESS_SEARCH_DEFAULT_RESULTS: Final[int] = 10
_PROCESS_SEARCH_MAX_RESULTS: Final[int] = 50
_QUERY_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9]+")

COMPLETION_ROUTE_ROLE_KEY: Final[str] = "completion_route_role"
"""Flow ``trigger_data`` key naming where this flow's job completion is delivered.

Its READER is ``ananta.core.state.job_completion_route`` (which restates the
literal, matching the ``bridge_process_call`` precedent in
``job_completion_reach``); this module is its sole WRITER.

Deliberately NOT a member of the ``caller_attribution_*`` family, though it is
stamped from the same resolved value at the same instant. That family is
documented as single-consumer — read only by the send verbs' sender-stamping —
so that an attributed CLI call can never re-point a flow's inference vertex, and
a second reader would spend a bright line that is worth more intact than the one
key it saves. The two also carry DIFFERENT predicates: attribution records who
called, this records where a completion should land. They coincide today and are
free to diverge (a caller routing completions to a coordinator lane, or opting
out of push delivery, changes one and not the other).

ABSENT means unaddressable and is never guessed: written only when a durable
role was actually resolved, so completion delivery reads presence as permission.
"""


def _role_for_instance(
    state_service: Any, agent_instance_id: str, *, purpose: str,
) -> str:
    """The durable role held by ``agent_instance_id``, or "" — degrade-silent.

    Single lookup shared by the two role tags a ``process_call`` flow carries:
    the originating bridge's own role (``inference_vertex_role``) and, for an
    unregistered caller, the role of the session its §34.6 attribution key
    resolved to (``caller_attribution_role``). ``purpose`` names the caller in
    the degrade log so the two stay distinguishable in a fault trace.

    Module-level rather than a method so it depends on the state service alone
    — the surface's own methods are bound onto minimal duck-typed harnesses in
    the smokes, and a method-to-method call would couple those to the whole
    class.
    """
    if state_service is None or not agent_instance_id:
        return ""
    try:
        roles = list_roles_for_agent_instance(state_service, agent_instance_id)
    except Exception:  # noqa: BLE001 — degrade to roleless; never break dispatch
        logger.warning(
            "role lookup for %s failed (agent_instance_id=%s); degrading to "
            "roleless tag — process_call dispatch proceeds",
            purpose,
            agent_instance_id,
            exc_info=True,
        )
        return ""
    return roles[0] if roles else ""


class BridgeError(Exception):
    """Failure raised by :class:`PlatformSurface` methods.

    The ``code`` attribute always carries a stable ``bridge.*`` token so
    the HTTP layer can map the failure to a status code (and the MCP
    client receives the same token verbatim).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code: str = code
        self.message: str = message


@dataclass(frozen=True, slots=True)
class DownloadedBlob:
    """In-memory blob payload returned by :meth:`PlatformSurface.download`."""

    content: bytes
    filename: str
    mime_type: str


@dataclass(frozen=True, slots=True)
class CallerAttribution:
    """§34.6: SERVER-DERIVED sender provenance for an unregistered caller.

    An unregistered caller (the local CLI's one-shot bridge) asserts only an
    opaque ``agent_session_id``; every field here is read out of the peer
    registry row that key resolved to, never out of the request. All-empty is
    the honest "could not resolve" state, and the send path treats it exactly
    as it treats a caller that supplied nothing — the system sentinel.
    """

    agent_id: str = ""
    agent_instance_id: str = ""
    session_label: str = ""
    role: str = ""

    def as_trigger_fields(self) -> dict[str, object]:
        """The ``caller_attribution_*`` keys for a flow's ``trigger_data``.

        Deliberately a SEPARATE key family from ``inference_vertex_*``: that
        one is consumed by the inference-vertex resolver to bind a per-bridge
        ``SessionInferenceProvider``, so reusing it would route a CLI-triggered
        flow's INFERENCE to the attributed session — far outside attribution.
        """
        return {
            "caller_attribution_agent_id": self.agent_id,
            "caller_attribution_instance_id": self.agent_instance_id,
            "caller_attribution_label": self.session_label,
            "caller_attribution_role": self.role,
        }


class PlatformSurface:
    """Façade over the platform services the bridge HTTP layer needs.

    Constructor receives every dependency by name; nothing is resolved
    lazily through an orchestrator reference.  Methods are deliberately
    small and return plain dicts so the FastAPI handlers stay thin.
    """

    def __init__(
        self,
        *,
        action_factory: Any,
        flow_manager: Any,
        compilation_context_builder: Any,
        bridge_manager: BridgeSessionManager,
        peer_registry: PeerRegistry | None = None,
        process_registry: dict[str, object] | None = None,
        discovery_service: Any | None = None,
        state_service: Any | None = None,
        blob_storage_service: Any | None = None,
        memory_service: Any | None = None,
        plugin_manager: Any | None = None,
        export_policy: ProcessExportPolicy | None = None,
        max_message_chars: int = 120_000,
    ) -> None:
        self._action_factory = action_factory
        self._flow_manager = flow_manager
        self._compilation_context_builder = compilation_context_builder
        self._bridge_manager = bridge_manager
        self._peer_registry = peer_registry
        self._process_registry = process_registry
        self._discovery_service = discovery_service
        self._state_service = state_service
        self._blob_storage_service = blob_storage_service
        self._memory_service = memory_service
        self._plugin_manager = plugin_manager
        self._export_policy = export_policy or ProcessExportPolicy(enabled=False)
        self._max_message_chars = max_message_chars
        # B1 Finding-B: vault ``is_operator_equivalent`` lookup, wired post-vault
        # via :meth:`set_operator_equivalent_check`. None until wired (test /
        # pre-readiness), which resolves to non-operator — the safe default.
        self._operator_equivalent_check: Callable[[str], bool] | None = None

    # ------------------------------------------------------------------
    # process_search / process_schema — discovery surface
    # ------------------------------------------------------------------

    def process_search(
        self,
        query: str,
        max_results: int = _PROCESS_SEARCH_DEFAULT_RESULTS,
        *,
        bridge_id: str | None = None,
    ) -> dict[str, Any]:
        """Embedding-search the process registry, filtered by export policy.

        M5 §14.5: when ``bridge_id`` is supplied, the result set is
        additionally filtered against the bridge session's per-session
        allowlist. Out-of-allowlist process_keys are dropped from
        search results — never surfaced as "exists but you can't call
        it" (information leak prevention).
        """
        self._require_export_enabled()
        if not query.strip():
            raise BridgeError(
                ERR_INVALID_PROCESS_KEY,
                "query must be a non-empty string",
            )
        clamped = max(1, min(int(max_results), _PROCESS_SEARCH_MAX_RESULTS))
        discovery = self._discovery_service
        if discovery is None:
            raise BridgeError(
                ERR_DISCOVERY_UNAVAILABLE,
                "Discovery service is not available",
            )
        search_limit = clamped
        bridge = None
        if bridge_id is not None:
            bridge = self._resolve_bridge(bridge_id)
            if bridge.client_id:
                search_limit = _PROCESS_SEARCH_MAX_RESULTS
        payload = discovery.query_process_registry(
            query=query, max_results=search_limit,
        )
        if not isinstance(payload, dict):
            raise BridgeError(
                ERR_DISCOVERY_UNAVAILABLE,
                "Discovery service returned an unexpected payload",
            )
        filtered = filter_discovery_payload(payload, self._export_policy)
        if bridge is not None:
            filtered = _filter_payload_against_session_policy(filtered, bridge)
            filtered = self._append_matching_session_allowlist_entries(
                filtered,
                bridge=bridge,
                discovery=discovery,
                query=query,
                max_results=clamped,
            )
            filtered = _limit_discovery_payload(filtered, clamped)
        return filtered

    def process_schema(
        self,
        process_key: str,
        *,
        bridge_id: str | None = None,
    ) -> dict[str, Any]:
        """Return the registered invocation schema for ``process_key``.

        M5 §14.5: when ``bridge_id`` is supplied, the request is
        additionally validated against the bridge session's allowlist
        before the discovery lookup runs.
        """
        self._require_export_enabled()
        if not is_valid_process_key_shape(process_key):
            raise BridgeError(
                ERR_INVALID_PROCESS_KEY,
                "process_key must be 'provider_type::provider::function_name'",
            )
        if not self._export_policy.is_allowed(process_key):
            raise BridgeError(
                ERR_PROCESS_NOT_ALLOWED,
                f"Process {process_key} is not allowed by export policy",
            )
        if bridge_id is not None:
            bridge = self._resolve_bridge(bridge_id)
            self._validate_process_against_session_policy(process_key, bridge)
        discovery = self._discovery_service
        if discovery is None:
            raise BridgeError(
                ERR_DISCOVERY_UNAVAILABLE,
                "Discovery service is not available",
            )
        wrapped = discovery.get_process_schema(process_key)
        if not isinstance(wrapped, dict):
            raise BridgeError(
                ERR_DISCOVERY_UNAVAILABLE,
                "Discovery service returned an unexpected payload",
            )
        if wrapped.get("action_status") != ActionStatus.COMPLETED.value:
            raise BridgeError(
                ERR_INVALID_PROCESS_KEY,
                str(wrapped.get("error") or "Unknown discovery error"),
            )
        data = wrapped.get("data")
        if not isinstance(data, dict):
            raise BridgeError(
                ERR_DISCOVERY_UNAVAILABLE,
                "Discovery schema response missing data",
            )
        return data

    # ------------------------------------------------------------------
    # process_call / process_result — direct, inference-free invocation
    # ------------------------------------------------------------------

    def process_call(
        self,
        process_key: str,
        arguments: dict[str, Any],
        *,
        trigger_data: dict[str, Any] | None = None,
        deliver_to_bridge: bool = True,
    ) -> dict[str, Any]:
        """Submit a process invocation against a bridge-bound session.

        ``trigger_data`` is required to carry ``bridge_id`` and
        ``session_id``; the HTTP route layer is responsible for sourcing
        both from its bridge lookup before delegating here.

        M5 §14.5: bridge is resolved BEFORE per-session policy check
        so the check operates on bridge state (client_id + allowlist),
        not on global export policy alone.
        """
        self._require_export_enabled()
        self._require_dispatch_dependencies()
        self._validate_process_call_inputs(process_key, arguments)

        ctx = trigger_data or {}
        bridge_id = ctx.get("bridge_id")
        session_id = ctx.get("session_id")
        if not isinstance(bridge_id, str) or not isinstance(session_id, str):
            raise BridgeError(
                ERR_NO_BRIDGE,
                "trigger_data must include bridge_id and session_id",
            )
        bridge = self._resolve_bridge(bridge_id)
        bridge.touch()
        # M5 §14.5: enforce per-session allowlist after bridge is resolved.
        self._validate_process_against_session_policy(process_key, bridge)

        reason_raw = ctx.get("reason")
        reason = (
            str(reason_raw)
            if isinstance(reason_raw, str) and reason_raw.strip()
            else f"Direct process call: {process_key}"
        )
        return self._submit_process_call(
            bridge=bridge,
            process_key=process_key,
            arguments=arguments,
            reason=reason,
            deliver_to_bridge=deliver_to_bridge,
        )

    def process_result(self, action_id: str) -> dict[str, Any]:
        """Return the latest stored result for ``action_id``."""
        if not action_id:
            raise BridgeError(
                ERR_ACTION_RESULT_NOT_FOUND,
                "action_id must be a non-empty string",
            )
        state_service = self._state_service
        if state_service is None:
            raise BridgeError(
                ERR_STATE_UNAVAILABLE,
                "State service is not available",
            )
        event = self._read_action_event(state_service, action_id)
        if event is None:
            raise BridgeError(
                ERR_ACTION_RESULT_NOT_FOUND,
                f"Action {action_id} not found",
            )
        latest = self._read_latest_action_result(state_service, action_id)
        payload: dict[str, Any] = {
            "action_id": action_id,
            "status": str(event.get("status") or "unknown"),
            "process_key": str(event.get("process_key") or ""),
            "flow_id": str(event.get("core__flows_id") or ""),
            "error_message": event.get("error_message"),
        }
        if latest is not None:
            payload["result"] = latest.get("result_data") or {}
            payload["result_source"] = latest.get("result_source")
            payload["result_created_at"] = latest.get("created_at")
        return payload

    # ------------------------------------------------------------------
    # download — blob retrieval
    # ------------------------------------------------------------------

    def download(self, blob_id: str) -> DownloadedBlob:
        """Return the bytes + filename + mime-type for ``blob_id``."""
        if not blob_id:
            raise BridgeError(
                ERR_BLOB_NOT_FOUND,
                "blob_id must be a non-empty string",
            )
        if self._blob_storage_service is None:
            raise BridgeError(
                ERR_BLOB_UNAVAILABLE,
                "Blob storage is not available",
            )
        data = self._fetch_blob_data(blob_id)
        return DownloadedBlob(
            content=_decode_blob_content(data.get("content")),
            filename=_resolve_blob_filename(data, blob_id),
            mime_type=_resolve_blob_mime(data),
        )

    def _fetch_blob_data(self, blob_id: str) -> dict[str, object]:
        blob_svc = self._blob_storage_service
        if blob_svc is None:
            raise BridgeError(
                ERR_BLOB_UNAVAILABLE,
                "Blob storage is not available",
            )
        result = blob_svc.retrieve_blob(blob_id)
        if not isinstance(result, dict) or result.get(
            "action_status",
        ) != ActionStatus.COMPLETED.value:
            raise BridgeError(
                ERR_BLOB_NOT_FOUND,
                f"Blob not found: {blob_id}",
            )
        data = result.get("data") or {}
        if not isinstance(data, dict):
            raise BridgeError(
                ERR_BLOB_NOT_FOUND,
                f"Blob payload malformed for {blob_id}",
            )
        return data

    # ------------------------------------------------------------------
    # internals — dispatch dependencies + bridge resolution
    # ------------------------------------------------------------------

    def _require_dispatch_dependencies(self) -> None:
        missing: list[str] = []
        if self._flow_manager is None:
            missing.append("flow_manager")
        if self._action_factory is None:
            missing.append("action_factory")
        if self._compilation_context_builder is None:
            missing.append("compilation_context_builder")
        if missing:
            raise BridgeError(
                ERR_DEPENDENCIES_NOT_READY,
                f"Required services not injected: {', '.join(missing)}",
            )

    def _require_export_enabled(self) -> None:
        if not self._export_policy.enabled:
            raise BridgeError(
                ERR_BRIDGE_DISABLED,
                "Process export is disabled by plugin configuration",
            )

    def _resolve_bridge(self, bridge_id: str) -> BridgeSessionState:
        bridge = self._bridge_manager.get(bridge_id)
        if bridge is None or bridge.closed:
            raise BridgeError(
                ERR_NO_BRIDGE,
                f"Bridge {bridge_id} not found or closed",
            )
        return bridge

    def _append_matching_session_allowlist_entries(
        self,
        payload: dict[str, Any],
        *,
        bridge: BridgeSessionState,
        discovery: Any,
        query: str,
        max_results: int,
    ) -> dict[str, Any]:
        """Add matching allowlisted process rows that embedding search missed."""
        allowlist = _restricted_allowlist_for_bridge(bridge)
        raw_processes = _payload_process_entries(payload)
        if not allowlist or raw_processes is None or len(raw_processes) >= max_results:
            return payload
        additions = _matching_allowlist_entries(
            discovery=discovery,
            export_policy=self._export_policy,
            allowlist=allowlist,
            existing=_process_keys_in_entries(raw_processes),
            query=query,
            slots=max_results - len(raw_processes),
        )
        if not additions:
            return payload
        return _augment_discovery_payload(payload, raw_processes, additions)

    # ------------------------------------------------------------------
    # internals — process_call
    # ------------------------------------------------------------------

    def _validate_process_call_inputs(
        self, process_key: object, arguments: object,
    ) -> None:
        if not isinstance(process_key, str) or not is_valid_process_key_shape(
            process_key,
        ):
            raise BridgeError(
                ERR_INVALID_PROCESS_KEY,
                "process_key must be 'provider_type::provider::function_name'",
            )
        if not self._export_policy.is_allowed(process_key):
            raise BridgeError(
                ERR_PROCESS_NOT_ALLOWED,
                f"Process {process_key} is not allowed by export policy",
            )
        if not isinstance(arguments, dict):
            raise BridgeError(
                ERR_INVALID_PROCESS_KEY,
                "arguments must be an object",
            )

    def _validate_process_against_session_policy(
        self, process_key: str, bridge: BridgeSessionState,
    ) -> None:
        """M5 §14.5: enforce per-session allowlist after bridge resolution.

        Per the bridge_sessions sentinel scheme:
        * Stdio bridges (``bridge.client_id == ""``) bypass the check
          entirely — they predate the OAuth-principal model; per-session
          allowlists semantically apply only to OAuth-bound bridges where
          the policy resolver minted a real allowlist from the bearer's
          client_id. Skipping here keeps stdio MCP working without
          weakening the OAuth path.
        * ``_UNRESTRICTED`` (identity-checked) bypasses the membership test —
          operator-equivalent OAuth clients get every allowed process.
        * Any other tuple is membership-checked; out-of-allowlist
          process_keys are rejected with ``ERR_PROCESS_NOT_ALLOWED``.

        ``EMPTY_ALLOWLIST = ()`` (fail-closed) rejects everything; that's
        the correct outcome for an OAuth bridge with no resolved policy
        (a client neither operator_equivalent nor paired-shipper).
        """
        # M5.B post-merge hot-fix: stdio bridges have no OAuth principal,
        # so the allowlist semantics don't apply. Skip the check entirely
        # rather than fail-closed against legitimate first-party callers.
        if not bridge.client_id:
            return
        # Late import to avoid circular: bridge_sessions imports models +
        # auth; this module imports models directly. Importing the
        # sentinel lazily keeps the dep DAG acyclic.
        from .bridge_sessions import _UNRESTRICTED  # noqa: PLC0415

        if bridge.process_export_allowlist is _UNRESTRICTED:
            return
        if process_key not in bridge.process_export_allowlist:
            raise BridgeError(
                ERR_PROCESS_NOT_ALLOWED,
                (
                    f"Process {process_key} is not in the bridge session's "
                    "per-session allowlist"
                ),
            )

    def _submit_process_call(
        self,
        *,
        bridge: BridgeSessionState,
        process_key: str,
        arguments: dict[str, Any],
        reason: str,
        deliver_to_bridge: bool,
    ) -> dict[str, Any]:
        # Fire-and-forget dispatch — the result flows back through the
        # bridge-delivery dispatcher to deliver_result / deliver_error,
        # never through inference.
        flow_id = self._flow_manager.create_flow(
            session_id=bridge.session_id,
            trigger_type="bridge_process_call",
            trigger_data=self._build_process_call_trigger_data(
                bridge=bridge,
                process_key=process_key,
                reason=reason,
                inference_vertex_role=self._resolve_originating_role(bridge),
                operator_equivalent=self._resolve_operator_equivalent(bridge),
                caller_attribution=self._resolve_caller_attribution(bridge),
            ),
            priority=5,
        )
        action_def: dict[str, Any] = {
            "process_key": process_key,
            "arguments": arguments,
            "notes": reason,
            "session_id": bridge.session_id,
            "flow_id": flow_id,
        }
        if deliver_to_bridge:
            action_def.update(
                {
                    "result_processor_kind": ResultProcessorKind.BRIDGE_DELIVERY.value,
                    "error_processor_kind": ErrorProcessorKind.BRIDGE_DELIVERY.value,
                },
            )
        else:
            action_def.update(
                {
                    "result_processor": None,
                    "error_processor": None,
                    "result_processor_kind": None,
                    "error_processor_kind": None,
                },
            )
        try:
            compilation_context = self._compilation_context_builder.build_context(
                session_id=bridge.session_id, flow_id=flow_id,
            )
            action_id = self._action_factory.submit_action_definition(
                action_definition=action_def, context=compilation_context,
            )
        except Exception as exc:
            self._flow_manager.update_flow_status(flow_id, "failed")
            logger.error("process_call dispatch failed: %s", exc, exc_info=True)
            error_code = getattr(exc, "error_code", None)
            if error_code == UNKNOWN_ARGUMENTS_ERROR_CODE:
                raise BridgeError(error_code, str(exc)) from exc
            raise BridgeError(ERR_PROCESS_CALL_FAILED, str(exc)) from exc
        return {
            "status": "queued",
            "action_id": action_id,
            "flow_id": flow_id,
            "process_key": process_key,
        }

    def _resolve_originating_role(self, bridge: BridgeSessionState) -> str:
        """◆R2: the durable role name held by the originating bridge, or "".

        Reverse-lookup over ``agent_role_binding`` (the sole resolution
        authority) via ``role_binding_store`` — the same table
        ``peer_send_by_name`` routes over, never a parallel path. The role
        is written into the flow's ``trigger_data`` so the Phase-5 vertex
        resolver can bind BY ROLE (agent_instance_id is minted fresh per
        bridge launch and cannot survive a restart / reconnect; the durable
        role can). v1 assumes a session holds at most one inference-vertex
        role; the first (sorted) is chosen deterministically. Empty string
        when the bridge holds no role, has no instance identity yet, or no
        state service is bound — all of which the resolver treats as the
        roleless / instance-only path.

        B1 (Rev-C): the role tag is ANCILLARY — role="" still routes
        correctly via the instance path. This lookup runs as an argument to
        ``create_flow`` on the PRIMARY MCP surface (every bridge
        ``process_call``), OUTSIDE the caller's error envelope, so any
        ``query_state`` fault (e.g. a transient PoolTimeout under scram)
        MUST degrade to roleless here rather than break dispatch. Tag write
        can degrade, never break — swallow ANY lookup error and return "".
        """
        return _role_for_instance(
            self._state_service,
            bridge.agent_instance_id or "",
            purpose="inference_vertex_role",
        )

    def _resolve_caller_attribution(
        self, bridge: BridgeSessionState,
    ) -> CallerAttribution:
        """§34.6: derive sender provenance for an UNREGISTERED caller, or empty.

        The local CLI opens a one-shot bridge and deliberately never registers
        a peer identity (registering would sweep its own session's registry row
        by ``session_label`` and delete it again at close), so
        ``bridge.agent_instance_id`` is empty and every CLI-originated send was
        stamped ``System (Scheduler)``. The caller instead supplies the opaque
        launcher-exported ``agent_session_id`` at ``bridge/open``; the identity
        is read HERE, out of the peer-registry row that key resolves to — the
        caller asserts a routing key, the server binds the content.

        Skipped entirely for a bridge that HAS registered: its own registered
        identity already flows through ``inference_vertex_*`` and is strictly
        better evidence than a key the caller typed.

        Degrade-silent on every fault, matching
        :meth:`_resolve_originating_role`: this runs outside the caller's error
        envelope on every ``process_call``, and
        ``resolve_by_agent_session_id`` raises ``PeerSessionAmbiguousError``
        when one session id somehow carries two live bindings. Attribution is
        best-effort provenance — never a precondition for dispatch.
        """
        if bridge.agent_instance_id or not bridge.caller_agent_session_id:
            return CallerAttribution()
        if self._peer_registry is None:
            return CallerAttribution()
        try:
            binding = self._peer_registry.resolve_by_agent_session_id(
                bridge.caller_agent_session_id,
            )
        except Exception:  # noqa: BLE001 — degrade to unattributed; never break dispatch
            logger.warning(
                "caller attribution lookup failed (agent_session_id=%s); "
                "degrading to the system sentinel — dispatch proceeds",
                bridge.caller_agent_session_id,
                exc_info=True,
            )
            return CallerAttribution()
        if binding is None:
            return CallerAttribution()
        return CallerAttribution(
            agent_id=binding.agent_id,
            agent_instance_id=binding.agent_instance_id,
            session_label=binding.session_label,
            role=_role_for_instance(
                self._state_service,
                binding.agent_instance_id,
                purpose="caller_attribution_role",
            ),
        )

    def set_operator_equivalent_check(
        self, callback: Callable[[str], bool],
    ) -> None:
        """Wire the vault ``is_operator_equivalent`` lookup (post-readiness).

        Mirrors ``SessionLedgerService.set_operator_equivalent_check`` — the
        bridge plugin calls this once the vault OAuth registry is live, so a
        VERIFIED operator_equivalent client keeps operator authority
        (``for_operator_equivalent``) after the no-auth flip. Until wired, the
        check resolves to non-operator (the safe default).
        """
        self._operator_equivalent_check = callback

    def _resolve_operator_equivalent(self, bridge: BridgeSessionState) -> bool:
        """Whether the bridge's OAuth client is operator_equivalent in the vault.

        B1 Finding-B: authority derives from the AUTHORITATIVE
        ``oauth_client.operator_equivalent`` record (via the wired vault
        lookup), never from the export-policy allowlist. Degrade-safe like
        :meth:`_resolve_originating_role`: an unset callback, an empty
        client_id (stdio / no-auth bridge), or a throwing lookup all resolve to
        False — the stamp is an authority TAG that can degrade, never break
        dispatch, and False (non-operator) is the safe default.
        """
        if self._operator_equivalent_check is None or not bridge.client_id:
            return False
        try:
            return bool(self._operator_equivalent_check(bridge.client_id))
        except Exception:  # noqa: BLE001 — degrade to non-operator; never break dispatch
            logger.warning(
                "operator_equivalent check threw for client_id=%s; "
                "degrading to non-operator (dispatch proceeds)",
                bridge.client_id,
                exc_info=True,
            )
            return False

    @staticmethod
    def _build_process_call_trigger_data(
        *,
        bridge: BridgeSessionState,
        process_key: str,
        reason: str,
        inference_vertex_role: str,
        operator_equivalent: bool,
        caller_attribution: CallerAttribution = CallerAttribution(),  # noqa: B008 — frozen+slots dataclass, immutable shared default
    ) -> dict[str, object]:
        """Build the per-call trigger_data carried through the flow service.

        M5 §14.8: includes ``authenticated_principal`` so service handlers
        can do server-side authz on bridge identity. The principal is
        lifted into the handler's ``state`` dict by
        :meth:`ActionProcessor._inject_session_context` at dispatch time.
        Empty client_id (stdio bridges) carries an absent principal —
        handlers that require it raise ``PermissionError`` per the
        ``acknowledge_quarantine`` precedent in M1.
        """
        trigger: dict[str, object] = {
            "source_namespace": PLUGIN_NAME,
            "bridge_id": bridge.bridge_id,
            "session_id": bridge.session_id,
            "process_key": process_key,
            "reason": reason,
            "bridge_plugin_namespace": PLUGIN_NAME,
            "deliver_result_process_key": _DELIVER_RESULT_PROCESS_KEY,
            "deliver_error_process_key": _DELIVER_ERROR_PROCESS_KEY,
            # D-IF7 / v4 §6 + CR-v3-3: tag the flow with the originating
            # bridge's agent_instance_id so the inference-service wrapper
            # can look up a per-bridge SessionInferenceProvider via
            # AgentMessagingPlugin.get_inference_provider(...). Empty
            # string when the bridge has not yet registered a peer
            # identity — the wrapper treats absent / empty / unknown
            # identically (fallback to default_inference_plugin).
            "inference_vertex_session_id": bridge.agent_instance_id or "",
            # ◆R2 (Phase 5): the DURABLE role held by the originating
            # session. agent_instance_id is ephemeral (minted per bridge
            # launch) so a flow tagged with a now-dead instance cannot
            # resolve to a restarted session; the role can. The resolver
            # binds BY ROLE first (role → current instance via
            # agent_role_binding), falling back to the instance tag only
            # for roleless sessions. Empty string when the bridge holds no
            # role.
            "inference_vertex_role": inference_vertex_role,
            # §34.6: SERVER-DERIVED provenance for an unregistered caller (the
            # local CLI). A separate key family from ``inference_vertex_*`` on
            # purpose — only the send verbs' sender-stamping reads these, so an
            # attributed CLI call can never re-point the flow's inference
            # vertex at the attributed session. All-empty for every registered
            # caller and for an unresolvable key.
            **caller_attribution.as_trigger_fields(),
        }
        # Lane W: the completion-delivery ROUTE, stamped here from the same
        # already-resolved attribution value rather than re-derived at
        # completion time. Conditional on purpose — see
        # COMPLETION_ROUTE_ROLE_KEY: presence IS the permission to push, so a
        # roleless caller must leave the key absent rather than carry "".
        if caller_attribution.role:
            trigger[COMPLETION_ROUTE_ROLE_KEY] = caller_attribution.role
        if bridge.client_id:
            trigger["authenticated_principal"] = {
                "client_id": bridge.client_id,
                "agent_id": bridge.agent_instance_id or "",
                "agent_instance_id": bridge.agent_instance_id or "",
                "bridge_id": bridge.bridge_id,
                "session_id": bridge.session_id,
                # B1 Finding-B: a VERIFIED operator_equivalent OAuth client
                # resolves to for_operator_equivalent (is_operator_like) in
                # _build_call_context, preserving operator authority after the
                # no-auth flip. Default False → for_external (non-operator).
                "operator_equivalent": operator_equivalent,
            }
        return trigger

    # ------------------------------------------------------------------
    # internals — process_result state-service plumbing
    # ------------------------------------------------------------------

    def _read_action_event(
        self, state_service: Any, action_id: str,
    ) -> dict[str, object] | None:
        result = state_service.read_state(
            namespace="core",
            query={"table": "action_events", "filters": {"id": action_id}},
        )
        return self._extract_first_record(result)

    def _read_latest_action_result(
        self, state_service: Any, action_id: str,
    ) -> dict[str, object] | None:
        result = state_service.read_state(
            namespace="core",
            query={
                "table": "action_results",
                "filters": {"core__action_events_id": action_id},
            },
        )
        record = self._latest_record_by_created_at(result)
        if record is None:
            return None
        raw = record.get("result_data")
        if isinstance(raw, str):
            try:
                record["result_data"] = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                record["result_data"] = None
        return record

    @staticmethod
    def _latest_record_by_created_at(
        result: object,
    ) -> dict[str, object] | None:
        records = PlatformSurface._extract_record_list(result)
        if not records:
            return None
        return max(records, key=lambda r: str(r.get("created_at") or ""))

    @staticmethod
    def _extract_record_list(result: object) -> list[dict[str, object]]:
        if not isinstance(result, dict):
            return []
        data = result.get("data")
        if not isinstance(data, dict):
            return []
        candidates: object = []
        inner = data.get("result")
        if isinstance(inner, dict):
            candidates = inner.get("records") or []
        if not candidates:
            candidates = data.get("records") or []
        if not isinstance(candidates, list):
            return []
        return [r for r in candidates if isinstance(r, dict)]

    @staticmethod
    def _extract_first_record(result: object) -> dict[str, object] | None:
        # Mirrors the heterogeneous record extraction in FlowManager.
        if not isinstance(result, dict):
            return None
        data = result.get("data")
        if not isinstance(data, dict):
            return None
        inner = data.get("result")
        if isinstance(inner, dict):
            record = PlatformSurface._first_record_from_list(
                inner.get("records"),
            )
            if record:
                return record
        record = PlatformSurface._first_record_from_list(data.get("records"))
        if record:
            return record
        if data and "records" not in data and "result" not in data:
            return data
        return None

    @staticmethod
    def _first_record_from_list(records: object) -> dict[str, object] | None:
        if not isinstance(records, list) or not records:
            return None
        first = records[0]
        return first if isinstance(first, dict) else None

def _decode_blob_content(content: object) -> bytes:
    return bytes.fromhex(content) if isinstance(content, str) else b""


def _resolve_blob_filename(data: dict[str, object], blob_id: str) -> str:
    metadata_raw = data.get("metadata")
    metadata: dict[str, object] = (
        metadata_raw if isinstance(metadata_raw, dict) else {}
    )
    filename = metadata.get("filename") or data.get("filename")
    return filename if isinstance(filename, str) else f"{blob_id}.bin"


def _resolve_blob_mime(data: dict[str, object]) -> str:
    metadata_raw = data.get("metadata")
    metadata: dict[str, object] = (
        metadata_raw if isinstance(metadata_raw, dict) else {}
    )
    mime = metadata.get("mime_type")
    return mime if isinstance(mime, str) else "application/octet-stream"


def _filter_payload_against_session_policy(
    payload: dict[str, Any], bridge: BridgeSessionState,
) -> dict[str, Any]:
    """M5 §14.5: drop discovery results outside the bridge's allowlist.

    Stdio bridges (``bridge.client_id == ""``) get the payload
    untouched — per-session allowlists apply only to OAuth-bound
    bridges (M5.B post-merge hot-fix; matches
    ``_validate_process_against_session_policy``'s stdio bypass).
    Operator-equivalent bridges (``_UNRESTRICTED`` sentinel) pass the
    payload through untouched. Other OAuth bridges keep only entries
    whose ``process_key`` appears in ``bridge.process_export_allowlist``.
    Search results never surface "exists but you can't call it" — out-
    of-allowlist process_keys are silently dropped.
    """
    # M5.B post-merge hot-fix: stdio bridges (no OAuth principal) get
    # the full search payload without filtering. Allowlist filtering is
    # only meaningful for OAuth-bound bridges (non-empty client_id).
    if not bridge.client_id:
        return payload
    # Late import: bridge_sessions imports models + auth; this module
    # imports models directly. Lazy import keeps the dep DAG acyclic.
    from .bridge_sessions import _UNRESTRICTED  # noqa: PLC0415

    if bridge.process_export_allowlist is _UNRESTRICTED:
        return payload
    allowlist = set(bridge.process_export_allowlist)
    filtered = dict(payload)
    raw_processes = payload.get("processes", [])
    kept: list[dict[str, Any]] = []
    if isinstance(raw_processes, list):
        for entry in raw_processes:
            if not isinstance(entry, dict):
                continue
            key = entry.get("process_key")
            if isinstance(key, str) and key in allowlist:
                kept.append(entry)
    filtered["processes"] = kept
    filtered["process_keys"] = [
        entry["process_key"] for entry in kept
        if isinstance(entry.get("process_key"), str)
    ]
    filtered["process_count"] = len(kept)
    return filtered


def _restricted_allowlist_for_bridge(bridge: BridgeSessionState) -> tuple[str, ...]:
    if not bridge.client_id:
        return ()
    from .bridge_sessions import _UNRESTRICTED  # noqa: PLC0415

    if bridge.process_export_allowlist is _UNRESTRICTED:
        return ()
    return tuple(bridge.process_export_allowlist)


def _payload_process_entries(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    raw_processes = payload.get("processes", [])
    if not isinstance(raw_processes, list):
        return None
    return [entry for entry in raw_processes if isinstance(entry, dict)]


def _process_keys_in_entries(entries: list[dict[str, Any]]) -> set[object]:
    return {entry.get("process_key") for entry in entries}


def _matching_allowlist_entries(
    *,
    discovery: Any,
    export_policy: ProcessExportPolicy,
    allowlist: tuple[str, ...],
    existing: set[object],
    query: str,
    slots: int,
) -> list[dict[str, Any]]:
    additions: list[dict[str, Any]] = []
    for process_key in allowlist:
        if len(additions) >= slots:
            break
        if process_key in existing or not export_policy.is_allowed(process_key):
            continue
        entry = _lookup_discovery_entry(discovery, process_key)
        if entry is not None and _entry_matches_query(entry, query):
            additions.append(entry)
    return additions


def _augment_discovery_payload(
    payload: dict[str, Any],
    raw_processes: list[dict[str, Any]],
    additions: list[dict[str, Any]],
) -> dict[str, Any]:
    augmented = dict(payload)
    augmented["processes"] = [*raw_processes, *additions]
    augmented["process_keys"] = [
        entry["process_key"] for entry in augmented["processes"]
        if isinstance(entry.get("process_key"), str)
    ]
    augmented["process_count"] = len(augmented["processes"])
    return augmented


def _lookup_discovery_entry(discovery: Any, process_key: str) -> dict[str, Any] | None:
    try:
        wrapped = discovery.get_process_schema(process_key)
    except Exception:  # noqa: BLE001
        logger.debug("allowlist schema lookup failed for %s", process_key, exc_info=True)
        return None
    if not isinstance(wrapped, dict):
        return None
    if wrapped.get("action_status") != ActionStatus.COMPLETED.value:
        return None
    data = wrapped.get("data")
    if not isinstance(data, dict):
        return None
    return _schema_data_to_discovery_entry(process_key, data)


def _schema_data_to_discovery_entry(
    process_key: str, data: dict[str, Any],
) -> dict[str, Any]:
    parts = process_key.split("::", 2)
    provider_type, provider, function_name = (
        parts if len(parts) == 3 else ("", "", "")
    )
    return {
        "process_key": str(data.get("process_key") or process_key),
        "provider_type": provider_type,
        "provider": provider,
        "function_name": function_name,
        "description": str(data.get("description") or ""),
        "invocation_schema": data.get("invocation_schema") or {},
        "is_long_running": bool(data.get("is_long_running", False)),
        "deprecation": data.get("deprecation"),
    }


def _entry_matches_query(entry: dict[str, Any], query: str) -> bool:
    tokens = _query_tokens(query)
    if not tokens:
        return False
    haystack = " ".join(
        str(entry.get(field) or "")
        for field in ("process_key", "description", "provider", "function_name")
    )
    haystack_tokens = set(_query_tokens(haystack))
    return all(token in haystack_tokens for token in tokens)


def _query_tokens(text: str) -> tuple[str, ...]:
    return tuple(match.group(0).lower() for match in _QUERY_TOKEN_RE.finditer(text))


def _limit_discovery_payload(
    payload: dict[str, Any], max_results: int,
) -> dict[str, Any]:
    """Clamp a discovery payload after policy filtering.

    OAuth sessions may over-fetch from the registry first so allowlisted
    entries are not lost just because forbidden results ranked above them.
    The client still receives only its requested result count.
    """
    limited = dict(payload)
    raw_processes = payload.get("processes", [])
    if not isinstance(raw_processes, list):
        return limited
    kept = [
        entry for entry in raw_processes[:max_results]
        if isinstance(entry, dict)
    ]
    limited["processes"] = kept
    limited["process_keys"] = [
        entry["process_key"] for entry in kept
        if isinstance(entry.get("process_key"), str)
    ]
    limited["process_count"] = len(kept)
    return limited


__all__ = [
    "ERR_ACTION_RESULT_NOT_FOUND",
    "ERR_ATTACHMENT_MISSING",
    "ERR_BLOB_NOT_FOUND",
    "ERR_BLOB_UNAVAILABLE",
    "ERR_BRIDGE_DISABLED",
    "ERR_DEPENDENCIES_NOT_READY",
    "ERR_DISCOVERY_UNAVAILABLE",
    "ERR_INVALID_PROCESS_KEY",
    "ERR_NO_BRIDGE",
    "ERR_PROCESS_CALL_FAILED",
    "ERR_PROCESS_NOT_ALLOWED",
    "ERR_STATE_UNAVAILABLE",
    "BridgeError",
    "DownloadedBlob",
    "PlatformSurface",
]
