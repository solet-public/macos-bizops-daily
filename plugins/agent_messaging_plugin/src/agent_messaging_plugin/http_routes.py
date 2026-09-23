# pyright: reportUnusedFunction=false
"""FastAPI route registration for the merged bridge plugin.

Wires every ``/api/v1/bridge/*`` route in the consolidated plan's
route table onto a single ``FastAPI`` app, delegating to the layered
collaborators that own each concern:

* :class:`BridgeSessionManager` — bridge lifecycle + long-poll events.
* :class:`PeerRegistry` — peer bindings + native wake adapters.
* :class:`PlatformSurface` — process_* / download.
* ``agent_messaging_service`` — durable thread/message persistence.

Handlers stay deliberately thin: validate the request shape, delegate,
serialize.  Every collaborator-specific exception maps to a single
HTTP status code so MCP clients see a stable contract irrespective of
which subsystem failed.

Ported from ``agent_channel_plugin.plugin`` and
``claude_code_channel_plugin.plugin`` during the bridge-consolidation
work — see
``workbench/2026-05-16_codex_mcp_channel_and_inter_agent_outstanding_work.md``
sub-phase 2e for the route table and dispatch semantics.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import secrets
from datetime import datetime as _dt
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Callable

from ananta.core.actions.action_path_liveness import ACTION_PATH_LIVENESS
from ananta.llm.agent_messaging.models import (
    PeerInboxRequest,
    TextPart,
)
from ananta.llm.agent_messaging.schema import (
    META_KEY_DELIVERY_EXTERNAL_ID,
    RECIPIENT_KIND_ROLE,
    ROLE_THREAD_PREFIX,
)
from ananta.llm.agent_messaging.service import (
    AgentMessagingError,
    role_message_external_id,
)
from ananta.llm.agent_messaging.state_results import StateOperationError
from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from .bridge_lifecycle import run_full_bridge_cleanup
from .bridge_sessions import (
    BridgeNotFoundError,
    BridgeQueueFullError,
    BridgeSessionManager,
)
from .operator_codex_registration import (
    OperatorClaudeRegistration,
    OperatorCodexRegistration,
    OperatorHostQualificationError,
    claude_operator_tmux_host_ref,
    register_operator_claude_host,
    register_operator_codex_host,
)
from .peer_dispatch import (
    EVENT_PEER_MESSAGE,
    EVENT_POST_MESSAGE,
    IMPORTANT_MARKER_RE,
    NativeWakeError,
    binding_is_live,
    dispatch_peer_send,
    dispatch_role_send,
)
from .peer_inbox_view import (
    serialize_peer_inbox_page,
)
from .peer_list_view import (
    serialize_peer_list,
)
from .peer_registry import (
    PeerAmbiguousError,
    PeerRegistry,
    PeerSessionAmbiguousError,
    PeerUnreachableError,
)
from .platform_surface import BridgeError, PlatformSurface
from .role_binding_store import (
    UNCLAIMED_SESSION_ID,
    ResolvedRole,
    RoleBindingVacantError,
    holds_role,
    list_roles_for_agent_instance,
    refresh_role_binding_cas,
    resolve_role_binding,
    sole_role_for_reply_address,
)
from .role_claim import (
    RoleClaimFailure,
    RoleClaimOrigin,
    claim_role_for_session,
)
from .sender_provenance import SENDER_PRINCIPAL_KIND_STDIO_AGENT
from .session_hosts import OPERATOR_HOST
from .session_lifecycle_store import (
    ManagedSessionSpec,
    backfill_registration,
    insert_managed_session,
)

if TYPE_CHECKING:
    from .models import BridgeBinding, QueuedEvent

logger = logging.getLogger(__name__)


# Bridge-event types that carry an IMPORTANT peer/role delivery (the native
# wake rides ``post_message``; the no-adapter path rides ``peer_message``).
# The watcher-ack consumption reconcile only inspects these.
_WATCHER_DELIVERY_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {EVENT_POST_MESSAGE, EVENT_PEER_MESSAGE},
)


API_PREFIX: Final[str] = "/api/v1/bridge"

_AGENT_ID_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._-]{1,64}")

# Map BridgeError.code → HTTP status.  Anything not in this table maps
# to 400, matching the platform_surface convention of "bad input"
# being the default failure mode.
_BRIDGE_ERROR_STATUS: Final[dict[str, int]] = {
    "bridge.no_active_bridge": 404,
    "bridge.action_result_not_found": 404,
    "bridge.blob_not_found": 404,
    "bridge.blob_storage_unavailable": 503,
    "bridge.state_service_unavailable": 503,
    "bridge.discovery_unavailable": 503,
    "bridge.dependencies_not_ready": 503,
    "bridge.process_export_disabled": 503,
    "bridge.process_call_failed": 500,
    "bridge.attachment_missing": 400,
    "bridge.invalid_process_key": 400,
    "bridge.process_not_allowed": 403,
}


# ---------------------------------------------------------------------------
# Inline request models — kept here so the wire contract is greppable from
# one place and so the handlers stay declarative.
# ---------------------------------------------------------------------------


class OpenBridgeBody(BaseModel):
    """Optional parent_pid for sibling-bridge pairing inside one OS tree.

    ``caller_agent_session_id`` (§34.6) is the launcher-exported ``ases-...``
    key an UNREGISTERED caller supplies so its sends can be attributed. It is
    a lookup key, not an identity claim, and never reaches the registry.
    """

    parent_pid: int | None = None
    caller_agent_session_id: str = ""


class ProcessSearchBody(BaseModel):
    query: str
    max_results: int = 10


class ProcessSchemaBody(BaseModel):
    process_key: str


class ProcessCallBody(BaseModel):
    process_key: str
    arguments: dict[str, Any]
    reason: str | None = None


class PeerRegisterBody(BaseModel):
    # Explicit local operator launch qualification; omitted by managed workers.
    operator_tmux_host: str = ""
    agent_id: str
    agent_instance_id: str
    session_label: str = ""
    parent_pid: int | None = None
    # S1 (agent_session_id splice): the STABLE per-logical-session key the client
    # already sends (mcp_bridge/__main__.py + forwarder.py). The model previously
    # DROPPED it silently; it is now stored on the BridgeBinding and drives the
    # reconnect state-table self-refresh (S2). Empty when the launcher did not
    # export AGENT_SESSION_ID -> self-refresh disabled (S1.5 loud log).
    agent_session_id: str = ""
    # D-IF7: opt-in flag for the per-bridge SessionInferenceProvider
    # sidecar (v4 §4). When True, the post-register hook binds an
    # inference vertex for this bridge so the wrapper at
    # ``inference_service/__init__.py`` can route process_error +
    # process_results through the calling coding-agent session.
    # Defaults to False so non-coding-agent peers (older Codex sessions,
    # MCP-only consumers) remain unaffected.
    provides_inference: bool = False
    # codex-watch-migration wake_capable design (2026-08-06): declared by the
    # bridge subprocess itself (mcp_bridge/__main__.py's _run(), computed the
    # same way as provides_inference). Defaults True — the opposite polarity
    # from provides_inference above — because an omitting/older client must
    # read as "has native wake" (today's universal reality) not "does not",
    # matching BridgeBinding.wake_capable's own default. See models.py for
    # the full field rationale.
    wake_capable: bool = True
    # MSG-04/identity-unification (2026-08-20): declared by `solet-bridge watch`
    # itself (local_cli/cli.py's peer_register call), matching
    # BridgeBinding.watcher_declared's own default. See models.py for the
    # full field rationale — this is the explicit signal that replaces the
    # `agi-watch-` prefix convention for a watcher registering under its
    # ledger AGENT_INSTANCE_ID. Defaults False: every non-watch caller is
    # unaffected.
    watcher_declared: bool = False
    # The session's CONFIGURED standing role, sent so the response can answer
    # "do I still hold it?" (``session_role_held``) on the INFRA register route.
    # Without that answer the forwarder's steady-state re-assert has to issue a
    # blind re-claim through the MODEL_INITIATED ``/process/call`` route every
    # few minutes forever, which phantom-stamps ``last_model_activity_at`` with
    # no model turn and silently consumes owed wakes (the F1 class this module's
    # classification exists to prevent). Empty when the session has no
    # configured role -> ``unknown``, and the forwarder claims as before.
    session_role: str = ""


class PeerSendBody(BaseModel):
    # User-facing field is ``peer_id`` (matches every MCP tool schema and
    # the legacy Node bridges' contract).  The underlying service layer
    # calls the same value ``peer_agent_id`` — translation happens at
    # the service-call boundary inside _peer_send_impl.
    peer_id: str
    peer_agent_instance_id: str | None = None
    # A2: stable logical-session fallback carried beside (never instead of) the
    # instance id in reply hints. Dispatch consults it only after the exact
    # instance is unreachable; a live instance always wins.
    peer_agent_session_id: str | None = None
    content: list[dict[str, Any]] = Field(default_factory=list)


class PeerClaimRoleBody(BaseModel):
    # Deliberately carries ONLY the role name. Every identity field
    # (agent_id / agent_instance_id / agent_session_id / session_label) is read
    # from the calling bridge's REGISTERED binding rather than the request body:
    # the forwarder's identity IS the bridge's identity by construction, so
    # accepting it from the body would add a spoofing surface and a mismatch
    # class for nothing. The MODEL_INITIATED ``/process/call`` verb keeps its
    # caller-supplied identity — the ``/rename`` skill threads values it read
    # from its own ``peer_register`` response.
    name: str
    # Explicit escape hatch for a LIVE holder (§4.3.3a). Typed bool, so the
    # transport rejects a string rather than this route having to guess what
    # "false" meant. ONE-SHOT BY CONSTRUCTION: it authorizes the claim carried
    # in THIS request body and is stored nowhere, so the watcher's reconnect and
    # heartbeat re-claims — which post their own bodies without it — cannot
    # replay a takeover. That is the whole reason it lives in the body and not
    # in the bridge's registered binding.
    takeover: bool = False


class PeerSendByNameBody(BaseModel):
    name: str
    content: str


class PeerDrainBody(BaseModel):
    # v10 Control #5: the repair loop POSTs this to fetch the oldest page of
    # IMPORTANT role messages owed to the roles the calling bridge holds.
    limit: int = 50


class PeerDeliveredBody(BaseModel):
    # v10 Control #5: the repair loop / live emit path POSTs this after a
    # successful emit to flip ``delivered=true``. ``recipient_key`` is echoed
    # from the drain row and ownership-fenced server-side (the caller must
    # currently hold that role).
    external_id: str
    recipient_key: str


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_routes(
    app: FastAPI,
    *,
    bridge_manager: BridgeSessionManager,
    peer_registry: PeerRegistry,
    platform_surface: PlatformSurface,
    agent_messaging_service: Any,
    config: Any,
    state_service: Any | None = None,
    readiness_probe: Callable[[], bool] | None = None,
    inference_provider_register: Callable[..., None] | None = None,
    inference_provider_clear: Callable[[str], int] | None = None,
    autonomic_on_register: Callable[..., str] | None = None,
    autonomic_on_close: Callable[[str], str] | None = None,
) -> None:
    """Attach every ``/api/v1/bridge/*`` route onto ``app``.

    All collaborators are passed by keyword so the call site is the
    single source of truth for dependency wiring; nothing in this
    module reaches back into a plugin instance.

    On ``peer/register`` the route re-points every role this session holds in the
    ``agent_role_binding`` state table to the reconnected ``agent_instance_id``
    (``_state_table_self_refresh`` → ``refresh_role_binding_cas``, keyed on the
    stable ``agent_session_id``). This is the reconnect-survival path (S1–S3
    splice) that replaced the retired address-book self-refresh.

    ``readiness_probe`` is an optional zero-arg callable returning True
    once every load-bearing surface is serving (bridge uvicorn +, when
    enabled, the streamable HTTP MCP listener). When provided and
    returning False, ``/api/v1/bridge/health`` answers ``503 starting``
    so external probes (cloud ALB target-group health check, cross-host
    smokes, monitoring) gate on the full surface rather than just the
    bridge uvicorn being bound. Omitting the parameter preserves the
    legacy unconditional-200 contract for local dev / tests that don't
    enable the streamable transport. See iter-9 finding in
    ``workbench/2026-06-12_aws_swap_smoke_run_report.md`` §3 Bug 2.
    """
    long_poll_timeout_s = _config_int(config, "long_poll_timeout_seconds", 25)
    # REL-05 re-emit window + cap (Q1: plugin-config-surfaced, constants
    # defaults). Read via getattr — ``config`` is the _BridgeRuntimeConfig
    # dataclass (no ``.get``), so _config_int would only ever yield the default.
    re_emit_window_s = float(getattr(config, "re_emit_window_seconds", 300))
    re_emit_cap = int(getattr(config, "re_emit_cap", 3))

    _register_bridge_lifecycle_routes(
        app,
        bridge_manager=bridge_manager,
        peer_registry=peer_registry,
        agent_messaging_service=agent_messaging_service,
        long_poll_timeout_s=long_poll_timeout_s,
        inference_provider_clear=inference_provider_clear,
        autonomic_on_close=autonomic_on_close,
    )
    _register_platform_surface_routes(
        app,
        platform_surface=platform_surface,
        bridge_manager=bridge_manager,
    )
    _register_peer_routes(
        app,
        bridge_manager=bridge_manager,
        peer_registry=peer_registry,
        agent_messaging_service=agent_messaging_service,
        state_service=state_service,
        inference_provider_register=inference_provider_register,
        autonomic_on_register=autonomic_on_register,
        re_emit_window_s=re_emit_window_s,
        re_emit_cap=re_emit_cap,
    )

    @app.get(f"{API_PREFIX}/health")
    async def health() -> JSONResponse:
        if readiness_probe is not None and not readiness_probe():
            return JSONResponse(
                content={"status": "starting"},
                status_code=503,
            )
        # D5 (INCIDENT.md 2026-08-15): this endpoint answered ``healthy`` for
        # 3h20m through a total action-queue freeze because it never touched
        # the action path — it is served by the bridge surface, which was the
        # half of the process that stayed alive. The liveness view below is
        # read from in-process counters the poller maintains: no database
        # query, no lock, no await. That is deliberate. A DB read here would
        # need the GIL and would therefore HANG under exactly the condition it
        # exists to detect, taking the target group down with it instead of
        # reporting the problem.
        liveness = ACTION_PATH_LIVENESS.snapshot()
        # ``degraded`` still answers 200: the bridge surface IS serving, and
        # failing the probe would make a supervisor kill a process whose
        # messaging half is healthy. The status string is the alarm; the HTTP
        # code stays honest about what this endpoint can actually attest.
        status = "degraded" if liveness["action_path_stalled"] else "healthy"
        return JSONResponse(
            content={"status": status, "action_path": liveness},
            status_code=200,
        )


# ---------------------------------------------------------------------------
# Bridge lifecycle: open / close / events
# ---------------------------------------------------------------------------


def _register_bridge_lifecycle_routes(
    app: FastAPI,
    *,
    bridge_manager: BridgeSessionManager,
    peer_registry: PeerRegistry,
    agent_messaging_service: Any,
    long_poll_timeout_s: int,
    inference_provider_clear: Callable[[str], int] | None = None,
    autonomic_on_close: Callable[[str], str] | None = None,
) -> None:
    @app.post(f"{API_PREFIX}/open")
    async def open_bridge(
        body: OpenBridgeBody = Body(default_factory=OpenBridgeBody),  # noqa: B008
    ) -> JSONResponse:
        # Bridge open is the one operation that needs the solet
        # name — the session_id_factory bound onto the manager closes
        # over it, so the handler doesn't need it explicitly.
        bridge = bridge_manager.open(
            solet_name="",
            parent_pid=body.parent_pid,
        )
        # §34.6 sender attribution: park the caller's opaque session key on the
        # in-memory bridge state ONLY. No registry write, no agent_instance_id,
        # no delivery route — an unregistered caller stays unregistered. The
        # identity is derived server-side from the REGISTERED binding this key
        # resolves to, at trigger-data-build time.
        bridge.caller_agent_session_id = body.caller_agent_session_id.strip()
        return JSONResponse(
            content={
                "bridge_id": bridge.bridge_id,
                "session_id": bridge.session_id,
                "long_poll_timeout_seconds": long_poll_timeout_s,
            },
            status_code=200,
        )

    @app.post(f"{API_PREFIX}/{{bridge_id}}/close")
    async def close_bridge(
        bridge_id: str,
        body: dict[str, Any] = Body(default_factory=dict),  # noqa: B008,ARG001
    ) -> JSONResponse:
        # REL-09: the SAME full cleanup the idle sweeper runs — sidecar
        # clear + tombstone (D-IF7, pre-unregister so list_by_bridge sees
        # the bindings), the sys:autonomic Trigger-2 hook (INF-01 §D.9,
        # pre-unregister for the same reason), then the registry
        # unregister. Runs BEFORE bridge close so a concurrent peer_send
        # racing the close cannot resolve to a dead bridge; without the
        # unregister, open/close cycles accumulate zombie bindings and
        # peer_send eventually hits peer_ambiguous.
        run_full_bridge_cleanup(
            bridge_id,
            inference_provider_clear=inference_provider_clear,
            autonomic_on_close=autonomic_on_close,
            unregister=peer_registry.unregister,
        )
        bridge_manager.close(bridge_id)
        return JSONResponse(content={"status": "closed"}, status_code=200)

    @app.get(f"{API_PREFIX}/{{bridge_id}}/events")
    async def events_bridge(
        bridge_id: str,
        after: int = -1,
    ) -> JSONResponse:
        try:
            acked, events = await bridge_manager.events_after(
                bridge_id,
                after,
                timeout_s=long_poll_timeout_s,
            )
        except BridgeNotFoundError:
            return _bridge_not_found(bridge_id)
        # An actively long-polling registered client (the no-MCP watcher) is
        # alive — bump its binding so "last active" liveness agrees with the
        # delivery path, same as peer_inbox does. Without this, a watcher that
        # only ever long-polls looks inactive to binding-liveness consumers
        # while its bridge keeps answering, one half of the persisted_silent
        # black hole (field-observed on a live deployment).
        binding = _lookup_binding_for_bridge(peer_registry, bridge_id)
        if binding is not None:
            peer_registry.touch_binding(binding.agent_instance_id)
            # A watcher's cursor ack proves the acked events streamed into its
            # watch output — the pull equivalent of entering a turn. Stamps
            # role rows consumed. MCP-transport bridges confirm via
            # /peer/drain instead — never here (their forwarder drains events
            # without the model having read them).
            if binding.is_watcher and acked:
                _consume_watcher_acked_events(agent_messaging_service, acked)
        next_cursor = events[-1].cursor if events else after
        return JSONResponse(
            content={
                "events": [dataclasses.asdict(e) for e in events],
                "next_cursor": next_cursor,
            },
            status_code=200,
        )


# ---------------------------------------------------------------------------
# Platform surface: process_* / download
# ---------------------------------------------------------------------------


def _register_platform_surface_routes(
    app: FastAPI,
    *,
    platform_surface: PlatformSurface,
    bridge_manager: BridgeSessionManager,
) -> None:
    @app.post(f"{API_PREFIX}/{{bridge_id}}/process/search")
    async def process_search_route(
        bridge_id: str,
        body: ProcessSearchBody,
    ) -> JSONResponse:
        # M5 §14.7: pass bridge_id through so process_search applies the
        # bridge session's per-session allowlist on top of global policy.
        try:
            payload = platform_surface.process_search(
                query=body.query,
                max_results=body.max_results,
                bridge_id=bridge_id,
            )
        except BridgeError as exc:
            return _bridge_error_response(exc)
        return JSONResponse(content=payload, status_code=200)

    @app.post(f"{API_PREFIX}/{{bridge_id}}/process/schema")
    async def process_schema_route(
        bridge_id: str,
        body: ProcessSchemaBody,
    ) -> JSONResponse:
        # M5 §14.7: pass bridge_id so the schema lookup is gated by the
        # bridge session's allowlist before the discovery call runs.
        try:
            payload = platform_surface.process_schema(
                process_key=body.process_key,
                bridge_id=bridge_id,
            )
        except BridgeError as exc:
            return _bridge_error_response(exc)
        return JSONResponse(content=payload, status_code=200)

    @app.post(f"{API_PREFIX}/{{bridge_id}}/process/call")
    async def process_call_route(
        bridge_id: str,
        body: ProcessCallBody,
    ) -> JSONResponse:
        bridge = bridge_manager.get(bridge_id)
        if bridge is None or bridge.closed:
            return _bridge_not_found(bridge_id)
        trigger_data: dict[str, Any] = {
            "bridge_id": bridge_id,
            "session_id": bridge.session_id,
        }
        if body.reason is not None:
            trigger_data["reason"] = body.reason
        try:
            payload = platform_surface.process_call(
                process_key=body.process_key,
                arguments=body.arguments,
                trigger_data=trigger_data,
            )
        except BridgeError as exc:
            return _bridge_error_response(exc)
        return JSONResponse(content=payload, status_code=200)

    @app.get(f"{API_PREFIX}/{{bridge_id}}/process/result/{{action_id}}")
    async def process_result_route(
        bridge_id: str,
        action_id: str,
    ) -> JSONResponse:
        _ = bridge_id
        try:
            payload = platform_surface.process_result(action_id=action_id)
        except BridgeError as exc:
            return _bridge_error_response(exc)
        return JSONResponse(content=payload, status_code=200)

    @app.get(f"{API_PREFIX}/{{bridge_id}}/download/{{blob_id}}")
    async def download_route(
        bridge_id: str,
        blob_id: str,
    ) -> Response:
        _ = bridge_id
        try:
            blob = platform_surface.download(blob_id=blob_id)
        except BridgeError as exc:
            return _bridge_error_response(exc)
        return Response(
            content=blob.content,
            media_type=blob.mime_type,
            headers={
                "Content-Disposition": (f'attachment; filename="{blob.filename}"'),
            },
        )


# ---------------------------------------------------------------------------
# Peer routes: register / list / send / inbox
# ---------------------------------------------------------------------------


def _managed_session_registration_backfill(
    state_service: Any | None,
    *,
    agent_instance_id: str,
    agent_id: str,
    agent_session_id: str,
    session_label: str = "",
) -> None:
    """Fleet session-management D1 (§3.2/§5) registration-hook fix: fires the
    ``spawning -> live`` edge and backfills ``agent_session_id``/``agent_id``
    on an existing spawned row. When neither primary nor recovered spawn
    identity resolves, it creates the honest operator inventory row: it has
    identity and joinability, but no fabricated report-by or TTL contract.
    It is an operator-existing-checkout row, never a provisioned lane
    worktree. ``model`` and ``effort`` deliberately remain empty here
    (iss_f2ca1599): the generic registration envelope contains neither and
    the server cannot truthfully recover the operator's local launcher flags.
    NEVER raises: a fault here is loud but registration MUST still succeed, mirroring
    :func:`_state_table_self_refresh`'s posture.
    """
    if state_service is None:
        logger.warning(
            "peer/register: state_service unbound — managed_session "
            "registration backfill skipped (agi=%s)",
            agent_instance_id,
        )
        return
    try:
        matched_existing = backfill_registration(
            state_service,
            agent_instance_id=agent_instance_id,
            agent_id=agent_id,
            agent_session_id=agent_session_id,
        )
        if matched_existing:
            return
        insert_managed_session(
            state_service,
            ManagedSessionSpec(
                agent_instance_id=agent_instance_id,
                lane_id="",
                brief_ref="",
                work_class="",
                budget_line="",
                host=OPERATOR_HOST,
                report_by_seconds=0,
                ttl_seconds=0,
                directed_by="registration",
                provisioning_mode="operator_existing_checkout",
                # The effective label is what peer_registry accepted for this
                # registration.  An empty label is legitimate, but cannot be
                # an honest local identity, so retain the unique registering
                # instance id rather than minting an empty collision key.
                local_name=session_label or agent_instance_id,
            ),
        )
        if not backfill_registration(
            state_service,
            agent_instance_id=agent_instance_id,
            agent_id=agent_id,
            agent_session_id=agent_session_id,
        ):
            raise RuntimeError(
                "peer/register: inserted operator managed_session row could not be backfilled"
            )
    except Exception:  # noqa: BLE001 — best-effort; registration MUST still succeed
        logger.exception(
            "peer/register: managed_session registration backfill FAULTED "
            "(agi=%s) — registration kept 200, but the session's ledger row "
            "(if any) was NOT updated",
            agent_instance_id,
        )


def _state_table_self_refresh(
    state_service: Any | None,
    *,
    agent_session_id: str,
    new_agent_instance_id: str,
) -> str:
    """Reconnect self-refresh: re-point every role this session holds in the
    ``agent_role_binding`` STATE TABLE to the rotated ``agent_instance_id`` (S2).

    Replaces the retired address-book self-refresh. The state table is the SOLE
    resolution authority (v10 Control #2.C), so re-pointing it is what actually
    keeps role wakes reaching a reconnected holder (whose ``agent_instance_id``
    rotated). Filtered on the stable ``agent_session_id`` — one CAS re-points ALL
    held roles. NEVER raises: a self-refresh fault is loud but the bridge
    registration MUST still succeed (200).

    Tokens: ``rerouted:<n>`` (re-pointed n held roles) / ``no_roles`` (session
    holds none) / ``no_session_key`` (S1.5: launched without a stable
    AGENT_SESSION_ID -> self-refresh disabled, LOUD) / ``no_state_service`` /
    ``error`` (a state fault — logged loud with traceback, registration kept).
    """
    if not agent_session_id or agent_session_id == UNCLAIMED_SESSION_ID:
        logger.warning(
            "peer/register: no stable agent_session_id (%r) — this session's roles "
            "will NOT survive reconnect (state-table self-refresh disabled). Launch "
            "with AGENT_SESSION_ID exported to enable reconnect survival.",
            agent_session_id,
        )
        return "no_session_key"
    if state_service is None:
        logger.warning(
            "peer/register: state_service unbound — role self-refresh skipped (session %r)",
            agent_session_id,
        )
        return "no_state_service"
    try:
        rerouted = refresh_role_binding_cas(
            state_service,
            agent_session_id=agent_session_id,
            new_agent_instance_id=new_agent_instance_id,
        )
    except Exception:  # noqa: BLE001 — a self-refresh fault is loud-but-non-fatal; the bridge MUST still register
        # Systemic tradeoff (one-shot-at-register, NO retry): during a reconnect
        # STORM a state fault brings the cohort up 200 with roles NOT re-pointed —
        # they strand until the next reconnect / re-claim. Accepted + LOUD (this
        # error log fires per fault); a retry/queue is a separate hardening call.
        logger.exception(
            "peer/register: state-table role self-refresh FAULTED (session %r, new "
            "agi=%s); registration kept 200 but held roles were NOT re-pointed and "
            "will strand until re-claim",
            agent_session_id,
            new_agent_instance_id,
        )
        return "error"
    if rerouted >= 1:
        logger.info(
            "peer/register: re-pointed %d role(s) to agent_instance_id=%s on "
            "reconnect (session %r)",
            rerouted,
            new_agent_instance_id,
            agent_session_id,
        )
        return f"rerouted:{rerouted}"
    return "no_roles"


def _session_role_held_token(
    state_service: Any | None,
    *,
    session_role: str,
    agent_session_id: str,
    self_refresh: str,
) -> str:
    """Does this session STILL hold its configured standing role?

    Answered on ``peer/register`` — an INFRA route — so the forwarder's
    steady-state re-assert can skip a blind re-claim. The claim path runs through
    ``/process/call``, which :mod:`route_activity` classifies MODEL_INITIATED;
    issuing it from the poll loop stamps ``last_model_activity_at`` with NO model
    turn, which can mark an owed IMPORTANT wake to an idle session consumed. That
    is the F1 class the classification exists to prevent, and the re-assert runs
    every few minutes forever, so it accumulates.

    Tokens: ``held`` / ``not_held`` / ``unknown``. ``unknown`` means "cannot
    answer" (no configured role, no stable session key, no state service, a
    faulted self-refresh, or a malformed binding row) — the forwarder then claims
    exactly as it did before, so an unanswerable check can never suppress a
    genuine recovery. A malformed row is logged LOUD rather than swallowed, but
    never fails the registration: same loud-but-non-fatal posture as the
    self-refresh helpers above.

    ``self_refresh == "error"`` is NOT answerable and must not read as ``held``.
    :func:`holds_role` compares the stable ``agent_session_id`` ALONE — it cannot
    see whether the binding's ``agent_instance_id`` pointer is live. When the
    re-point CAS just faulted, the binding is exactly the "held by my session id
    but pointing at my DEAD instance" state whose documented remedy is the
    re-claim (see :func:`_state_table_self_refresh`: "will strand until
    re-claim"). Answering ``held`` there would skip the one thing that heals it.
    """
    if not session_role:
        return "unknown"
    if self_refresh == "error":
        return "unknown"
    if not agent_session_id or agent_session_id == UNCLAIMED_SESSION_ID:
        return "unknown"
    if state_service is None:
        return "unknown"
    try:
        held = holds_role(state_service, session_role, agent_session_id)
    except Exception:  # noqa: BLE001 — never fail a registration on this probe
        logger.exception(
            "peer/register: session_role_held probe FAULTED for role %r "
            "(session %r); registration kept 200 and the caller will re-claim "
            "as before",
            session_role,
            agent_session_id,
        )
        return "unknown"
    return "held" if held else "not_held"


# A4 (2026-08-04): _direct_wake_self_refresh + _role_wake_self_refresh
# (the REL-05/H1-role owed-row reconnect re-home helpers) retired here — their
# sole purpose was keeping the retiring escalation/consumption-reconcile join
# keys (rehome_owed_direct_wakes / rehome_owed_role_wakes) correct across a
# reconnect. _state_table_self_refresh (role BINDING re-point) is unrelated
# and stays untouched.


def _effective_registration_agent_session_id(
    peer_registry: PeerRegistry,
    *,
    agent_instance_id: str,
    incoming_agent_session_id: str,
) -> str:
    """Preserve a known logical-session key across empty auto-registers."""
    if incoming_agent_session_id and incoming_agent_session_id != UNCLAIMED_SESSION_ID:
        return incoming_agent_session_id
    stored_agent_session_id = peer_registry.agent_session_id_for_instance(
        agent_instance_id,
    )
    if stored_agent_session_id and stored_agent_session_id != UNCLAIMED_SESSION_ID:
        return stored_agent_session_id
    return incoming_agent_session_id


def _register_peer_routes(
    app: FastAPI,
    *,
    bridge_manager: BridgeSessionManager,
    peer_registry: PeerRegistry,
    agent_messaging_service: Any,
    state_service: Any | None = None,
    inference_provider_register: Callable[..., None] | None = None,
    autonomic_on_register: Callable[..., str] | None = None,
    re_emit_window_s: float = 300.0,
    re_emit_cap: int = 3,
) -> None:
    @app.post(f"{API_PREFIX}/{{bridge_id}}/peer/register")
    async def peer_register_route(
        bridge_id: str,
        body: PeerRegisterBody,
    ) -> JSONResponse:
        bridge = bridge_manager.get(bridge_id)
        if bridge is None or bridge.closed:
            return _bridge_not_found(bridge_id)
        agent_id = body.agent_id.strip()
        if not _AGENT_ID_RE.fullmatch(agent_id):
            return _validation_error(
                "invalid_agent_id",
                "agent_id must match [A-Za-z0-9._-]{1,64}",
            )
        agent_instance_id = body.agent_instance_id.strip()
        if not agent_instance_id:
            return _validation_error(
                "missing_agent_instance_id",
                "agent_instance_id is required",
            )
        bridge.agent_instance_id = agent_instance_id
        if body.parent_pid is not None:
            bridge.parent_pid = body.parent_pid
        effective_agent_session_id = _effective_registration_agent_session_id(
            peer_registry,
            agent_instance_id=agent_instance_id,
            incoming_agent_session_id=body.agent_session_id,
        )
        # WS-2e §4.3.3a — refuse to bind under a session id a DIFFERENT LIVE
        # session already holds. Registering here would not merely add a row:
        # the S2 self-refresh below re-points EVERY role that session holds at
        # the newcomer's instance, silently, which is how a watcher armed from
        # another session's shell inherits its AGENT_SESSION_ID and takes its
        # routes. Refused BEFORE register so nothing is mutated on the way out.
        refusal = _session_id_conflict(
            peer_registry=peer_registry,
            bridge_manager=bridge_manager,
            agent_session_id=effective_agent_session_id,
            agent_instance_id=agent_instance_id,
        )
        if refusal is not None:
            return refusal
        # iss_0cc6f884 — a DEAD incumbent under this session id is ordinarily
        # subprocess succession and is deliberately allowed above. When the dead
        # incumbent belonged to a DIFFERENT host process, this registration is
        # adopting an inherited id, not resuming its own: downgrade to the
        # degraded (self-refresh-disabled) binding so the CAS below cannot hand
        # it that session's roles. The row still registers and still appears in
        # peer_list — labelled, never hidden.
        orphan_session_id_downgraded = _orphan_session_id_downgrade(
            peer_registry=peer_registry,
            bridge_manager=bridge_manager,
            agent_session_id=effective_agent_session_id,
            agent_instance_id=agent_instance_id,
            parent_pid=body.parent_pid,
        )
        if orphan_session_id_downgraded:
            logger.warning(
                "orphan_session_id: bridge %s agi=%s parent_pid=%s registered "
                "under agent_session_id %r last held by a DEAD binding of a "
                "DIFFERENT host process; clearing the session id for this "
                "registration so the reconnect self-refresh cannot re-point "
                "that session's role bindings to it. The peer row is kept and "
                "remains addressable by agent_instance_id; claim any role "
                "explicitly. Export a distinct AGENT_SESSION_ID for this "
                "process to register without the downgrade (iss_0cc6f884).",
                bridge_id,
                agent_instance_id,
                body.parent_pid,
                effective_agent_session_id,
            )
            effective_agent_session_id = ""
        operator_host: dict[str, str] = {}
        operator_claude_host: dict[str, str] = {}
        claude_host_ref = body.operator_tmux_host
        implicit_claude_enrichment = (
            not claude_host_ref and agent_id == "claude_code"
        )
        if implicit_claude_enrichment:
            try:
                claude_host_ref = claude_operator_tmux_host_ref(body.session_label)
            except OperatorHostQualificationError as exc:
                logger.warning(
                    "peer/register: Claude operator-host discovery failed; "
                    "continuing ordinary registration (agi=%s): %s",
                    agent_instance_id,
                    exc,
                )
        if body.operator_tmux_host or claude_host_ref:
            try:
                if agent_id == "codex":
                    operator_host = register_operator_codex_host(
                        state_service,
                        OperatorCodexRegistration(
                            host_ref=body.operator_tmux_host,
                            agent_id=agent_id,
                            agent_instance_id=agent_instance_id,
                            agent_session_id=effective_agent_session_id,
                            session_label=body.session_label,
                            parent_pid=body.parent_pid,
                            watcher_declared=body.watcher_declared,
                        ),
                    )
                elif agent_id == "claude_code":
                    operator_claude_host = register_operator_claude_host(
                        state_service,
                        OperatorClaudeRegistration(
                            host_ref=claude_host_ref,
                            agent_id=agent_id,
                            agent_instance_id=agent_instance_id,
                            agent_session_id=effective_agent_session_id,
                            session_label=body.session_label,
                            parent_pid=body.parent_pid,
                            watcher_declared=body.watcher_declared,
                        ),
                    )
                else:
                    raise OperatorHostQualificationError(
                        "operator tmux host is supported only for Codex or Claude",
                    )
            except (OperatorHostQualificationError, StateOperationError) as exc:
                if implicit_claude_enrichment:
                    logger.warning(
                        "peer/register: Claude operator-host qualification "
                        "failed; continuing ordinary registration (agi=%s): %s",
                        agent_instance_id,
                        exc,
                    )
                    operator_claude_host = {}
                else:
                    return _validation_error("operator_host_unqualified", str(exc))
        # Import here to avoid a circular import at module load time;
        # models is pulled in via TYPE_CHECKING for the type hints.
        from .models import BridgeBinding  # noqa: PLC0415

        binding = BridgeBinding(
            bridge_id=bridge_id,
            agent_id=agent_id,
            agent_instance_id=agent_instance_id,
            session_label=body.session_label,
            parent_pid=body.parent_pid,
            agent_session_id=effective_agent_session_id,
            wake_capable=body.wake_capable,
            watcher_declared=body.watcher_declared,
        )
        # ``register`` returns the EFFECTIVE label — the preserve-on-empty
        # path (2026-06-01 §4.2) restores a stored label when the incoming
        # one is empty (auto-reconnect's stale subprocess cache). The
        # response + the bridge.session_label cache MUST reflect that
        # restored value, otherwise the peer-side reconnect announcement
        # interpolates the wrong (empty) label.
        # §4.3.2: the label sweep now spares a LIVE different-session row.
        # The predicate is supplied here because the registry is a store
        # wrapper with no bridge manager; passing it explicitly is what makes
        # the knob real rather than a default nobody reaches.
        effective_label = peer_registry.register(
            binding,
            is_live=lambda existing: binding_is_live(
                bridge_manager=bridge_manager,
                binding=existing,
                window_seconds=bridge_manager.binding_liveness_window_s,
            ),
        )
        bridge.session_label = effective_label
        # D-IF7 sidecar populate (v4 §4) — bind the per-bridge inference
        # vertex AFTER peer_registry.register succeeds so the wrapper can
        # resolve a provider for this agent_instance_id on its next
        # process_error / process_results call. Best-effort try/except —
        # sidecar bind failures must not block the registration response.
        if inference_provider_register is not None and body.provides_inference:
            try:
                inference_provider_register(
                    bridge_id=bridge_id,
                    agent_instance_id=agent_instance_id,
                    agent_id=agent_id,
                    session_label=effective_label,
                )
            except Exception:  # noqa: BLE001 — sidecar populate is best-effort
                logger.warning(
                    "inference_provider_register raised for bridge %s "
                    "agent_instance_id=%s; registration kept; provider sidecar "
                    "WILL be missing for this peer",
                    bridge_id,
                    agent_instance_id,
                    exc_info=True,
                )
        self_refresh_action = _state_table_self_refresh(
            state_service,
            agent_session_id=effective_agent_session_id,
            new_agent_instance_id=agent_instance_id,
        )
        # D1 registration hook (§3.2/§5, Dawn ruling arm-11511b07) — fires
        # spawning->live and backfills agent_session_id/agent_id on this
        # agent_instance_id's managed_session row, or births an honest
        # operator inventory row when no spawn lineage exists. It deliberately
        # runs after S2's reconnect CAS: registration must preserve that
        # recovery operation's state-write and failure semantics.
        _managed_session_registration_backfill(
            state_service,
            agent_instance_id=agent_instance_id,
            agent_id=agent_id,
            agent_session_id=effective_agent_session_id,
            session_label=effective_label,
        )
        # Steady-state re-assert support: answer "do I still hold my configured
        # role?" HERE, on the INFRA route, so the caller never has to ask via the
        # MODEL_INITIATED /process/call claim path just to find out. Read AFTER
        # the self-refresh above, so a reconnecting holder that just re-pointed
        # its binding reads back as held (no spurious re-claim).
        session_role_held = _session_role_held_token(
            state_service,
            session_role=body.session_role.strip(),
            agent_session_id=effective_agent_session_id,
            self_refresh=self_refresh_action,
        )
        # INF-01 Trigger-1 (§D.9): fill a vacant / dead-holder sys:autonomic
        # slot with the just-registered session. Runs AFTER the sidecar
        # populate (a provider must exist for the claim to serve) and AFTER
        # the S2 self-refresh (a reconnecting holder re-points its binding
        # first, so it reads back as live-held → no-op). Best-effort: the
        # registration response never fails on lifecycle policy.
        autonomic_action = "disabled"
        if autonomic_on_register is not None:
            try:
                autonomic_action = autonomic_on_register(
                    agent_id=agent_id,
                    agent_instance_id=agent_instance_id,
                    agent_session_id=effective_agent_session_id,
                    session_label=effective_label,
                    provides_inference=body.provides_inference,
                )
            except Exception:  # noqa: BLE001 — lifecycle policy never blocks a registration
                logger.warning(
                    "autonomic_on_register raised for bridge %s agi=%s; registration kept",
                    bridge_id,
                    agent_instance_id,
                    exc_info=True,
                )
                autonomic_action = "error"
        return JSONResponse(
            content={
                "agent_id": agent_id,
                "agent_instance_id": agent_instance_id,
                "agent_session_id": effective_agent_session_id,
                "session_label": effective_label,
                "parent_pid": body.parent_pid,
                "bridge_id": bridge_id,
                "status": "registered",
                "operator_codex_host": operator_host,
                "operator_claude_host": operator_claude_host,
                "self_refresh": self_refresh_action,
                "autonomic": autonomic_action,
                "session_role_held": session_role_held,
                # Named so the registering peer can SEE the downgrade rather
                # than infer it from an unexpectedly empty agent_session_id.
                "session_id_downgrade": (
                    ORPHAN_SESSION_ID_DOWNGRADE
                    if orphan_session_id_downgraded
                    else ""
                ),
            },
            status_code=200,
        )

    @app.get(f"{API_PREFIX}/{{bridge_id}}/peer/list")
    async def peer_list_route(bridge_id: str) -> JSONResponse:
        _ = bridge_id
        snapshot = peer_registry.list_agent_ids()
        return JSONResponse(content=serialize_peer_list(snapshot), status_code=200)

    @app.get(f"{API_PREFIX}/{{bridge_id}}/current_identity")
    async def current_identity_route(bridge_id: str) -> JSONResponse:
        bridge = bridge_manager.get(bridge_id)
        if bridge is None or bridge.closed:
            return _bridge_not_found(bridge_id)
        binding = _lookup_binding_for_bridge(peer_registry, bridge_id)
        if binding is None:
            return _validation_error(
                "identity_not_registered",
                "this bridge has not registered an agent_id; POST /peer/register first",
            )
        roles_or_error = _read_roles_held(
            state_service,
            agent_instance_id=binding.agent_instance_id,
        )
        if isinstance(roles_or_error, JSONResponse):
            return roles_or_error
        return JSONResponse(
            content={
                "transport": "bridge_http",
                "solet_name": "",
                "agent_id": binding.agent_id,
                "agent_instance_id": binding.agent_instance_id,
                "agent_session_id": binding.agent_session_id,
                "session_label": binding.session_label,
                "bridge_id": bridge_id,
                "mcp_session_id": "",
                "roles_held": roles_or_error,
                "identity_trust": "bridge_registered",
                "streamable_no_auth": False,
            },
            status_code=200,
        )

    @app.post(f"{API_PREFIX}/{{bridge_id}}/peer/send")
    async def peer_send_route(
        bridge_id: str,
        body: PeerSendBody,
    ) -> JSONResponse:
        return _peer_send_impl(
            bridge_id=bridge_id,
            body=body,
            bridge_manager=bridge_manager,
            peer_registry=peer_registry,
            agent_messaging_service=agent_messaging_service,
            state_service=state_service,
        )

    @app.post(f"{API_PREFIX}/{{bridge_id}}/peer/claim_role")
    async def peer_claim_role_route(
        bridge_id: str,
        body: PeerClaimRoleBody,
    ) -> JSONResponse:
        return _peer_claim_role_impl(
            bridge_id=bridge_id,
            body=body,
            bridge_manager=bridge_manager,
            peer_registry=peer_registry,
            agent_messaging_service=agent_messaging_service,
            state_service=state_service,
        )

    @app.post(f"{API_PREFIX}/{{bridge_id}}/peer/send_by_name")
    async def peer_send_by_name_route(
        bridge_id: str,
        body: PeerSendByNameBody,
    ) -> JSONResponse:
        return _peer_send_by_name_impl(
            bridge_id=bridge_id,
            body=body,
            bridge_manager=bridge_manager,
            peer_registry=peer_registry,
            agent_messaging_service=agent_messaging_service,
            state_service=state_service,
        )

    @app.get(f"{API_PREFIX}/{{bridge_id}}/peer/inbox")
    async def peer_inbox_route(
        bridge_id: str,
        after: str | None = None,
        limit: int = 50,
        include_important: bool = True,
        role_after: str | None = None,
        observer: bool = False,
    ) -> JSONResponse:
        bridge = bridge_manager.get(bridge_id)
        if bridge is None or bridge.closed:
            return _bridge_not_found(bridge_id)
        sender_binding = _lookup_binding_for_bridge(peer_registry, bridge_id)
        if sender_binding is None:
            return _validation_error(
                "identity_not_registered",
                "this bridge has not registered an agent_id; POST /peer/register first",
            )
        try:
            after_dt = _parse_iso_after(after)
        except ValueError as exc:
            return _validation_error("invalid_after", str(exc))
        try:
            page = agent_messaging_service.peer_inbox(
                PeerInboxRequest(
                    recipient_agent_id=sender_binding.agent_id,
                    recipient_agent_instance_id=sender_binding.agent_instance_id,
                    recipient_agent_session_id=sender_binding.agent_session_id,
                    after_created_at=after_dt,
                    limit=max(1, min(limit, 100)),
                    include_important=include_important,
                    # Opaque role-section cursor; the service validates it and
                    # raises AgentMessagingError on a malformed/forged token
                    # (caught below → error response). Fail-closed by design.
                    role_after=role_after,
                    # Watch/status catch-up observes pending work; it does not
                    # claim successful display for a model turn.
                    observer=observer or sender_binding.is_watcher,
                ),
            )
        except AgentMessagingError as exc:
            return _agent_messaging_error_response(exc)
        # The bridge calling peer_inbox is alive — bump its binding so
        # peer_list shows recent activity even if no peer_send has run.
        peer_registry.touch_binding(sender_binding.agent_instance_id)
        # The watch client's arm-time catch-up drain prints every returned
        # entry into the watch output — stamp the surfaced role rows
        # consumed. Watcher-only: an MCP session's consumption authority
        # stays the /peer/drain reconcile.
        # A watcher is an observer.  Rendering its transport catch-up cannot
        # create a weak receipt or mutate the strong handover boundary.
        return JSONResponse(
            content=_serialize_peer_inbox(page, sender_binding),
            status_code=200,
        )

    @app.post(f"{API_PREFIX}/{{bridge_id}}/peer/drain")
    async def peer_drain_route(
        bridge_id: str,
        body: PeerDrainBody,
    ) -> JSONResponse:
        # v10 Control #5: return the oldest page of un-CONSUMED IMPORTANT ROLE
        # messages owed to the roles this bridge holds. The binding is derived
        # server-side from ``bridge_id`` (NOT caller-supplied).
        #
        # A4 (2026-08-04): the REL-05 Guard-1 consumption reconcile (this
        # bridge's live last_model_activity_at marking owed rows consumed) and
        # the DIRECT-wake half of this route retired here — Guard 1 ("no
        # model-initiated call since emission") was blind to local-only work;
        # sweep_overdue_sessions + _notify_steward_of_overdue is the sole
        # successor, keyed on the recipient's own report_by promise. Role
        # consumption stays authoritative via /peer/delivered (the forwarder's
        # explicit confirm) and the watcher events-ack path — both untouched.
        bridge = bridge_manager.get(bridge_id)
        if bridge is None or bridge.closed:
            return _bridge_not_found(bridge_id)
        sender_binding = _lookup_binding_for_bridge(peer_registry, bridge_id)
        if sender_binding is None:
            return _validation_error(
                "identity_not_registered",
                "this bridge has not registered an agent_id; POST /peer/register first",
            )
        agent_instance_id = sender_binding.agent_instance_id
        limit = max(1, min(body.limit, 100))
        try:
            rows = agent_messaging_service.list_undelivered_for_instance(
                agent_instance_id=agent_instance_id,
                limit=limit,
                re_emit_window_s=re_emit_window_s,
                cap=re_emit_cap,
            )
        except AgentMessagingError as exc:
            return _agent_messaging_error_response(exc)
        peer_registry.touch_binding(agent_instance_id)
        return JSONResponse(
            content={
                "undelivered": [_serialize_role_drain_row(r) for r in rows],
                # N3: the forwarder marks a re-emit [re-emit n/cap ...] — cap
                # rides the envelope so the client never hard-codes it.
                "re_emit_cap": re_emit_cap,
            },
            status_code=200,
        )

    @app.post(f"{API_PREFIX}/{{bridge_id}}/peer/delivered")
    async def peer_delivered_route(
        bridge_id: str,
        body: PeerDeliveredBody,
    ) -> JSONResponse:
        # v10 Control #5: flip ``delivered=true`` after a successful emit.
        # Idempotent + ownership-fenced (a displaced holder can't mark
        # deliveries) — ``flagged=false`` if the caller no longer holds the role.
        # REL-05: also records the emit bookkeeping (emit_count/last_emitted_at +
        # F3 emitted_to instance) inside the fenced confirm.
        bridge = bridge_manager.get(bridge_id)
        if bridge is None or bridge.closed:
            return _bridge_not_found(bridge_id)
        sender_binding = _lookup_binding_for_bridge(peer_registry, bridge_id)
        if sender_binding is None:
            return _validation_error(
                "identity_not_registered",
                "this bridge has not registered an agent_id; POST /peer/register first",
            )
        try:
            flagged = agent_messaging_service.mark_delivered_for_instance(
                external_id=body.external_id,
                recipient_key=body.recipient_key,
                agent_instance_id=sender_binding.agent_instance_id,
                # H1-role: stamp the STABLE session key beside the instance id so
                # a restart's re-home can find this row (the instance id rotates,
                # this does not).
                agent_session_id=sender_binding.agent_session_id,
                # QUIET-GAP capture: this bridge IS the recipient confirming its
                # own emission, so its CURRENT last_model_activity_at is exactly
                # "the recipient's activity state as of this emission". Frozen on
                # the row so guard 2 cannot drift afterwards.
                activity_at_emission=bridge.last_model_activity_at or None,
            )
        except AgentMessagingError as exc:
            return _agent_messaging_error_response(exc)
        return JSONResponse(content={"flagged": flagged}, status_code=200)


# ---------------------------------------------------------------------------
# peer/send dispatch — the IMPORTANT-marker semantics live here so the
# route handler stays a thin wrapper.
# ---------------------------------------------------------------------------


def _peer_send_impl(
    *,
    bridge_id: str,
    body: PeerSendBody,
    bridge_manager: BridgeSessionManager,
    peer_registry: PeerRegistry,
    agent_messaging_service: Any,
    state_service: Any | None = None,
) -> JSONResponse:
    """Thin route adapter — validate inputs, delegate to :mod:`peer_dispatch`.

    Routing-table membership (bridge existence, sender binding) is the
    HTTP-layer concern: if a request hits ``/bridge/<bridge_id>/peer/send``
    and the bridge is closed, that's a 404 specific to this transport.
    The IMPORTANT-marker semantics + wake-vs-channel dispatch live in
    :func:`agent_messaging_plugin.peer_dispatch.dispatch_peer_send`,
    shared with the streamable transport.
    """
    bridge = bridge_manager.get(bridge_id)
    if bridge is None or bridge.closed:
        return _bridge_not_found(bridge_id)
    sender_binding = _lookup_binding_for_bridge(peer_registry, bridge_id)
    if sender_binding is None:
        return _validation_error(
            "identity_not_registered",
            "this bridge has not registered an agent_id; POST /peer/register first",
        )
    try:
        content = _parse_text_parts(body.content)
    except AgentMessagingError as exc:
        return _agent_messaging_error_response(exc)
    try:
        outcome = dispatch_peer_send(
            bridge_manager=bridge_manager,
            peer_registry=peer_registry,
            agent_messaging_service=agent_messaging_service,
            state_service=state_service,
            sender_bridge_id=bridge_id,
            sender_agent_id=sender_binding.agent_id,
            sender_agent_instance_id=sender_binding.agent_instance_id,
            sender_session_label=sender_binding.session_label,
            sender_parent_pid=sender_binding.parent_pid,
            peer_id=body.peer_id,
            peer_agent_instance_id=body.peer_agent_instance_id,
            content=content,
            peer_agent_session_id=body.peer_agent_session_id,
            # WS-2c V4, direct-send half: the sender's own role (when it holds
            # exactly one) so the reply address survives the SENDER's next
            # reconnect. Previously omitted entirely, so every direct send handed
            # out an instance id that a restart wave invalidates.
            reply_to_role=sole_role_for_reply_address(
                state_service,
                sender_binding.agent_instance_id,
            ),
        )
    except PeerAmbiguousError as exc:
        return JSONResponse(
            content={
                "code": "peer_ambiguous",
                "message": str(exc),
                "peer_agent_id": exc.peer_agent_id,
                "candidate_instance_ids": exc.candidate_instance_ids,
                "candidate_session_labels": exc.candidate_session_labels,
            },
            status_code=400,
        )
    except PeerUnreachableError as exc:
        return JSONResponse(
            content={
                "code": "peer_unreachable",
                "message": str(exc),
                **exc.response_data(),
            },
            status_code=404,
        )
    except BridgeNotFoundError as exc:
        return JSONResponse(
            content={
                "code": "peer_unreachable",
                "message": (f"recipient bridge is no longer registered: {exc}"),
            },
            status_code=404,
        )
    except BridgeQueueFullError:
        return JSONResponse(
            content={
                "code": "peer_queue_full",
                "message": (f"recipient {body.peer_id} event queue is full"),
            },
            status_code=503,
        )
    except NativeWakeError as exc:
        return JSONResponse(
            content={"code": "native_wake_failed", "message": str(exc)},
            status_code=502,
        )
    except AgentMessagingError as exc:
        return _agent_messaging_error_response(exc)
    return JSONResponse(content=outcome.to_payload(), status_code=200)


def _peer_send_by_name_sender(
    *,
    bridge_id: str,
    body: PeerSendByNameBody,
    bridge_manager: BridgeSessionManager,
    peer_registry: PeerRegistry,
    state_service: Any | None,
) -> BridgeBinding | JSONResponse:
    """Validate the send envelope, returning the sender binding or an error response."""
    bridge = bridge_manager.get(bridge_id)
    if bridge is None or bridge.closed:
        return _bridge_not_found(bridge_id)
    sender_binding = _lookup_binding_for_bridge(peer_registry, bridge_id)
    if sender_binding is None:
        return _validation_error(
            "identity_not_registered",
            "this bridge has not registered an agent_id; POST /peer/register first",
        )
    if not body.name.strip():
        return _validation_error(
            "missing_name",
            "peer_send_by_name requires a non-empty role 'name'.",
        )
    if not body.content:
        return _validation_error(
            "missing_content",
            "peer_send_by_name requires non-empty content.",
        )
    if state_service is None:
        return _state_unavailable(
            "state_service is not bound; cannot resolve role bindings.",
        )
    return sender_binding


def _resolve_role_for_send(
    state_service: Any,
    role_name: str,
) -> ResolvedRole | JSONResponse:
    """Resolve ``role_name`` to a routable target, or return an error response."""
    try:
        role = resolve_role_binding(state_service, role_name)
    except RoleBindingVacantError as exc:
        return JSONResponse(
            content={
                "code": "peer_role_vacant",
                "message": str(exc),
                "name": role_name,
            },
            status_code=404,
        )
    except StateOperationError as exc:
        return _state_unavailable(str(exc))
    if not role.agent_instance_id:
        return _validation_error(
            "peer_role_malformed",
            (
                f"role binding for {role_name!r} is missing agent_instance_id; "
                "re-claim the role first"
            ),
        )
    return role


_CLAIM_ROLE_ERROR_STATUS: Final[dict[str, int]] = {
    "missing_argument": 400,
    "missing_session_id": 400,
    "system_slot_claim_denied": 403,
    # A LIVE holder refused displacement (§4.3.2). 409 rather than 403: this is
    # a conflict with current state, not a permissions decision, and the state
    # can change. MUST be listed — the lookup below defaults an unmapped code
    # to 400, so an omission here would not fail loudly, it would just make the
    # wire status quietly wrong while the code stayed correct.
    "role_held_live": 409,
    "state_service_unavailable": 503,
}


def _peer_claim_role_impl(
    *,
    bridge_id: str,
    body: PeerClaimRoleBody,
    bridge_manager: BridgeSessionManager,
    peer_registry: PeerRegistry,
    agent_messaging_service: Any,
    state_service: Any | None,
) -> JSONResponse:
    """Claim a role for the CALLING bridge, synchronously (the INFRA transport).

    Synchronous by requirement, not by taste. ``peer_claim_role`` is an EDGE
    process, so reaching it through ``/process/call`` enqueues an action and
    delivers the outcome later as an EDGE_SINK ``bridge_delivery_result``
    notification — which is the second half of the defect this route exists to
    remove: the forwarder's re-assert fired one such notification at every tick,
    forever. Returning the outcome in the response body means there is no
    fan-out to leak, and it lets the forwarder drop its ``/process/result``
    poll (itself a MODEL_INITIATED route it had no business touching).

    Identity comes from the bridge's REGISTERED binding, never the request body
    — see :class:`PeerClaimRoleBody`. ``call_context`` is ``None`` because no
    plugin principal is involved, which makes the §6.1 gate refuse every
    ``sys:`` name from this transport. That is deliberate and fail-closed: the
    forwarder claims standing roles, never system slots.
    """
    bridge = bridge_manager.get(bridge_id)
    if bridge is None or bridge.closed:
        return _bridge_not_found(bridge_id)
    name = body.name.strip()
    if not name:
        return _validation_error("missing_role_name", "name is required")
    binding = _lookup_binding_for_bridge(peer_registry, bridge_id)
    if binding is None:
        return _validation_error(
            "peer_identity_unregistered",
            (
                "this bridge has no registered peer identity; "
                "call peer/register before claiming a role"
            ),
        )
    result = claim_role_for_session(
        origin=RoleClaimOrigin.INFRA,
        name=name,
        agent_id=binding.agent_id,
        agent_instance_id=binding.agent_instance_id,
        agent_session_id=binding.agent_session_id,
        session_label=binding.session_label,
        state_service=state_service,
        bridge_manager=bridge_manager,
        peer_registry=peer_registry,
        agent_messaging_service=agent_messaging_service,
        call_context=None,
        takeover=body.takeover,
    )
    if isinstance(result, RoleClaimFailure):
        return JSONResponse(
            content={"code": result.code, "message": result.message},
            status_code=_CLAIM_ROLE_ERROR_STATUS.get(result.code, 400),
        )
    return JSONResponse(content=result.to_public(), status_code=200)


def _peer_send_by_name_impl(
    *,
    bridge_id: str,
    body: PeerSendByNameBody,
    bridge_manager: BridgeSessionManager,
    peer_registry: PeerRegistry,
    agent_messaging_service: Any,
    state_service: Any | None,
) -> JSONResponse:
    """Resolve a durable role name and dispatch from the current bridge identity."""
    sender = _peer_send_by_name_sender(
        bridge_id=bridge_id,
        body=body,
        bridge_manager=bridge_manager,
        peer_registry=peer_registry,
        state_service=state_service,
    )
    if isinstance(sender, JSONResponse):
        return sender
    role_name = body.name.strip()
    role = _resolve_role_for_send(state_service, role_name)
    if isinstance(role, JSONResponse):
        return role
    try:
        outcome = dispatch_role_send(
            bridge_manager=bridge_manager,
            peer_registry=peer_registry,
            agent_messaging_service=agent_messaging_service,
            state_service=state_service,
            role_name=role_name,
            role=role,
            sender_bridge_id=bridge_id,
            sender_agent_id=sender.agent_id,
            sender_agent_instance_id=sender.agent_instance_id,
            sender_session_label=sender.session_label,
            sender_principal_kind=SENDER_PRINCIPAL_KIND_STDIO_AGENT,
            sender_parent_pid=sender.parent_pid,
            content=[TextPart(type="text", text=body.content)],
            message_id=f"arm-{secrets.token_hex(16)}",
            # WS-2c V4 (MEASURED): this was hardcoded ``""`` since the route
            # landed, so EVERY role-addressed send originated by an MCP session
            # handed the recipient an INSTANCE reply-to — on a healthy fleet, not
            # only after churn. The platform verb resolved it correctly all along
            # (``plugin.py::peer_send_by_name``); only this transport caller froze
            # the default.
            reply_to_role=sole_role_for_reply_address(
                state_service,
                sender.agent_instance_id,
            ),
        )
    except AgentMessagingError as exc:
        return _agent_messaging_error_response(exc)
    return JSONResponse(content=outcome.to_payload(), status_code=200)


# ---------------------------------------------------------------------------
# Helpers — parsing
# ---------------------------------------------------------------------------


def _parse_text_parts(value: list[dict[str, Any]]) -> list[TextPart]:
    if not value:
        raise AgentMessagingError(
            "content must be a non-empty list of text parts",
        )
    parts: list[TextPart] = []
    for raw in value:
        kind = str(raw.get("type") or "text")
        if kind != "text":
            raise AgentMessagingError(
                f"content part type {kind!r} is not supported",
            )
        parts.append(TextPart(type="text", text=str(raw.get("text") or "")))
    return parts


def _parse_iso_after(value: str | None) -> _dt | None:
    if not value:
        return None
    try:
        return _dt.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"after must be ISO-8601 datetime: {exc}") from exc


# ---------------------------------------------------------------------------
# Helpers — serialization
# ---------------------------------------------------------------------------


def _serialize_peer_inbox(page: Any, sender_binding: BridgeBinding) -> dict[str, Any]:
    return serialize_peer_inbox_page(page, sender_binding.agent_instance_id)


def _role_drain_prose(content_raw: object) -> str:
    """Join stored role-message parts to prose, stripping the IMPORTANT marker.

    ``persist_role_message`` stores the ORIGINAL content (the IMPORTANT marker
    still embedded in the first part). The live wake path delivers
    ``prose[marker_match.end():]`` (marker stripped). The repair drain MUST
    deliver the SAME marker-stripped prose so a re-delivered message reads
    identically to a live one — no leading "IMPORTANT" artifact, byte-for-byte
    delivery parity. The content round-trips from the JSONB column as a native
    list of ``{"type","text"}`` dicts (psycopg deserialises JSONB → objects).
    """
    if not isinstance(content_raw, list):
        return ""
    prose = "\n".join(str(part.get("text") or "") for part in content_raw if isinstance(part, dict))
    marker_match = IMPORTANT_MARKER_RE.match(prose)
    if marker_match is not None:
        return prose[marker_match.end() :]
    return prose


def _serialize_role_drain_row(row: dict[str, Any]) -> dict[str, Any]:
    """Project an undelivered role envelope row to the drain wire shape.

    Carries exactly what the forwarder's repair loop needs to emit the message
    and flip its flag: the deterministic ``external_id`` + ``recipient_key``
    (echoed back to ``POST /peer/delivered``, ownership-fenced), the
    ``message_id`` + sender provenance (the targeted-reply meta on the native
    wake), the synthetic thread handle, and the marker-stripped content prose
    (delivery parity with the live wake path — see :func:`_role_drain_prose`).
    REL-05: ``emit_count`` + ``created_at`` ride along so a re-emit can be marked
    ``[re-emit n/cap ... originally sent <created_at>]`` (N3).
    """
    return {
        "external_id": row.get("external_id"),
        "recipient_key": row.get("recipient_key"),
        "message_id": row.get("message_id"),
        "sender_agent_id": row.get("sender_agent_id"),
        "sender_agent_instance_id": row.get("sender_agent_instance_id"),
        "sender_session_label": row.get("sender_session_label"),
        "thread_id": row.get("thread_id"),
        "important": bool(row.get("important", False)),
        "emit_count": int(row.get("emit_count") or 0),
        "created_at": _iso_or_empty(row.get("created_at")),
        "content": _role_drain_prose(row.get("content")),
    }


def _iso_or_empty(value: object) -> str:
    """A stored timestamp cell as a string (empty for a missing/non-string cell)."""
    return value if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# Helpers — error responses
# ---------------------------------------------------------------------------


def _bridge_error_response(exc: BridgeError) -> JSONResponse:
    status = _BRIDGE_ERROR_STATUS.get(exc.code, 400)
    return JSONResponse(
        content={"code": exc.code, "message": exc.message},
        status_code=status,
    )


def _agent_messaging_error_response(exc: AgentMessagingError) -> JSONResponse:
    return JSONResponse(
        content={"code": exc.code, "message": str(exc)},
        status_code=exc.http_status,
    )


def _bridge_not_found(bridge_id: str) -> JSONResponse:
    return JSONResponse(
        content={
            "code": "bridge_not_found",
            "message": f"bridge {bridge_id} not found or closed",
        },
        status_code=404,
    )


def _validation_error(code: str, message: str) -> JSONResponse:
    return JSONResponse(
        content={"code": code, "message": message},
        status_code=400,
    )


# WS-2e §4.3.3a. 409 rather than 400 or 403: this is a conflict with current
# state, not a malformed request or a permissions decision, and the state can
# change (the incumbent may exit). Same reasoning as `role_held_live`.
SESSION_ID_BOUND_TO_LIVE_SESSION: Final[str] = "session_id_bound_to_live_session"


ORPHAN_SESSION_ID_DOWNGRADE: Final[str] = "orphan_session_id"


def _orphan_session_id_downgrade(
    *,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    agent_session_id: str,
    agent_instance_id: str,
    parent_pid: int | None,
) -> bool:
    """Is this registration ADOPTING a dead session's id rather than resuming
    its own? (iss_0cc6f884, measured 2026-09-05)

    THE DEFECT THIS EXISTS TO CLOSE. ``_resolve_agent_session_id`` in the MCP
    bridge reads ``AGENT_SESSION_ID`` from the environment unconditionally, so
    ANY long-lived parent that once had it exported leaks it to every child
    that does not override it — measured live on two unrelated parents: a
    Claude Code pre-warmed spare forked from ``claude daemon run`` (which
    captured the fleet's primary coordination role, refused the real seat's
    ``peer_claim_role`` with
    ``role_held_live``, and left 40 role-addressed messages unread), and the
    tmux server at pid 36540, whose argv still carries Git-Controller's
    ``AGENT_SESSION_ID`` for every pane it hosts (iss_c2a66577). Registering
    under an inherited id is not merely an extra row: ``refresh_role_binding_cas``
    re-points EVERY role binding filtered on ``agent_session_id`` ALONE, with no
    claim and no error, so inheriting the id silently inherits the ROLES.

    WHY THIS IS NOT JUST A TIGHTER ``_session_id_conflict``. That gate refuses a
    LIVE incumbent and deliberately waves through a DEAD one, because a dead
    incumbent is the ordinary subprocess-succession path: the bridge subprocess
    restarts under the same host process, minting a fresh ``agent_instance_id``
    under the same session id. That allowance is CORRECT and must stay — removing
    it would break every legitimate restart while still not stopping capture,
    since capture happens through the CAS, not through the liveness check.

    So the discriminator here is not liveness and not the host: it is whether the
    dead incumbent was THIS host process. A bridge resuming its own session keeps
    its ``parent_pid`` (the same ``claude``/``codex`` process re-spawned its MCP
    child); a process that merely inherited the variable has a parent_pid of its
    own. Same parent → succession, allowed unchanged. Different parent → adoption,
    downgraded.

    DOWNGRADE, NOT REFUSE, and the choice is load-bearing. Returning a 409 would
    leave the process unregistered and therefore INVISIBLE in ``peer_list`` — and
    a hidden row is worse than a labelled one for exactly this defect, whose whole
    cost was an unexplained holder nobody could see. Instead the caller registers
    normally and keeps its row, with its ``agent_session_id`` cleared to ``""`` —
    the pre-existing, already-correct degraded binding whose self-refresh is
    disabled. ``refresh_role_binding_cas`` fails closed on an empty id, so the
    downgraded registration can inherit nothing, while remaining fully visible,
    fully addressable by instance id, and free to claim a role EXPLICITLY.

    Deliberately conservative — every ambiguous case allows, because this gate
    can only ever subtract trust from a registration that would otherwise
    succeed:

    * no session id, or no incumbent under it — nothing to adopt;
    * the SAME instance id — a re-arm/reconnect, untouched;
    * a LIVE incumbent — ``_session_id_conflict``'s 409 owns that case. This
      gate declines it explicitly rather than relying on running second: a
      downgrade and a refusal are different answers, and whichever gate ran
      first would silently win. Checking liveness here costs one call and makes
      the two gates partition the space by MEANING (live conflict vs dead
      adoption) instead of by call order;
    * EITHER parent_pid unknown — no evidence to convict on, so the existing
      succession behaviour is preserved (a client that sends no ``parent_pid``,
      e.g. Streamable HTTP, is never downgraded by this rule). This is a real
      residual: a caller that omits ``parent_pid`` keeps the old hole. Closing it
      needs ``parent_pid`` to become mandatory on the register body, which is a
      contract change and is NOT in this unit.

    Residual risk, stated: pid REUSE could let an adopting process coincidentally
    match a week-dead incumbent's ``parent_pid`` and be waved through. That is a
    strictly smaller hole than the one being closed, and it fails in the safe
    direction relative to refusing legitimate restarts.
    """
    if not agent_session_id or agent_session_id == UNCLAIMED_SESSION_ID:
        return False
    if parent_pid is None:
        return False
    try:
        incumbent = peer_registry.resolve_by_agent_session_id(agent_session_id)
    except PeerSessionAmbiguousError:
        # Ambiguity is handled (and refused) by _session_id_conflict, which runs
        # first. Reaching here would mean that gate changed; do not double-judge.
        return False
    if incumbent is None or incumbent.agent_instance_id == agent_instance_id:
        return False
    if binding_is_live(
        bridge_manager=bridge_manager,
        binding=incumbent,
        window_seconds=bridge_manager.binding_liveness_window_s,
    ):
        return False
    if incumbent.parent_pid is None:
        return False
    return incumbent.parent_pid != parent_pid


def _session_id_conflict(
    *,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    agent_session_id: str,
    agent_instance_id: str,
) -> JSONResponse | None:
    """Refuse a register that would steal a LIVE session's routes (§4.3.3a).

    Returns a 409 refusal, or ``None`` to allow. Three cases pass deliberately:

    * no session id — nothing to conflict with (streamable / older clients);
    * SAME instance id — that is a re-arm or reconnect, which must keep its
      existing replace semantics;
    * a DEAD incumbent — subprocess succession, which must stay cheap. This is
      why the gate is liveness-gated rather than existence-gated: an existence
      gate would refuse every legitimate restart, since a restart always brings
      a new instance id under the same session id.

    Ambiguity counts as LIVE and refuses: >1 binding for one session id is
    precisely the confusion this exists to stop, so it must never wave through.
    """
    if not agent_session_id:
        return None
    try:
        incumbent = peer_registry.resolve_by_agent_session_id(agent_session_id)
    except PeerSessionAmbiguousError as exc:
        return JSONResponse(
            content={
                "code": SESSION_ID_BOUND_TO_LIVE_SESSION,
                "message": (
                    f"agent_session_id {agent_session_id!r} already resolves to "
                    f"more than one binding ({exc}); refusing to add another."
                ),
            },
            status_code=409,
        )
    if incumbent is None or incumbent.agent_instance_id == agent_instance_id:
        return None
    if not binding_is_live(
        bridge_manager=bridge_manager,
        binding=incumbent,
        window_seconds=bridge_manager.binding_liveness_window_s,
    ):
        return None
    bridge = bridge_manager.get(incumbent.bridge_id)
    last_seen = bridge.last_seen_at if bridge is not None else "unknown"
    return JSONResponse(
        content={
            "code": SESSION_ID_BOUND_TO_LIVE_SESSION,
            "message": (
                f"agent_session_id {agent_session_id!r} is already held by a LIVE "
                f"session: label {incumbent.session_label!r}, instance "
                f"{incumbent.agent_instance_id}, last seen {last_seen}. "
                f"Registering {agent_instance_id} under it would re-point every "
                f"role that session holds. Export a distinct AGENT_SESSION_ID "
                f"for this process, or stop the incumbent first."
            ),
        },
        status_code=409,
    )


def _state_unavailable(message: str) -> JSONResponse:
    return _bridge_error_response(
        BridgeError("bridge.state_service_unavailable", message),
    )


def _read_roles_held(
    state_service: Any | None,
    *,
    agent_instance_id: str,
) -> list[str] | JSONResponse:
    if state_service is None:
        return _state_unavailable(
            "state_service is not bound; cannot read agent_role_binding.",
        )
    try:
        return list_roles_for_agent_instance(state_service, agent_instance_id)
    except StateOperationError as exc:
        return _state_unavailable(str(exc))


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def _lookup_binding_for_bridge(
    peer_registry: PeerRegistry,
    bridge_id: str,
) -> BridgeBinding | None:
    """Find the BridgeBinding registered for ``bridge_id`` (linear scan)."""
    for bindings in peer_registry.list_agent_ids().values():
        for binding in bindings:
            if binding.bridge_id == bridge_id:
                return binding
    return None


def _consume_watcher_acked_events(
    agent_messaging_service: Any,
    acked: list[QueuedEvent],
) -> None:
    """Stamp watcher-acked IMPORTANT role deliveries consumed.

    Role deliveries are recognised by the Control #5 ``delivery_external_id``
    meta key (stamped on both the native-wake and channel-event transports).
    Predicated (``consumed=false``), so a re-ack or a non-delivery event is a
    no-op.

    A4 (2026-08-04): the direct-wake branch (``message_id``-only meta,
    ``mark_direct_consumed_on_ack``) retired here with the direct-wake outbox
    table it stamped.
    """
    for event in acked:
        if event.event_type not in _WATCHER_DELIVERY_EVENT_TYPES:
            continue
        external_id = str(event.meta.get(META_KEY_DELIVERY_EXTERNAL_ID) or "")
        if external_id:
            agent_messaging_service.mark_role_consumed_on_ack(
                external_id=external_id,
            )


def _consume_watcher_inbox_page(
    agent_messaging_service: Any,
    page: Any,
) -> None:
    """Stamp watcher catch-up-drained IMPORTANT role rows consumed.

    Role entries recover their role name from the synthetic ``role:`` thread
    handle and re-derive the deterministic delivery external_id; the
    predicated mark skips silent and already-consumed rows.

    A4 (2026-08-04): the instance-section (direct-wake) loop retired here —
    it stamped the direct-wake outbox table, which no longer exists;
    ``page.entries`` is still valid message history, just no longer paired
    with an outbox row to mark.
    """
    for entry in page.role_entries:
        role_name = str(entry.thread_id).removeprefix(ROLE_THREAD_PREFIX)
        agent_messaging_service.mark_role_consumed_on_ack(
            external_id=role_message_external_id(
                RECIPIENT_KIND_ROLE,
                role_name,
                str(entry.message.id),
            ),
        )


def _config_int(config: Any, key: str, default: int) -> int:
    if config is None:
        return default
    getter = getattr(config, "get", None)
    if not callable(getter):
        return default
    value = getter(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return int(value)


__all__ = [
    "API_PREFIX",
    "EVENT_PEER_MESSAGE",
    "register_routes",
]
