"""``agent_messaging_plugin`` — consolidated bridge plugin.

This plugin wears three hats (see plugin.yaml for the headline summary):

1. **AgentMessagingServiceInterface** — durable ``core__agent_thread`` /
   ``core__agent_message`` schema host for peer messaging and the
   session-ledger's unscoped thread/message reads.
2. **IOInterfacePlugin** — ``start_interface`` / ``stop_interface`` /
   ``post_message`` / ``get_supported_capabilities``.  The solet
   delivers prose to Claude Code (or any MCP-connected peer) through this surface.
3. **Bridge service** — FastAPI HTTP API on a dynamically allocated
   port, peer registry with multi-instance routing, bridge sessions
   with long-poll event queues, native-wake adapter for
   ``agent_id="claude_code"``, and the bridge-delivery EDGE_SINK pair
   (``deliver_result`` / ``deliver_error``).

The plugin intentionally does NOT register through the service-binding
system (``ServiceName`` enum + ``service_bindings.json``).  Bound
ServiceProviders are skipped from the ``plugin::<name>::*`` registry
namespace by ``process_registry/builder.py::_should_skip_plugin``,
which would hide ``send_peer_message`` / ``peer_send_by_name`` / the
session-lifecycle EDGE processes from ``submit_action_definition``.
``AgentMessagingServiceInterface`` is satisfied by structural
delegation; callers resolve us via
``plugin_manager.plugins["agent_messaging_plugin"]`` and call our
public methods directly.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

from ananta.constants import FRAMEWORK_ASYNC_JOBS_TABLE, FRAMEWORK_NAMESPACE
from ananta.core.actions.action_factory import reject_retired_session_arguments
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
from ananta.core.domain.types import ActionResult, ErrorDetail
from ananta.core.plugins.plugin_base import ServicePlugin
from ananta.core.plugins.profile_manifest import load_manifest_plugin_set
from ananta.core.runtime import find_available_port, write_routerless_bridge_port_file
from ananta.core.state.job_completion_reach import (
    COMPLETION_REACH_KEY,
    REACH_ROLE_INBOX_DELIVERED,
    write_job_metadata,
)
from ananta.error_handling import FrameworkError
from ananta.interfaces import IOInterfacePlugin
from ananta.interfaces.agent_messaging_service_interface import (
    AgentMessagingServiceInterface,
)
from ananta.interfaces.chat_interface_support import build_initial_vertex_action
from ananta.interfaces.edge_process_provider import (
    EdgeProcessDefinition,
    EdgeProcessProvider,
)
from ananta.interfaces.io_capabilities import IOCapability
from ananta.llm.agent_messaging.models import PeerInbox, PeerInboxRequest, TextPart
from ananta.llm.agent_messaging.repository import AgentMessagingRepository
from ananta.llm.agent_messaging.role_binding import (
    SYS_AUTONOMIC_SLOT,
    is_system_role,
)
from ananta.llm.agent_messaging.schema import (
    get_agent_direct_wake_schema,
    get_agent_messaging_schema,
    get_agent_role_message_schema,
    get_role_covered_mark_schema,
    get_role_read_schema,
)
from ananta.llm.agent_messaging.service import (
    AgentMessagingConfig,
    AgentMessagingError,
    AgentMessagingService,
    AgentRequestInvalidError,
)
from ananta.llm.session_ledger.trigger_data import extract_authenticated_principal
from ananta.services.inference_service.completion_request_queue import (
    SERVE_SERVED,
    serve_completion_request,
)
from ananta.services.inference_service.completion_request_schema import (
    COL_CORRELATION as COL_ICR_CORRELATION,
)
from ananta.services.inference_service.completion_request_schema import (
    COL_MESSAGES as COL_ICR_MESSAGES,
)
from ananta.services.inference_service.completion_request_schema import (
    COL_PURPOSE as COL_ICR_PURPOSE,
)
from ananta.services.inference_service.completion_request_schema import (
    COL_REQUEST_ID as COL_ICR_REQUEST_ID,
)
from ananta.services.inference_service.completion_request_schema import (
    COL_RESUME_PROCESS_KEY as COL_ICR_RESUME_PROCESS_KEY,
)

from .autonomic_assignment import AutonomicAssignment
from .bridge_lifecycle import (
    BridgeLifecycleSweeper,
    purge_preboot_bindings,
    run_full_bridge_cleanup,
)
from .bridge_sessions import (
    DEFAULT_BINDING_LIVENESS_WINDOW_S,
    BridgeNotFoundError,
    BridgeQueueFullError,
    BridgeSessionManager,
)
from .budget_report import build_budget_report as lifecycle_build_budget_report
from .caller_provenance import (
    CallerProvenanceError,
)
from .caller_provenance import (
    resolve_caller_provenance as lifecycle_resolve_caller_provenance,
)
from .choreography_verbs import (
    ACTION_GENERATE_CURATION_REPORT,
    ACTION_RESTART_SESSION,
    ACTION_ROTATE_SESSION,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_ERROR,
    JOB_STATUS_PROCESSING,
    JOB_STATUS_QUEUED,
    PROVIDER_PLUGIN_NAME,
    GenerateCurationReportDispatchRequest,
    RestartSessionDispatchRequest,
    RotateSessionDispatchRequest,
    dispatch_generate_curation_report,
    dispatch_restart_session,
    dispatch_rotate_session,
)
from .choreography_verbs import (
    check_choreography_job_status as lifecycle_check_choreography_job_status,
)
from .constants import (
    PLUGIN_NAME,
    SYSTEM_AGENT_ID,
)
from .context_status_verbs import report_context_status as lifecycle_report_context_status
from .context_status_verbs import session_context_status as lifecycle_session_context_status
from .dispatch_tier_selection import TierSelectionError
from .fleet_check_run_verbs import (
    recent_fleet_liveness_runs as lifecycle_recent_fleet_liveness_runs,
)
from .fleet_check_run_verbs import (
    recent_fleet_progress_runs as lifecycle_recent_fleet_progress_runs,
)
from .fleet_check_run_verbs import (
    record_fleet_liveness_run as lifecycle_record_fleet_liveness_run,
)
from .fleet_check_run_verbs import (
    record_fleet_progress_run as lifecycle_record_fleet_progress_run,
)
from .fleet_status import FleetStatusError
from .fleet_status import fleet_status as lifecycle_fleet_status
from .gauge_canary import (
    direct_canary_arrest as canary_direct_arrest,
)
from .gauge_canary import (
    register_synthetic_session as canary_register_synthetic_session,
)
from .gauge_canary import (
    retire_gauge_canary as canary_retire_gauge_canary,
)
from .gauge_canary import (
    verify_canary as canary_verify,
)
from .gauge_canary_store import CanaryError, register_canary
from .gauge_notice_record_store import MAX_READ_ROWS as GAUGE_NOTICE_READ_ROWS
from .gauge_notice_records import (
    gauge_notice_records as gauge_read_notice_records,
)
from .gauge_series import (
    session_context_status_history as gauge_session_context_status_history,
)
from .held_authorization_verbs import (
    list_held_authorizations as lifecycle_list_held_authorizations,
)
from .held_authorization_verbs import (
    record_held_authorization as lifecycle_record_held_authorization,
)
from .held_authorization_verbs import (
    retire_held_authorization as lifecycle_retire_held_authorization,
)
from .http_routes import register_routes
from .inbox_consumption_verbs import (
    report_inbox_consumption as lifecycle_report_inbox_consumption,
)
from .inbox_consumption_verbs import (
    session_inbox_consumption_status as lifecycle_session_inbox_consumption_status,
)
from .managed_dispatch import (
    DispatchActor,
    DispatchError,
    DispatchSpec,
    mint_dispatch_id,
)
from .managed_dispatch import (
    dispatch_managed_work as lifecycle_dispatch_managed_work,
)
from .managed_dispatch import (
    managed_dispatch_events as lifecycle_managed_dispatch_events,
)
from .managed_dispatch import (
    managed_dispatch_inventory as lifecycle_managed_dispatch_inventory,
)
from .managed_dispatch import (
    managed_dispatch_status as lifecycle_managed_dispatch_status,
)
from .managed_dispatch import (
    report_managed_dispatch as lifecycle_report_managed_dispatch,
)
from .managed_dispatch import (
    resolve_managed_dispatch as lifecycle_resolve_managed_dispatch,
)
from .mcp_streamable import (
    BearerVerifier,
    StreamableSessionManager,
    build_streamable_router,
)
from .mcp_streamable.auth import HMAC_KEY_BYTE_LENGTH, PermissiveBearerVerifier
from .mcp_streamable.oauth import (
    DEFAULT_TOKEN_TTL_SECONDS,
    OAuthEndpoints,
    build_dynamic_oauth_router,
    build_endpoints,
    build_oauth_router,
)
from .mcp_streamable.router import STREAMABLE_ALIAS_PATH, STREAMABLE_PATH
from .memory_curation_verbs import (
    build_curation_report,
    build_fact_index,
    origin_tag,
    resolve_memory_id_by_slug,
    slug_to_slot_tag,
)
from .message_important_backfill import backfill_message_important
from .model_capability_store import CatalogError
from .model_capability_verbs import (
    read_model_capability_catalog as catalog_read,
)
from .model_capability_verbs import (
    record_model_capability_cell as catalog_record_cell,
)
from .model_capability_verbs import (
    refresh_model_capability_catalog as catalog_refresh,
)
from .model_capability_verbs import (
    seed_model_capability_catalog as catalog_seed,
)
from .model_capability_verbs import (
    select_dispatch_tier as catalog_select_tier,
)
from .model_dispatch_policy import DispatchPolicyError
from .operator_session_liveness_reconciliation import (
    reconcile_operator_session_liveness as lifecycle_reconcile_operator_session_liveness,
)
from .peer_dispatch import (
    EVENT_POST_MESSAGE,
    NativeWakeError,
    build_wake_reply_hint,
    dispatch_peer_send,
    dispatch_role_send,
)
from .peer_inbox_view import serialize_peer_inbox_page
from .peer_list_view import serialize_peer_list
from .peer_registry import (
    PeerAmbiguousError,
    PeerRegistry,
    PeerSessionAmbiguousError,
    PeerUnreachableError,
)
from .platform_surface import PlatformSurface
from .process_exposure import ProcessExportPolicy
from .register_unit_client import (
    CONFIG_PSOLET_CLI,
    PsoletRegisterUnitClient,
    resolve_psolet_cli,
)
from .role_binding_store import (
    RoleBindingMalformedError,
    RoleBindingVacantError,
    holds_role,
    release_role_binding_v4,
    resolve_role_binding,
    resolve_role_binding_v4,
    run_cutover_migration_at_readiness,
    sole_role_for_reply_address,
)
from .role_claim import (
    RoleClaimFailure,
    RoleClaimOrigin,
    claim_role_for_session,
    send_handover_notice,
)
from .role_class_backfill import backfill_role_class
from .role_message_consumed_backfill import backfill_role_message_consumed
from .rotation_self_notice import (
    BandEdgeLatch,
    SelfNoticeCounts,
    sweep_rotation_self_notice,
)
from .route_activity import make_model_activity_middleware
from .schema import (
    get_agent_role_binding_schema_definition,
    get_peer_binding_schema_definition,
    get_role_model_schema_definition,
    get_session_lifecycle_schema_definition,
)
from .sender_provenance import (
    SENDER_PRINCIPAL_KIND_OAUTH_CLIENT,
    SENDER_PRINCIPAL_KIND_STDIO_AGENT,
    SENDER_PRINCIPAL_KIND_SYSTEM,
)
from .session_claude_mapping_ingest import (
    detect_hook_absent_sessions as lifecycle_detect_hook_absent_sessions,
)
from .session_claude_mapping_ingest import (
    drain_session_claude_mapping_spool as lifecycle_drain_session_claude_mapping_spool,
)
from .session_claude_mapping_store import (
    list_session_claude_mappings as lifecycle_list_session_claude_mappings,
)
from .session_context_status_store import GAUGE_HISTORY_RETENTION
from .session_inference_provider import SessionInferenceProvider
from .session_lifecycle_store import format_directed_by
from .session_lifecycle_store import resolve_lane_charter as lifecycle_resolve_lane_charter
from .session_lifecycle_verbs import (
    LIST_SESSIONS_DEFAULT_LIMIT,
    LIST_SESSIONS_MAX_LIMIT,
    ArmSessionDependencyRequest,
    CaptureLaneCharterRequest,
    LegislateRoleRequest,
    SpawnSessionRequest,
    VerbError,
)
from .session_lifecycle_verbs import arm_session_dependency as lifecycle_arm_session_dependency
from .session_lifecycle_verbs import capture_lane_charter as lifecycle_capture_lane_charter
from .session_lifecycle_verbs import clear_session as lifecycle_clear_session
from .session_lifecycle_verbs import compact_session as lifecycle_compact_session
from .session_lifecycle_verbs import drive_session as lifecycle_drive_session
from .session_lifecycle_verbs import legislate_role as lifecycle_legislate_role
from .session_lifecycle_verbs import list_sessions as lifecycle_list_sessions
from .session_lifecycle_verbs import report_alive as lifecycle_report_alive
from .session_lifecycle_verbs import resolve_local_name as lifecycle_resolve_local_name
from .session_lifecycle_verbs import (
    resolve_provisioned_role_class as lifecycle_resolve_provisioned_role_class,
)
from .session_lifecycle_verbs import retire_session as lifecycle_retire_session
from .session_lifecycle_verbs import session_status as lifecycle_session_status
from .session_lifecycle_verbs import spawn_session as lifecycle_spawn_session
from .session_lifecycle_verbs import terminate_session as lifecycle_terminate_session
from .session_role_claim_store import delete_session_role_claim_if_still_holds
from .session_sweep import (
    NoticeLatch,
    SessionRoleClaimPruner,
    StewardNoticeCounts,
    sweep_deadline_dependencies,
    sweep_gauge_coverage,
    sweep_gauge_staleness,
    sweep_lane_closed_dependencies,
    sweep_managed_dispatches,
    sweep_overdue_sessions,
    sweep_rotation_due_sessions,
    sweep_unregistered_spawning_sessions,
)
from .system_slots import (
    validate_system_slot_declarations,
)

if TYPE_CHECKING:  # pragma: no cover — type-only references
    from collections.abc import Callable, Mapping

    from ananta.core.orchestration.interfaces import ISessionManager
    from ananta.core.orchestration.managers.flow_manager import FlowManager
    from ananta.core.state.async_job_manager import AsyncJobManager
    from ananta.llm.agent_messaging.models import (
        AgentThreadMessagesPage,
        AgentThreadsPage,
        ListAgentThreadsRequest,
        PeerSendRequest,
        PeerSendResult,
        ReadThreadMessagesRequest,
    )
    from ananta.types.schema_types import SchemaDefinition
    from fastapi import FastAPI

    from .models import BridgeBinding, BridgeSessionState

logger = logging.getLogger(__name__)
SYSTEM_SCHEDULER_ID: Final[str] = "system:scheduler"
SYSTEM_SCHEDULER_LABEL: Final[str] = "System (Scheduler)"
SYSTEM_JOB_COMPLETION_ID: Final[str] = "system:job-completion"
"""Sender sentinel for a job completion pushed into a role inbox (Lane W).

Deliberately NOT :data:`SYSTEM_SCHEDULER_ID`. Two reasons, both load-bearing:
a completion is not scheduler-originated and labelling it so would be a false
provenance claim; and peer threads are keyed on
``(sender_bridge_id, peer_instance)``, so its own sentinel gives completions
their own thread per recipient instead of interleaving them with scheduler
traffic.
"""
SYSTEM_JOB_COMPLETION_LABEL: Final[str] = "System (Job Completion)"
# peer_inbox page size. Deliberately far below the route's 50: a freshly
# /clear'd Coordinator-Dawn measured a 422,513-character page at 50 instance +
# 50 role entries on 2026-08-01 — roughly 4KB per entry, because an entry
# carries the whole message. ``limit`` bounds the COUNT, so bytes are the
# caller's arithmetic, not the platform's promise: 5 is a page a session can
# read and still act on, and the two cursors exist to fetch the rest.
PEER_INBOX_DEFAULT_LIMIT: Final[int] = 5
PEER_INBOX_MIN_LIMIT: Final[int] = 1
# Parity with the /peer/inbox route's own clamp — one ceiling, both surfaces.
PEER_INBOX_MAX_LIMIT: Final[int] = 100


def _git_controller_launcher_report(role_name: str) -> dict[str, object]:
    """Report the rendered launcher gate state without changing it.

    The on-request provisioning verb may create a controller, but an unarmed
    launcher gate remains an operator-visible deployment condition.  Reading
    the two generated coding-agent launchers keeps that distinction explicit;
    this helper never repairs their contents.
    """
    app_home = os.environ.get("APP_HOME", "").strip()
    codex_line = f'export GIT_CONTROLLER_NAME="{role_name}"'
    if not app_home:
        return {
            "status": "unavailable",
            "armed": False,
            "reason": "APP_HOME is unset, so this deployment's launcher paths cannot be resolved.",
            "manual_remedy_lines": [],
            "launchers": [],
        }
    root = Path(app_home).resolve().parent
    solet_name = os.environ.get("SOLET_NAME", "").strip()
    if not solet_name:
        return {
            "status": "unavailable",
            "armed": False,
            "reason": "SOLET_NAME is unset, so this deployment's launcher names cannot be resolved.",
            "manual_remedy_lines": [],
            "launchers": [],
        }
    launchers = [
        (root / "client" / "bin" / f"claude-{solet_name}", f'GIT_CONTROLLER_NAME="{role_name}" \\'),
        (root / "client" / "bin" / f"codex-{solet_name}", codex_line),
    ]
    details: list[dict[str, object]] = []
    for path, manual_remedy_line in launchers:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            details.append(
                {
                    "path": str(path),
                    "armed": False,
                    "manual_remedy_line": manual_remedy_line,
                    "error": str(exc),
                }
            )
            continue
        details.append(
            {
                "path": str(path),
                "armed": 'GIT_CONTROLLER_NAME="' in content
                and 'GIT_CONTROLLER_NAME=""' not in content,
                "manual_remedy_line": manual_remedy_line,
            }
        )
    armed = bool(details) and all(item["armed"] is True for item in details)
    return {
        "status": "armed" if armed else "not_armed",
        "armed": armed,
        "launchers": details,
    }


def _require_provisioner_authority(
    state_service: Any,
    spawned_by_role: str,
    actor: DispatchActor,
) -> None:
    if not holds_role(state_service, spawned_by_role, actor.agent_session_id):
        raise DispatchError(
            "coordinator_authority_denied",
            "Authenticated caller does not hold spawned_by_role.",
        )


def _provisioned_live_role_holder(
    plugin: AgentMessagingPlugin,
    state_service: Any,
    role_name: str,
) -> dict[str, object] | None:
    """Return the current live holder before provisioning a named role.

    The durable binding supplies the holder instance id; ``peer_holds_role``
    then derives that instance's current stable session id from the live peer
    registry and re-checks the binding. A stale binding remains replaceable,
    while a live one is returned without starting another worker.
    """
    try:
        binding = resolve_role_binding(state_service, role_name)
    except (RoleBindingVacantError, RoleBindingMalformedError):
        return None
    if not binding.agent_instance_id:
        return None
    outcome = plugin.peer_holds_role(
        {
            "parameters": {
                "name": role_name,
                "agent_instance_id": binding.agent_instance_id,
            },
        },
        {},
    )
    data = outcome.get("data")
    if outcome.get("action_status") != "completed" or not isinstance(data, dict):
        return None
    if data.get("holds") is not True:
        return None
    return {
        "name": role_name,
        "agent_instance_id": binding.agent_instance_id,
        "agent_session_id": str(data.get("agent_session_id") or ""),
        "delivery_route_attached": data.get("delivery_route_attached") is True,
    }


def _provision_legislation(
    state_service: Any,
    *,
    needs_legislation: bool,
    role_name: str,
    role_class: str,
    brief_ref: str,
    directed_by: str,
) -> dict[str, object]:
    if not needs_legislation:
        return {"action": "not_required"}
    return lifecycle_legislate_role(
        state_service,
        LegislateRoleRequest(
            name=role_name,
            role_class=role_class,
            brief_ref=brief_ref,
            directed_by=directed_by,
        ),
    )


class _UploadRouteAuth(Protocol):
    """Keyword-callable matching the chatgpt + claude_ai source plugins'
    ``AuthCheckProtocol``. Their AuthCheckProtocols define a single
    method ``__call__(self, authorization_header: str | None) -> object``;
    Pyright treats ``Callable[[str | None], object]`` as positional-only
    and rejects the assignment. Declaring the structural shape here keeps
    the local closure compatible with both source plugins without
    importing either at module load (they are profile-conditional).
    """

    def __call__(self, authorization_header: str | None) -> object: ...


# Bridge namespace error tokens for deliver_result / deliver_error.
_ERR_NO_ACTIVE_BRIDGE = "bridge.no_active_bridge"
_ERR_QUEUE_FULL = "bridge.queue_full"
_ERR_PROCESS_CALL_FAILED = "bridge.process_call_failed"

# Standard IO interface error tokens for post_message.
_ERR_SESSION_NOT_BOUND = "session_not_bound"
_ERR_VALIDATION = "ValidationError"

# Default per-message size guard used until config provider has run.
_DEFAULT_MAX_MESSAGE_CHARS = 120_000

# ◆R2 (Phase 5): bound on the disconnected-inference-instance tombstone.
# The tombstone records agent_instance_ids that HELD a SessionInferenceProvider
# earlier in this process lifetime but whose bridge has since dropped, so the
# vertex resolver can DEFER (never silent-Qwen) a flow explicitly bound to a
# now-disconnected roleless session, distinguishing it from a never-bound flow.
# LRU-bounded so a long-running process with churny reconnects can't leak.
# N1 (Rev-C ruling 2026-07-02): eviction past this cap is a DOCUMENTED,
# principled tradeoff — for a ROLELESS instance (no durable ◆R2 identity),
# "never silent-Qwen a bound session" and "never-bound/streamable MUST go
# DEFAULT" are irreconcilable under bounded memory. Role-bound flows are
# IMMUNE (the ◆R2 durable path never DEFAULTs). Eviction is made LOUD (see
# _clear_inference_providers_for_bridge) so the rare roleless-aged-out case
# is VISIBLE. 2048 (small strings) widens the practical roleless window.
_INFERENCE_TOMBSTONE_CAP = 2048

# How long we wait for the uvicorn server thread to acknowledge startup
# before assuming the bind silently failed.
_SERVER_START_TIMEOUT_S = 10.0
_SERVER_JOIN_TIMEOUT_S = 5.0

# The plugin whose presence in the active manifest means "this solet
# has a blue-green router" (D11 ruling R1). Must match
# macos_self_deployment_plugin.constants.PLUGIN_NAME — duplicated as a
# plain string rather than cross-plugin-imported (no plugin in this
# codebase imports another plugin's package directly).
_ROUTER_PLUGIN_NAME = "macos_self_deployment_plugin"


# Vault entry holding the HMAC secret that signs Streamable HTTP MCP
# bearer tokens (HS256). Generated on first solet boot if absent;
# never rotated except by explicit operator action (vault entry
# replacement + solet restart invalidates all outstanding tokens, which
# is the expected one-time disruption window). See
# ``workbench/2026-05-24_hmac_bearer_tokens_design.md`` §3.
#
# Scoped per master plan §3.3.1: <solet>.<plugin>.<credential>.
# Built at module-import time from SOLET_NAME; fast-fails if unset.
# Per W-ADDRESS-BOOK-RENAME §A.2.4 path b — write under the scoped name
# directly so W-VAULT-CALLER-ENFORCE Tier 2 doesn't need a compat-mode
# entry for this row. The lazy-create path in `_load_or_create_bearer_hmac_key`
# below now writes under the scoped name on first streamable-MCP boot.
def _coerce_takeover(raw: object) -> bool:
    """Coerce the ``takeover`` parameter FAIL-CLOSED.

    Only a real ``True`` or the exact string ``"true"`` (case-insensitively,
    trimmed) authorizes displacing a live holder. Everything else — including
    the strings ``"false"`` and ``"0"``, ``None``, and any non-boolean type —
    is ``False``.

    A plain truthiness test would be wrong here in the one direction that
    matters: this parameter crosses a JSON transport, so a caller sending
    ``"false"`` as a STRING would take the role, which is the exact opposite of
    what they asked for, on the exact parameter whose purpose is that it must be
    deliberate. Refusing an intended takeover costs one retry with a clear
    message; performing an unintended one silently moves another session's
    deliveries.
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() == "true"
    return False


def _coerce_dry_run(raw: object) -> bool:
    """Return a safe ``dry_run`` value from a public process parameter.

    Omission and ``None`` are report-only. JSON booleans retain their value;
    bridge strings are accepted only for their spelled boolean values. Any
    other value fails before the reconciliation engine can apply a transition.
    """
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    raise ValueError("dry_run must be a boolean or the string 'true' or 'false'")


def _bearer_hmac_key_vault_name() -> str:
    name = os.environ.get("SOLET_NAME", "").strip()
    if not name:
        raise RuntimeError(
            "agent_messaging_plugin: SOLET_NAME env var is required to "
            "resolve the scoped bearer_token_hmac_key vault entry name.",
        )
    return f"{name}.agent_messaging_plugin.bearer_token_hmac_key"


_BEARER_HMAC_KEY_VAULT_NAME = _bearer_hmac_key_vault_name()


class VaultEnvelopeError(RuntimeError):
    """Raised by :func:`_vault_retrieve_value` when a vault ``retrieve``
    call returns anything other than a well-formed hit or a well-formed
    miss — a REAL vault error (``action_status == "error"``, e.g. keychain
    unavailable) or an unrecognized envelope shape. This is deliberately
    NOT swallowed into "key absent": a caller treating a malformed/error
    envelope as a miss is exactly the vault-read envelope bug this seam
    exists to close (Dax Part 36 §36.2) — a weird envelope must never
    silently trigger a read-or-create caller's mint-and-store path."""


def _vault_retrieve_value(vault: Any, name: str) -> str | None:
    """Single seam for every vault ``retrieve`` consumer in this plugin.

    Keys on the REAL ``macos_vault_plugin`` ``ActionResult`` envelope
    (``plugin.py::_success``/``_not_found``): ``action_status`` is
    ``"completed"`` for BOTH a hit and a genuine miss — the vault never
    returns a top-level ``"status"`` key, so a caller keyed on that (the
    §36.2 bug) never recognizes a hit and re-mints on every read. A hit
    and a miss are distinguished by ``data`` shape instead: a hit carries
    a present, non-empty string ``data["value"]``; a well-formed miss
    carries ``data["found"] is False`` (``_not_found``'s exact shape,
    no ``"value"`` key at all).

    Returns the stored string value on a well-formed hit, ``None`` on a
    well-formed miss, and raises :class:`VaultEnvelopeError` on anything
    else — fast-fail, no silent fallback.
    """
    retrieved = vault.retrieve(name)
    if not isinstance(retrieved, dict):
        raise VaultEnvelopeError(
            f"vault.retrieve({name!r}) returned {type(retrieved).__name__}, not a dict envelope",
        )
    if retrieved.get("action_status") != ActionStatus.COMPLETED.value:
        raise VaultEnvelopeError(
            f"vault.retrieve({name!r}) did not complete: "
            f"action_status={retrieved.get('action_status')!r}, "
            f"error={retrieved.get('error')!r}",
        )
    data = retrieved.get("data")
    if not isinstance(data, dict):
        raise VaultEnvelopeError(
            f"vault.retrieve({name!r}) returned action_status="
            f"'completed' with a non-dict data payload: {data!r}",
        )
    value = data.get("value")
    if isinstance(value, str) and value:
        return value
    if data.get("found") is False:
        return None
    raise VaultEnvelopeError(
        f"vault.retrieve({name!r}) returned action_status='completed' "
        "with an unrecognized data shape (neither a hit with a "
        f"non-empty 'value' nor a well-formed miss with found=False): {data!r}",
    )


def _load_or_create_bearer_hmac_key(vault: Any) -> bytes:
    """Return the solet's HMAC bearer-signing secret as raw bytes.

    Reads from the vault under :data:`_BEARER_HMAC_KEY_VAULT_NAME` via
    :func:`_vault_retrieve_value`; on first boot the entry is absent so
    we mint a fresh ``secrets.token_bytes(HMAC_KEY_BYTE_LENGTH)`` and
    persist its base64 encoding before returning. The value is
    base64-encoded in storage because the vault's ``store`` interface
    accepts a string.
    """
    stored_value = _vault_retrieve_value(vault, _BEARER_HMAC_KEY_VAULT_NAME)
    if stored_value is not None:
        return base64.b64decode(stored_value)
    fresh = secrets.token_bytes(HMAC_KEY_BYTE_LENGTH)
    vault.store(
        _BEARER_HMAC_KEY_VAULT_NAME,
        base64.b64encode(fresh).decode("ascii"),
        tags=["bearer_token", "hmac", "task53"],
        metadata={
            "description": (
                "HMAC secret for Streamable HTTP MCP bearer-token signing "
                "(HS256). Replacing this value invalidates every outstanding "
                "bearer token; connected clients must re-authenticate."
            ),
            "byte_length": str(HMAC_KEY_BYTE_LENGTH),
            "algorithm": "HS256",
        },
    )
    return fresh


@dataclass(frozen=True, slots=True)
class _BridgeRuntimeConfig:
    """Bridge / IO surface runtime config.

    Kept separate from :class:`AgentMessagingConfig` (which is frozen and
    scoped to the agent-messaging service) so the bridge surface can read
    its own settings without forcing the service config to grow new
    fields it doesn't use.
    """

    host: str = "127.0.0.1"
    # Preferred bridge HTTP port. ``None`` (default) -> ``find_available_port``
    # returns an OS-assigned port via ``bind(0)``. Setting this to a
    # fixed value (via the ANANTA_PLUGIN_AGENT_MESSAGING_PLUGIN_PORT env
    # var or plugin yaml) pins the port — used by dry-run solet
    # deployments that need a stable host port mapping (8001:8000) for
    # first-boot orchestration. The port is in-process only: Slice 3 of
    # the bridge-port-routing design eliminated the per-color port file
    # in favor of cross-plugin lookup of ``self.bridge_port`` and direct
    # ``register_color`` calls against the router.
    port: int | None = None
    long_poll_timeout_seconds: int = 25
    bridge_idle_timeout_seconds: int = 3_600
    max_pending_events: int = 200
    max_message_chars: int = _DEFAULT_MAX_MESSAGE_CHARS
    # INF-01 §D.9 Trigger-2: grace window between a sys:autonomic holder's
    # bridge close and the succession check, so a reconnecting holder (whose
    # register re-points its bindings via the S2 self-refresh) is never
    # displaced by a mere reconnect gap. Must comfortably absorb a
    # fleet-wide bridge reconnect (observed reconnects are seconds).
    autonomic_grace_seconds: int = 120
    # REL-09: cadence of the bridge-lifecycle idle sweeper (the driver for
    # BridgeSessionManager.sweep_idle + the full per-bridge cleanup). The
    # idle THRESHOLD stays bridge_idle_timeout_seconds; this knob only
    # bounds detection latency past it.
    bridge_sweep_interval_seconds: int = 300
    # WS-2a W3 / WS-2e §4.3.2 — ONE knob, TWO consumers. A binding counts as
    # LIVE iff it resolves to a bridge whose ``last_seen_at`` is within this
    # window. Both transports long-poll continuously (the events poll holds
    # ~25s server-side and the client re-polls immediately), so a live
    # session's bridge never lags more than ~30s: 90 is >3x the worst-case
    # healthy gap and far under the 3_600s idle sweep, which makes staleness a
    # clean discriminator rather than a heuristic.
    #
    # Consumer 1 (here): dispatch refuses to report ``queued_watcher`` against
    # a bridge nobody is polling — a SIGKILLed watcher leaves its server-side
    # session alive, so ``append_event`` succeeds and the label lies for up to
    # the full idle sweep (~65 min).
    # Consumer 2 (pending operator sign-off): the duplicate-role claim gate.
    binding_liveness_window_seconds: int = DEFAULT_BINDING_LIVENESS_WINDOW_S
    # INF-02: serve window for autonomic-routed completion requests. A
    # pending request whose forward stamp is older than this without a
    # served/failed transition is re-queued by the serve-timeout sweep
    # (riding the bridge-lifecycle sweeper cadence above). Sized for a
    # frontier session mid-task: generous, but never forever.
    completion_serve_window_seconds: int = 900
    # INF-06 reliability: serve window for a forwarded Surface-1 action-decode
    # vertex. A 'forwarded' deferred_vertex row whose forward stamp is older than
    # this without the holder self-executing is re-driven by the forwarded
    # serve-timeout sweep. Holder turns legitimately run minutes (§2f) — a window
    # shorter than normal holder-turn latency re-drives every forward spuriously,
    # so this is generous; the attempts cap bounds the tail.
    forward_serve_window_seconds: int = 900
    # INF-06: monotone re-drive attempts cap for a forwarded vertex. At the cap
    # the row flips to the terminal 'failed' state (durable stall record + loud
    # log) instead of re-driving forever (§2g).
    forward_attempts_cap: int = 5
    # INF-06: age (seconds) after which a terminal 'failed' forwarded-vertex row
    # is hard-deleted by the GC sweep, so the durable stall records never grow
    # unbounded (§8-bis retention rider). Generous — the record stays readable
    # for well over a day of diagnosis before it is reaped.
    terminal_gc_after_seconds: int = 172_800
    # REL-05 (Q1): direct/role IMPORTANT re-emit window + cap. Window = the
    # minimum gap between emissions of one owed message (most sessions turn
    # within it); cap = total emissions (original + re-emits) before escalation.
    re_emit_window_seconds: int = 300
    re_emit_cap: int = 3
    # Streamable HTTP MCP transport — opt-in.  Off by default so the
    # laptop dev mode is unchanged; container deployments flip it on
    # via plugin yaml override and bind to 0.0.0.0:9000 for phone
    # connectivity (host port 9001 fronted by Caddy / mkcert TLS).
    streamable_enabled: bool = False
    streamable_host: str = "0.0.0.0"  # noqa: S104 — container bind, gated by streamable_enabled
    streamable_port: int = 9000
    streamable_allowed_origins: tuple[str, ...] = ()
    streamable_bearer_max_age_seconds: int = 300
    # OAuth 2.1 client_credentials surface for the streamable transport.
    # Required for claude.ai's custom-connector validator: when blank,
    # /.well-known/oauth-authorization-server returns 404 and claude.ai
    # gives up before issuing /oauth/token.  When set, the issuer URL
    # is echoed into the well-known docs verbatim — pin it to the
    # public hostname the connector is configured against.
    oauth_enabled: bool = False
    oauth_issuer_url: str = ""
    oauth_resource_aliases: tuple[str, ...] = ()
    # OAuth clients that act as the operator's management console.
    # These get a narrow read/search + role-dispatch process allowlist,
    # not blanket operator-equivalent authority.
    oauth_management_client_ids: tuple[str, ...] = ()
    # Access-token TTL for browser/hosted MCP clients; refresh-token
    # rotation covers longer-term reuse after the initial account link.
    oauth_token_ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS
    # Authorization-code grant: TTL on the in-process auth-code cache.
    # Single-use, short-lived; matches RFC 6749 §4.1.2's "SHOULD <= 10
    # minutes" recommendation by default.
    oauth_auth_code_ttl_seconds: int = 600
    # Refresh-token TTL.  30 days = canonical OAuth 2.1 value; once
    # this elapses the client falls back to a full authorize round.
    oauth_refresh_token_ttl_seconds: int = 30 * 24 * 60 * 60
    # When enabled, the streamable router validates the bearer's ``aud``
    # claim against the canonical MCP URI derived from the issuer.
    # Disabling is for laptop dev mode where no canonical URL is pinned.
    oauth_require_audience: bool = True
    # When enabled, /oauth/token's authorization_code response includes
    # a refresh_token + the refresh_token grant is honoured.  Disable
    # to fall back to client_credentials + auth_code only.
    oauth_refresh_tokens_enabled: bool = True
    # CORS Origin allow-list for the streamable + OAuth endpoints.
    # ``https://claude.ai`` is required for the browser-driven custom
    # connector validator's preflight; add any other browser-driven
    # client domains here.
    streamable_cors_origins: tuple[str, ...] = ()
    # Operator-opt-in bypass: when True, replaces the bearer-token
    # verifier with a permissive variant that returns a synthetic
    # claim for every request. Use ONLY when an outer security
    # boundary (OpenAI tunnel-client + runtime API key, mTLS, or
    # explicit network isolation) is the auth gate. Default off —
    # opt-in opens an otherwise-closed surface. See
    # ``workbench/2026-06-06_openai_tunnel_client_setup.md`` for the
    # tunnel-as-security-boundary pattern. Eventual cleanup target:
    # remove this flag once ``bridge_hmac_key`` lazy creation lands
    # via the Tier 5 vault path (state-service consolidation campaign).
    streamable_no_auth: bool = False

    def get(self, key: str, default: object = None) -> object:
        """Provide a ``.get`` shim so :mod:`http_routes` can look up keys."""
        value = getattr(self, key, default)
        return value if value is not None else default


@dataclass(frozen=True, slots=True)
class _SessionLifecyclePolicyConfig:
    """Fleet session-management Phase B, §6 L3 rule 1 policy config.

    Kept separate from :class:`AgentMessagingConfig` for the same reason as
    :class:`_BridgeRuntimeConfig`: a distinct concern (spawn-time defaults for
    the L1 verb surface) reads its own settings without forcing the
    core agent-messaging service config to grow fields it doesn't use.

    ``work_class_defaults`` is operator-editable policy DATA (``plugin.yaml``'s
    ``config:`` block), not a code default: "cheapest capable model per
    work_class" is a values-laden business call this module does not make
    unilaterally (the same posture ``FLEET_HEADLESS_PERMISSION_MODE`` already
    takes for permission mode). Empty (the shipped default) means
    ``spawn_session`` behaves exactly as it did before this config existed —
    an unconfigured work_class leaves ``model``/``effort`` at whatever the
    caller passed (usually empty).

    ``work_class_tool_allowlists`` is the §6 permission-mode design's
    (2026-08-03) spawn-time tool allowlist, consumed by
    ``headless_adapter.py``'s PreToolUse gate
    (``.claude/hooks/headless_tool_allowlist_gate.py``). Operator ruling,
    same day, effective now ("we don't have any restrictions now"): shipped
    empty means the gate is UNARMED by default (``headless_adapter.py``'s
    ``_spawn_env`` only sets the hook's env var when an allowlist is
    actually non-empty) — the mechanism stays landed as shelf capability,
    armed per-``work_class`` whenever usage data argues for it, not
    exercised by default.

    ``headless_permission_mode`` is declared config (not a process env var —
    a config value is as declared as an env var, no LaunchAgent edit needed
    to change it). Shipped default ``"bypassPermissions"`` (flipped from
    ``"default"`` — D2 finding: Claude Code's own ``"default"`` interactive-
    approval mode leaves an unattended spawn with EMPTY effective grants,
    since ``--setting-sources project`` excludes every allowlist and no
    human exists to approve a prompt). Per the same operator ruling, no
    value (including ``"bypassPermissions"``) is rejected here — the knob is
    fail-closed only when it resolves to NOTHING at all
    (``headless_adapter.py.verify_config()``'s separate, unconditional
    floor), which is operational sanity, not a restriction.

    ``default_fleet_transport`` is the fleet-watch-transport-migration
    lane's single declared default-transport knob (phase 2 slice 2), the
    ONE configuration point the operator's verbatim charter's "easy to
    change later" clause names. Shipped default ``"watch"`` — the charter's
    own instruction that non-MCP must be the fleet's PRIMARY transport now,
    MCP retained as backup/chat-class only. Landed as shelf capability
    ahead of its consumers (same posture ``work_class_tool_allowlists``
    shipped in before ``headless_adapter.py`` read it): phase-2 slice 1
    wires the host adapters to read this value when building spawn env: it
    is not yet consumed by any spawn path as of this slice.
    """

    work_class_defaults: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    work_class_tool_allowlists: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    headless_permission_mode: str = ""
    default_fleet_transport: str = ""


def _run_counted_leg(
    legs: list[str],
    label: str,
    description: str,
    run: Callable[[], int],
    summarise: Callable[[int], str],
    on_finding: Callable[[int], None],
) -> None:
    """Run one count-returning notice leg, fault-isolated, and record it.

    ★ THE SHAPE THIS REMOVES, and why it is worth a helper rather than a
    fourth copy. Each leg was try / except-log-FAULTED-and-append /
    else-append-and-maybe-warn -- identical control flow, three different
    strings. The duplication was not merely untidy: it put the rider's
    fault-isolation contract in four places, so a leg added later could
    silently omit the except clause and cost every leg after it that tick.
    Here the contract exists once and a new leg cannot forget it.

    BOTH INVARIANTS THE RIDER DEPENDS ON LIVE HERE:

    * A FAULTED leg is still NAMED in ``legs``. An all-clear summary built
      only from legs that succeeded shrinks silently, and a shrinking line
      reads as a healthy one -- the failure the GAU-02 work exists to stop.
    * ``on_finding`` fires only on a NON-ZERO count, so a healthy tick stays
      quiet at WARNING while still reporting itself at INFO through the
      summary.

    The callables are passed rather than the sweep function plus arguments
    so each leg keeps its own keyword shape and its own prose at the call
    site, where a reader comparing legs can see them side by side.
    """
    try:
        count = run()
    except Exception:  # noqa: BLE001 — one leg's fault must not skip the others
        logger.exception("%s %s leg FAULTED; rider continues", label, description)
        legs.append(f"{label}=FAULTED")
        return
    legs.append(f"{label}={summarise(count)}")
    if count:
        on_finding(count)


class AgentMessagingPlugin(
    ServicePlugin,
    IOInterfacePlugin,
    EdgeProcessProvider,
    AgentMessagingServiceInterface,
):
    """Consolidated bridge plugin (IO + bridge + agent messaging).

    Implements ``AgentMessagingServiceInterface`` by delegating to an
    underlying :class:`AgentMessagingService`; implements
    :class:`IOInterfacePlugin` directly via ``start_interface`` /
    ``stop_interface`` / ``post_message`` / ``get_supported_capabilities``.
    Exposes ``send_peer_message``, the session-lifecycle EDGE processes,
    and the bridge-delivery and IO EDGE_SINK processes through
    ``@platform_process`` decorators.

    NOTE: This plugin intentionally does NOT declare
    ``service_interfaces`` (the property would mark it as a
    ServiceProvider).  Bound ServiceProviders are skipped from the
    ``plugin::<name>::*`` registry namespace
    (process_registry/builder.py::_should_skip_plugin), which would
    hide those EDGE processes from ``submit_action_definition``.
    Instead, callers resolve us via
    ``plugin_manager.plugins["agent_messaging_plugin"]`` and use our
    public methods directly.
    """

    name: str = PLUGIN_NAME

    def __init__(self) -> None:
        super().__init__()
        self.name = PLUGIN_NAME
        self._service: AgentMessagingService | None = None
        self._services_started: bool = False
        # AgentMessagingServiceInterface injection (set via setter
        # pattern in startup_sequence).
        self._flow_manager: FlowManager | None = None
        self._compilation_context_builder: Any | None = None
        # IOInterfacePlugin injection.
        self._memory_service: Any | None = None
        self._session_manager: ISessionManager | None = None
        self._context_management_service: Any | None = None
        # VaultServiceProxy injected via set_vault_service (W-VAULT-INTERFACE-EXTEND
        # Phase D-2, 2026-06-07). Proxy is caller-bound to this plugin's name.
        self._vault_service: object | None = None
        # Bridge / IO runtime state — populated by start_interface.
        self._bridge_manager: BridgeSessionManager | None = None
        self._peer_registry: PeerRegistry | None = None
        self._session_role_claim_pruner: SessionRoleClaimPruner | None = None
        # L4 rotation-surface latches (one per notice kind, never shared: the
        # same agent_instance_id can legitimately be rotation-due AND dark, and
        # one latch would let whichever fired first suppress the other).
        # Process-lifetime scope by design — see NoticeLatch's docstring for
        # the bound that buys.
        self._rotation_due_latch: NoticeLatch = NoticeLatch()
        self._gauge_coverage_latch: NoticeLatch = NoticeLatch()
        # L4d (GAU-01(b)) gets its OWN instance for the reason stated above, and
        # it is the sharpest case yet: "no gauge row at all" and "a gauge row
        # that stopped" are conditions the SAME session moves BETWEEN. A shared
        # latch would let the missing-row episode suppress the arrest notice
        # that follows it -- silencing the finding precisely when the session
        # started reporting again but its gauge did not.
        self._gauge_stale_latch: NoticeLatch = NoticeLatch()
        # L4c keys on (session, BAND) rather than on the session alone, so a
        # NoticeLatch cannot serve it -- see BandEdgeLatch's docstring for why
        # the difference is a correctness one and not a refinement.
        self._rotation_self_latch: BandEdgeLatch = BandEdgeLatch()
        self._platform_surface: PlatformSurface | None = None
        # D-IF7/D-IF8 sidecar: per-bridge SessionInferenceProvider keyed
        # by agent_instance_id. Populated post-success in the stdio
        # peer_register route when ``provides_inference=True``; cleared
        # post-success in close_bridge via ``PeerRegistry.list_by_bridge``.
        # Streamable peer_register paths DO NOT populate this sidecar per
        # v4 D-IF11 (scope-out for v1 — fallback to default_inference_plugin
        # via the wrapper's None-handling path).
        self._inference_providers: dict[str, SessionInferenceProvider] = {}
        self._inference_providers_lock: threading.Lock = threading.Lock()
        # ◆R2 tombstone (case 3b): agent_instance_ids that were bound to a
        # provider earlier in this lifetime but whose bridge has dropped.
        # Guarded by ``_inference_providers_lock`` (same critical section as
        # the sidecar it shadows). LRU-bounded via _INFERENCE_TOMBSTONE_CAP.
        self._inference_provider_tombstones: OrderedDict[str, None] = OrderedDict()
        # INF-01 sub-slice-2: the sys:autonomic auto-assignment lifecycle
        # (Trigger-1/2 hook bodies + manual-set + first-claim drain).
        # Built in start_interface once the bridge collaborators exist.
        self._autonomic_assignment: AutonomicAssignment | None = None
        # REL-09: the idle-sweep driver (sweep_idle had NO caller before) —
        # routes every expired bridge through unregister's full cleanup.
        self._bridge_sweeper: BridgeLifecycleSweeper | None = None
        self._port: int | None = None
        self._host: str | None = None
        self._app: FastAPI | None = None
        self._server_thread: threading.Thread | None = None
        self._server_loop: asyncio.AbstractEventLoop | None = None
        self._server_started_event: threading.Event = threading.Event()
        self._service_started_at: str | None = None
        self._max_message_chars: int = _DEFAULT_MAX_MESSAGE_CHARS
        # Streamable HTTP MCP transport — separate uvicorn server bound
        # to a configurable host:port so the container can expose
        # 0.0.0.0:9000 to the phone while the bridge HTTP stays on
        # 127.0.0.1:<dyn> for local CLI subprocesses.
        self._streamable_session_manager: StreamableSessionManager | None = None
        self._streamable_server_thread: threading.Thread | None = None
        self._streamable_server_loop: asyncio.AbstractEventLoop | None = None
        self._streamable_server_started_event: threading.Event = threading.Event()
        self._streamable_host: str | None = None
        self._streamable_port: int | None = None
        # M4/M9 upload-route auth (chatgpt_export + claude_ai_export) shares
        # the streamable transport's BearerVerifier. Lazy because
        # _build_fastapi_app runs BEFORE _mount_streamable_transport; the
        # upload-route auth closure captures self and resolves the verifier
        # at request time (by which point startup is complete).
        self._streamable_bearer_verifier: BearerVerifier | None = None
        self._active: bool = True
        # maintenance-verbs M1 choreography jobs (rotate_session/restart_session,
        # D0.3-ratified deferred-completion shape). AsyncJobManager is lazily
        # pulled from orchestrator_ref (comfyui_image_generation_plugin's own
        # `_try_acquire_job_manager` precedent — there is no generic per-plugin
        # push-injection for it), not pushed at boot. Single dedicated worker
        # thread per the architect-pass constraint: FlowManager._sequence_cache
        # is an unlocked shared dict hit on every action submission, and the
        # comfyui pattern is only race-free because it runs exactly one
        # background worker — this thread must stay single and serialized,
        # never spawn a second concurrent choreography worker.
        self._async_job_manager: AsyncJobManager | None = None
        self._choreography_stop_event: threading.Event = threading.Event()
        self._choreography_worker_thread: threading.Thread | None = None

    @property
    def bridge_port(self) -> int | None:
        """Bridge HTTP server port, or ``None`` before ``start_interface``.

        Cross-plugin discovery surface for Slice 2 of
        ``workbench/2026-06-05_bridge_port_routing_and_session_lifecycle_design.md``:
        ``macos_self_deployment_plugin``'s heartbeat reads this attribute
        via ``orchestrator_ref.plugin_manager.plugins`` to learn the
        child's actual bound port, replacing the file-mediated detour
        through ``<name>-<color>.bridge.port`` that pre-dated the
        spawn-path-guarantee invariant I2. Returns ``None`` until
        ``start_interface`` allocates and binds; callers must tolerate
        that initial-window absence rather than fail-loud.
        """
        return self._port

    @property
    def streamable_bound_port(self) -> int | None:
        """Streamable HTTP listener's own port, or ``None`` before bind.

        BLG-04: cross-plugin discovery surface mirroring :attr:`bridge_port`
        exactly, same tolerance for the initial-window absence. Read by
        ``macos_self_deployment_plugin``'s heartbeat
        (``_lookup_streamable_port``) so it can report this color's
        EPHEMERAL streamable port to the router via ``register_color``,
        which is what lets the router proxy its own stable external port to
        it. ``None`` when this color runs with ``streamable_enabled=False``,
        same as any other "capability not running" case.
        """
        return self._streamable_port

    # ------------------------------------------------------------------
    # Platform-injected setters
    # ------------------------------------------------------------------

    def set_flow_manager(self, flow_manager: Any) -> None:
        logger.info("%s set_flow_manager called", self.name)
        self._flow_manager = flow_manager

    def set_compilation_context_builder(self, compilation_context_builder: Any) -> None:
        logger.info("%s set_compilation_context_builder called", self.name)
        self._compilation_context_builder = compilation_context_builder

    def set_action_factory(self, action_factory: Any) -> None:
        logger.info("%s set_action_factory called", self.name)
        self.action_factory = action_factory

    def set_memory_service(self, memory_service: Any) -> None:
        logger.info("%s set_memory_service called", self.name)
        self._memory_service = memory_service

    def set_async_job_manager(self, async_job_manager: AsyncJobManager) -> None:
        """Accept an AsyncJobManager if ever pushed generically (no injection
        loop calls this today — see ``_try_acquire_async_job_manager`` for
        the actual lazy-pull path, mirroring
        ``comfyui_image_generation_plugin``'s identical precedent)."""
        logger.info("%s set_async_job_manager called", self.name)
        self._async_job_manager = async_job_manager

    def _try_acquire_async_job_manager(self) -> AsyncJobManager | None:
        """Lazily pull ``AsyncJobManager`` from ``orchestrator_ref`` the first
        time it's needed — the same pattern
        ``comfyui_image_generation_plugin._try_acquire_job_manager`` uses,
        verified at source (2026-08-09): there is no generic per-plugin
        push-injection for this service, only the attribute sitting on the
        orchestrator once platform boot wires it."""
        if self._async_job_manager is not None:
            return self._async_job_manager
        if not self.orchestrator_ref:
            return None
        job_manager = getattr(self.orchestrator_ref, "async_job_manager", None)
        if job_manager:
            self.set_async_job_manager(job_manager)
        return self._async_job_manager

    def set_vault_service(self, vault_service: object) -> None:
        """Receive caller-bound VaultServiceProxy from lifecycle injection.

        W-VAULT-INTERFACE-EXTEND Phase D-2 (P0 Tier 1, 2026-06-07): the
        proxy was constructed by ``_inject_vault_service`` in
        ``startup_sequence.py`` with this plugin's name baked into its
        bound ``CallContext``. Do NOT acquire vault via
        ``orchestrator.get_service`` — the proxy is the only allowed
        handle.
        """
        logger.info("%s set_vault_service called", self.name)
        self._vault_service = vault_service

    # ------------------------------------------------------------------
    # VaultKeysProvider — W-PLUGIN-LAUNCH-KEYS (P0 Tier 2 sub-1, 2026-06-07)
    # ------------------------------------------------------------------

    def get_required_vault_keys(self) -> list[str]:
        """Scoped vault keys whose existence is required at readiness.

        The bearer_token_hmac_key is LAZY-CREATED on first streamable-
        MCP boot by ``_load_or_create_bearer_hmac_key``; not required
        at readiness — the plugin must load with no row present so the
        first boot can create it. Returns empty list per W-CLASSIFY
        §A.2.4 path (b) plus brief §3.5.
        """
        return []

    def get_declared_vault_keys(self) -> list[str]:
        """All scoped vault keys this plugin reads or writes.

        Per W-ADDRESS-BOOK-RENAME §A.2.4 the bearer_token_hmac_key now
        writes/reads under the scoped form built in
        ``_bearer_hmac_key_vault_name()``.
        """
        return [_BEARER_HMAC_KEY_VAULT_NAME]

    # ------------------------------------------------------------------
    # ServicePlugin lifecycle (no background workers; lazy build)
    # ------------------------------------------------------------------

    async def start_services(self) -> ActionResult:
        self._services_started = True
        self._service_started_at = _now_iso()
        self._start_choreography_worker()
        return ActionResult(
            action_status="completed",
            data={
                "message": f"{self.name} services started",
                "started_at": self._service_started_at,
            },
            actions=[],
            error=None,
            timestamp=_now_iso(),
        )

    async def stop_services(self) -> ActionResult:
        # D1 §5: terminate every tracked headless worker so a graceful
        # shutdown/restart never leaves an orphaned Claude Code process
        # burning tokens with nothing tracking it (start_new_session=True
        # detaches it from this process's own group on purpose).
        from .session_hosts import shutdown_all_drivers  # noqa: PLC0415

        shutdown_all_drivers()
        self._stop_choreography_worker()
        self._services_started = False
        self._service = None
        self._service_started_at = None
        return ActionResult(
            action_status="completed",
            data={"message": f"{self.name} services stopped"},
            actions=[],
            error=None,
            timestamp=_now_iso(),
        )

    # ------------------------------------------------------------------
    # maintenance-verbs M1 choreography worker (rotate_session/restart_session)
    # ------------------------------------------------------------------

    _CHOREOGRAPHY_POLL_INTERVAL_SECONDS = 2.0
    _CHOREOGRAPHY_DIRECTED_BY = "agent_messaging_plugin.choreography_worker"

    def _start_choreography_worker(self) -> None:
        """Start the SINGLE dedicated choreography worker thread — mirrors
        ``comfyui_image_generation_plugin``'s ``_worker_thread`` lifecycle
        exactly (started in ``start_services``, joined with a timeout in
        ``stop_services``). Deliberately ONE thread, never a pool: the
        architect-pass constraint (2026-08-09) is that
        ``FlowManager._sequence_cache`` is an unlocked shared dict hit on
        every action submission, and single-worker execution is what keeps
        this safe without fixing that race — do not parallelize this loop."""
        self._choreography_stop_event.clear()
        self._choreography_worker_thread = threading.Thread(
            target=self._choreography_worker_loop,
            name=f"{PLUGIN_NAME}-choreography-worker",
            daemon=True,
        )
        self._choreography_worker_thread.start()

    def _stop_choreography_worker(self) -> None:
        self._choreography_stop_event.set()
        thread = self._choreography_worker_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=30.0)
            if thread.is_alive():
                logger.error("%s choreography worker did not stop within timeout", self.name)
        self._choreography_worker_thread = None

    def _next_queued_choreography_job(
        self,
        job_manager: AsyncJobManager,
        provider_name: str,
    ) -> dict[str, Any] | None:
        """The oldest queued job for one provider_name, or ``None``. Split out
        of :func:`_choreography_worker_loop` to keep it a straight-line
        dispatcher (radon cc)."""
        jobs_result = job_manager.list_jobs(
            status=JOB_STATUS_QUEUED,
            provider_name=provider_name,
            limit=1,
            order_by="created_at ASC",
        )
        jobs = (jobs_result.get("data") or {}).get("jobs", [])
        if not isinstance(jobs, list) or not jobs:
            return None
        job = jobs[0]
        return job if isinstance(job, dict) else None

    def _poll_and_process_one_choreography_job(
        self,
        job_manager: AsyncJobManager,
        state_service: Any,
    ) -> None:
        """One tick's worth of work: check rotate, restart, then
        curation-report's queue for a single oldest job and process it —
        split out of :func:`_choreography_worker_loop` to keep it a
        straight-line dispatcher (radon cc)."""
        for provider_name in (
            f"{PROVIDER_PLUGIN_NAME}.{ACTION_ROTATE_SESSION}",
            f"{PROVIDER_PLUGIN_NAME}.{ACTION_RESTART_SESSION}",
            f"{PROVIDER_PLUGIN_NAME}.{ACTION_GENERATE_CURATION_REPORT}",
        ):
            if self._choreography_stop_event.is_set():
                return
            job = self._next_queued_choreography_job(job_manager, provider_name)
            if job is not None:
                self._process_choreography_job(job, job_manager, state_service)

    def _choreography_worker_loop(self) -> None:
        """Poll for queued rotate_session/restart_session jobs and process
        them ONE AT A TIME, serially — never concurrently (see the
        single-worker constraint on ``_start_choreography_worker``). Modeled
        directly on ``comfyui_image_generation_plugin._worker_loop``: a
        top-level ``except Exception`` per tick so one bad job can never kill
        the loop, and a wait-based poll interval rather than a hot spin."""
        while not self._choreography_stop_event.is_set():
            try:
                job_manager = self._try_acquire_async_job_manager()
                state_service = self._get_state_service()
                if job_manager is not None and state_service is not None:
                    self._poll_and_process_one_choreography_job(job_manager, state_service)
            except Exception:
                logger.exception("%s choreography worker loop error", self.name)
            self._choreography_stop_event.wait(self._CHOREOGRAPHY_POLL_INTERVAL_SECONDS)
        logger.debug("%s choreography worker loop exited", self.name)

    def _update_choreography_progress(
        self,
        job_manager: AsyncJobManager,
        job_id: str,
        *,
        progress_percent: int,
        leg: str,
    ) -> None:
        """One ``update_status`` call per choreography leg, per the D0.3-ratified
        shape. Logs the leg name for operator observability — the job ledger's
        own ``progress_percent`` is the only durable per-leg signal
        ``AsyncJobManager`` exposes; there is no free-text leg-name column."""
        logger.info("%s choreography job %s: leg=%s", self.name, job_id, leg)
        job_manager.update_job(
            job_id,
            {"status": JOB_STATUS_PROCESSING, "progress_percent": progress_percent},
        )

    def _complete_choreography_job(
        self,
        job_manager: AsyncJobManager,
        job_id: str,
        result: dict[str, Any],
    ) -> None:
        job_manager.update_job(job_id, {"status": JOB_STATUS_COMPLETED, "result": result})

    def _fail_choreography_job(
        self,
        job_manager: AsyncJobManager,
        job_id: str,
        code: str,
        message: str,
    ) -> None:
        job_manager.update_job(
            job_id,
            {"status": JOB_STATUS_ERROR, "error": {"code": code, "message": message}},
        )

    def _resolve_choreography_job_request(
        self,
        job: dict[str, Any],
        job_manager: AsyncJobManager,
    ) -> tuple[str, str, dict[str, Any]] | None:
        """``(job_id, provider_name, request_data)``, or ``None`` after
        already failing the job itself — split out of
        :func:`_process_choreography_job` to keep it a straight-line
        dispatcher (radon cc). The caller only needs to check for ``None``;
        every failure path here has already reached a terminal job status."""
        job_id = str(job.get("id") or "")
        provider_name = str(job.get("provider_name") or "")
        if not job_id or not provider_name:
            logger.error("%s choreography job missing id/provider_name: %r", self.name, job)
            return None
        payload_result = job_manager.get_job_payload(job_id, "request")
        if payload_result.get("action_status") != "completed":
            self._fail_choreography_job(
                job_manager,
                job_id,
                "request_payload_missing",
                "could not read the job's own request payload",
            )
            return None
        payload_data = payload_result.get("data")
        request_data = payload_data.get("payload") if isinstance(payload_data, dict) else None
        if not isinstance(request_data, dict):
            self._fail_choreography_job(
                job_manager,
                job_id,
                "request_payload_invalid",
                "job request payload was not an object",
            )
            return None
        return job_id, provider_name, request_data

    def _process_choreography_job(
        self,
        job: dict[str, Any],
        job_manager: AsyncJobManager,
        state_service: Any,
    ) -> None:
        """Dispatch one queued job to the rotate/restart/curation-report
        runner by ``provider_name`` suffix, and guarantee it reaches a
        TERMINAL status — every exception path here ends in
        ``_fail_choreography_job``, never a job left stranded at
        ``processing`` (the D0.3 doc's own named crash/reap gap is a
        platform-level absence this function must not add to by letting an
        exception escape uncaught)."""
        resolved = self._resolve_choreography_job_request(job, job_manager)
        if resolved is None:
            return
        job_id, provider_name, request_data = resolved
        try:
            if provider_name.endswith(f".{ACTION_ROTATE_SESSION}"):
                self._run_rotate_session_job(job_id, request_data, job_manager, state_service)
            elif provider_name.endswith(f".{ACTION_RESTART_SESSION}"):
                self._run_restart_session_job(job_id, request_data, job_manager, state_service)
            elif provider_name.endswith(f".{ACTION_GENERATE_CURATION_REPORT}"):
                self._run_generate_curation_report_job(job_id, request_data, job_manager)
            else:
                self._fail_choreography_job(
                    job_manager,
                    job_id,
                    "unknown_action",
                    f"unrecognized provider_name {provider_name!r}",
                )
        except VerbError as exc:
            logger.error(
                "%s choreography job %s failed: code=%s message=%s",
                self.name,
                job_id,
                exc.code,
                exc.message,
            )
            self._fail_choreography_job(job_manager, job_id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 — a job must reach a terminal status, never strand
            logger.exception("%s choreography job %s crashed", self.name, job_id)
            self._fail_choreography_job(job_manager, job_id, "internal_error", str(exc))

    # 2026-08-10 fix: measured live in gsuite-async's first production
    # rotation (job-2ns5on395r9xz) — the real post-clear first-turn latency
    # in a loaded production lane was ~77s from drive_session, and the new
    # claude_session_id was captured only ~6.7s after the OLD 60s window's
    # deadline had already declared verify_timeout on an otherwise-healthy
    # rotation (session_claude_mapping rows: drive_session leg logged
    # 2026-08-10T03:48:54.824Z, new id captured_at 2026-08-10T03:50:11.977Z).
    # A healthy rotation reporting as an error is a false negative any
    # job-status-driven automation would be misled by (the plausibility-
    # fence-below-the-plausible-range class). Raised with real margin, not
    # tuned tightly to this one sample.
    _ROTATE_VERIFY_MAX_WAIT_SECONDS = 300.0
    _ROTATE_VERIFY_POLL_INTERVAL_SECONDS = 5.0
    _RESTART_VERIFY_MAX_WAIT_SECONDS = 90.0
    _RESTART_VERIFY_POLL_INTERVAL_SECONDS = 5.0

    def _check_for_new_claude_session(
        self,
        state_service: Any,
        agent_instance_id: str,
        existing_ids: set[str],
    ) -> list[str]:
        """One point-in-time check for a ``claude_session_id`` outside
        ``existing_ids`` — split out of :func:`_wait_for_new_claude_session`
        so the poll loop and its post-deadline final re-check share exactly
        one query+diff, never two copies to drift."""
        current_ids = {
            str(m.get("claude_session_id") or "")
            for m in lifecycle_list_session_claude_mappings(state_service, agent_instance_id)
        }
        new_ids = current_ids - existing_ids
        new_ids.discard("")
        return sorted(new_ids)

    def _wait_for_new_claude_session(
        self,
        state_service: Any,
        agent_instance_id: str,
        existing_ids: set[str],
        max_wait_seconds: float,
        poll_interval_seconds: float,
    ) -> list[str]:
        """Poll ``list_session_claude_mappings`` until a ``claude_session_id``
        outside ``existing_ids`` appears, or the deadline passes — plus ONE
        final check immediately after the deadline, closing the narrow race
        where the id lands in the gap between the last poll and the
        deadline rather than genuinely never arriving. A NEW id appearing is
        a positive, mechanically-checked observation that a fresh session
        generation actually started (the SessionStart hook fired) — the
        ARMED-vs-FIRED distinction the M0 design names explicitly, not a
        bare status re-read or a fixed sleep."""
        deadline = datetime.now(UTC).timestamp() + max_wait_seconds
        while (
            datetime.now(UTC).timestamp() < deadline and not self._choreography_stop_event.is_set()
        ):
            new_ids = self._check_for_new_claude_session(
                state_service,
                agent_instance_id,
                existing_ids,
            )
            if new_ids:
                return new_ids
            self._choreography_stop_event.wait(poll_interval_seconds)
        return self._check_for_new_claude_session(state_service, agent_instance_id, existing_ids)

    def _wait_for_role_claim(self, role_name: str, agent_instance_id: str) -> bool:
        """Poll ``peer_holds_role`` (called as a plain method — ``@platform_process``
        is a metadata-only decorator, verified at source, so this executes
        identically to a dispatched call) until the new session claims
        ``role_name``, or the deadline passes."""
        deadline = datetime.now(UTC).timestamp() + self._RESTART_VERIFY_MAX_WAIT_SECONDS
        while (
            datetime.now(UTC).timestamp() < deadline and not self._choreography_stop_event.is_set()
        ):
            result = self.peer_holds_role(
                {"parameters": {"name": role_name, "agent_instance_id": agent_instance_id}},
                {},
            )
            if result.get("action_status") == "completed":
                data = result.get("data")
                if isinstance(data, dict) and data.get("holds") is True:
                    return True
            self._choreography_stop_event.wait(self._RESTART_VERIFY_POLL_INTERVAL_SECONDS)
        return False

    def _run_rotate_session_job(
        self,
        job_id: str,
        request_data: dict[str, Any],
        job_manager: AsyncJobManager,
        state_service: Any,
    ) -> None:
        """§2.1 choreography, run OFF the dispatch path: resolve -> durable
        pickup -> clear -> drive -> verify. Every VerbError raised by a
        composed lifecycle verb propagates to :func:`_process_choreography_job`,
        which fails the job with that verb's own code/message — no
        catch-and-continue here."""
        agent_instance_id = _str_field(request_data.get("agent_instance_id"))
        role_name = _str_field(request_data.get("role_name"))
        pickup_text = _str_field(request_data.get("pickup_text"))
        park_first = bool(request_data.get("park_first", False))

        self._update_choreography_progress(
            job_manager,
            job_id,
            progress_percent=10,
            leg="resolve_ledger_row",
        )
        row = lifecycle_session_status(state_service, agent_instance_id)
        agent_runtime = _str_field(row.get("agent_runtime")) or "claude_code"

        existing_ids = set()
        if agent_runtime == "claude_code":
            existing_ids = {
                str(m.get("claude_session_id") or "")
                for m in lifecycle_list_session_claude_mappings(
                    state_service,
                    agent_instance_id,
                )
            }

        self._update_choreography_progress(
            job_manager,
            job_id,
            progress_percent=25,
            leg="durable_pickup_dispatch",
        )
        send_result = self.peer_send_by_name(
            {"parameters": {"name": role_name, "content": pickup_text}},
            {},
        )
        if send_result.get("action_status") != "completed":
            raise VerbError(
                "pickup_dispatch_failed",
                f"peer_send_by_name to role {role_name!r} did not complete cleanly: "
                f"{send_result!r}",
            )

        self._update_choreography_progress(
            job_manager,
            job_id,
            progress_percent=45,
            leg="clear_session",
        )
        lifecycle_clear_session(
            state_service,
            agent_instance_id=agent_instance_id,
            park=park_first,
            directed_by=self._CHOREOGRAPHY_DIRECTED_BY,
        )

        self._update_choreography_progress(
            job_manager,
            job_id,
            progress_percent=65,
            leg="drive_session",
        )
        lifecycle_drive_session(
            state_service,
            agent_instance_id=agent_instance_id,
            text=pickup_text,
            directed_by=self._CHOREOGRAPHY_DIRECTED_BY,
        )

        self._update_choreography_progress(job_manager, job_id, progress_percent=85, leg="verify")
        if agent_runtime == "codex":
            # Both Codex channels acknowledge pickup before send() returns:
            # app-server returns a JSON-RPC turn/start|steer response; tmux
            # observes a styled pane-state transition after a separate Enter.
            # That acknowledgement is the Codex-native generation witness;
            # the Claude-only session-mapping spool cannot observe it.
            self._complete_choreography_job(
                job_manager,
                job_id,
                {
                    "turn_observed": True,
                    "agent_runtime": "codex",
                    "verification": "driver_channel_acknowledged",
                    "new_claude_session_ids": [],
                },
            )
            return
        new_ids = self._wait_for_new_claude_session(
            state_service,
            agent_instance_id,
            existing_ids,
            self._ROTATE_VERIFY_MAX_WAIT_SECONDS,
            self._ROTATE_VERIFY_POLL_INTERVAL_SECONDS,
        )
        if not new_ids:
            raise VerbError(
                "verify_timeout",
                f"no new claude_session_id observed for {agent_instance_id!r} within "
                f"{self._ROTATE_VERIFY_MAX_WAIT_SECONDS}s of drive_session — the turn "
                "may not have started (ARMED ≠ FIRED).",
            )
        self._complete_choreography_job(
            job_manager,
            job_id,
            {"turn_observed": True, "new_claude_session_ids": new_ids},
        )

    def _build_restart_spawn_params(
        self,
        old_row: dict[str, Any],
        role_class: str,
        lane_id: str,
        role_name: str,
    ) -> dict[str, Any]:
        """Carry the old ledger row's dispatch config forward into the fresh
        spawn's raw params — split out of :func:`_run_restart_session_job` to
        keep it a straight-line dispatcher (radon cc: each field extraction's
        own truthiness check lives here, not stacked onto the caller's
        count). Feeds :func:`_spawn_session_request_from_params`, the SAME
        raw-params builder ``spawn_session()`` itself uses (2026-08-10 fix:
        this path previously built a ``SpawnSessionRequest`` directly and
        skipped every policy-resolution step spawn_session() runs —
        permission_mode/allowed_tools/transport are not columns on
        managed_session, so they were silently lost every restart; routing
        through the shared params+policy path closes that class of drift for
        good, not just this one field)."""
        return {
            "role_class": role_class,
            "lane_id": lane_id,
            "brief_ref": _str_field(old_row.get("brief_ref")),
            "unit_id": _str_field(old_row.get("unit_id")),
            "work_class": _str_field(old_row.get("work_class")),
            "budget_line": _str_field(old_row.get("budget_line")),
            # Runtime is restart-sticky for the same reason host/model/effort
            # are: defaulting this fresh request would silently respawn a
            # Codex worker as Claude.  Older rows have the compatibility floor.
            "agent_runtime": _str_field(old_row.get("agent_runtime")) or "claude_code",
            "role_name": role_name,
            "host": _str_field(old_row.get("host")),
            "visibility": _str_field(old_row.get("visibility")),
            "model": _str_field(old_row.get("model")),
            "effort": _str_field(old_row.get("effort")),
            "dispatch_kind": _str_field(old_row.get("dispatch_kind")),
            "reviewed_report_vendor": _str_field(old_row.get("reviewed_report_vendor")),
            "pair_id": _str_field(old_row.get("pair_id")),
            # Restart-sticky like model/effort: dropping the declared floor
            # scope would let a restart respawn floored work on a cheaper pair.
            "scope_tags": list(old_row.get("scope_tags") or []),
            "spawned_by_role": self._CHOREOGRAPHY_DIRECTED_BY,
        }

    def _run_restart_session_job(
        self,
        job_id: str,
        request_data: dict[str, Any],
        job_manager: AsyncJobManager,
        state_service: Any,
    ) -> None:
        """§2.2 choreography, run OFF the dispatch path: capture -> terminate
        -> spawn -> (conditional) role-reclaim drive -> verify. Per the M0
        design's own gap finding, the role-reclaim drive defaults to ALWAYS
        firing unless a lane charter is already on file (option (b), coordinator-
        seat ruled default) — never trusts the automatic first turn alone to carry
        the role-claim instruction."""
        old_agent_instance_id = _str_field(request_data.get("agent_instance_id"))
        role_name = _str_field(request_data.get("role_name"))
        role_class = _str_field(request_data.get("role_class"))
        grace_seconds_raw = request_data.get("grace_seconds")
        grace_seconds = grace_seconds_raw if isinstance(grace_seconds_raw, int) else 30

        self._update_choreography_progress(
            job_manager,
            job_id,
            progress_percent=10,
            leg="capture_old_row",
        )
        old_row = lifecycle_session_status(state_service, old_agent_instance_id)
        lane_id = _str_field(old_row.get("lane_id"))

        self._update_choreography_progress(
            job_manager,
            job_id,
            progress_percent=25,
            leg="terminate_session",
        )
        lifecycle_terminate_session(
            state_service,
            agent_instance_id=old_agent_instance_id,
            directed_by=self._CHOREOGRAPHY_DIRECTED_BY,
            grace_seconds=grace_seconds,
        )

        self._update_choreography_progress(
            job_manager,
            job_id,
            progress_percent=45,
            leg="spawn_session",
        )
        raw_params = self._build_restart_spawn_params(old_row, role_class, lane_id, role_name)
        spawn_req = _spawn_session_request_from_params(raw_params, self._CHOREOGRAPHY_DIRECTED_BY)
        spawn_req = _apply_spawn_session_policy(
            spawn_req, self._build_session_lifecycle_policy_config()
        )
        spawn_result = lifecycle_spawn_session(state_service, spawn_req)
        new_agent_instance_id = str(spawn_result.get("agent_instance_id") or "")
        if not new_agent_instance_id:
            raise VerbError(
                "spawn_failed",
                "restart_session: spawn_session returned no agent_instance_id.",
            )

        self._update_choreography_progress(
            job_manager,
            job_id,
            progress_percent=65,
            leg="role_reclaim_drive",
        )
        charter = lifecycle_resolve_lane_charter(state_service, lane_id) if lane_id else None
        role_reclaim_driven = charter is None
        if role_reclaim_driven:
            lifecycle_drive_session(
                state_service,
                agent_instance_id=new_agent_instance_id,
                text=(
                    f"claim role '{role_name}' via the rename skill / arm a watch "
                    f"process for it — this is a restart continuing lane {lane_id!r}, "
                    "not a fresh unbriefed spawn."
                ),
                directed_by=self._CHOREOGRAPHY_DIRECTED_BY,
            )

        self._update_choreography_progress(job_manager, job_id, progress_percent=85, leg="verify")
        holds = self._wait_for_role_claim(role_name, new_agent_instance_id)
        if not holds:
            raise VerbError(
                "verify_timeout",
                f"new session {new_agent_instance_id!r} never claimed role "
                f"{role_name!r} within {self._RESTART_VERIFY_MAX_WAIT_SECONDS}s of "
                "spawn (claim circle not broken — ARMED ≠ FIRED).",
            )
        self._complete_choreography_job(
            job_manager,
            job_id,
            {
                "old_agent_instance_id": old_agent_instance_id,
                "new_agent_instance_id": new_agent_instance_id,
                "role_reclaim_driven": role_reclaim_driven,
                "role_reclaim_verified": True,
            },
        )

    def _run_generate_curation_report_job(
        self,
        job_id: str,
        request_data: dict[str, Any],
        job_manager: AsyncJobManager,
    ) -> None:
        """M2.2 choreography, run OFF the dispatch path: fetch this origin's
        memory records once, build the fact index, rank the caller-supplied
        head lines. Raises ``VerbError`` (``memory_service_unavailable``,
        ``solet_name_unset``, ``memory_fetch_failed``) on any precondition
        this job cannot proceed without — propagates to
        :func:`_process_choreography_job`, which fails the job with that
        code/message, same contract as rotate/restart."""
        head_lines_raw = request_data.get("head_lines")
        head_lines = [str(x) for x in head_lines_raw] if isinstance(head_lines_raw, list) else []
        bottom_n_raw = request_data.get("bottom_n")
        bottom_n = bottom_n_raw if isinstance(bottom_n_raw, int) else 10
        byte_budget_raw = request_data.get("byte_budget")
        byte_budget = byte_budget_raw if isinstance(byte_budget_raw, int) else 17_000
        line_budget_raw = request_data.get("line_budget")
        line_budget = line_budget_raw if isinstance(line_budget_raw, int) else 132

        self._update_choreography_progress(
            job_manager,
            job_id,
            progress_percent=25,
            leg="fetch_memory_records",
        )
        if self._memory_service is None:
            raise VerbError(
                "memory_service_unavailable",
                "memory_service is not bound on this solet.",
            )
        solet_name = _resolve_solet_name_for_memory_tags()
        if not solet_name:
            raise VerbError(
                "solet_name_unset",
                "Could not resolve a solet name to scope the memory fetch to this "
                "origin -- SOLET_NAME is unset, root_manifest.yaml is unreadable or "
                "still carries its unwritten placeholder, and CLAUDE_PROJECT_DIR is unset "
                "(the final fallback needs it too).",
            )
        fetch_result = self._memory_service.get_memories_by_tag(tag=origin_tag(solet_name))
        records = fetch_result.get("memories") if isinstance(fetch_result, dict) else None
        if not isinstance(records, list):
            raise VerbError(
                "memory_fetch_failed",
                f"get_memories_by_tag returned no usable 'memories' list: {fetch_result!r}",
            )

        self._update_choreography_progress(
            job_manager,
            job_id,
            progress_percent=65,
            leg="build_index_and_rank",
        )
        fact_index = build_fact_index(records, solet_name)
        report = build_curation_report(
            head_lines,
            fact_index,
            bottom_n=bottom_n,
            byte_budget=byte_budget,
            line_budget=line_budget,
        )
        self._complete_choreography_job(job_manager, job_id, report)

    def set_active(self, active: bool) -> None:
        """L3 blue-green Slice D color-active gate (peer dispatch + inbox poll).

        The plugin runs FastAPI servers + per-request handlers, not a continuous
        tick loop. The active color is the one the router routes to; the
        inactive color should not normally receive peer_send / peer_inbox calls.
        This setter flips ``self._active`` so request handlers can refuse if
        they want to; ``peer_inbox`` short-circuits to an empty result and
        ``peer_send`` raises so a misrouted request fails fast and loud.
        """
        self._active = active

    def initialize(self, config: dict[str, object]) -> None:
        """Bind the config provider so plugin.yaml defaults take effect.

        Called by the platform during plugin discovery with the
        per-plugin config dict.  Without this override
        ``self.config_provider`` stays None and ``_build_config``
        falls back to hardcoded defaults instead of the yaml/JSON
        config.
        """
        from ananta.core.config.config_provider import ConfigProvider  # noqa: PLC0415

        self.config_provider = ConfigProvider(self.name, config)

    def prepare_for_readiness(self) -> None:
        """Validate dependencies available at readiness time.

        Flow / action-factory / compilation-context-builder arrive
        after readiness, so we only check the dependencies that must
        be present right now.  Service constructs lazily on first use.
        """
        orchestrator = getattr(self, "orchestrator_ref", None)
        if orchestrator is None:
            raise RuntimeError(
                f"{self.name}: orchestrator_ref not injected",
            )
        if orchestrator.get_service("state_service") is None:
            raise RuntimeError(
                f"{self.name}: state_service unavailable at readiness",
            )
        if getattr(orchestrator, "plugin_manager", None) is None:
            raise RuntimeError(
                f"{self.name}: plugin_manager unavailable at readiness",
            )
        # §6.1: fail loud at readiness on a malformed system-slot declaration — the
        # platform's slot-constant registry must be well-formed before any claim or
        # gate reads it (a declaration bug is a startup-blocking error, not a
        # silently-tolerated state). The binding-STATE boot invariant (session-filled
        # LOUD-WARN / plugin-filled fail-boot) rides the INF-01 autonomic readiness
        # lane (§D.9); this is the declaration-INTEGRITY check that precedes it.
        validate_system_slot_declarations()
        # §9 CUTOVER migration — a PRE-SERVE HARD GATE. This readiness hook BLOCKS
        # until it returns; a parity failure RAISES (CutoverParityError) so green
        # refuses to serve while blue keeps serving — never a half-migrated live
        # table. One-shot marker-gated; the migrate→parity→[re-run]→flip loop IS the
        # §9 explicit-claim quiesce-equivalent. state_service confirmed available above.
        run_cutover_migration_at_readiness(
            orchestrator.get_service("state_service"),
            self._best_effort_memory_service(),
        )
        logger.info(
            "%s ready (service constructs lazily on first invocation)",
            self.name,
        )

    def _best_effort_memory_service(self) -> object | None:
        """The bound ``memory_service`` for the migration's best-effort role-entity
        ingest (§7), or ``None`` — the ingest is optional and NEVER gates readiness."""
        orchestrator = getattr(self, "orchestrator_ref", None)
        if orchestrator is None:
            return None
        get_service = getattr(orchestrator, "get_service", None)
        if not callable(get_service):
            return None
        try:
            return get_service("memory_service")
        except Exception:  # noqa: BLE001 — best-effort ingest must never gate readiness
            return None

    # ------------------------------------------------------------------
    # IOInterfacePlugin contract
    # ------------------------------------------------------------------

    def get_supported_capabilities(self) -> set[IOCapability]:
        return {IOCapability.TEXT}

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        """Declare the EDGE processes this plugin owns.

        EDGE_SINK processes (``deliver_result``, ``deliver_error``,
        ``post_message``, ``start_interface``, ``stop_interface``) are
        NOT declared here — the platform's process registry builder
        filters this dict by ``ProcessorPolicyCategory.EDGE`` and will
        reject EDGE_SINK entries with "no @platform_process method
        with that name exists".  EDGE_SINK methods register themselves
        through their ``@platform_process`` decorators alone (see
        ``claude_code_channel_plugin`` for the same pattern).
        """
        return {
            "send_peer_message": EdgeProcessDefinition(
                name="send_peer_message",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "peer_send_by_name": EdgeProcessDefinition(
                name="peer_send_by_name",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "deliver_job_completion": EdgeProcessDefinition(
                name="deliver_job_completion",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    # Matches its role-send sibling: a failed delivery leaves
                    # the unreached stamp in place and the drain owns recovery,
                    # so an automatic retry would duplicate a push whose
                    # persist half may already have landed.
                    retryable=False,
                ),
            ),
            "peer_claim_role": EdgeProcessDefinition(
                name="peer_claim_role",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "peer_release_role": EdgeProcessDefinition(
                name="peer_release_role",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "peer_holds_role": EdgeProcessDefinition(
                name="peer_holds_role",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "spawn_session": EdgeProcessDefinition(
                name="spawn_session",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "dispatch_managed_work": EdgeProcessDefinition(
                name="dispatch_managed_work",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "provision_role_session": EdgeProcessDefinition(
                name="provision_role_session",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "report_managed_dispatch": EdgeProcessDefinition(
                name="report_managed_dispatch",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "resolve_managed_dispatch": EdgeProcessDefinition(
                name="resolve_managed_dispatch",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "managed_dispatch_status": EdgeProcessDefinition(
                name="managed_dispatch_status",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            "managed_dispatch_inventory": EdgeProcessDefinition(
                name="managed_dispatch_inventory",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            "managed_dispatch_events": EdgeProcessDefinition(
                name="managed_dispatch_events",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            "legislate_role": EdgeProcessDefinition(
                name="legislate_role",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "capture_lane_charter": EdgeProcessDefinition(
                name="capture_lane_charter",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    # A fresh INSERT, not idempotent on conflict -- an
                    # automatic retry after an uncertain result would write
                    # a SECOND charter row, which resolve_lane_charter would
                    # then treat as the superseding one. Never safe to retry
                    # blind.
                    retryable=False,
                ),
            ),
            "arm_session_dependency": EdgeProcessDefinition(
                name="arm_session_dependency",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    # A fresh INSERT, not idempotent on conflict (unlike
                    # legislate_role's on_conflict=do_nothing) -- an
                    # automatic retry after an uncertain result would arm a
                    # SECOND edge for the same condition, never safe.
                    retryable=False,
                ),
            ),
            "list_sessions": EdgeProcessDefinition(
                name="list_sessions",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            # Read-only existing-data projection: a retry after a transient
            # state-read fault cannot create, wake, or reclassify anything.
            "fleet_status": EdgeProcessDefinition(
                name="fleet_status",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            # Append-only operational observations: a retry could write a
            # second run record, so recording is deliberately non-retryable.
            "record_fleet_liveness_run": EdgeProcessDefinition(
                name="record_fleet_liveness_run",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "record_fleet_progress_run": EdgeProcessDefinition(
                name="record_fleet_progress_run",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "recent_fleet_liveness_runs": EdgeProcessDefinition(
                name="recent_fleet_liveness_runs",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            "recent_fleet_progress_runs": EdgeProcessDefinition(
                name="recent_fleet_progress_runs",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            "budget_report": EdgeProcessDefinition(
                name="budget_report",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    # Read-only (issues no writes) -- always safe to retry.
                    retryable=True,
                ),
            ),
            "drain_session_claude_mapping_spool": EdgeProcessDefinition(
                name="drain_session_claude_mapping_spool",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    # Idempotent by construction (upsert on the spool
                    # filename's own conflict triple) -- an automatic retry
                    # after an uncertain result is always safe, unlike
                    # arm_session_dependency's fresh-INSERT-only sibling above.
                    retryable=True,
                ),
            ),
            # usage-capture-attribution D2 follow-on (2026-08-06, workbench
            # 2026-08-06_usage_capture_attribution_findings_usage-capture-impl.md):
            # a read-only listing verb over session_claude_mapping, so a
            # future budget_report diagnosis can read the mapping table
            # directly instead of inferring its contents (as this lane's own
            # D1/D2 had to).
            "list_session_claude_mappings": EdgeProcessDefinition(
                name="list_session_claude_mappings",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    # Read-only (issues no writes) -- always safe to retry.
                    retryable=True,
                ),
            ),
            "session_status": EdgeProcessDefinition(
                name="session_status",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "reconcile_operator_session_liveness": EdgeProcessDefinition(
                name="reconcile_operator_session_liveness",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "clear_session": EdgeProcessDefinition(
                name="clear_session",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "compact_session": EdgeProcessDefinition(
                name="compact_session",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "drive_session": EdgeProcessDefinition(
                name="drive_session",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "terminate_session": EdgeProcessDefinition(
                name="terminate_session",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "retire_session": EdgeProcessDefinition(
                name="retire_session",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "report_alive": EdgeProcessDefinition(
                name="report_alive",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            # maintenance-verbs M1 (workbench
            # 2026-08-09_maintenance_verbs_m0_design_mverbs-impl.md §2.3).
            # Retryable: an overwrite upsert of the caller's OWN latest
            # snapshot is idempotent-on-repeat by construction (same row,
            # same conflict key) — a retry after a transient fault can never
            # double-record or corrupt an earlier value.
            "report_context_status": EdgeProcessDefinition(
                name="report_context_status",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            # Read-only (issues no writes) -- always safe to retry.
            "session_context_status": EdgeProcessDefinition(
                name="session_context_status",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            # GAU-15 (2026-08-19): the SERIES behind that single row. Read-only
            # like its sibling -- a bounded ordered page plus one lifecycle row.
            "session_context_status_history": EdgeProcessDefinition(
                name="session_context_status_history",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            # CDX-06 part C (2026-08-24) — the honesty field. Retryable: an
            # overwrite upsert of the caller's OWN latest row is
            # idempotent-on-repeat, same posture as report_context_status.
            "report_inbox_consumption": EdgeProcessDefinition(
                name="report_inbox_consumption",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            # Read-only (issues no writes) -- always safe to retry.
            "session_inbox_consumption_status": EdgeProcessDefinition(
                name="session_inbox_consumption_status",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            # GAU-21 (2026-08-19): the DURABLE record of which gauge
            # notices fired. Read-only and NON-CONSUMING -- unlike the bridge
            # event queue it exists to replace, whose only reader REMOVES what
            # it reads.
            "gauge_notice_records": EdgeProcessDefinition(
                name="gauge_notice_records",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            # GAU-15 item 4 (2026-08-19): the tamper canary's surface.
            # register/arrest each WRITE an audit row -- NOT retryable, because
            # a retry after a transient dispatch fault whose write already
            # landed would create a SECOND registration or a SECOND arrest
            # window for the same act, and a duplicated arrest window is
            # exactly the attribution ambiguity the audit log exists to
            # prevent. verify is read-only and retries safely.
            "register_gauge_canary": EdgeProcessDefinition(
                name="register_gauge_canary",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "arrest_gauge_canary": EdgeProcessDefinition(
                name="arrest_gauge_canary",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            # iss_48ea8171 (2026-09-20): the model capability catalog.
            # seed WRITES a run row plus cells -- idempotent by design (a
            # re-seed rewrites pending cells with the same values and never
            # touches an accepted one), so a retry is safe. read and select
            # are read-only and retry safely.
            "seed_model_capability_catalog": EdgeProcessDefinition(
                name="seed_model_capability_catalog",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            "read_model_capability_catalog": EdgeProcessDefinition(
                name="read_model_capability_catalog",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            "select_dispatch_tier": EdgeProcessDefinition(
                name="select_dispatch_tier",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            # iss_d136ae29: the real crosscheck refresh. Not retryable -- it
            # opens and closes its own refresh_run row and reconciles cells
            # from a live external fetch; a retry after a transient fault
            # whose write already landed would open a second run for the
            # same intent rather than resuming the first.
            "refresh_model_capability_catalog": EdgeProcessDefinition(
                name="refresh_model_capability_catalog",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            # record_model_capability_cell WRITES a refresh_run row, one
            # observation per metric and the accepted cell (operator ruling
            # rul_9e7a67ba); a retry after a transient fault whose write
            # already landed would record the same reading twice as two runs.
            "record_model_capability_cell": EdgeProcessDefinition(
                name="record_model_capability_cell",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            # register_synthetic_session WRITES the lifecycle ledger row, so it
            # is not retryable for the same reason its siblings are not: a
            # retry after a transient dispatch fault whose write already landed
            # would ask for a SECOND identity for one canary. (It would in fact
            # be refused with session_exists -- but a caller reading a retryable
            # flag should not have to depend on a downstream guard to avoid
            # duplicating a write.)
            "register_synthetic_session": EdgeProcessDefinition(
                name="register_synthetic_session",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            # GAU-24 (2026-08-19): the retire-side of the canary surface,
            # closing the leak register/arrest/register_synthetic_session had
            # no counterpart for. Two writes (ledger retire, then registry
            # mark) -- NOT retryable for the same reason its siblings above
            # are not: a retry after a transient dispatch fault whose writes
            # already landed would re-enter an idempotent-but-not-free
            # sequence rather than a clean single act.
            "retire_gauge_canary": EdgeProcessDefinition(
                name="retire_gauge_canary",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "verify_gauge_canary": EdgeProcessDefinition(
                name="verify_gauge_canary",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            # R1 held-authorization queue (2026-08-17). NOT retryable: an
            # unconditional INSERT per call -- a retry after a transient
            # dispatch fault whose actual write already landed would create a
            # genuine SECOND open entry for the same refusal, not merely a
            # spurious error. list/retire are read-only / predicated,
            # respectively, so they retry safely; record does not.
            "record_held_authorization": EdgeProcessDefinition(
                name="record_held_authorization",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            # Read-only (issues no writes) -- always safe to retry.
            "list_held_authorizations": EdgeProcessDefinition(
                name="list_held_authorizations",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            # Predicated on retired_at IS NULL -- a retry after a successful
            # prior call just re-raises entry_not_found_or_already_retired
            # rather than double-acting, so it is safe to retry.
            "retire_held_authorization": EdgeProcessDefinition(
                name="retire_held_authorization",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            # maintenance-verbs M1, D0.3-ratified deferred-completion shape.
            # NOT retryable: a repeat call creates a SECOND choreography job
            # (AsyncJobManager.create_job mints a fresh job_id every call, no
            # idempotency key) -- a naive retry after a transient dispatch
            # fault would double-drive the same worker. The caller re-checks
            # via check_choreography_job_status before ever re-dispatching.
            "rotate_session": EdgeProcessDefinition(
                name="rotate_session",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "restart_session": EdgeProcessDefinition(
                name="restart_session",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            # Read-only (issues no writes) -- always safe to retry. Also
            # serves generate_curation_report's job family -- it is a
            # generic AsyncJobManager job-row reader, not scoped to
            # rotate/restart specifically (verified at source).
            "check_choreography_job_status": EdgeProcessDefinition(
                name="check_choreography_job_status",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            # M2.2, same D0.3 dispatch shape as rotate/restart_session above.
            # NOT retryable for the identical reason: a repeat call mints a
            # SECOND job (no idempotency key on create_job), double-queuing
            # the same report -- the caller re-checks via
            # check_choreography_job_status before ever re-dispatching.
            "generate_curation_report": EdgeProcessDefinition(
                name="generate_curation_report",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            # Idempotent in effect (a second reinforce on the same slug just
            # adds another retrieval timestamp) but NOT marked retryable --
            # a naive retry after a transient dispatch fault would still
            # double-reinforce the target memory, over-counting
            # retrieval_count for a citation that only happened once. Mirrors
            # peer_claim_role's own "side effect, so don't auto-retry" stance.
            "reinforce_by_slug": EdgeProcessDefinition(
                name="reinforce_by_slug",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            # Pull-surface boundary (design §2). Retryable: the write is
            # monotonic (an attestation at or below the stored mark is a
            # no-op), so a retry after a transient fault re-attests the same
            # value harmlessly rather than double-advancing anything.
            "peer_mark_role_covered": EdgeProcessDefinition(
                name="peer_mark_role_covered",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            # Pull receive verb. Retryable: it is a pure
            # read whose only write is the liveness touch, so a repeat is
            # harmless and a transient state-read fault is worth re-running.
            "peer_inbox": EdgeProcessDefinition(
                name="peer_inbox",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            # Acknowledges a page only after the caller has rendered it. The
            # service persists immutable item receipts and a monotonic
            # watermark, so retrying the same token after a transient result
            # delivery fault is safe.
            "peer_ack_role_read_page": EdgeProcessDefinition(
                name="peer_ack_role_read_page",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            # Exact receipt lookup is read-only and bounded; retrying it cannot
            # create, acknowledge, or otherwise advance delivery state.
            "peer_role_read_receipts": EdgeProcessDefinition(
                name="peer_role_read_receipts",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            # Peer-enumeration asymmetry close (WS-1a pattern, operator-
            # prompted 2026-08-02): a no-MCP session could read its own mail
            # via peer_inbox but had no way to see who else was live.
            # Retryable: a pure, unfiltered registry snapshot with no write
            # at all, so a repeat after a transient state-read fault is
            # always harmless.
            "peer_list": EdgeProcessDefinition(
                name="peer_list",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            # INF-01 sub-slice-2 manual-set lane. Sensitivities mirror the
            # verb's ACTUAL return (action/name/agent_instance_id — the same
            # claim-outcome shape as peer_claim_role).
            "set_autonomic_slot": EdgeProcessDefinition(
                name="set_autonomic_slot",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            # INF-02 serve verb (holder → platform completion callback).
            # Sensitivities mirror the verb's ACTUAL return (status /
            # request_id / resume_process_key); not retryable — the serve
            # CAS is the idempotency gate, a retry would just report
            # already_served.
            "submit_autonomic_completion": EdgeProcessDefinition(
                name="submit_autonomic_completion",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            # Fleet-qualification pair (landing 35). peer_identity is a pure
            # read of the call's own server-supplied attribution context —
            # no write at all, so a repeat after a transient state-read
            # fault is always harmless.
            "peer_identity": EdgeProcessDefinition(
                name="peer_identity",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            "resolve_caller_provenance": EdgeProcessDefinition(
                name="resolve_caller_provenance",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type="caller_provenance",
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            # qualify_fleet spawns, drives, and retires a real bounded
            # worker; a retry after an ambiguous failure would mint a
            # second spawn, so the caller decides, never the error
            # processor.
            "qualify_fleet": EdgeProcessDefinition(
                name="qualify_fleet",
                result_processor_template_customizations=MergeResultProcessorCustomizations(),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
        }

    # ------------------------------------------------------------------
    # AgentMessagingServiceInterface — delegation
    # ------------------------------------------------------------------

    def list_threads(
        self,
        request: ListAgentThreadsRequest,
    ) -> AgentThreadsPage:
        return self._require_service().list_threads(request)

    def read_thread_messages(
        self,
        request: ReadThreadMessagesRequest,
    ) -> AgentThreadMessagesPage:
        return self._require_service().read_thread_messages(request)

    def peer_send(self, request: PeerSendRequest) -> PeerSendResult:
        if not self._active:
            raise RuntimeError(
                f"{self.name}: peer_send refused — this color is inactive "
                "(see LifecycleManaged.set_active). The router should route "
                "peer dispatch to the active color; a request landing here "
                "indicates a routing race or misconfiguration.",
            )
        return self._require_service().peer_send(request)

    def peer_inbox(self, request: PeerInboxRequest) -> PeerInbox:
        if not self._active:
            return PeerInbox(
                recipient_agent_id=request.recipient_agent_id,
                entries=(),
                next_after_created_at=None,
                instance_exhausted=True,
            )
        return self._require_service().peer_inbox(request)

    def get_schema_definitions(self) -> list[SchemaDefinition]:
        return [
            get_agent_messaging_schema(),
            get_agent_role_message_schema(),
            get_agent_direct_wake_schema(),
            get_role_covered_mark_schema(),
            get_role_read_schema(),
            get_peer_binding_schema_definition(),
            get_agent_role_binding_schema_definition(),
            get_role_model_schema_definition(),
            get_session_lifecycle_schema_definition(),
        ]

    # ------------------------------------------------------------------
    # Peer messaging — send_peer_message (scheduler-callable)
    # ------------------------------------------------------------------

    @platform_process(
        name="send_peer_message",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "peer_id": ParameterMetadata(
                description="Agent ID of the recipient peer (e.g. 'claude_code')",
                required=True,
                type=ParameterType.STRING,
            ),
            "peer_agent_instance_id": ParameterMetadata(
                description=(
                    "Specific instance ID of the recipient peer. "
                    "Required when multiple instances of peer_id are registered."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "peer_agent_session_id": ParameterMetadata(
                description=(
                    "Stable recipient session ID from peer_list. Used only if "
                    "peer_agent_instance_id cannot resolve a live binding, which "
                    "reaches a no-claim watcher registration without inventing a role."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "content": ParameterMetadata(
                description="Message text to deliver to the peer.",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description="Delivery outcome with thread and message identifiers",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Peer message delivery result",
            properties={
                "thread_id": ParameterMetadata(type=ParameterType.STRING),
                "message_id": ParameterMetadata(type=ParameterType.STRING),
                "delivery": ParameterMetadata(type=ParameterType.STRING),
                "drive_on_delivery": ParameterMetadata(type=ParameterType.STRING),
                "delivered_to_agent_id": ParameterMetadata(type=ParameterType.STRING),
                "delivered_to_agent_instance_id": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def send_peer_message(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Instance-addressed peer send, stamped with the CALLER's identity.

        §34.6: this verb used to hardcode the ``system`` / ``system:scheduler``
        / ``System (Scheduler)`` sentinel, so a message sent through it arrived
        unattributable no matter which transport the caller used — including a
        registered MCP session, whose identity was already sitting unread in
        ``state``. It now resolves the sender through the same ladder
        ``peer_send_by_name`` uses (:func:`_resolve_role_send_sender`), which
        reads only SERVER-STAMPED state keys; the sentinel remains the honest
        answer for a genuinely scheduler-originated send.

        ``sender_bridge_id`` deliberately stays :data:`SYSTEM_SCHEDULER_ID`:
        peer threads are keyed on ``(sender_bridge_id, peer_instance)``, so
        substituting the caller's live (or one-shot) bridge id would fork a new
        thread per send. Only the identity triple changes — never the key.
        """
        if self._peer_registry is None or self._bridge_manager is None:
            return _failure_result(
                code="bridge.not_running",
                message="Bridge not started — call start_interface first",
            )
        raw = params.get("parameters", params)
        peer_id = str(raw.get("peer_id", ""))
        peer_agent_instance_id = raw.get("peer_agent_instance_id") or None
        peer_agent_session_id = raw.get("peer_agent_session_id") or None
        content_text = str(raw.get("content", ""))
        content: list[TextPart] = [TextPart(type="text", text=content_text)]
        # Unchanged from before this lane: state_service may be None (not yet
        # bound at bootstrap) and _resolve_role_send_sender already degrades
        # gracefully on that — never hard-fail this verb over it. The new
        # dispatch_peer_send param accepts None for exactly this case
        # (drive_on_delivery is best-effort and no-ops without one).
        state_service = self._get_state_service()
        sender = _resolve_role_send_sender(state, state_service)
        try:
            outcome = dispatch_peer_send(
                bridge_manager=self._bridge_manager,
                peer_registry=self._peer_registry,
                agent_messaging_service=self._require_service(),
                state_service=state_service,
                sender_bridge_id=SYSTEM_SCHEDULER_ID,
                sender_agent_id=sender.agent_id,
                sender_agent_instance_id=sender.agent_instance_id,
                sender_session_label=sender.session_label,
                sender_parent_pid=None,
                peer_id=peer_id,
                peer_agent_instance_id=peer_agent_instance_id,
                peer_agent_session_id=peer_agent_session_id,
                content=content,
                # WS-2c V4: resolved from the SENDER's registered instance, not
                # from ``sender.reply_to_role`` — the ladder's role rung takes
                # ``sorted(roles)[0]``, which is fine as a flow tag but would
                # misroute a multi-role sender's replies (DEF-3).
                reply_to_role=sole_role_for_reply_address(
                    self._get_state_service(),
                    sender.agent_instance_id,
                ),
            )
        except (
            PeerAmbiguousError,
            PeerUnreachableError,
            BridgeNotFoundError,
            BridgeQueueFullError,
            NativeWakeError,
        ) as exc:
            return _failure_result(code="peer_send_failed", message=str(exc))
        return _success_result(data=outcome.to_payload())

    # ------------------------------------------------------------------
    # Peer addressing by role name — peer_send_by_name / peer_claim_role
    # / peer_release_role. v10 Control #2 made the ``agent_role_binding``
    # state table (StateManagementInterface) the sole resolution + CAS
    # authority, retiring the former address-book backing; see
    # ``role_binding_store`` and the v10 cutover note in
    # ``workbench/2026-05-29_address_book_driven_peer_addressing.md``.
    # ------------------------------------------------------------------

    @platform_process(
        name="peer_send_by_name",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "name": ParameterMetadata(
                description=(
                    "Role name registered in agent_role_binding "
                    "(e.g. 'Coordinator', 'Architect', 'Git-Controller')."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "content": ParameterMetadata(
                description=(
                    "Message text. Delivery is a transport property, not a "
                    "sender-declared one (A4, 2026-08-04): every send is "
                    "delivery-attempted against the resolved recipient's "
                    "live binding, waking it if a native adapter is "
                    "registered. A leading 'IMPORTANT:' is stripped as "
                    "input hygiene only; it no longer changes delivery."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "Delivery outcome plus the resolved (agent_id, agent_instance_id, "
            "session_label) the name pointed at when the call was made."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="peer_send_by_name delivery + resolution result",
            properties={
                "thread_id": ParameterMetadata(type=ParameterType.STRING),
                "message_id": ParameterMetadata(type=ParameterType.STRING),
                "delivery": ParameterMetadata(type=ParameterType.STRING),
                "drive_on_delivery": ParameterMetadata(type=ParameterType.STRING),
                "resolved_agent_id": ParameterMetadata(type=ParameterType.STRING),
                "resolved_agent_instance_id": ParameterMetadata(
                    type=ParameterType.STRING,
                ),
                "resolved_session_label": ParameterMetadata(
                    type=ParameterType.STRING,
                ),
            },
        ),
    )
    def peer_send_by_name(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Resolve ``name`` via the ``agent_role_binding`` table, then peer_send."""
        if self._peer_registry is None or self._bridge_manager is None:
            return _failure_result(
                code="bridge.not_running",
                message="Bridge not started — call start_interface first",
            )
        raw = params.get("parameters", params)
        name = str(raw.get("name", "")).strip()
        content_text = str(raw.get("content", ""))
        if not name:
            return _failure_result(
                code="missing_name",
                message="peer_send_by_name requires a non-empty role 'name'.",
            )
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        # v10 Control #2.C cutover: resolution authority is now the
        # agent_role_binding table (state-interface), not the address book. A
        # backfilled (UNCLAIMED) binding still resolves — the send then queues
        # for replay rather than rejecting.
        try:
            role = resolve_role_binding(state_service, name)
        except RoleBindingVacantError as exc:
            return _failure_result(code="peer_role_vacant", message=str(exc))
        if not role.agent_instance_id:
            return _failure_result(
                code="peer_role_malformed",
                message=(
                    f"agent_role_binding row for {name!r} is missing "
                    "'agent_instance_id'; re-claim via peer_claim_role."
                ),
            )
        content: list[TextPart] = [TextPart(type="text", text=content_text)]
        # v10 Control #4: persist-first role dispatch. The role row was just
        # existence-gated above (Scope-decision C: an unknown name is rejected
        # before any persist). dispatch_role_send writes the authoritative
        # envelope, then best-effort delivers to the current holder — an
        # offline/zombie holder yields ``queued_for_replay`` (success; the row
        # is durable and the repair loop re-delivers), never a hard failure.
        # ``message_id`` is minted ONCE per logical send (stable across any
        # transport retry, so the deterministic external_id stays idempotent).
        message_id = f"arm-{secrets.token_hex(16)}"
        # REL-01 Fork 4 (Control #3.2 realised): stamp the sender from the
        # caller's DURABLE role — lifted into ``state`` from the flow trigger_data
        # by ``ActionProcessor._lift_inference_vertex_identity`` — so a role reply
        # routes back to whoever holds the caller's role (reconnect-surviving),
        # closing the KB-08 §4 sender-stamping wart. Uses the originating
        # instance when present, then the system scheduler sentinel for scheduler sends.
        sender = _resolve_role_send_sender(state, state_service)
        outcome = dispatch_role_send(
            bridge_manager=self._bridge_manager,
            peer_registry=self._peer_registry,
            agent_messaging_service=self._require_service(),
            state_service=state_service,
            role_name=name,
            role=role,
            sender_bridge_id=sender.bridge_id,
            sender_agent_id=sender.agent_id,
            sender_agent_instance_id=sender.agent_instance_id,
            sender_session_label=sender.session_label,
            sender_principal_kind=_sender_principal_kind_from_state(state),
            sender_parent_pid=None,
            reply_to_role=sender.reply_to_role,
            content=content,
            message_id=message_id,
        )
        return _success_result(data=outcome.to_payload())

    @platform_process(
        name="deliver_job_completion",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "name": ParameterMetadata(
                description=(
                    "Role name to deliver the completion to, taken from the "
                    "originating flow's completion_route_role stamp. Never "
                    "caller-supplied in normal operation."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "job_id": ParameterMetadata(
                description="The completed job's id, as the dispatch returned it.",
                required=True,
                type=ParameterType.STRING,
            ),
            "provider_name": ParameterMetadata(
                description="Originating 'plugin.verb' that produced the job.",
                required=False,
                type=ParameterType.STRING,
            ),
            "status": ParameterMetadata(
                description="Terminal job status: completed or error.",
                required=True,
                type=ParameterType.STRING,
            ),
            "payload": ParameterMetadata(
                description=(
                    "The job's attached result or error payload, embedded in "
                    "the delivered message so the recipient needs no second "
                    "lookup."
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
        },
        output_type="object",
        output_description=(
            "Delivery outcome plus whether the job's completion_reach was "
            "upgraded to role_inbox_delivered."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="deliver_job_completion delivery + stamp result",
            properties={
                "thread_id": ParameterMetadata(type=ParameterType.STRING),
                "message_id": ParameterMetadata(type=ParameterType.STRING),
                "delivery": ParameterMetadata(type=ParameterType.STRING),
                "drive_on_delivery": ParameterMetadata(type=ParameterType.STRING),
                "resolved_agent_id": ParameterMetadata(type=ParameterType.STRING),
                "resolved_agent_instance_id": ParameterMetadata(
                    type=ParameterType.STRING,
                ),
                "resolved_session_label": ParameterMetadata(
                    type=ParameterType.STRING,
                ),
                "reach_stamped": ParameterMetadata(type=ParameterType.BOOLEAN),
            },
        ),
    )
    def deliver_job_completion(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Deliver a finished job's outcome into a durable role-addressed inbox.

        The push half of the background-job completion contract, whose durable
        + retrievable half ``job_service`` already ships. Submitted by
        ``AsyncJobManager`` in place of the inference continuation when the
        originating flow carries a ``completion_route_role``.

        Distinct from :meth:`peer_send_by_name` for ONE load-bearing reason:
        the sender. That verb runs the REL-01 resolution ladder over the flow's
        own trigger_data, whose caller-attribution rung would resolve to the
        job's originator — i.e. the recipient — and stamp the completion as
        having been sent by the very session it is being delivered to. A job
        completion has no human sender, so the sentinel is hardcoded here and
        never taken from the flow or the caller.

        Stamping is downstream of a MEASURED hand-off: the reach value is
        upgraded only after ``dispatch_role_send`` returns, because the
        submission of an action cannot observe the delivery it requests.
        ``queued_for_replay`` counts as success — the persist-first contract
        means the envelope is durable and the repair drain owns re-delivery —
        and that is exactly the property the stamp is asserting.
        """
        if self._peer_registry is None or self._bridge_manager is None:
            return _failure_result(
                code="bridge.not_running",
                message="Bridge not started — call start_interface first",
            )
        raw = params.get("parameters", params)
        name = str(raw.get("name", "")).strip()
        job_id = str(raw.get("job_id", "")).strip()
        if not name or not job_id:
            return _failure_result(
                code="missing_arguments",
                message=("deliver_job_completion requires a non-empty 'name' and 'job_id'."),
            )
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        try:
            role = resolve_role_binding(state_service, name)
        except RoleBindingVacantError as exc:
            return _failure_result(code="peer_role_vacant", message=str(exc))
        if not role.agent_instance_id:
            return _failure_result(
                code="peer_role_malformed",
                message=(
                    f"agent_role_binding row for {name!r} is missing "
                    "'agent_instance_id'; re-claim via peer_claim_role."
                ),
            )
        status = str(raw.get("status", "")).strip()
        payload = raw.get("payload")
        content: list[TextPart] = [
            TextPart(
                type="text",
                text=_format_job_completion_message(
                    job_id=job_id,
                    provider_name=str(raw.get("provider_name", "")).strip(),
                    status=status,
                    payload=payload if isinstance(payload, dict) else None,
                ),
            )
        ]
        outcome = dispatch_role_send(
            bridge_manager=self._bridge_manager,
            peer_registry=self._peer_registry,
            agent_messaging_service=self._require_service(),
            state_service=state_service,
            role_name=name,
            role=role,
            sender_bridge_id=SYSTEM_JOB_COMPLETION_ID,
            sender_agent_id=SYSTEM_AGENT_ID,
            sender_agent_instance_id=SYSTEM_JOB_COMPLETION_ID,
            sender_session_label=SYSTEM_JOB_COMPLETION_LABEL,
            sender_principal_kind=SENDER_PRINCIPAL_KIND_SYSTEM,
            sender_parent_pid=None,
            # A completion has no conversational counterpart to reply to; the
            # job row and its payloads remain the authoritative record.
            reply_to_role="",
            content=content,
            message_id=f"arm-{secrets.token_hex(16)}",
        )
        stamped = _stamp_role_inbox_delivered(state_service, job_id)
        payload_out = dict(outcome.to_payload())
        payload_out["reach_stamped"] = stamped
        return _success_result(data=payload_out)

    def _send_handover_notice(
        self,
        *,
        peer_id: str,
        peer_agent_instance_id: str,
        prose: str,
        kind: str,
    ) -> bool:
        """Bind this plugin's bridge collaborators to the shared REL-04 sender.

        Kept as a bound method because ``AutonomicAssignment`` takes it as its
        ``send_notice`` callable. The behaviour lives in :mod:`role_claim` so the
        verb, the bridge route, and the autonomic lane all emit the same notice.
        """
        return send_handover_notice(
            bridge_manager=self._bridge_manager,
            peer_registry=self._peer_registry,
            agent_messaging_service=self._handover_service(),
            state_service=self._get_state_service(),
            peer_id=peer_id,
            peer_agent_instance_id=peer_agent_instance_id,
            prose=prose,
            kind=kind,
        )

    def _handover_service(self) -> Any:
        """The messaging service the REL-04 notices dispatch through, or ``None``.

        ``_require_service`` builds the service on first use, so the raw
        ``_service`` attribute is not a substitute for it. Only built when the
        bridge collaborators exist: without them a notice cannot be dispatched
        at all, and building the service would be wasted work that can raise on
        a plugin whose orchestrator is not injected.
        """
        if self._bridge_manager is None or self._peer_registry is None:
            return None
        return self._require_service()

    def _claimant_session_id(self, agent_instance_id: str) -> str:
        """A session's stable id from its live ``peer_binding`` row (REL-07(1)).

        ``peer_holds_role`` reads it here rather than trusting a caller-supplied
        session id — the whole point of that verb is that it compares PULL-TRUTH.
        The claim path sources the same value inside
        :func:`role_claim.claim_role_for_session`, from the same registry.
        Returns ``""`` when the bridge is not started or the instance is
        unregistered.
        """
        if self._peer_registry is None:
            return ""
        return self._peer_registry.agent_session_id_for_instance(agent_instance_id)

    def _dispatch_actor_from_state(self, state: dict[str, Any]) -> DispatchActor:
        """Derive managed-dispatch authority from a server-derived identity."""
        try:
            principal = extract_authenticated_principal(state)
        except PermissionError as exc:
            instance_id = str(
                state.get("inference_vertex_session_id")
                or state.get("caller_attribution_instance_id")
                or ""
            ).strip()
            if not instance_id:
                raise DispatchError("dispatch_authentication_required", str(exc)) from exc
            session_id = self._claimant_session_id(instance_id)
            if not session_id:
                raise DispatchError(
                    "dispatch_identity_unregistered",
                    "Registered local bridge identity has no live peer binding.",
                ) from exc
            return DispatchActor(
                agent_instance_id=instance_id,
                agent_session_id=session_id,
                authority_source="live_peer_binding",
            )
        instance_id = principal.agent_instance_id.strip()
        session_id = self._claimant_session_id(instance_id)
        if not instance_id or not session_id:
            raise DispatchError(
                "dispatch_identity_unregistered",
                "Authenticated caller has no registered durable session identity.",
            )
        return DispatchActor(
            agent_instance_id=instance_id,
            agent_session_id=session_id,
            authority_source="oauth_principal",
        )

    @platform_process(
        name="peer_claim_role",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "name": ParameterMetadata(
                description="Role name to claim (e.g. 'Coordinator').",
                required=True,
                type=ParameterType.STRING,
            ),
            "agent_id": ParameterMetadata(
                description="Claiming session's agent_id (e.g. 'claude_code').",
                required=True,
                type=ParameterType.STRING,
            ),
            "agent_instance_id": ParameterMetadata(
                description="Claiming session's agent_instance_id (agi-...).",
                required=True,
                type=ParameterType.STRING,
            ),
            "agent_session_id": ParameterMetadata(
                description=(
                    "Stable logical session id for reconnect-safe role binding. "
                    "When omitted, the plugin sources it from the claimant's "
                    "live peer_binding row."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "session_label": ParameterMetadata(
                description=(
                    "Display label as of the claim. May or may not match "
                    "``name``; both are stored separately."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "takeover": ParameterMetadata(
                description=(
                    "Explicitly take the role from a LIVE holder, displacing it. "
                    "Default false, in which case a live holder is refused with "
                    "``role_held_live`` and the claim does nothing. This is the "
                    "escape hatch that refusal's message names: it exists so a "
                    "deliberate, operator-confirmed handover is possible while an "
                    "accidental one still fails. It authorizes THIS claim only and "
                    "is never persisted. With no live holder it is a silent no-op — "
                    "an ordinary claim — so callers never have to pre-check "
                    "liveness and race their own answer."
                ),
                required=False,
                type=ParameterType.BOOLEAN,
            ),
        },
        output_type="object",
        output_description=(
            "agent_role_binding claim outcome (v10): action='claimed', the "
            "role name, and the bound agent_instance_id."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="peer_claim_role outcome (v10 agent_role_binding claim)",
            properties={
                "action": ParameterMetadata(type=ParameterType.STRING),
                "name": ParameterMetadata(type=ParameterType.STRING),
                "agent_instance_id": ParameterMetadata(type=ParameterType.STRING),
                "agent_session_id": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def peer_claim_role(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Claim-or-replace the ``agent_role_binding`` row for ``name`` (v10 #2.C).

        The MODEL_INITIATED transport for a claim: reached through
        ``/process/call``, so a genuine model turn (the ``/rename`` skill) stamps
        ``last_model_activity_at`` as it should. The forwarder's housekeeping
        claim deliberately does NOT come here — it uses the INFRA
        ``peer/claim_role`` bridge route, which shares this body via
        :func:`role_claim.claim_role_for_session` but is classified so it never
        stamps. Splitting the transports is what separates "the model claimed a
        role" from "the bridge re-asserted its binding"; see the module docstring
        of :mod:`role_claim`.

        Caller-supplied identity (the ``/rename`` skill threads ``agent_id`` /
        ``agent_instance_id`` from the ``peer_register`` response, and
        ``agent_session_id`` from the same response when a carrier set it). A
        full-row upsert replaces any prior binding for the name in place —
        including a backfilled UNCLAIMED ``agent_session_id``.
        """
        raw = params.get("parameters", params)
        result = claim_role_for_session(
            origin=RoleClaimOrigin.MODEL_TURN,
            name=str(raw.get("name", "")),
            agent_id=str(raw.get("agent_id", "")),
            agent_instance_id=str(raw.get("agent_instance_id", "")),
            agent_session_id=str(raw.get("agent_session_id", "")),
            session_label=str(raw.get("session_label", "")),
            # Explicit escape hatch for a LIVE holder (§4.3.3a). bool() rather
            # than a truthiness test on the raw value: the transport hands JSON,
            # so a caller sending the STRING "false" would otherwise take the
            # role — the opposite of what they asked for, on the one parameter
            # whose whole purpose is that it must be deliberate.
            takeover=_coerce_takeover(raw.get("takeover")),
            state_service=self._get_state_service(),
            bridge_manager=self._bridge_manager,
            peer_registry=self._peer_registry,
            # NOT ``self._service`` — that attribute is lazily populated, and
            # ``_require_service`` is what BUILDS it on first use. Reading the
            # raw attribute would hand the claim body ``None`` whenever nothing
            # had happened to construct the service yet, and the handover notices
            # would then be skipped with a "bridge not started" log while the
            # claim reported success: a displaced holder never told it lost the
            # role. Guarded exactly as ``_send_handover_notice`` guards it — with
            # no bridge collaborators there is nothing to notify anyway, so
            # building the service would be pointless work that can raise.
            agent_messaging_service=self._handover_service(),
            # SERVER-BUILT context, lifted into ``state`` by the action processor
            # — never read from caller ``params``, so slot ownership cannot be
            # forged.
            call_context=state.get("call_context"),
        )
        if isinstance(result, RoleClaimFailure):
            return _failure_result(code=result.code, message=result.message)
        return _success_result(data=dict(result.to_public()))

    @platform_process(
        name="peer_release_role",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "name": ParameterMetadata(
                description="Role name to release (delete its agent_role_binding row).",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "agent_role_binding release outcome (v10): the released flag and the role name."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="peer_release_role outcome (v10 agent_role_binding release)",
            properties={
                "released": ParameterMetadata(type=ParameterType.BOOLEAN),
                "name": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def peer_release_role(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> dict[str, Any]:
        """Delete the ``agent_role_binding`` row for ``name`` (v10 #2.C)."""
        raw = params.get("parameters", params)
        name = str(raw.get("name", "")).strip()
        if not name:
            return _failure_result(
                code="missing_name",
                message="peer_release_role requires a non-empty role 'name'.",
            )
        # §6.1 no-vacant-release: a system slot (reserved 'sys:' keyspace) is only
        # ever RE-BOUND (a claim that atomically replaces the holder), never
        # released to vacant — a vacant system slot strands its capability (e.g.
        # the autonomic inference lane). Reject the release.
        if is_system_role(name):
            return _failure_result(
                code="system_slot_release_denied",
                message=(
                    f"system slot {name!r} cannot be released to vacant (reserved "
                    f"keyspace); a system slot is only ever re-bound, never released."
                ),
            )
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        # Fleet session-management Phase B, D1 (§2 rule 3, Architect ratification
        # #2): capture the PRE-release holder's agent_session_id so the
        # session_role_claim row can be pruned AFTER the binding release —
        # binding-release strictly precedes the session-key-row delete (never
        # the reverse: row-first + a crash between lets a fresh INSERT for a
        # new role slip past the still-standing old binding, a double-claim).
        prior_session_id = ""
        with contextlib.suppress(RoleBindingVacantError, RoleBindingMalformedError):
            prior_session_id = resolve_role_binding_v4(state_service, name).agent_session_id
        # §9 CUTOVER: hard-delete the v4 role_binding row (no-tombstone §5.1).
        outcome = release_role_binding_v4(state_service, name)
        if prior_session_id and not is_system_role(name):
            delete_session_role_claim_if_still_holds(
                state_service,
                agent_session_id=prior_session_id,
                expected_held_role=name,
            )
        return _success_result(data=outcome)

    @platform_process(
        name="spawn_session",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "role_class": ParameterMetadata(
                description=(
                    "ephemeral | project | principal (§2 taxonomy; primary/chat "
                    "are never spawn-assigned)."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "lane_id": ParameterMetadata(
                description="The lane this session is spawned for (provenance).",
                required=True,
                type=ParameterType.STRING,
            ),
            "brief_ref": ParameterMetadata(
                description="Workbench path or dispatch id backing the spawn (provenance).",
                required=True,
                type=ParameterType.STRING,
            ),
            "unit_id": ParameterMetadata(
                description="Optional project-solet work-unit identity resolvable by the spawned lane.",
                required=False,
                type=ParameterType.STRING,
            ),
            "repository_root": ParameterMetadata(
                description=(
                    "Optional absolute Git checkout for this lane. Required when the "
                    "dispatched unit targets a repository other than the serving Solet's "
                    "own checkout; validated before any worktree side effect."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "work_class": ParameterMetadata(
                description="read_only | analysis_deliverable | production_mutation.",
                required=True,
                type=ParameterType.STRING,
            ),
            "budget_line": ParameterMetadata(
                description="The token-budget ledger key this spawn rolls up to.",
                required=True,
                type=ParameterType.STRING,
            ),
            "agent_runtime": ParameterMetadata(
                description=(
                    "Worker runtime using the exact peer-registry agent_id vocabulary: "
                    "claude_code | codex. Orthogonal to host; omitted defaults to "
                    "claude_code for compatibility."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "role_name": ParameterMetadata(
                description=(
                    "Named role to fill on boot (project: may mint; principal: "
                    "fill-never-mint, must already be legislated). Empty for ephemeral."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "host": ParameterMetadata(
                description=(
                    "Per-spawn host override (tmux | headless | operator). Empty "
                    "falls to FLEET_SESSION_HOST env, then the platform default."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "visibility": ParameterMetadata(
                description="visible | headless (spawn-time operator/primary parameter).",
                required=False,
                type=ParameterType.STRING,
            ),
            "model": ParameterMetadata(
                description="Dispatch model override.",
                required=False,
                type=ParameterType.STRING,
            ),
            "dispatch_kind": ParameterMetadata(
                description="Required nonblank unit-kind provenance text; the value does not choose a model pair.",
                required=True,
                type=ParameterType.STRING,
            ),
            "difficulty_score": ParameterMetadata(
                description="Required capability score passed to select_dispatch_tier.",
                required=True,
                type=ParameterType.FLOAT,
            ),
            "selection_receipt": ParameterMetadata(
                description="Required selection_receipt returned by select_dispatch_tier; replayed before spawn.",
                required=True,
                type=ParameterType.OBJECT,
            ),
            "reviewed_report_vendor": ParameterMetadata(
                description="For review: vendor that authored the reviewed report (codex | claude_code).",
                required=False,
                type=ParameterType.STRING,
            ),
            "pair_id": ParameterMetadata(
                description="For diagnose/design: shared cross-vendor producer pair identity.",
                required=False,
                type=ParameterType.STRING,
            ),
            "scope_tags": ParameterMetadata(
                description="Caller-declared work scope tags, e.g. [\"state_schema\"] for a state-service table change. Tags preserve provenance and do not by themselves restrict the model; the state_schema model floor is retired (rul_0c6ec7c7).",
                required=False,
                type=ParameterType.LIST,
            ),
            "effort": ParameterMetadata(
                description="Dispatch effort override.",
                required=False,
                type=ParameterType.STRING,
            ),
            "allowed_tools": ParameterMetadata(
                description=(
                    "Explicit tool-name allowlist override for the headless "
                    "PreToolUse gate (§6 permission-mode ruling, 2026-08-03). "
                    "Omitted -> resolved from plugin.yaml's per-work_class "
                    "work_class_tool_allowlists; unconfigured -> empty (the "
                    "spawn is still gated, just with nothing extra allowed)."
                ),
                required=False,
                type=ParameterType.LIST,
            ),
            "permission_mode": ParameterMetadata(
                description=(
                    "Explicit --permission-mode override for the headless host "
                    "driver (§6 permission-mode design, 2026-08-03). Omitted -> "
                    "resolved from plugin.yaml's headless_permission_mode. No "
                    "value is rejected (operator ruling, 2026-08-03: 'we don't "
                    "have any restrictions now'); the driver still refuses if "
                    "this and the config both resolve to nothing at all."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "allow_askuserquestion": ParameterMetadata(
                description=(
                    "Per-spawn escape hatch for the AskUserQuestion default-deny "
                    "(operator ruling, 2026-08-14: the structured-choice picker "
                    "stalls an unattended/peer-driven session, so it is denied by "
                    "default). Only the tmux host driver honors this -- it "
                    "launches a real interactive claude CLI, the one Claude-"
                    "runtime host where the picker can render. The headless "
                    "driver never enumerates the tool at all (measured: its "
                    "stream-json mode omits it from the tool list by "
                    "construction), so this flag is inert there, and the codex "
                    "runtime has no equivalent tool. No plugin.yaml default -- "
                    "the ruling fixes the global default (deny); this is purely "
                    "a per-call override, named after the seed launcher's own "
                    "SOLET_ALLOW_ASKUSERQUESTION=1."
                ),
                required=False,
                type=ParameterType.BOOLEAN,
            ),
            "report_by_seconds": ParameterMetadata(
                description="Initial report-or-die deadline, in seconds from spawn.",
                required=False,
                type=ParameterType.INTEGER,
            ),
            "spawned_by_instance_id": ParameterMetadata(
                description="Lineage: the spawning session's own agent_instance_id.",
                required=False,
                type=ParameterType.STRING,
            ),
            "spawned_by_role": ParameterMetadata(
                description="Lineage: the spawning session's role name at spawn time, if any.",
                required=False,
                type=ParameterType.STRING,
            ),
            "dispatch_id": ParameterMetadata(
                description=(
                    "Server-issued preparing managed_dispatch identity. Required for "
                    "project-class work; ordinary callers use dispatch_managed_work."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "local_name": ParameterMetadata(
                description=(
                    "Exact name the worker answers to and registers locally; explicit "
                    "Git-Controller preserves the mutation guard identity."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "degraded_hooks_acknowledged": ParameterMetadata(
                description="Explicit acknowledgement that required worker hooks are degraded.",
                required=False,
                type=ParameterType.BOOLEAN,
            ),
        },
        output_type="object",
        output_description=(
            "spawn_session outcome: the new session's identity + host dispatch result."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="spawn_session outcome (D1 §4)",
            properties={
                "agent_instance_id": ParameterMetadata(type=ParameterType.STRING),
                "agent_runtime": ParameterMetadata(type=ParameterType.STRING),
                "host": ParameterMetadata(type=ParameterType.STRING),
                "host_ref": ParameterMetadata(type=ParameterType.STRING),
                "dispatch_id": ParameterMetadata(type=ParameterType.STRING),
                "lifecycle_state": ParameterMetadata(type=ParameterType.STRING),
                "first_turn_source": ParameterMetadata(
                    description="charter | fallback — which text was driven as turn 1 "
                    "(phase 2 slice 6).",
                    type=ParameterType.STRING,
                ),
                "first_turn_delivered": ParameterMetadata(
                    description="Whether the first-turn send succeeded. False never blocks "
                    "the spawn itself; the failure is logged separately.",
                    type=ParameterType.BOOLEAN,
                ),
                "first_turn_error": ParameterMetadata(
                    description="Non-empty error detail when first_turn_delivered is False; "
                    "empty on success.",
                    type=ParameterType.STRING,
                ),
            },
        ),
    )
    def spawn_session(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """§4 ``spawn_session`` — validate, write the ledger row (spawning,
        BEFORE host dispatch), dispatch through the resolved host driver.

        ``operator`` (degenerate, cannot spawn), ``headless`` (D1, the
        registered default), and ``tmux`` (D2) all ship registered — see
        ``session_hosts.py`` for the current registry. A call with
        ``host="operator"`` (or an unconfigured ``headless``/``tmux``
        environment) still ends in ``host_cannot_spawn``, with the specific
        remedies in the error; an undeclared/typo'd host name ends in
        ``host_mechanism_missing``.
        """
        raw = params.get("parameters", params)
        try:
            reject_retired_session_arguments("plugin::agent_messaging_plugin::spawn_session", raw)
        except FrameworkError as exc:
            return _failure_result(code=str(exc.error_code), message=str(exc))
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        req = _spawn_session_request_from_params(raw, format_directed_by(state.get("call_context")))
        if req.role_class == "project":
            return _failure_result(
                code="managed_dispatch_required",
                message=(
                    "Public spawn_session cannot create project work, even with a prepared ID; "
                    "use dispatch_managed_work or resolve_managed_dispatch(request_retry)."
                ),
            )
        req = _apply_spawn_session_policy(req, self._build_session_lifecycle_policy_config())
        try:
            result = lifecycle_spawn_session(state_service, req)
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="dispatch_managed_work",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "role_class": ParameterMetadata(required=True, type=ParameterType.STRING),
            "lane_id": ParameterMetadata(required=True, type=ParameterType.STRING),
            "role_name": ParameterMetadata(required=True, type=ParameterType.STRING),
            "brief_ref": ParameterMetadata(required=True, type=ParameterType.STRING),
            "repository_root": ParameterMetadata(required=False, type=ParameterType.STRING),
            # Register Unit mint (design unt_57725090 s4.1). Empty unit_id mints a
            # Unit before spawn; a supplied one is verified against the register.
            "unit_id": ParameterMetadata(
                required=False, type=ParameterType.STRING,
                description="Existing register Unit to verify; empty mints one before spawn.",
            ),
            "repository_id": ParameterMetadata(
                required=False, type=ParameterType.STRING,
                description="Expected register repository; must equal the one resolved from the lane root.",
            ),
            "unit_key": ParameterMetadata(
                required=False, type=ParameterType.STRING,
                description="Minted unit_key override; default <lane_id>-<dispatch_id>.",
            ),
            "addresses": ParameterMetadata(
                required=False, type=ParameterType.LIST,
                description="Issue ids the minted Unit addresses; ignored when unit_id is supplied.",
            ),
            "reference_basis": ParameterMetadata(
                required=False, type=ParameterType.STRING,
                description="Register reference basis for a minted fix Unit (existing_pattern or no_existing_pattern).",
            ),
            "reference_basis_reason": ParameterMetadata(
                required=False, type=ParameterType.STRING,
                description="Nonblank reason required by no_existing_pattern.",
            ),
            "brief_repository_root": ParameterMetadata(
                required=False, type=ParameterType.STRING,
                description=(
                    "Absolute root of the registered repository holding a brief that lives outside the lane "
                    "root (psolet --brief-repo). Never inferred: an outside brief without it is refused."
                ),
            ),
            "brief_sha256": ParameterMetadata(required=True, type=ParameterType.STRING),
            "expected_path": ParameterMetadata(required=True, type=ParameterType.STRING),
            "completion_contract": ParameterMetadata(required=True, type=ParameterType.OBJECT),
            "work_class": ParameterMetadata(required=True, type=ParameterType.STRING),
            "budget_line": ParameterMetadata(required=True, type=ParameterType.STRING),
            "model": ParameterMetadata(required=True, type=ParameterType.STRING),
            "dispatch_kind": ParameterMetadata(
                required=True, type=ParameterType.STRING,
                description="Required nonblank unit-kind provenance text; the value does not choose a model pair.",
            ),
            "difficulty_score": ParameterMetadata(required=True, type=ParameterType.FLOAT),
            "selection_receipt": ParameterMetadata(required=True, type=ParameterType.OBJECT),
            "reviewed_report_vendor": ParameterMetadata(required=False, type=ParameterType.STRING),
            "pair_id": ParameterMetadata(required=False, type=ParameterType.STRING),
            "scope_tags": ParameterMetadata(
                required=False, type=ParameterType.LIST, description="Caller-declared work scope tags, e.g. [\"state_schema\"] for a state-service table change. Tags preserve provenance and do not by themselves restrict the model; the state_schema model floor is retired (rul_0c6ec7c7).",
            ),
            "effort": ParameterMetadata(required=True, type=ParameterType.STRING),
            "agent_runtime": ParameterMetadata(required=True, type=ParameterType.STRING),
            "allowed_hosts": ParameterMetadata(required=True, type=ParameterType.LIST),
            "host": ParameterMetadata(required=True, type=ParameterType.STRING),
            "spawned_by_role": ParameterMetadata(required=True, type=ParameterType.STRING),
            "visibility": ParameterMetadata(required=True, type=ParameterType.STRING),
            "local_name": ParameterMetadata(required=True, type=ParameterType.STRING),
            "report_by_seconds": ParameterMetadata(required=True, type=ParameterType.INTEGER),
            "allowed_tools": ParameterMetadata(required=True, type=ParameterType.LIST),
            "permission_mode": ParameterMetadata(required=True, type=ParameterType.STRING),
            "transport": ParameterMetadata(required=True, type=ParameterType.STRING),
            "allow_askuserquestion": ParameterMetadata(required=True, type=ParameterType.BOOLEAN),
            "degraded_hooks_acknowledged": ParameterMetadata(
                required=True,
                type=ParameterType.BOOLEAN,
            ),
            "uptake_due_at": ParameterMetadata(required=True, type=ParameterType.STRING),
            "report_by": ParameterMetadata(required=True, type=ParameterType.STRING),
            "watchdog_due_at": ParameterMetadata(required=True, type=ParameterType.STRING),
        },
        output_type="object",
        output_description="Prepared dispatch plus linked current attempt and first-turn evidence.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Managed dispatch plus linked current attempt.",
            properties={
                "dispatch": ParameterMetadata(type=ParameterType.OBJECT),
                "attempt": ParameterMetadata(type=ParameterType.OBJECT),
            },
        ),
    )
    def dispatch_managed_work(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        raw = params.get("parameters", params)
        try:
            reject_retired_session_arguments("plugin::agent_messaging_plugin::dispatch_managed_work", raw)
        except FrameworkError as exc:
            return _failure_result(code=str(exc.error_code), message=str(exc))
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        directed_by = format_directed_by(state.get("call_context"))
        spawn_req = _spawn_session_request_from_params(raw, directed_by)
        spawn_req = _apply_spawn_session_policy(
            spawn_req,
            self._build_session_lifecycle_policy_config(),
        )
        try:
            actor = self._dispatch_actor_from_state(state)
            spawn_req = replace(
                spawn_req,
                spawned_by_instance_id=actor.agent_instance_id,
            )
            if not holds_role(state_service, spawn_req.spawned_by_role, actor.agent_session_id):
                raise DispatchError(
                    "coordinator_authority_denied",
                    "Authenticated caller does not hold spawned_by_role.",
                )
            spec = _dispatch_spec_from_params(raw, spawn_req, directed_by)
            result = lifecycle_dispatch_managed_work(
                state_service, spec, spawn_req, register=self._register_unit_client(),
            )
        except (DispatchError, VerbError) as exc:
            return _failure_result(
                code=exc.code,
                message=exc.message,
                data=exc.data if isinstance(exc, DispatchError) else None,
            )
        return _success_result(data=result)

    @platform_process(
        name="provision_role_session",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "role_name": ParameterMetadata(required=True, type=ParameterType.STRING),
            "requested_role_class": ParameterMetadata(required=False, type=ParameterType.STRING),
            "lane_id": ParameterMetadata(required=True, type=ParameterType.STRING),
            "brief_ref": ParameterMetadata(required=True, type=ParameterType.STRING),
            "reference_basis": ParameterMetadata(
                required=False, type=ParameterType.STRING,
                description="Register reference basis for the minted Unit; required when dispatch_kind is fix.",
            ),
            "reference_basis_reason": ParameterMetadata(
                required=False, type=ParameterType.STRING,
                description="Nonblank reason required by no_existing_pattern.",
            ),
            "brief_repository_root": ParameterMetadata(
                required=False, type=ParameterType.STRING,
                description=(
                    "Absolute root of the registered repository holding a brief that lives outside the lane "
                    "root (psolet --brief-repo). Never inferred: an outside brief without it is refused."
                ),
            ),
            "brief_sha256": ParameterMetadata(required=True, type=ParameterType.STRING),
            "expected_path": ParameterMetadata(required=True, type=ParameterType.STRING),
            "completion_contract": ParameterMetadata(required=True, type=ParameterType.OBJECT),
            "work_class": ParameterMetadata(required=True, type=ParameterType.STRING),
            "budget_line": ParameterMetadata(required=True, type=ParameterType.STRING),
            "model": ParameterMetadata(required=True, type=ParameterType.STRING),
            "dispatch_kind": ParameterMetadata(
                required=True, type=ParameterType.STRING,
                description="Required nonblank unit-kind provenance text; the value does not choose a model pair.",
            ),
            "difficulty_score": ParameterMetadata(required=True, type=ParameterType.FLOAT),
            "selection_receipt": ParameterMetadata(required=True, type=ParameterType.OBJECT),
            "reviewed_report_vendor": ParameterMetadata(required=False, type=ParameterType.STRING),
            "pair_id": ParameterMetadata(required=False, type=ParameterType.STRING),
            "scope_tags": ParameterMetadata(
                required=False, type=ParameterType.LIST, description="Caller-declared work scope tags, e.g. [\"state_schema\"] for a state-service table change. Tags preserve provenance and do not by themselves restrict the model; the state_schema model floor is retired (rul_0c6ec7c7).",
            ),
            "effort": ParameterMetadata(required=True, type=ParameterType.STRING),
            "agent_runtime": ParameterMetadata(required=True, type=ParameterType.STRING),
            "allowed_hosts": ParameterMetadata(required=True, type=ParameterType.LIST),
            "host": ParameterMetadata(required=True, type=ParameterType.STRING),
            "spawned_by_role": ParameterMetadata(required=True, type=ParameterType.STRING),
            "visibility": ParameterMetadata(required=True, type=ParameterType.STRING),
            "report_by_seconds": ParameterMetadata(required=True, type=ParameterType.INTEGER),
            "allowed_tools": ParameterMetadata(required=True, type=ParameterType.LIST),
            "permission_mode": ParameterMetadata(required=True, type=ParameterType.STRING),
            "transport": ParameterMetadata(required=True, type=ParameterType.STRING),
            "allow_askuserquestion": ParameterMetadata(required=True, type=ParameterType.BOOLEAN),
            "degraded_hooks_acknowledged": ParameterMetadata(
                required=True,
                type=ParameterType.BOOLEAN,
            ),
            "uptake_due_at": ParameterMetadata(required=True, type=ParameterType.STRING),
            "report_by": ParameterMetadata(required=True, type=ParameterType.STRING),
            "watchdog_due_at": ParameterMetadata(required=True, type=ParameterType.STRING),
        },
        output_type="object",
        output_description=(
            "One-call role provisioning: resolved class, optional principal legislation, "
            "managed dispatch attempt, and read-only Git-controller gate status."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Provisioned role-session outcome.",
            properties={
                "provisioning_action": ParameterMetadata(type=ParameterType.STRING),
                "resolved_role_class": ParameterMetadata(type=ParameterType.STRING),
                "legislation": ParameterMetadata(type=ParameterType.OBJECT),
                "existing_holder": ParameterMetadata(type=ParameterType.OBJECT),
                "dispatch": ParameterMetadata(type=ParameterType.OBJECT),
                "attempt": ParameterMetadata(type=ParameterType.OBJECT),
                "git_controller_gate": ParameterMetadata(type=ParameterType.OBJECT),
            },
        ),
    )
    def provision_role_session(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Provision a routable project role without making callers guess its class.

        The worker, not this composing verb, claims the durable binding: the
        shared first-turn frame always tells named workers to claim first.
        """
        raw = params.get("parameters", params)
        try:
            reject_retired_session_arguments("plugin::agent_messaging_plugin::provision_role_session", raw)
        except FrameworkError as exc:
            return _failure_result(code=str(exc.error_code), message=str(exc))
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        try:
            data = self._provision_role_session_data(state_service, raw, state)
        except (DispatchError, VerbError) as exc:
            return _failure_result(
                code=exc.code,
                message=exc.message,
                data=exc.data if isinstance(exc, DispatchError) else None,
            )
        return _success_result(data=data)

    def _provision_role_session_data(
        self,
        state_service: Any,
        raw: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, object]:
        directed_by = format_directed_by(state.get("call_context"))
        role_name = str(raw.get("role_name") or "").strip()
        resolved_role_class, needs_legislation = lifecycle_resolve_provisioned_role_class(
            state_service,
            role_name=role_name,
            requested_role_class=str(raw.get("requested_role_class") or ""),
        )
        actor = self._dispatch_actor_from_state(state)
        _require_provisioner_authority(
            state_service,
            str(raw.get("spawned_by_role") or ""),
            actor,
        )
        existing_holder = _provisioned_live_role_holder(self, state_service, role_name)
        if existing_holder is not None:
            return {
                "provisioning_action": "existing_live_holder",
                "resolved_role_class": resolved_role_class,
                "legislation": {"action": "not_required"},
                "existing_holder": existing_holder,
                "dispatch": {},
                "attempt": {},
                "git_controller_gate": _git_controller_launcher_report(role_name),
            }
        legislation = _provision_legislation(
            state_service,
            needs_legislation=needs_legislation,
            role_name=role_name,
            role_class=resolved_role_class,
            brief_ref=str(raw.get("brief_ref") or ""),
            directed_by=directed_by,
        )
        spawn_raw = dict(raw)
        spawn_raw["role_class"] = resolved_role_class
        spawn_raw["local_name"] = lifecycle_resolve_local_name(
            role_class=resolved_role_class,
            role_name=role_name,
            lane_id=str(raw.get("lane_id") or ""),
        )
        spawn_req = _spawn_session_request_from_params(spawn_raw, directed_by)
        spawn_req = _apply_spawn_session_policy(
            spawn_req,
            self._build_session_lifecycle_policy_config(),
        )
        spawn_req = replace(spawn_req, spawned_by_instance_id=actor.agent_instance_id)
        result = lifecycle_dispatch_managed_work(
            state_service,
            _dispatch_spec_from_params(spawn_raw, spawn_req, directed_by),
            spawn_req,
            register=self._register_unit_client(),
        )
        return {
            "provisioning_action": "spawned",
            "resolved_role_class": resolved_role_class,
            "legislation": legislation,
            "existing_holder": {},
            "dispatch": result["dispatch"],
            "attempt": result["attempt"],
            "git_controller_gate": _git_controller_launcher_report(role_name),
        }

    @platform_process(
        name="report_managed_dispatch",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "dispatch_id": ParameterMetadata(required=True, type=ParameterType.STRING),
            "event_id": ParameterMetadata(required=True, type=ParameterType.STRING),
            "event_kind": ParameterMetadata(required=True, type=ParameterType.STRING),
            "attempt_agent_instance_id": ParameterMetadata(
                required=True,
                type=ParameterType.STRING,
            ),
            "prior_version": ParameterMetadata(required=True, type=ParameterType.INTEGER),
            "payload": ParameterMetadata(required=True, type=ParameterType.OBJECT),
        },
        output_type="object",
        output_description="Validated worker event and resulting dispatch projection.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Validated worker event projection.",
        ),
    )
    def report_managed_dispatch(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        try:
            actor = self._dispatch_actor_from_state(state)
            result = lifecycle_report_managed_dispatch(
                state_service,
                dispatch_id=str(raw.get("dispatch_id") or ""),
                event_id=str(raw.get("event_id") or ""),
                event_kind=str(raw.get("event_kind") or ""),
                attempt_agent_instance_id=str(raw.get("attempt_agent_instance_id") or ""),
                actor=actor,
                prior_version=int(raw.get("prior_version") or 0),
                payload=_as_object(raw.get("payload")),
                observed_at=datetime.now(UTC),
            )
        except DispatchError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="resolve_managed_dispatch",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "dispatch_id": ParameterMetadata(required=True, type=ParameterType.STRING),
            "event_id": ParameterMetadata(required=True, type=ParameterType.STRING),
            "action": ParameterMetadata(required=True, type=ParameterType.STRING),
            "prior_version": ParameterMetadata(required=True, type=ParameterType.INTEGER),
            "payload": ParameterMetadata(required=True, type=ParameterType.OBJECT),
        },
        output_type="object",
        output_description="Coordinator decision and resulting dispatch projection.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Coordinator decision projection.",
        ),
    )
    def resolve_managed_dispatch(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        try:
            actor = self._dispatch_actor_from_state(state)
            result = lifecycle_resolve_managed_dispatch(
                state_service,
                dispatch_id=str(raw.get("dispatch_id") or ""),
                event_id=str(raw.get("event_id") or ""),
                action=str(raw.get("action") or ""),
                actor=actor,
                prior_version=int(raw.get("prior_version") or 0),
                payload=_as_object(raw.get("payload")),
                observed_at=datetime.now(UTC),
                register=self._register_unit_client(),
            )
        except DispatchError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="managed_dispatch_status",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "dispatch_id": ParameterMetadata(required=True, type=ParameterType.STRING),
        },
        output_type="object",
        output_description="Decision-oriented aggregate managed-dispatch status.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Decision-oriented dispatch aggregate.",
        ),
    )
    def managed_dispatch_status(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        try:
            result = lifecycle_managed_dispatch_status(
                state_service,
                str(raw.get("dispatch_id") or ""),
            )
        except DispatchError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="managed_dispatch_inventory",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "state": ParameterMetadata(
                required=False,
                type=ParameterType.STRING,
                description="Optional exact managed-dispatch state filter.",
            ),
            "limit": ParameterMetadata(
                required=False,
                type=ParameterType.INTEGER,
                description="Page size from 1 through 250; defaults to 100.",
            ),
            "after_created_at": ParameterMetadata(
                required=False,
                type=ParameterType.STRING,
                description="Echo next_cursor.created_at with after_id.",
            ),
            "after_id": ParameterMetadata(
                required=False,
                type=ParameterType.STRING,
                description="Echo next_cursor.id with after_created_at.",
            ),
        },
        output_type="object",
        output_description="Tie-safe page of managed-dispatch inventory rows.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Managed-dispatch inventory page.",
            properties={
                "dispatches": ParameterMetadata(type=ParameterType.LIST),
                "returned": ParameterMetadata(type=ParameterType.INTEGER),
                "truncated": ParameterMetadata(type=ParameterType.BOOLEAN),
                "next_cursor": ParameterMetadata(type=ParameterType.OBJECT),
            },
        ),
    )
    def managed_dispatch_inventory(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        try:
            result = lifecycle_managed_dispatch_inventory(
                state_service,
                state_name=str(raw.get("state") or ""),
                limit=raw.get("limit", 100),
                after_created_at=raw.get("after_created_at"),
                after_id=raw.get("after_id"),
            )
        except DispatchError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="managed_dispatch_events",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "dispatch_id": ParameterMetadata(
                required=False,
                type=ParameterType.STRING,
                description="Optional owning dispatch filter; omit to enumerate all events.",
            ),
            "event_kind": ParameterMetadata(
                required=False,
                type=ParameterType.STRING,
                description="Optional exact event-kind filter.",
            ),
            "accepted": ParameterMetadata(
                required=False,
                type=ParameterType.BOOLEAN,
                description="Optional accepted/rejected filter.",
            ),
            "limit": ParameterMetadata(
                required=False,
                type=ParameterType.INTEGER,
                description="Page size from 1 through 250; defaults to 100.",
            ),
            "after_event_at": ParameterMetadata(
                required=False,
                type=ParameterType.STRING,
                description="Echo next_cursor.event_at with after_id.",
            ),
            "after_id": ParameterMetadata(
                required=False,
                type=ParameterType.STRING,
                description="Echo next_cursor.id with after_event_at.",
            ),
        },
        output_type="object",
        output_description="Tie-safe page of append-only managed-dispatch events.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Managed-dispatch event page.",
            properties={
                "events": ParameterMetadata(type=ParameterType.LIST),
                "returned": ParameterMetadata(type=ParameterType.INTEGER),
                "truncated": ParameterMetadata(type=ParameterType.BOOLEAN),
                "next_cursor": ParameterMetadata(type=ParameterType.OBJECT),
            },
        ),
    )
    def managed_dispatch_events(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        try:
            result = lifecycle_managed_dispatch_events(
                state_service,
                dispatch_id=str(raw.get("dispatch_id") or ""),
                event_kind=str(raw.get("event_kind") or ""),
                accepted=raw["accepted"] if "accepted" in raw else None,
                limit=raw.get("limit", 100),
                after_event_at=raw.get("after_event_at"),
                after_id=raw.get("after_id"),
            )
        except DispatchError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="legislate_role",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "name": ParameterMetadata(
                description=(
                    "The role name to legislate (e.g. 'Coordinator-Main' for a "
                    "role_class='primary' seat)."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "role_class": ParameterMetadata(
                description=(
                    "primary | principal — the ONE two-value taxonomy this "
                    "governance act may assign; project/ephemeral/chat are "
                    "minted, never legislated."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "brief_ref": ParameterMetadata(
                description=(
                    "Workbench path, dispatch id, or ruling reference "
                    "authorizing this act (provenance — mirrors spawn_session's "
                    "brief_ref)."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description="legislate_role outcome: the legislated name + role_class.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="legislate_role outcome (D4 Part B item 1)",
            properties={
                "action": ParameterMetadata(type=ParameterType.STRING),
                "name": ParameterMetadata(type=ParameterType.STRING),
                "role_class": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def legislate_role(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """D4 Part B item 1 — governance-act creation of a ``role`` row with
        an authority-carrying ``role_class`` (``primary``/``principal``)
        stamped at birth. The ONE sanctioned path outside ``peer_claim_role``
        (§3.1 Q1: claim-time is enforce-by-class, never class-assignment).

        ``directed_by`` is server-built from ``call_context`` via
        ``format_directed_by`` — the SAME provenance convention
        ``spawn_session`` uses, never caller-supplied.
        """
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        req = LegislateRoleRequest(
            name=str(raw.get("name") or ""),
            role_class=str(raw.get("role_class") or ""),
            brief_ref=str(raw.get("brief_ref") or ""),
            directed_by=format_directed_by(state.get("call_context")),
        )
        try:
            result = lifecycle_legislate_role(state_service, req)
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="capture_lane_charter",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "lane_id": ParameterMetadata(
                description="The lane this charter founds.",
                required=True,
                type=ParameterType.STRING,
            ),
            "charter_text": ParameterMetadata(
                description=(
                    "The operator's verbatim founding words, captured byte-exact. "
                    "Driven unmodified as a spawned worker's literal first turn "
                    "(spawn_session resolves the LATEST captured row for the "
                    "worker's lane_id)."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "captured_at": ParameterMetadata(
                description=(
                    "ISO-8601 timestamp of when the operator spoke these words in "
                    "the seat conversation — NOT the row-write time."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "brief_ref": ParameterMetadata(
                description="Workbench path or dispatch id this charter accompanies.",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description="capture_lane_charter outcome: the newly written charter row.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="capture_lane_charter outcome (phase 2 slice 6)",
            properties={
                "lane_id": ParameterMetadata(type=ParameterType.STRING),
                "charter_text": ParameterMetadata(type=ParameterType.STRING),
                "brief_ref": ParameterMetadata(type=ParameterType.STRING),
                "captured_at": ParameterMetadata(type=ParameterType.STRING),
                "directed_by": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def capture_lane_charter(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """Phase 2 slice 6, design check-in ruling item 3(a) — the seat-
        invoked governance act that writes a ``lane_charter`` row.
        Insert-only: calling this again for the same ``lane_id`` supersedes
        by recency, it never edits a prior charter's text in place.

        ``directed_by`` is server-built from ``call_context`` via
        ``format_directed_by`` — the SAME provenance convention
        ``spawn_session``/``legislate_role`` use, never caller-supplied.
        """
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        req = CaptureLaneCharterRequest(
            lane_id=str(raw.get("lane_id") or ""),
            charter_text=str(raw.get("charter_text") or ""),
            captured_at=str(raw.get("captured_at") or ""),
            brief_ref=str(raw.get("brief_ref") or ""),
            directed_by=format_directed_by(state.get("call_context")),
        )
        try:
            result = lifecycle_capture_lane_charter(state_service, req)
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="arm_session_dependency",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "waiter_instance_id": ParameterMetadata(
                description=(
                    "The waiting session's agent_instance_id. Required — v1 is "
                    "session-scoped ONLY; lane-scoped arming is unsupported by "
                    "construction (there is no waiter_lane_id parameter)."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "condition_kind": ParameterMetadata(
                description="lane_closed | session_terminal | deadline.",
                required=True,
                type=ParameterType.STRING,
            ),
            "condition_ref": ParameterMetadata(
                description=(
                    "The condition_kind's referent: a lane_id (lane_closed), "
                    "an agent_instance_id (session_terminal), or an ISO-8601 "
                    "timestamp (deadline). Shape-checked per kind."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "arm_session_dependency outcome (drive-on-delivery lane rider) — the armed wake edge."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="arm_session_dependency outcome",
            properties={
                "waiter_instance_id": ParameterMetadata(type=ParameterType.STRING),
                "condition_kind": ParameterMetadata(type=ParameterType.STRING),
                "condition_ref": ParameterMetadata(type=ParameterType.STRING),
                "armed": ParameterMetadata(type=ParameterType.BOOLEAN),
            },
        ),
    )
    def arm_session_dependency(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Rider verb (drive-on-delivery lane, slice 2, 2026-08-04) — the
        FIRST caller of the D1 ``session_dependency`` wake-edge machinery.
        See :func:`session_lifecycle_verbs.arm_session_dependency` for the
        full contract (session-scoped only, no waiter-existence check,
        per-kind ``condition_ref`` shape validation)."""
        del state
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        req = ArmSessionDependencyRequest(
            waiter_instance_id=str(raw.get("waiter_instance_id") or ""),
            condition_kind=str(raw.get("condition_kind") or ""),
            condition_ref=str(raw.get("condition_ref") or ""),
        )
        try:
            result = lifecycle_arm_session_dependency(state_service, req)
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="drain_session_claude_mapping_spool",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        output_type="object",
        output_description=(
            "T1 usage-capture lane — drains the SessionStart hook's "
            "file-per-firing spool into session_claude_mapping."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="drain_session_claude_mapping_spool outcome",
            properties={
                "files_seen": ParameterMetadata(type=ParameterType.INTEGER),
                "upserted": ParameterMetadata(type=ParameterType.INTEGER),
                "skipped_malformed": ParameterMetadata(type=ParameterType.INTEGER),
            },
        ),
    )
    def drain_session_claude_mapping_spool(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """T1 usage-capture lane (ruling 2026-08-05) — testable/on-demand
        entry point for :func:`session_claude_mapping_ingest.
        drain_session_claude_mapping_spool`; the SAME function is also
        called directly from ``_run_session_lifecycle_sweep`` (the sweep-tick
        wiring the ruling requires — a verb nobody calls is bound-in-name-only)."""
        del params, state
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        result = lifecycle_drain_session_claude_mapping_spool(state_service)
        return _success_result(data=result)

    @platform_process(
        name="list_sessions",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "lane_id": ParameterMetadata(
                description="Filter to one lane_id.",
                required=False,
                type=ParameterType.STRING,
            ),
            "work_class": ParameterMetadata(
                description="Filter to one work_class.",
                required=False,
                type=ParameterType.STRING,
            ),
            "host": ParameterMetadata(
                description="Filter to one host.",
                required=False,
                type=ParameterType.STRING,
            ),
            "lifecycle_state": ParameterMetadata(
                description="Filter to one lifecycle_state.",
                required=False,
                type=ParameterType.STRING,
            ),
            "live_only": ParameterMetadata(
                description="When true, include only non-terminal lifecycle states.",
                required=False,
                type=ParameterType.BOOLEAN,
            ),
            "limit": ParameterMetadata(
                description=(
                    f"Page size; defaults to {LIST_SESSIONS_DEFAULT_LIMIT} and may not exceed "
                    f"{LIST_SESSIONS_MAX_LIMIT}. A full page returns next_cursor; never infer "
                    "that a full page is the complete roster."
                ),
                required=False,
                type=ParameterType.INTEGER,
            ),
            "after_created_at": ParameterMetadata(
                description=(
                    "First cursor component: echo next_cursor.created_at from the prior page "
                    "with after_id. Omit both on the first page."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "after_id": ParameterMetadata(
                description=(
                    "Second cursor component: echo next_cursor.id from the prior page with "
                    "after_created_at. Omit both on the first page."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description="§4 list_sessions — the ONE fleet list.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="list_sessions outcome",
            properties={
                "sessions": ParameterMetadata(type=ParameterType.LIST),
                "returned": ParameterMetadata(type=ParameterType.INTEGER),
                "truncated": ParameterMetadata(type=ParameterType.BOOLEAN),
                "next_cursor": ParameterMetadata(type=ParameterType.OBJECT),
            },
        ),
    )
    def list_sessions(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        """§4 ``list_sessions`` — the ONE fleet list.

        Operator registrations appear as inventory rows through the normal
        registration path, but carry no invented report-by or TTL contract.
        """
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        filters = {
            key: str(raw[key])
            for key in ("lane_id", "work_class", "host", "lifecycle_state")
            if raw.get(key)
        }
        raw_limit = raw.get("limit", LIST_SESSIONS_DEFAULT_LIMIT)
        try:
            result = lifecycle_list_sessions(
                state_service,
                filters or None,
                live_only=raw.get("live_only") is True,
                limit=raw_limit,
                after_created_at=raw.get("after_created_at"),
                after_id=raw.get("after_id"),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="fleet_status",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "scope": ParameterMetadata(
                description="Fleet population: 'lanes' (default) or 'all'.",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "Bounded read-only fleet classification over managed sessions, armed "
            "dependencies, context gauges, and role-message owed records."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="fleet_status outcome",
            properties={
                "sessions": ParameterMetadata(type=ParameterType.LIST),
                "class_counts": ParameterMetadata(type=ParameterType.OBJECT),
                "unregistered": ParameterMetadata(type=ParameterType.OBJECT),
                "owed_messages": ParameterMetadata(type=ParameterType.LIST),
                "legacy_direct_delivery_unknown": ParameterMetadata(type=ParameterType.BOOLEAN),
            },
        ),
    )
    def fleet_status(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        """U1 fleet status: a read-only, bounded existing-data projection."""
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        raw = params.get("parameters", params)
        try:
            result = lifecycle_fleet_status(state_service, scope=raw.get("scope", "lanes"))
        except FleetStatusError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="record_fleet_liveness_run",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "observed_at": ParameterMetadata(required=True, type=ParameterType.STRING),
            "checked": ParameterMetadata(required=True, type=ParameterType.OBJECT),
            "stuck": ParameterMetadata(required=True, type=ParameterType.LIST),
            "actions": ParameterMetadata(required=True, type=ParameterType.LIST),
            "outcome": ParameterMetadata(required=True, type=ParameterType.STRING),
            "role_inbox_drain": ParameterMetadata(required=True, type=ParameterType.OBJECT),
            "sleep_check": ParameterMetadata(required=True, type=ParameterType.OBJECT),
            "escalations": ParameterMetadata(required=True, type=ParameterType.LIST),
        },
        output_type="object",
        output_description="Append one structured Phase-A fleet-liveness run in deployment-native state.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="record_fleet_liveness_run outcome",
            properties={"status": ParameterMetadata(type=ParameterType.STRING)},
        ),
    )
    def record_fleet_liveness_run(
        self, params: dict[str, Any], state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(code="state_service_unavailable", message="state_service is not bound on this solet.")
        try:
            result = lifecycle_record_fleet_liveness_run(
                state_service,
                observed_at=raw.get("observed_at") if isinstance(raw.get("observed_at"), str) else "",
                checked=raw.get("checked"), stuck=raw.get("stuck"), actions=raw.get("actions"),
                outcome=raw.get("outcome") if isinstance(raw.get("outcome"), str) else "",
                role_inbox_drain=raw.get("role_inbox_drain"), sleep_check=raw.get("sleep_check"),
                escalations=raw.get("escalations"),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="record_fleet_progress_run",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "reviewed_at": ParameterMetadata(required=True, type=ParameterType.STRING),
            "workstream_id": ParameterMetadata(required=True, type=ParameterType.STRING),
            "objective_citation": ParameterMetadata(required=True, type=ParameterType.STRING),
            "metrics": ParameterMetadata(required=True, type=ParameterType.OBJECT),
            "delta": ParameterMetadata(required=True, type=ParameterType.OBJECT),
            "assessment": ParameterMetadata(required=True, type=ParameterType.STRING),
            "recommendation": ParameterMetadata(required=True, type=ParameterType.STRING),
            "independent_critique": ParameterMetadata(required=True, type=ParameterType.OBJECT),
            "escalations": ParameterMetadata(required=True, type=ParameterType.LIST),
            "phase_a_run_id": ParameterMetadata(required=False, type=ParameterType.STRING),
        },
        output_type="object",
        output_description="Append one structured Phase-B workstream assessment in deployment-native state.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="record_fleet_progress_run outcome",
            properties={"status": ParameterMetadata(type=ParameterType.STRING)},
        ),
    )
    def record_fleet_progress_run(
        self, params: dict[str, Any], state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(code="state_service_unavailable", message="state_service is not bound on this solet.")
        try:
            result = lifecycle_record_fleet_progress_run(
                state_service,
                reviewed_at=raw.get("reviewed_at") if isinstance(raw.get("reviewed_at"), str) else "",
                workstream_id=raw.get("workstream_id") if isinstance(raw.get("workstream_id"), str) else "",
                objective_citation=raw.get("objective_citation") if isinstance(raw.get("objective_citation"), str) else "",
                metrics=raw.get("metrics"), delta=raw.get("delta"),
                assessment=raw.get("assessment") if isinstance(raw.get("assessment"), str) else "",
                recommendation=raw.get("recommendation") if isinstance(raw.get("recommendation"), str) else "",
                independent_critique=raw.get("independent_critique"),
                escalations=raw.get("escalations"), phase_a_run_id=_opt_str(raw.get("phase_a_run_id")),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="recent_fleet_liveness_runs",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "limit": ParameterMetadata(required=False, type=ParameterType.INTEGER),
            "after_observed_at": ParameterMetadata(required=False, type=ParameterType.STRING),
            "after_id": ParameterMetadata(required=False, type=ParameterType.STRING),
        },
        output_type="object",
        output_description="Read a bounded newest-first page of structured Phase-A liveness runs.",
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="recent_fleet_liveness_runs outcome"),
    )
    def recent_fleet_liveness_runs(
        self, params: dict[str, Any], state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(code="state_service_unavailable", message="state_service is not bound on this solet.")
        try:
            result = lifecycle_recent_fleet_liveness_runs(
                state_service, limit=raw.get("limit", 64),
                after_observed_at=_opt_str(raw.get("after_observed_at")), after_id=_opt_str(raw.get("after_id")),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="recent_fleet_progress_runs",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "workstream_id": ParameterMetadata(required=False, type=ParameterType.STRING),
            "limit": ParameterMetadata(required=False, type=ParameterType.INTEGER),
            "after_reviewed_at": ParameterMetadata(required=False, type=ParameterType.STRING),
            "after_id": ParameterMetadata(required=False, type=ParameterType.STRING),
        },
        output_type="object",
        output_description="Read a bounded newest-first page of structured Phase-B assessments, optionally by workstream.",
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="recent_fleet_progress_runs outcome"),
    )
    def recent_fleet_progress_runs(
        self, params: dict[str, Any], state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(code="state_service_unavailable", message="state_service is not bound on this solet.")
        try:
            result = lifecycle_recent_fleet_progress_runs(
                state_service, workstream_id=_opt_str(raw.get("workstream_id")), limit=raw.get("limit", 64),
                after_reviewed_at=_opt_str(raw.get("after_reviewed_at")), after_id=_opt_str(raw.get("after_id")),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="budget_report",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "lane_id": ParameterMetadata(
                description="Filter to one lane_id.",
                required=False,
                type=ParameterType.STRING,
            ),
            "budget_line": ParameterMetadata(
                description="Filter to one budget_line.",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "T1 S3 -- per-budget_line token-usage rollup, joining managed_session/"
            "session_claude_mapping against session_ledger's session/event tables."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="budget_report outcome",
            properties={
                "budget_lines": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "One entry per distinct budget_line among matching "
                        "managed_session rows. Each entry: budget_line (str), "
                        "sessions_covered (int, contributed >=1 usage-bearing "
                        "session_ledger event), sessions_uncovered (int, no "
                        "mapping row or no ledger usage events -- the S2c "
                        "absence-detection population), as_of (ISO-8601 str or "
                        "null -- the latest event_at among included usage "
                        "events; null when sessions_covered is 0), usage "
                        "(dict[str, number] -- per-field sums of whatever "
                        "numeric keys actually appear in the vendor's verbatim "
                        "usage_json, no fixed schema), and by_model (dict "
                        "keyed on managed_session.model, empty string for "
                        "unset, each value the same "
                        "sessions_covered/sessions_uncovered/as_of/usage shape "
                        "scoped to that model)."
                    ),
                ),
            },
        ),
    )
    def budget_report(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        """T1 S3 -- read-only token-usage rollup per budget_line. See
        ``budget_report.py``'s module docstring for the join mechanism (the
        first cross-plugin state read against session_ledger's own tables)
        and the seat's three S3 design rails (staleness marker, coverage
        disclosure, compile-time schema coupling)."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        result = lifecycle_build_budget_report(
            state_service,
            lane_id=str(raw.get("lane_id") or ""),
            budget_line=str(raw.get("budget_line") or ""),
        )
        return _success_result(data=result)

    @platform_process(
        name="list_session_claude_mappings",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The managed_session whose mapping rows to list.",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "usage-capture-attribution D2 follow-on -- every live "
            "session_claude_mapping row observed for one agent_instance_id."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="list_session_claude_mappings outcome",
            properties={
                "mappings": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "Every live session_claude_mapping row for the given "
                        "agent_instance_id, oldest-observation-order NOT "
                        "guaranteed (callers needing order sort by "
                        "captured_at themselves). Each row: agent_instance_id "
                        "(str), claude_session_id (str, the Claude Code "
                        "session_id this firing captured), captured_at "
                        "(ISO-8601 str, the hook payload's own timestamp), "
                        "capture_source (str: hook:startup | hook:clear | "
                        "hook:resume | init_event), plus the standard "
                        "state-layer row fields (id, namespace, created_at, "
                        "updated_at, created_by, updated_by, name, "
                        "is_deleted, external_id) every state-managed table "
                        "carries. An empty list means no mapping has EVER "
                        "been observed for this worker (SessionStart hook "
                        "never fired, or the worker predates the capture "
                        "landing) -- not an error."
                    ),
                ),
            },
        ),
    )
    def list_session_claude_mappings(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """usage-capture-attribution D2 follow-on (workbench
        2026-08-06_usage_capture_attribution_findings_usage-capture-impl.md)
        -- a read-only listing verb over session_claude_mapping, named
        during that lane's D1 diagnosis as the missing piece that forced
        inference instead of measurement. Read-only, issues no writes;
        thin wrapper over the same store-layer function budget_report.py
        already uses internally."""
        raw = params.get("parameters", params)
        agent_instance_id = str(raw.get("agent_instance_id") or "")
        if not agent_instance_id:
            return _failure_result(
                code="missing_agent_instance_id",
                message="list_session_claude_mappings requires a non-empty agent_instance_id.",
            )
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        mappings = lifecycle_list_session_claude_mappings(state_service, agent_instance_id)
        return _success_result(data={"mappings": mappings})

    @platform_process(
        name="session_status",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The managed_session to look up.",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description="§4 session_status — the ledger row.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="The live managed_session row.",
            properties={
                "agent_instance_id": ParameterMetadata(type=ParameterType.STRING),
                "agent_session_id": ParameterMetadata(type=ParameterType.STRING),
                "agent_id": ParameterMetadata(type=ParameterType.STRING),
                "spawned_by_instance_id": ParameterMetadata(type=ParameterType.STRING),
                "spawned_by_role": ParameterMetadata(type=ParameterType.STRING),
                "lane_id": ParameterMetadata(type=ParameterType.STRING),
                "brief_ref": ParameterMetadata(type=ParameterType.STRING),
                "model": ParameterMetadata(type=ParameterType.STRING),
                "effort": ParameterMetadata(type=ParameterType.STRING),
                "work_class": ParameterMetadata(type=ParameterType.STRING),
                "budget_line": ParameterMetadata(type=ParameterType.STRING),
                "visibility": ParameterMetadata(type=ParameterType.STRING),
                "host": ParameterMetadata(type=ParameterType.STRING),
                "host_ref": ParameterMetadata(type=ParameterType.STRING),
                "capability_report": ParameterMetadata(type=ParameterType.OBJECT),
                "report_by": ParameterMetadata(type=ParameterType.STRING),
                "lifecycle_state": ParameterMetadata(type=ParameterType.STRING),
                "last_transition_at": ParameterMetadata(type=ParameterType.STRING),
                "directed_by": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def session_status(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        """§4 ``session_status`` — the ledger row. (Host-liveness enrichment
        is deferred to whichever caller has the host driver registry; this
        verb's contract is the ledger truth, always available.)"""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        try:
            row = lifecycle_session_status(state_service, str(raw.get("agent_instance_id", "")))
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=row)

    @platform_process(
        name="reconcile_operator_session_liveness",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "dry_run": ParameterMetadata(
                description=(
                    "True = classify and report only; pass false to act. When true, report "
                    "every operator managed_session classification and proposed transition "
                    "without writing."
                ),
                default=True,
                required=False,
                type=ParameterType.BOOLEAN,
            ),
        },
        output_type="object",
        output_description=(
            "R3-U1 operator presence reconciliation: classifications, proposed "
            "terminal transitions, and the rows applied when dry_run is false."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Operator managed_session liveness reconciliation result.",
            properties={
                "dry_run": ParameterMetadata(type=ParameterType.BOOLEAN),
                "classifications": ParameterMetadata(type=ParameterType.LIST),
                "applied": ParameterMetadata(type=ParameterType.LIST),
            },
        ),
    )
    def reconcile_operator_session_liveness(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """R3-U1's explicit, dry-run-first backfill surface.

        This is intentionally a caller-driven one-time operation, not a sweep
        rider.  R3s owns the later periodic schedule and calls the shared
        library primitive directly.
        """
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        if self._peer_registry is None or self._bridge_manager is None:
            return _failure_result(
                code="liveness_services_unavailable",
                message="peer registry or bridge manager is not bound on this solet.",
            )
        try:
            result = lifecycle_reconcile_operator_session_liveness(
                state_service,
                peer_registry=self._peer_registry,
                bridge_manager=self._bridge_manager,
                dry_run=_coerce_dry_run(raw.get("dry_run", True)),
            )
        except Exception as exc:  # noqa: BLE001 -- surface a failed reconciliation loudly
            logger.exception("operator session liveness reconciliation failed")
            return _failure_result(
                code="operator_liveness_reconciliation_failed",
                message=f"{type(exc).__name__}: {exc}",
            )
        return _success_result(data=result)

    @platform_process(
        name="clear_session",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The managed_session to clear.",
                required=True,
                type=ParameterType.STRING,
            ),
            "park": ParameterMetadata(
                description=(
                    "When true, also drives live/idle/overdue -> parked "
                    "(L3 rule 2, steward direction) after the clear is sent."
                ),
                required=False,
                type=ParameterType.BOOLEAN,
            ),
        },
        output_type="object",
        output_description=(
            "§4 clear_session (AMEND 5b) — context hygiene via the driver channel, with "
            "effect verification where the driver can read its target back (GAU-09)."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="clear_session outcome",
            properties={
                "lifecycle_state": ParameterMetadata(type=ParameterType.STRING),
                "parked": ParameterMetadata(type=ParameterType.BOOLEAN),
                "dispatched": ParameterMetadata(
                    description="The '/clear' reached the driver channel.",
                    type=ParameterType.BOOLEAN,
                ),
                "cleared": ParameterMetadata(
                    description=(
                        "True only on a POSITIVE observation that the target is now "
                        "clear; null when this driver has no read-back surface. Never "
                        "false -- a driver that looked and saw nothing raises "
                        "clear_unverified instead."
                    ),
                    type=ParameterType.BOOLEAN,
                ),
                "clear_verification": ParameterMetadata(
                    description="'confirmed' or 'unsupported_on_driver'.",
                    type=ParameterType.STRING,
                ),
            },
        ),
    )
    def clear_session(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """§4 ``clear_session`` (AMEND 5b) — sends ``/clear`` over the
        resolved host driver's channel and, where that driver can read its
        target back, VERIFIES the effect before answering (GAU-09: this verb
        used to report the send in a return value shaped like a verdict).
        ``park=True`` additionally drives the row to ``parked`` (the only
        writer of that edge), and is skipped when a verification failed."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        try:
            result = lifecycle_clear_session(
                state_service,
                agent_instance_id=str(raw.get("agent_instance_id", "")),
                park=bool(raw.get("park", False)),
                directed_by=format_directed_by(state.get("call_context")),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="compact_session",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The managed_session to compact.",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "§4 compact_session (AMEND 5b) — context hygiene via the driver channel."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="compact_session outcome",
            properties={
                "lifecycle_state": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def compact_session(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        """§4 ``compact_session`` (AMEND 5b) — fire-and-forget ``/compact``
        over the driver channel; no park mode, no lifecycle transition."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        try:
            result = lifecycle_compact_session(
                state_service,
                agent_instance_id=str(raw.get("agent_instance_id", "")),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="drive_session",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The managed_session to dispatch the work turn into.",
                required=True,
                type=ParameterType.STRING,
            ),
            "text": ParameterMetadata(
                description=(
                    "The work turn to send over the driver channel — a "
                    "self-contained dispatch (brief text or a workbench "
                    "brief pointer plus instructions). Must be non-empty."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "drive_session (D2-window rider) — work dispatch via the driver channel."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="drive_session outcome",
            properties={
                "lifecycle_state": ParameterMetadata(type=ParameterType.STRING),
                "unparked": ParameterMetadata(type=ParameterType.BOOLEAN),
                "dispatched": ParameterMetadata(type=ParameterType.BOOLEAN),
                "submitted": ParameterMetadata(type=ParameterType.BOOLEAN),
                "drive_verification": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def drive_session(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """``drive_session`` (D2-window rider, 2026-08-04; effect-verified
        public issue #9, 2026-08-19) — work dispatch over the resolved host
        driver's channel, WITH EFFECT VERIFICATION where the driver can
        provide it. ``submitted=True`` only on a positive observation that
        the driven text left the composer without ever being seen stranded
        there; ``None`` on drivers with no read-back surface. A driver that
        looked and found it stranded raises ``drive_unverified`` rather than
        returning a success-shaped lie. Owns the §3.2 ``parked -> live`` edge
        and re-arms ``report_by`` on every dispatch."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        try:
            result = lifecycle_drive_session(
                state_service,
                agent_instance_id=str(raw.get("agent_instance_id", "")),
                text=str(raw.get("text", "")),
                directed_by=format_directed_by(state.get("call_context")),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="terminate_session",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The managed_session to terminate.",
                required=True,
                type=ParameterType.STRING,
            ),
            "grace_seconds": ParameterMetadata(
                description=(
                    "Seconds to wait for a graceful host-level stop before SIGKILL. Default 30."
                ),
                required=False,
                type=ParameterType.INTEGER,
            ),
        },
        output_type="object",
        output_description=(
            "§4 terminate_session — graceful stop -> kill after grace -> "
            "ledger -> terminated. Also fires + best-effort delivers any "
            "armed session_terminal dependency edges waiting on this "
            "session, on both the transition and already-terminal paths."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="terminate_session outcome",
            properties={
                "already_terminal": ParameterMetadata(type=ParameterType.BOOLEAN),
                "lifecycle_state": ParameterMetadata(type=ParameterType.STRING),
                "session_terminal_edges_fired": ParameterMetadata(type=ParameterType.INTEGER),
            },
        ),
    )
    def terminate_session(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """§4 ``terminate_session`` — resolves the row's host driver and
        calls its real ``terminate()`` (driver-level stop, SIGKILL after
        grace) BEFORE the ledger write, so the ledger never claims
        ``terminated`` over a process still running. ``host='operator'``
        rows (degenerate driver, never spawned by us) still land the ledger
        transition — see ``session_lifecycle_verbs.terminate_session``'s
        docstring for why that's not a silent degradation. Idempotent on an
        already-terminal row."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        grace_raw = raw.get("grace_seconds")
        try:
            result = lifecycle_terminate_session(
                state_service,
                agent_instance_id=str(raw.get("agent_instance_id", "")),
                directed_by=format_directed_by(state.get("call_context")),
                **({"grace_seconds": int(grace_raw)} if grace_raw is not None else {}),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="retire_session",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The managed_session to retire (the lane-landing verb).",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "§4 retire_session — terminate + release + fire dependency edges + retired."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="retire_session outcome",
            properties={
                "already_retired": ParameterMetadata(type=ParameterType.BOOLEAN),
                "dependencies_fired": ParameterMetadata(type=ParameterType.INTEGER),
            },
        ),
    )
    def retire_session(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """§4 ``retire_session`` — the lane-landing verb; four idempotent
        steps, re-drivable by construction (session_lifecycle_verbs module
        docstring)."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        try:
            result = lifecycle_retire_session(
                state_service,
                agent_instance_id=str(raw.get("agent_instance_id", "")),
                directed_by=format_directed_by(state.get("call_context")),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="report_alive",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The reporting managed_session.",
                required=True,
                type=ParameterType.STRING,
            ),
            "status": ParameterMetadata(
                description="working | idle | heartbeat.",
                required=True,
                type=ParameterType.STRING,
            ),
            "status_note": ParameterMetadata(
                description="Optional free-text note, recorded on the audit trail.",
                required=False,
                type=ParameterType.STRING,
            ),
            "heartbeat_failures_since_last": ParameterMetadata(
                description="Failures carried forward by this successful heartbeat.",
                required=False,
                type=ParameterType.INTEGER,
            ),
            "heartbeat_failure_first_at": ParameterMetadata(
                description="First timestamp of the carried-forward failure episode.",
                required=False,
                type=ParameterType.STRING,
            ),
            "heartbeat_failure_last_reason": ParameterMetadata(
                description="Most recent carried-forward failure reason.",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description="§4 report_alive — re-arms report_by; status drives live<->idle.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="report_alive outcome",
            properties={
                "lifecycle_state": ParameterMetadata(type=ParameterType.STRING),
                "recovered": ParameterMetadata(type=ParameterType.BOOLEAN),
            },
        ),
    )
    def report_alive(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """§4 explicit ``report_alive`` re-arms ``report_by``; passive
        heartbeats record liveness only. A late explicit report from
        ``overdue`` recovers and sets ``recovered=True``."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        status = str(raw.get("status", ""))
        try:
            result = lifecycle_report_alive(
                state_service,
                agent_instance_id=str(raw.get("agent_instance_id", "")),
                status=status,
                status_note=str(raw.get("status_note", "") or ""),
                heartbeat_failures_since_last=int(raw.get("heartbeat_failures_since_last", 0) or 0),
                heartbeat_failure_first_at=_heartbeat_failure_first_at(
                    status=status,
                    raw=raw.get("heartbeat_failure_first_at"),
                ),
                heartbeat_failure_last_reason=str(raw.get("heartbeat_failure_last_reason", "") or ""),
                directed_by=format_directed_by(state.get("call_context")),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        except Exception as exc:  # noqa: BLE001 -- a heartbeat write must fail loud
            logger.exception("report_alive heartbeat write failed")
            return _failure_result(
                code="heartbeat_write_failed",
                message=f"{type(exc).__name__}: {exc}",
            )
        return _success_result(data=result)

    @platform_process(
        name="rotate_session",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The ledger agent_instance_id to rotate (from list_sessions — "
                "NEVER a peer_list/role-thread watch id).",
                required=True,
                type=ParameterType.STRING,
            ),
            "role_name": ParameterMetadata(
                description="The durable role this ledger row currently holds — used for "
                "the durable pickup dispatch (peer_send_by_name).",
                required=True,
                type=ParameterType.STRING,
            ),
            "pickup_text": ParameterMetadata(
                description="Pickup pointer driven as the post-clear turn (e.g. pointing "
                "at the worker's own handoff note + inbox).",
                required=True,
                type=ParameterType.STRING,
            ),
            "park_first": ParameterMetadata(
                description="Pass through to clear_session's park flag.",
                required=False,
                type=ParameterType.BOOLEAN,
            ),
        },
        output_type="object",
        output_description=(
            "maintenance-verbs M1, D0.3-ratified deferred-completion shape — dispatches "
            "a rotate_session choreography job and returns immediately; poll "
            "check_choreography_job_status for the outcome."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="rotate_session dispatch outcome",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING),
                "status": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def rotate_session(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """§2.1 ``rotate_session`` — ms-scale dispatch only (D0.3 mechanic 1);
        the actual clear/drive/verify choreography runs in the plugin's own
        single dedicated background worker (never inline in this handler)."""
        raw = params.get("parameters", params)
        job_manager = self._try_acquire_async_job_manager()
        if job_manager is None:
            return _failure_result(
                code="async_job_manager_unavailable",
                message="AsyncJobManager is not available on this solet.",
            )
        req = RotateSessionDispatchRequest(
            agent_instance_id=str(raw.get("agent_instance_id", "")),
            role_name=str(raw.get("role_name", "")),
            pickup_text=str(raw.get("pickup_text", "")),
            park_first=bool(raw.get("park_first", False)),
        )
        try:
            result = dispatch_rotate_session(job_manager, req, state)
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="restart_session",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The dying worker's ledger agent_instance_id.",
                required=True,
                type=ParameterType.STRING,
            ),
            "role_name": ParameterMetadata(
                description="The durable role the fresh spawn must reclaim.",
                required=True,
                type=ParameterType.STRING,
            ),
            "role_class": ParameterMetadata(
                description="The role_class to spawn under (managed_session carries no "
                "role_class column of its own — required from the caller).",
                required=True,
                type=ParameterType.STRING,
            ),
            "grace_seconds": ParameterMetadata(
                description="Pass through to terminate_session's grace_seconds.",
                required=False,
                type=ParameterType.INTEGER,
            ),
        },
        output_type="object",
        output_description=(
            "maintenance-verbs M1, D0.3-ratified deferred-completion shape — dispatches "
            "a restart_session choreography job and returns immediately; poll "
            "check_choreography_job_status for the outcome."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="restart_session dispatch outcome",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING),
                "status": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def restart_session(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """§2.2 ``restart_session`` — ms-scale dispatch only (D0.3 mechanic 1);
        the actual terminate/spawn/role-reclaim/verify choreography runs in
        the plugin's own single dedicated background worker."""
        raw = params.get("parameters", params)
        job_manager = self._try_acquire_async_job_manager()
        if job_manager is None:
            return _failure_result(
                code="async_job_manager_unavailable",
                message="AsyncJobManager is not available on this solet.",
            )
        req = RestartSessionDispatchRequest(
            agent_instance_id=str(raw.get("agent_instance_id", "")),
            role_name=str(raw.get("role_name", "")),
            role_class=str(raw.get("role_class", "")),
            grace_seconds=int(raw.get("grace_seconds", 30) or 30),
        )
        try:
            result = dispatch_restart_session(job_manager, req, state)
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="check_choreography_job_status",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "job_id": ParameterMetadata(
                description="The job_id returned by rotate_session/restart_session.",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "maintenance-verbs M1 — the caller-side polling answer for a "
            "rotate_session/restart_session job (the check_generation_status "
            "precedent); these jobs configure no completion_handlers, so this "
            "poll is the only way a direct caller learns the outcome."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Choreography job status",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING),
                "status": ParameterMetadata(type=ParameterType.STRING),
                "progress_percent": ParameterMetadata(type=ParameterType.INTEGER),
                "result": ParameterMetadata(type=ParameterType.OBJECT),
                "error": ParameterMetadata(type=ParameterType.OBJECT),
            },
        ),
    )
    def check_choreography_job_status(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """Read-only poll of a rotate_session/restart_session/
        generate_curation_report job's ledger row + terminal payload, if
        any — a generic ``AsyncJobManager`` job-row reader, not scoped to
        any one action name."""
        raw = params.get("parameters", params)
        job_manager = self._try_acquire_async_job_manager()
        if job_manager is None:
            return _failure_result(
                code="async_job_manager_unavailable",
                message="AsyncJobManager is not available on this solet.",
            )
        try:
            result = lifecycle_check_choreography_job_status(
                job_manager,
                str(raw.get("job_id", "")),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="generate_curation_report",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "head_lines": ParameterMetadata(
                description="The current curated head's lines, already split by the "
                "caller (index_render.split_head's output) — this plugin cannot import "
                "that local-CLI-only module itself.",
                required=True,
                type=ParameterType.LIST,
            ),
            "bottom_n": ParameterMetadata(
                description="How many lowest-activation demotion candidates to return.",
                required=False,
                type=ParameterType.INTEGER,
            ),
            "byte_budget": ParameterMetadata(
                description="The head's byte budget (index_render.DEFAULT_BYTE_BUDGET, "
                "17000 as of M2.2 — kept in sync by convention until M2.3's index-"
                "manifest record removes the need for this duplication).",
                required=False,
                type=ParameterType.INTEGER,
            ),
            "line_budget": ParameterMetadata(
                description="The head's line budget (index_render.DEFAULT_LINE_BUDGET, "
                "132 as of M2.2 — same sync caveat as byte_budget).",
                required=False,
                type=ParameterType.INTEGER,
            ),
        },
        output_type="object",
        output_description=(
            "maintenance-verbs M2.2, D0.3-ratified deferred-completion shape — dispatches "
            "an activation-ranked curation-report job and returns immediately; poll "
            "check_choreography_job_status for the outcome."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="generate_curation_report dispatch outcome",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING),
                "status": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def generate_curation_report(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """M2.2 ``generate_curation_report`` — ms-scale dispatch only (D0.3
        mechanic 1, same shape as rotate/restart_session); the actual
        memory_service query + ranking runs in the plugin's own single
        dedicated background worker (never inline in this handler)."""
        raw = params.get("parameters", params)
        job_manager = self._try_acquire_async_job_manager()
        if job_manager is None:
            return _failure_result(
                code="async_job_manager_unavailable",
                message="AsyncJobManager is not available on this solet.",
            )
        head_lines_raw = raw.get("head_lines")
        head_lines = (
            tuple(str(x) for x in head_lines_raw) if isinstance(head_lines_raw, list) else ()
        )
        req = GenerateCurationReportDispatchRequest(
            head_lines=head_lines,
            bottom_n=int(raw.get("bottom_n") or 10),
            byte_budget=int(raw.get("byte_budget") or 17_000),
            line_budget=int(raw.get("line_budget") or 132),
        )
        try:
            result = dispatch_generate_curation_report(job_manager, req, state)
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="reinforce_by_slug",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "slug": ParameterMetadata(
                description="The memory fact's slug — its local file name minus '.md' "
                "(e.g. 'feedback_operator_delegates_routine_operations_end_to_end'). "
                "Resolved to the canonical memory_id server-side via the fact's own "
                "slot tag; never pass a memory_id here.",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "maintenance-verbs M2.2 — resolve a memory fact's slug to its canonical "
            "memory_id via the slot tag convention, then reinforce it (ACT-R activation "
            "boost). Use when a fact is actually applied — cited in an incident, invoked "
            "in a review — never on a schedule or automatically."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="reinforce_by_slug outcome",
            properties={
                "memory_id": ParameterMetadata(type=ParameterType.STRING),
                "slug": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def reinforce_by_slug(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """M2.2 cite->reinforce wiring: resolves ``slug`` to a ``memory_id``
        via ``get_memories_by_tag`` on the fact's own slot tag (never a local
        export file — this plugin calls the injected ``memory_service``
        directly, the same dependency-injection seam ``store_interaction``
        already uses elsewhere in this file), then reinforces it. This verb
        IS the wiring the charter asks for; WHEN to call it (citation
        detection) stays a human/agent judgment call this slice, not an
        automated hook — disclosed, not silently assumed."""
        raw = params.get("parameters", params)
        slug = str(raw.get("slug", "")).strip()
        if not slug:
            return _failure_result(
                code="missing_argument",
                message="reinforce_by_slug requires a non-empty slug.",
            )
        if self._memory_service is None:
            return _failure_result(
                code="memory_service_unavailable",
                message="memory_service is not bound on this solet.",
            )
        solet_name = _resolve_solet_name_for_memory_tags()
        if not solet_name:
            return _failure_result(
                code="solet_name_unset",
                message="Could not resolve a solet name to resolve a slug's slot tag "
                "-- SOLET_NAME is unset, root_manifest.yaml is unreadable or still "
                "carries its unwritten placeholder, and CLAUDE_PROJECT_DIR is unset (the "
                "final fallback needs it too).",
            )
        tag = slug_to_slot_tag(solet_name, slug)
        lookup = self._memory_service.get_memories_by_tag(tag=tag)
        matches = lookup.get("memories") if isinstance(lookup, dict) else None
        try:
            memory_id = resolve_memory_id_by_slug(
                matches if isinstance(matches, list) else [], slug
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        self._memory_service.reinforce(memory_id=memory_id)
        return _success_result(data={"memory_id": memory_id, "slug": slug})

    @platform_process(
        name="report_context_status",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The reporting session's own id (ledger id for a worker; "
                "its own AGENT_INSTANCE_ID for the seat).",
                required=True,
                type=ParameterType.STRING,
            ),
            "runtime_session_id": ParameterMetadata(
                description="The reporting runtime's native stable session/thread identity.",
                required=True,
                type=ParameterType.STRING,
            ),
            "provider": ParameterMetadata(
                description="The model provider at measurement time (for example openai).",
                required=True,
                type=ParameterType.STRING,
            ),
            "runtime": ParameterMetadata(
                description="The reporting agent runtime (for example codex or claude_code).",
                required=True,
                type=ParameterType.STRING,
            ),
            "model": ParameterMetadata(
                description="Transcript message.model at measurement time.",
                required=True,
                type=ParameterType.STRING,
            ),
            "effort": ParameterMetadata(
                description="The runtime's selected reasoning-effort value.",
                required=True,
                type=ParameterType.STRING,
            ),
            "current_tokens": ParameterMetadata(
                description=(
                    "Model-visible input tokens for the most recent assistant call, "
                    "using the reporting runtime's native counter. Cache counters are "
                    "reported separately and must not be added twice."
                ),
                required=True,
                type=ParameterType.INTEGER,
            ),
            "ceiling": ParameterMetadata(
                description=(
                    "Positive effective context capacity reported by the active runtime. "
                    "This is distinct from a provider API maximum."
                ),
                required=True,
                type=ParameterType.INTEGER,
            ),
            "measured_at": ParameterMetadata(
                description="When the reporting hook computed this snapshot (ISO timestamp).",
                required=True,
                type=ParameterType.STRING,
            ),
            # ★ GAU-14 (D3): OPTIONAL, AND THAT IS NOT LAZINESS. This verb is
            # called by the INSTALLED plugin-cache copy of the reporting hook,
            # which updates only on a reinstall -- so a REQUIRED parameter here
            # would fail every un-upgraded copy's call outright, turning a
            # provenance improvement into an outage on exactly the copies that
            # are hardest to see. `required=False` is also what the column's
            # tri-state wants: omission records NOT REPORTED, which is a
            # positive finding about that reporter, never a synonym for
            # "same as measured_at".
            "reading_at": ParameterMetadata(
                description=(
                    "When the READING was produced — the source transcript "
                    "line's own timestamp, NOT the reporter's clock. "
                    "measured_at MINUS reading_at is the observation lag. Omit "
                    "if the transcript line carried no timestamp; omission "
                    "records NOT REPORTED, never a fabricated zero lag."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "cache_read_tokens": ParameterMetadata(
                description=(
                    "cache_read_input_tokens on THE MOST RECENT ASSISTANT CALL — "
                    "the same call current_tokens is summed from. 0 means that "
                    "call read nothing from cache and paid full price. Omit if "
                    "not measured; omission records NOT REPORTED, never 'warm'."
                ),
                required=False,
                type=ParameterType.INTEGER,
            ),
            "cache_write_tokens": ParameterMetadata(
                description=(
                    "Cache-write tokens on the same measured call. Omit when "
                    "the runtime does not expose the counter; never derive it "
                    "from total input."
                ),
                required=False,
                type=ParameterType.INTEGER,
            ),
            "cache_cold": ParameterMetadata(
                description=(
                    "True when the reporter classified the prompt cache as "
                    "expired. The classification EXCLUDES the first call after "
                    "a /clear — that call is cold by construction because the "
                    "clear rewrites the prefix. Omit if not measured."
                ),
                required=False,
                type=ParameterType.BOOLEAN,
            ),
            "cache_overage_signature": ParameterMetadata(
                description=(
                    "True when REPEATED cold calls across sub-TTL gaps show the "
                    "cache is not surviving its nominal window. A single cold "
                    "call after a long idle gap is ordinary expiry and must NOT "
                    "set this. Omit if not measured."
                ),
                required=False,
                type=ParameterType.BOOLEAN,
            ),
            "reporter_surface": ParameterMetadata(
                description=(
                    "Which registered COPY of the reporting hook is sending "
                    "this, as a path class: 'checkout' (under the repo's own "
                    ".claude/hooks, subdirectories included), 'plugin_cache' "
                    "(an installed plugin-cache copy), 'vendored' (the in-repo "
                    "source an install copies FROM), 'release' (that source "
                    "inside a deployed release tree), or 'unknown' when the "
                    "hook cannot classify its own location. Any other value is "
                    "rejected before any write. Omit only if the reporter "
                    "predates attribution."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "reporter_generation": ParameterMetadata(
                description=(
                    "The reporting hook's own content-generation constant, "
                    "bumped in lockstep with its reporting content. NOT a git "
                    "sha — a hook cannot know the commit it was copied from. "
                    "Lets a reader tell a current copy from an older one that "
                    "is still being served. Omit if not carried."
                ),
                required=False,
                type=ParameterType.INTEGER,
            ),
            "agent_session_id": ParameterMetadata(
                description=(
                    "The reporting session's STABLE $AGENT_SESSION_ID — the "
                    "ROUTING JOIN. This row keys on the reporter's LEDGER "
                    "instance id, but a watcher-held worker's live bridge "
                    "binding keys on its WATCH id, so without this a consumer "
                    "holding the row cannot reach the session that wrote it. "
                    "Pass the value VERBATIM; never rebuild it from "
                    "agent_instance_id, whose current 'ases-' + ledger-id "
                    "relationship is one launcher's convention and not a join. "
                    "Omit if the reporter has no session id — omission records "
                    "NOT REPORTED, never 'this session has no bridge'."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "maintenance-verbs M1 — overwrite the caller's own latest "
            "context-status snapshot (shape (a) cache write), including the "
            "optional cache-state fields the economic rotation policy's cold "
            "branch reads and the reporter-attribution fields that say which "
            "hook copy produced the row."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="report_context_status outcome",
            properties={
                "status": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def report_context_status(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """maintenance-verbs M1 — plain state upsert of a measurement the
        CALLER already took client-side; this handler does no file/subprocess
        I/O of its own (born-async-clean, no D0.3 dependency — sanctioned
        ms-scale state work)."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        try:
            result = lifecycle_report_context_status(
                state_service,
                agent_instance_id=str(raw.get("agent_instance_id", "")),
                runtime_session_id=str(raw.get("runtime_session_id", "")),
                provider=str(raw.get("provider", "")),
                runtime=str(raw.get("runtime", "")),
                model=str(raw.get("model", "")),
                effort=str(raw.get("effort", "")),
                current_tokens=int(raw.get("current_tokens", 0) or 0),
                ceiling=int(raw.get("ceiling", 0) or 0),
                cache_read_tokens=_opt_int(raw.get("cache_read_tokens")),
                cache_write_tokens=_opt_int(raw.get("cache_write_tokens")),
                cache_cold=_opt_bool(raw.get("cache_cold")),
                cache_overage_signature=_opt_bool(raw.get("cache_overage_signature")),
                reporter_surface=_opt_str(raw.get("reporter_surface")),
                reporter_generation=_opt_int(raw.get("reporter_generation")),
                agent_session_id=_opt_str(raw.get("agent_session_id")),
                measured_at=str(raw.get("measured_at", "")),
                reading_at=_opt_str(raw.get("reading_at")),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="session_context_status",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The session to read the cached context-status snapshot for.",
                required=True,
                type=ParameterType.STRING,
            ),
            "calculation_request": ParameterMetadata(
                description=(
                    "Optional explicit N-aware calculation inputs: required_actions, "
                    "measured action calibrations, cache multiplier, and versioned "
                    "capability/economics profile identities. Omit for gauge-only readback."
                ),
                required=False,
                type=ParameterType.OBJECT,
            ),
        },
        output_type="object",
        output_description=(
            "maintenance-verbs M1 — the cached context-window occupancy for "
            "one session; resolved=False (never a raised error) when no "
            "report has landed for it yet."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="session_context_status outcome",
            properties={
                "resolved": ParameterMetadata(type=ParameterType.BOOLEAN),
                "resolution_error": ParameterMetadata(type=ParameterType.STRING),
                "agent_instance_id": ParameterMetadata(
                    description=(
                        "The LEDGER id the returned row is actually keyed on -- not "
                        "necessarily the id that was queried (GAU-07)."
                    ),
                    type=ParameterType.STRING,
                ),
                # Watch-id join (GAU-07, 2026-08-18): a watcher-held session is
                # published by peer_list under its WATCH id while its gauge row
                # lives under the LEDGER id. The verb now accepts either.
                "queried_agent_instance_id": ParameterMetadata(
                    description="The id the caller passed, whichever of the two it was.",
                    type=ParameterType.STRING,
                ),
                "id_resolution": ParameterMetadata(
                    description=(
                        "'direct' (the queried id keyed the row), "
                        "'resolved_via_binding' (reached through the peer binding's "
                        "stable agent_session_id), or 'unresolved'."
                    ),
                    type=ParameterType.STRING,
                ),
                "runtime_session_id": ParameterMetadata(type=ParameterType.STRING),
                "provider": ParameterMetadata(type=ParameterType.STRING),
                "runtime": ParameterMetadata(type=ParameterType.STRING),
                "model": ParameterMetadata(type=ParameterType.STRING),
                "effort": ParameterMetadata(type=ParameterType.STRING),
                "current_tokens": ParameterMetadata(type=ParameterType.INTEGER),
                "ceiling": ParameterMetadata(type=ParameterType.INTEGER),
                "fraction": ParameterMetadata(type=ParameterType.FLOAT),
                "per_prompt_carriage_estimate_tokens": ParameterMetadata(
                    type=ParameterType.INTEGER,
                ),
                "rotation_due": ParameterMetadata(type=ParameterType.BOOLEAN),
                "measured_at": ParameterMetadata(type=ParameterType.STRING),
                # Cache state + derived band (2026-08-16). Declared here so the
                # schema describes what the verb ACTUALLY returns; the fields
                # shipped in the return dict ahead of this declaration.
                "cache_read_tokens": ParameterMetadata(type=ParameterType.INTEGER),
                "cache_write_tokens": ParameterMetadata(type=ParameterType.INTEGER),
                "cache_cold": ParameterMetadata(type=ParameterType.BOOLEAN),
                "cache_overage_signature": ParameterMetadata(type=ParameterType.BOOLEAN),
                "rotation_band": ParameterMetadata(type=ParameterType.STRING),
                "rotation_guidance": ParameterMetadata(type=ParameterType.STRING),
                # Reporter attribution (2026-08-16): which hook copy wrote the row.
                "reporter_surface": ParameterMetadata(type=ParameterType.STRING),
                "reporter_generation": ParameterMetadata(type=ParameterType.INTEGER),
                # The routing join (2026-08-18): null means the reporter
                # predates the column, NOT that the session has no bridge.
                "agent_session_id": ParameterMetadata(type=ParameterType.STRING),
                "calculated_verdict": ParameterMetadata(type=ParameterType.OBJECT),
            },
        ),
    )
    def session_context_status(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """maintenance-verbs M1 — trivial state read of the cached snapshot
        `report_context_status` writes; this handler never reads a
        transcript or resolves a path itself."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        try:
            result = lifecycle_session_context_status(
                state_service,
                agent_instance_id=str(raw.get("agent_instance_id", "")),
                calculation_request=(
                    dict(raw["calculation_request"])
                    if isinstance(raw.get("calculation_request"), dict)
                    else None
                ),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="session_context_status_history",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description=(
                    "The session whose gauge SERIES to read. Accepts either id "
                    "the session is known by (ledger or watch), same GAU-07 "
                    "join as session_context_status."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "limit": ParameterMetadata(
                description=(
                    "How many history rows to return, newest first. Capped at "
                    "the store's retention (64); the reply says whether the "
                    "page was truncated rather than leaving it implied."
                ),
                required=False,
                type=ParameterType.INTEGER,
            ),
        },
        output_type="object",
        output_description=(
            "GAU-15 — one session's bounded gauge series, newest first, with "
            "the series classified as healthy / stopped / idle / "
            "never_started / undetermined against the lifecycle's own last "
            "report_alive. resolved=False (never a raised error) when no "
            "series is on file."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="session_context_status_history outcome",
            properties={
                "resolved": ParameterMetadata(type=ParameterType.BOOLEAN),
                "resolution_error": ParameterMetadata(type=ParameterType.STRING),
                "agent_instance_id": ParameterMetadata(
                    description="The LEDGER id the returned series is keyed on.",
                    type=ParameterType.STRING,
                ),
                "queried_agent_instance_id": ParameterMetadata(
                    description="The id the caller passed, whichever of the two it was.",
                    type=ParameterType.STRING,
                ),
                "id_resolution": ParameterMetadata(
                    description="'direct' | 'resolved_via_binding' | 'unresolved'.",
                    type=ParameterType.STRING,
                ),
                "series_state": ParameterMetadata(
                    description=(
                        "healthy | stopped | idle | never_started | "
                        "undetermined. 'stopped' is the GAU-01 class (the "
                        "session works and its gauge is dark); 'idle' is a "
                        "normal fleet state and NOT an incident."
                    ),
                    type=ParameterType.STRING,
                ),
                "series_state_reason": ParameterMetadata(
                    description="The evidence the classification used, in numbers.",
                    type=ParameterType.STRING,
                ),
                "last_report_alive": ParameterMetadata(
                    description=(
                        "When report_alive last landed for this session, "
                        "derived from report_by minus report_by_seconds. Null "
                        "when the row carries no window -- absence of the "
                        "WINDOW is not evidence of absence of a TICK."
                    ),
                    type=ParameterType.STRING,
                ),
                "entries": ParameterMetadata(
                    description=(
                        "The history rows, newest first: recorded_at, "
                        "measured_at, reading_at, current_tokens, ceiling, "
                        "model, claude_session_id, agent_session_id, the three "
                        "cache fields, reporter_surface, reporter_generation."
                    ),
                    type=ParameterType.LIST,
                ),
                "returned": ParameterMetadata(type=ParameterType.INTEGER),
                "truncated": ParameterMetadata(
                    description="Whether more rows exist beyond this page.",
                    type=ParameterType.BOOLEAN,
                ),
                "retention": ParameterMetadata(
                    description="Rows the store keeps per session (the hard bound).",
                    type=ParameterType.INTEGER,
                ),
                "rotation_boundaries": ParameterMetadata(
                    description=(
                        "How many /clear boundaries this page spans, counted "
                        "as changes of claude_session_id under one instance id."
                    ),
                    type=ParameterType.INTEGER,
                ),
            },
        ),
    )
    def session_context_status_history(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """GAU-15 — read the bounded gauge series behind the cached snapshot.

        The cache answers "what is it now"; this answers "did it stop, and if
        so when" — the question a single upsert-only row structurally cannot,
        and the reason the original 85-minute freeze could never be analysed.
        """
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        limit = raw.get("limit")
        try:
            result = gauge_session_context_status_history(
                state_service,
                agent_instance_id=str(raw.get("agent_instance_id", "")),
                limit=int(limit)
                if isinstance(limit, (int, str)) and str(limit).strip()
                else GAUGE_HISTORY_RETENTION,
                peer_registry=self._peer_registry,
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="report_inbox_consumption",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The reporting session's own id (ledger id for a worker).",
                required=True,
                type=ParameterType.STRING,
            ),
            "runtime": ParameterMetadata(
                description="The reporting agent runtime (for example codex).",
                required=True,
                type=ParameterType.STRING,
            ),
            "checked_at": ParameterMetadata(
                description=(
                    "When the consumer hook last actually performed its "
                    "existence check (ISO timestamp), REGARDLESS of what it "
                    "found. Stamped on every call — this is the honesty "
                    "mechanism itself."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "pending_found_at": ParameterMetadata(
                description=(
                    "ISO timestamp of the last time this check found a "
                    "pending delivery and forced a Stop-hook block/continue. "
                    "Omit when this check found nothing."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "pending_reason": ParameterMetadata(
                description=(
                    "The fixed nudge text last delivered via decision:block. "
                    "Requires pending_found_at to be present alongside it."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "reporter_surface": ParameterMetadata(
                description=(
                    "checkout | plugin_cache | vendored | release | unknown "
                    "-- which copy of the hook wrote this row, same closed "
                    "vocabulary as report_context_status's field of the same "
                    "name. Omit only if the reporter predates attribution."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "agent_session_id": ParameterMetadata(
                description=(
                    "The reporting session's STABLE $AGENT_SESSION_ID, "
                    "captured for a future routing join. Omit if the "
                    "reporter has no session id."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "CDX-06 part C — overwrite the caller's own latest inbox-consumption-check snapshot."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="report_inbox_consumption outcome",
            properties={
                "status": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def report_inbox_consumption(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """CDX-06 part C — plain state upsert of a check the CALLER already
        performed; this handler does no subprocess I/O of its own."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        try:
            result = lifecycle_report_inbox_consumption(
                state_service,
                agent_instance_id=str(raw.get("agent_instance_id", "")),
                runtime=str(raw.get("runtime", "")),
                checked_at=str(raw.get("checked_at", "")),
                pending_found_at=_opt_str(raw.get("pending_found_at")),
                pending_reason=_opt_str(raw.get("pending_reason")),
                reporter_surface=_opt_str(raw.get("reporter_surface")),
                agent_session_id=_opt_str(raw.get("agent_session_id")),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="session_inbox_consumption_status",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The session to read the cached inbox-consumption snapshot for.",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "CDX-06 part C — the cached inbox-consumption-check state for "
            "one session; resolved=False (never a raised error, never a "
            "defaulted True) when no check has landed for it yet."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="session_inbox_consumption_status outcome",
            properties={
                "resolved": ParameterMetadata(type=ParameterType.BOOLEAN),
                "resolution_error": ParameterMetadata(type=ParameterType.STRING),
                "agent_instance_id": ParameterMetadata(type=ParameterType.STRING),
                "runtime": ParameterMetadata(type=ParameterType.STRING),
                "checked_at": ParameterMetadata(type=ParameterType.STRING),
                "pending_found_at": ParameterMetadata(type=ParameterType.STRING),
                "pending_reason": ParameterMetadata(type=ParameterType.STRING),
                "reporter_surface": ParameterMetadata(type=ParameterType.STRING),
                "agent_session_id": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def session_inbox_consumption_status(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """CDX-06 part C — trivial state read of the cached snapshot
        `report_inbox_consumption` writes; never estimates a fallback when
        `resolved=False`."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        try:
            result = lifecycle_session_inbox_consumption_status(
                state_service,
                agent_instance_id=str(raw.get("agent_instance_id", "")),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="gauge_notice_records",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "notice_type": ParameterMetadata(
                description=(
                    "Narrow to one detector: 'gauge_stale_notice' (a live "
                    "session's gauge ARRESTED) or 'gauge_coverage_notice' (a "
                    "live session produced NO gauge row at all). Omit for "
                    "both."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "agent_instance_id": ParameterMetadata(
                description=(
                    "Narrow to the SUBJECT session the notices are about (not "
                    "the steward they were sent to). Accepts either id the "
                    "session is known by, same GAU-07 join as "
                    "session_context_status. Omit for all subjects."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "since": ParameterMetadata(
                description=(
                    "ISO-8601 lower bound on emitted_at, inclusive. Omit for everything retained."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "limit": ParameterMetadata(
                description=(
                    "How many records to return, newest first. Capped at the "
                    "store's read ceiling (64); the reply says whether the "
                    "page was truncated rather than leaving it implied."
                ),
                required=False,
                type=ParameterType.INTEGER,
            ),
        },
        output_type="object",
        output_description=(
            "GAU-21 — the durable record of gauge notices that FIRED, newest "
            "first, filterable by type, subject and time window. Each record "
            "carries the delivery outcome (appended / no_steward_binding / "
            "append_failed), the threshold in force and the value measured "
            "against it, and the release whose detector fired it. Reading does "
            "NOT consume: the bridge event queue this replaces hands its only "
            "reader the events and then drops them."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="gauge_notice_records outcome",
            properties={
                "entries": ParameterMetadata(
                    description=(
                        "The records, newest first: notice_type, "
                        "agent_instance_id, emitted_at, steward_instance_id, "
                        "delivery_outcome, release_id, threshold_s, "
                        "observed_s, last_report_alive_at, gauge_measured_at."
                    ),
                    type=ParameterType.LIST,
                ),
                "returned": ParameterMetadata(type=ParameterType.INTEGER),
                "truncated": ParameterMetadata(
                    description="Whether more records exist beyond this page.",
                    type=ParameterType.BOOLEAN,
                ),
                "queried_agent_instance_id": ParameterMetadata(
                    description=(
                        "The subject id the caller passed, whichever of the "
                        "two it was. Null when unfiltered."
                    ),
                    type=ParameterType.STRING,
                ),
                "agent_instance_id": ParameterMetadata(
                    description=(
                        "The id the records are keyed on after the GAU-07 "
                        "join. Null when unfiltered."
                    ),
                    type=ParameterType.STRING,
                ),
                "id_resolution": ParameterMetadata(
                    description=(
                        "'direct' | 'resolved_via_binding' | 'unresolved'. "
                        "Null when no subject filter was given."
                    ),
                    type=ParameterType.STRING,
                ),
                "notice_type": ParameterMetadata(
                    description="The type filter applied, or null for both.",
                    type=ParameterType.STRING,
                ),
                "since": ParameterMetadata(
                    description="The lower bound applied, or null.",
                    type=ParameterType.STRING,
                ),
                "retention": ParameterMetadata(
                    description=("Records the store keeps per subject per type (the hard bound)."),
                    type=ParameterType.INTEGER,
                ),
                "delivery_outcomes": ParameterMetadata(
                    description=(
                        "The delivery_outcome domain, published so a reader "
                        "need not guess which values are possible."
                    ),
                    type=ParameterType.LIST,
                ),
                "notice_types": ParameterMetadata(
                    description="The notice_type domain this verb reads.",
                    type=ParameterType.LIST,
                ),
            },
        ),
    )
    def gauge_notice_records(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """GAU-21 — read which gauge notices actually fired, durably.

        The sweep's notices live only as in-memory bridge events, so until this
        record existed "the detector never alarmed" and "it alarmed and reached
        nobody" were the same silence — and a verifier polling that queue would
        have consumed the steward's own notice to find out.
        """
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        limit = raw.get("limit")
        notice_type = raw.get("notice_type")
        agent_instance_id = raw.get("agent_instance_id")
        since = raw.get("since")
        try:
            result = gauge_read_notice_records(
                state_service,
                notice_type=str(notice_type) if notice_type else None,
                agent_instance_id=str(agent_instance_id) if agent_instance_id else None,
                since=str(since) if since else None,
                limit=int(limit)
                if isinstance(limit, (int, str)) and str(limit).strip()
                else GAUGE_NOTICE_READ_ROWS,
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="register_gauge_canary",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The synthetic identity to mark as a gauge canary.",
                required=True,
                type=ParameterType.STRING,
            ),
            "purpose": ParameterMetadata(
                description=(
                    "Why this canary exists, in words — for whoever finds a "
                    "synthetic session in a listing and needs to know it is "
                    "deliberate."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "registered_by": ParameterMetadata(
                description=(
                    "Who is registering it. A synthetic identity in the fleet "
                    "is an act someone took, never an ambient fact."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "GAU-15 item 4 — mark one identity as a gauge canary at the STORE "
            "plane, in its own table. Deliberately never a column on the gauge "
            "row: the staleness detector reads that row, and a detector that "
            "can tell it is under test has stopped being the thing under test. "
            "Operational consumers filter canaries out by joining this "
            "registry; the detector never joins it."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="register_gauge_canary outcome",
            properties={
                "agent_instance_id": ParameterMetadata(type=ParameterType.STRING),
                "purpose": ParameterMetadata(type=ParameterType.STRING),
                "registered_at": ParameterMetadata(type=ParameterType.STRING),
                "registered_by": ParameterMetadata(type=ParameterType.STRING),
                "retired_at": ParameterMetadata(
                    description="Null while active; set when stood down.",
                    type=ParameterType.STRING,
                ),
            },
        ),
    )
    def register_gauge_canary(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """GAU-15 item 4 — mark an identity as a canary, at the store plane."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        try:
            result = register_canary(
                state_service,
                agent_instance_id=str(raw.get("agent_instance_id", "")),
                purpose=str(raw.get("purpose", "")),
                registered_by=str(raw.get("registered_by", "")),
            )
        except CanaryError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="arrest_gauge_canary",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description=(
                    "The CANARY to arrest. A tamper may only ever target a "
                    "registered canary — never a real session."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "directed_by": ParameterMetadata(
                description=(
                    "Who is ordering the tamper. An unattributable tamper is "
                    "the failure this verb exists to make impossible."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "arrest_from": ParameterMetadata(
                description="Start of the arrest window, inclusive, ISO-8601.",
                required=True,
                type=ParameterType.STRING,
            ),
            "arrest_until": ParameterMetadata(
                description=(
                    "End of the arrest window, exclusive, ISO-8601. Always "
                    "bounded: an arrest that never ends makes its alarms "
                    "attributable forever, which is the same as not being "
                    "attributable."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "expected_notice_type": ParameterMetadata(
                description=(
                    "Which alarm this arrest expects to provoke — "
                    "gauge_stale_notice or gauge_coverage_notice. Recorded "
                    "BEFORE the outcome is known, so the verifier cannot grade "
                    "its own expectations after the fact."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "reason": ParameterMetadata(
                description="Why this arrest was ordered, in words.",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "GAU-15 item 4 — order an AUDITED, bounded arrest of one canary's "
            "gauge, so the staleness detector can be exercised end to end. The "
            "arrest withholds only the canary's gauge write; its lifecycle "
            "clock keeps advancing, which is what reproduces the real freeze "
            "signature rather than an idle session. Every arrest is logged "
            "with who ordered it, so any alarm is mechanically attributable to "
            "a scheduled tamper or to a real fault. There is NO ambient "
            "test-mode environment variable and never will be: an env var "
            "leaves no audit trail and cannot be scoped to a window."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="arrest_gauge_canary outcome",
            properties={
                "agent_instance_id": ParameterMetadata(type=ParameterType.STRING),
                "directed_by": ParameterMetadata(type=ParameterType.STRING),
                "arrest_from": ParameterMetadata(type=ParameterType.STRING),
                "arrest_until": ParameterMetadata(type=ParameterType.STRING),
                "expected_notice_type": ParameterMetadata(type=ParameterType.STRING),
                "reason": ParameterMetadata(type=ParameterType.STRING),
                "recorded_at": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def arrest_gauge_canary(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """GAU-15 item 4 — the audited tamper. Constraint (d) in one verb."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        try:
            result = canary_direct_arrest(
                state_service,
                agent_instance_id=str(raw.get("agent_instance_id", "")),
                directed_by=str(raw.get("directed_by", "")),
                arrest_from=str(raw.get("arrest_from", "")),
                arrest_until=str(raw.get("arrest_until", "")),
                expected_notice_type=str(raw.get("expected_notice_type", "")),
                reason=str(raw.get("reason", "")),
            )
        except CanaryError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="register_synthetic_session",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description=(
                    "The ALREADY-REGISTERED canary to mint a lifecycle row "
                    "for. Any other identity is refused: this verb is the "
                    "inverse of the not_a_canary guard, and without that "
                    "inversion it would be a way to fabricate a session that "
                    "looks alive to every fleet surface."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "lane_id": ParameterMetadata(
                description=(
                    "The lane label the row carries. Must be ordinary-looking: "
                    "the staleness alarm quotes it verbatim, so a lane named "
                    "for the canary announces it in every alarm."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "spawned_by_instance_id": ParameterMetadata(
                description=(
                    "The steward to notify. REQUIRED — the staleness leg skips "
                    "every row without a spawner, so a canary registered "
                    "without one is invisible to the detector it exists to "
                    "exercise."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "directed_by": ParameterMetadata(
                description=("Who ordered this synthetic identity into the fleet ledger."),
                required=True,
                type=ParameterType.STRING,
            ),
            "report_by_seconds": ParameterMetadata(
                description=(
                    "The lifecycle reporting window, in seconds. Must be "
                    "positive: with no window the derived last report_alive is "
                    "None, which means NO EVIDENCE rather than 'not "
                    "advancing', and the staleness leg skips the row."
                ),
                required=True,
                type=ParameterType.INTEGER,
            ),
            "brief_ref": ParameterMetadata(
                description="Optional brief reference recorded on the row.",
                required=False,
                type=ParameterType.STRING,
            ),
            "budget_line": ParameterMetadata(
                description="Optional budget line recorded on the row.",
                required=False,
                type=ParameterType.STRING,
            ),
            "model": ParameterMetadata(
                description="Optional model label recorded on the row.",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "GAU-15 item 4 follow-up — give a registered gauge canary the "
            "managed_session row the staleness detector reads, WITHOUT "
            "dispatching any process. Closes a measured exercisability gap: "
            "the detector inspects live managed_session rows, the only writer "
            "of those rows was spawn_session, and spawn_session always "
            "launches a real process — so a canary could never acquire the row "
            "that makes it visible, and the pipeline was unfalsifiable while "
            "51 checks passed. The row declares host 'synthetic', for which no "
            "driver is registered, so every verb that would touch a process "
            "refuses it loudly instead of aiming at a pane that does not "
            "exist. It is minted in 'spawning', exactly where spawn_session "
            "leaves it; the first canary tick promotes it to 'live' through "
            "the real report_alive, so no second promotion rule exists."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="register_synthetic_session outcome",
            properties={
                "agent_instance_id": ParameterMetadata(type=ParameterType.STRING),
                "lane_id": ParameterMetadata(type=ParameterType.STRING),
                "host": ParameterMetadata(
                    description=(
                        "Always 'synthetic' — a degenerate, no-op host driver "
                        "(GAU-24): it refuses spawn/terminate loudly rather "
                        "than touching anything, so retire_gauge_canary can "
                        "reach this row without a real process behind it."
                    ),
                    type=ParameterType.STRING,
                ),
                "lifecycle_state": ParameterMetadata(
                    description=(
                        "'spawning' — the first canary tick's report_alive promotes it to live."
                    ),
                    type=ParameterType.STRING,
                ),
                "spawned_by_instance_id": ParameterMetadata(type=ParameterType.STRING),
                "report_by_seconds": ParameterMetadata(type=ParameterType.INTEGER),
                "report_by": ParameterMetadata(type=ParameterType.STRING),
                "promoted_by": ParameterMetadata(
                    description="Which path promotes this row to live, stated.",
                    type=ParameterType.STRING,
                ),
            },
        ),
    )
    def register_synthetic_session(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """GAU-15 item 4 follow-up — a canary's lifecycle row, no process."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        window = raw.get("report_by_seconds", 0)
        try:
            result = canary_register_synthetic_session(
                state_service,
                agent_instance_id=str(raw.get("agent_instance_id", "")),
                lane_id=str(raw.get("lane_id", "")),
                spawned_by_instance_id=str(raw.get("spawned_by_instance_id", "")),
                directed_by=str(raw.get("directed_by", "")),
                report_by_seconds=int(window) if str(window).strip().lstrip("-").isdigit() else 0,
                brief_ref=str(raw.get("brief_ref", "")),
                budget_line=str(raw.get("budget_line", "")),
                model=str(raw.get("model", "")),
            )
        except CanaryError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="retire_gauge_canary",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description=(
                    "The registered canary to stand down. If it has a "
                    "managed_session row, that row is retired first; the "
                    "registry mark is stamped retired_at only after that "
                    "succeeds (or there was never a row to retire)."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "directed_by": ParameterMetadata(
                description="Who is retiring it — recorded on the ledger transition.",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "GAU-24 — stand a canary down all the way: the ledger row (via "
            "the no-op synthetic host driver, if a row exists) AND the "
            "registry mark, ledger first. Closes the leak where every canary "
            "exercise left one active canary mark plus one permanently-live "
            "managed_session row that nothing could retire."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="retire_gauge_canary outcome",
            properties={
                "agent_instance_id": ParameterMetadata(type=ParameterType.STRING),
                "session_row_existed": ParameterMetadata(
                    description="Whether a managed_session row existed to retire.",
                    type=ParameterType.BOOLEAN,
                ),
                "session_retire_result": ParameterMetadata(
                    description=("retire_session's own outcome when a row existed, else null."),
                    type=ParameterType.OBJECT,
                ),
                "canary_mark_retired": ParameterMetadata(type=ParameterType.BOOLEAN),
            },
        ),
    )
    def retire_gauge_canary(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """GAU-24 — retire a canary's ledger row (if any) and its registry
        mark, ledger first."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        try:
            result = canary_retire_gauge_canary(
                state_service,
                agent_instance_id=str(raw.get("agent_instance_id", "")),
                directed_by=str(raw.get("directed_by", "")),
            )
        except (CanaryError, VerbError) as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="verify_gauge_canary",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The canary to judge.",
                required=True,
                type=ParameterType.STRING,
            ),
            "detector_deployed": ParameterMetadata(
                description=(
                    "Whether the staleness detector is present in the RUNNING "
                    "release, established out-of-band by the release "
                    "capability probe. Omit when it is not established: the "
                    "verifier then ABSTAINS rather than guessing."
                ),
                required=False,
                type=ParameterType.BOOLEAN,
            ),
            "since": ParameterMetadata(
                description=("ISO-8601 lower bound on the windows and alarms examined."),
                required=False,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "GAU-15 item 4 — judge BOTH edges for one canary: did it alarm "
            "when tampered, and was it quiet when healthy. The two are read "
            "from independent evidence and are not each other's complement — a "
            "canary that never alarms passes the second and fails the first. "
            "Alarms are attributed by time AND type: an alarm inside a logged "
            "window matching its expected type is SCHEDULED, anything else is "
            "UNATTRIBUTED and means a real fault or a gap in the audit log. "
            "Judged against the durable notice record, never the drain-once "
            "bridge queue, which a verifier reading it would consume. Returns "
            "'abstained' when the detector's deployment is not established, "
            "because a verdict about an instrument that is not running is a "
            "false accusation whichever way it lands."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="verify_gauge_canary outcome",
            properties={
                "verdict": ParameterMetadata(
                    description=(
                        "pass | fail | abstained | no_evidence. A pass requires "
                        "POSITIVE evidence — at least one closed arrest window "
                        "that produced its expected alarm. A run that exercised "
                        "nothing returns no_evidence, never pass: an unexercised "
                        "canary and a detector that can no longer fire are "
                        "identical from the quiet edge. Quote the verdict WITH "
                        "closed_windows and windows_with_expected_alarm."
                    ),
                    type=ParameterType.STRING,
                ),
                "verdict_reason": ParameterMetadata(
                    description="The evidence the verdict used, in numbers.",
                    type=ParameterType.STRING,
                ),
                "agent_instance_id": ParameterMetadata(type=ParameterType.STRING),
                "detector_deployed": ParameterMetadata(type=ParameterType.BOOLEAN),
                "windows_examined": ParameterMetadata(type=ParameterType.INTEGER),
                "closed_windows": ParameterMetadata(
                    description=(
                        "Windows that have ENDED — the only ones whose alarm "
                        "can be judged. An open window has not had its chance."
                    ),
                    type=ParameterType.INTEGER,
                ),
                "windows_with_expected_alarm": ParameterMetadata(
                    type=ParameterType.INTEGER,
                ),
                "silent_windows": ParameterMetadata(
                    description="Closed windows that produced no matching alarm.",
                    type=ParameterType.INTEGER,
                ),
                "alarms_examined": ParameterMetadata(type=ParameterType.INTEGER),
                "scheduled_alarms": ParameterMetadata(type=ParameterType.INTEGER),
                "unattributed_alarms": ParameterMetadata(
                    description=(
                        "Alarms outside every logged window. NOT canary noise to dismiss."
                    ),
                    type=ParameterType.INTEGER,
                ),
                "truncated": ParameterMetadata(type=ParameterType.BOOLEAN),
                "since": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def verify_gauge_canary(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """GAU-15 item 4 — both edges, judged against the durable record."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        deployed = raw.get("detector_deployed")
        since = raw.get("since")
        try:
            result = canary_verify(
                state_service,
                agent_instance_id=str(raw.get("agent_instance_id", "")),
                detector_deployed=bool(deployed) if deployed is not None else None,
                since=str(since) if since else None,
            )
        except CanaryError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="seed_model_capability_catalog",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "staleness_window_hours": ParameterMetadata(
                description=(
                    "Staleness window stamped on each seeded cell. Default 72 "
                    "(the accepted cadence design); a consumer refuses a cell "
                    "measured longer ago than this."
                ),
                required=False,
                type=ParameterType.INTEGER,
            ),
        },
        output_type="object",
        output_description=(
            "iss_48ea8171 -- write the declarative model_capability_seed.v1.json "
            "into the catalog as pending_crosscheck cells. A seed never accepts "
            "a cell and never touches one a real refresh run already accepted, "
            "so it is safe to run at every start-up."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="seed_model_capability_catalog outcome",
            properties={
                "run_id": ParameterMetadata(type=ParameterType.STRING, description="The model_capability_refresh_run row (trigger=seed)."),
                "seed_version": ParameterMetadata(type=ParameterType.STRING),
                "cells_seeded": ParameterMetadata(type=ParameterType.INTEGER, description="Cells written or rewritten as pending_crosscheck."),
                "accepted_cells_preserved": ParameterMetadata(type=ParameterType.LIST, description="model@effort of cells left untouched because a real run accepted them."),
            },
        ),
    )
    def seed_model_capability_catalog(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """iss_48ea8171 -- seed the capability catalog, pending only."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(code="state_service_unavailable", message="state_service is not bound on this solet.")
        window = raw.get("staleness_window_hours")
        try:
            result = catalog_seed(
                state_service,
                staleness_window_hours=int(window) if window not in (None, "") else None,
            )
        except (CatalogError, ValueError) as exc:
            code = exc.code if isinstance(exc, CatalogError) else "parameter_invalid"
            return _failure_result(code=code, message=str(exc))
        return _success_result(data=result)

    @platform_process(
        name="read_model_capability_catalog",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "runtime": ParameterMetadata(description="Filter: claude_code | codex.", required=False, type=ParameterType.STRING),
            "model": ParameterMetadata(description="Filter: canonical model id.", required=False, type=ParameterType.STRING),
            "include_unaccepted": ParameterMetadata(
                description="Also return pending_crosscheck / crosscheck_conflict / withdrawn cells. Default false.",
                required=False,
                type=ParameterType.BOOLEAN,
            ),
        },
        output_type="object",
        output_description=(
            "iss_48ea8171 -- the model x effort capability score and real USD "
            "cost-per-task table as stored, each cell annotated is_fresh and "
            "servable. What the hand-typed ticket-header JSON becomes: a read."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="read_model_capability_catalog outcome",
            properties={
                "cells": ParameterMetadata(type=ParameterType.LIST, description="Catalog rows plus is_fresh and servable."),
                "total_cells": ParameterMetadata(type=ParameterType.INTEGER, description="Rows matching the filters before the acceptance filter."),
                "servable_cells": ParameterMetadata(type=ParameterType.INTEGER, description="Rows dispatch may use right now."),
                "as_of": ParameterMetadata(type=ParameterType.STRING, description="The freshness reference instant, ISO-8601."),
            },
        ),
    )
    def read_model_capability_catalog(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """iss_48ea8171 -- read the capability catalog with servability marks."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(code="state_service_unavailable", message="state_service is not bound on this solet.")
        runtime = raw.get("runtime")
        model = raw.get("model")
        try:
            result = catalog_read(
                state_service,
                runtime=str(runtime) if runtime else None,
                model=str(model) if model else None,
                include_unaccepted=bool(raw.get("include_unaccepted", False)),
                now=datetime.now(UTC),
            )
        except CatalogError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="select_dispatch_tier",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "required_score": ParameterMetadata(
                description="Minimum capability score the work needs, on the Artificial Analysis Intelligence Index scale (0-100).",
                required=True,
                type=ParameterType.FLOAT,
            ),
            "dispatch_kind": ParameterMetadata(
                description="Optional nonblank unit-kind provenance text; it never filters the cheapest capability-clearing candidates.",
                required=False,
                type=ParameterType.STRING,
            ),
            "scope_tags": ParameterMetadata(
                description="Caller-declared work scope tags (e.g. [\"state_schema\"]), preserved as provenance. The retired state_schema floor does not restrict candidates; numeric score and fresh catalog acceptance still govern selection. Same vocabulary as spawn_session/dispatch_managed_work.",
                required=False,
                type=ParameterType.LIST,
            ),
            "billing_objective": ParameterMetadata(
                description="metered_usd (real dollars) or relative (cost over the cheapest cell, the flat-rate quota proxy). Default: relative when usage_economics declares a current flat-rate plan, else metered_usd.",
                required=False,
                type=ParameterType.STRING,
            ),
            "score_margin": ParameterMetadata(description="Headroom added to required_score. Default 0.", required=False, type=ParameterType.FLOAT),
            "cost_tolerance": ParameterMetadata(description="Near-tie band within which the lower effort wins. Default 0.05.", required=False, type=ParameterType.FLOAT),
            "max_staleness_hours": ParameterMetadata(description="Tightens (never loosens) the stored staleness window.", required=False, type=ParameterType.INTEGER),
        },
        output_type="object",
        output_description=(
            "iss_48ea8171 -- the cheapest fresh, accepted, policy-allowed (model, "
            "effort) cell clearing the required score, with the evidence: the "
            "selected model's effort ladder (marginal points per cost unit), the "
            "Pareto frontier, and every dominated pair. Refuses rather than guesses "
            "when the catalog is stale or nothing clears."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="select_dispatch_tier outcome",
            properties={
                "selected": ParameterMetadata(type=ParameterType.OBJECT, description="runtime, model, effort, capability_score, cost_per_task_usd, relative_cost_multiplier, measured_at, acceptance."),
                "billing_objective": ParameterMetadata(type=ParameterType.STRING),
                "required_score": ParameterMetadata(type=ParameterType.FLOAT),
                "effective_required_score": ParameterMetadata(type=ParameterType.FLOAT, description="required_score + score_margin."),
                "frontier": ParameterMetadata(type=ParameterType.LIST, description="Feasible cells no other beats on both axes, cheapest first."),
                "dominated": ParameterMetadata(type=ParameterType.LIST, description="{weaker, stronger} pairs where another model's cell wins on both axes."),
                "ladder": ParameterMetadata(type=ParameterType.LIST, description="The selected model's effort steps with score_gain, cost_delta, points_per_cost_unit."),
                "excluded": ParameterMetadata(type=ParameterType.OBJECT, description="Counts: not_accepted, stale, capability_floor_disallowed, quota_exhausted, quota_unknown, unpriced, below_threshold."),
                "catalog_run_id": ParameterMetadata(type=ParameterType.STRING, description="Newest refresh run among the accepted cells; null when none."),
                "policy_version": ParameterMetadata(type=ParameterType.STRING, description="model_dispatch_policy version consulted; null when neither dispatch_kind nor scope_tags was given."),
                "capability_floors": ParameterMetadata(type=ParameterType.LIST, description="Capability floors applied from scope_tags (tag, source, detail); empty when none."),
                "unscored_cells": ParameterMetadata(type=ParameterType.INTEGER, description="Stored rows with no capability score, dropped before selection."),
                "selection_receipt": ParameterMetadata(type=ParameterType.OBJECT, description="Exact input/result receipt required by spawn_session to replay this selection."),
            },
        ),
    )
    def select_dispatch_tier(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """iss_48ea8171 -- cost-aware (model, effort) selection over the catalog."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(code="state_service_unavailable", message="state_service is not bound on this solet.")
        try:
            result = catalog_select_tier(state_service, dict(raw))
        except (CatalogError, TierSelectionError, DispatchPolicyError) as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="refresh_model_capability_catalog",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "trigger": ParameterMetadata(
                description="manual | cron | policy_change -- how this run was started.",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "iss_d136ae29 -- the real crosscheck refresh: fetches live readings "
            "from two independently-rendered Artificial Analysis pages per "
            "roster (runtime, model, effort) cell, reconciles them against an "
            "explicit tolerance, and writes the result. This is the only verb "
            "that can move a cell to accepted -- seed_model_capability_catalog "
            "never does. A source that fails to fetch is named in "
            "sources_failed, never silently dropped."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="refresh_model_capability_catalog outcome",
            properties={
                "run_id": ParameterMetadata(type=ParameterType.STRING, description="The model_capability_refresh_run row this call wrote."),
                "trigger": ParameterMetadata(type=ParameterType.STRING),
                "readings_gathered": ParameterMetadata(type=ParameterType.INTEGER, description="Total observation rows written across every source."),
                "sources_ok": ParameterMetadata(type=ParameterType.LIST, description="source_ids that fetched cleanly."),
                "sources_failed": ParameterMetadata(type=ParameterType.LIST, description="source_id: reason for every fetch that failed."),
                "cells_accepted": ParameterMetadata(type=ParameterType.INTEGER, description="Roster cells this run moved to (or kept) accepted."),
                "cells_conflicted": ParameterMetadata(type=ParameterType.INTEGER, description="Roster cells whose 2 sources disagreed beyond tolerance."),
                "cells_unchanged": ParameterMetadata(type=ParameterType.INTEGER, description="Roster cells this run had insufficient new evidence for."),
            },
        ),
    )
    def refresh_model_capability_catalog(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """iss_d136ae29 -- fetch, reconcile, and write the real capability catalog."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(code="state_service_unavailable", message="state_service is not bound on this solet.")
        trigger = raw.get("trigger")
        try:
            result = catalog_refresh(state_service, trigger=str(trigger) if trigger else "")
        except CatalogError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="record_model_capability_cell",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "runtime": ParameterMetadata(description="claude_code | codex.", required=True, type=ParameterType.STRING),
            "model": ParameterMetadata(description="Canonical model id as the runtime names it, e.g. gpt-6-sol or claude-opus-5-5.", required=True, type=ParameterType.STRING),
            "effort": ParameterMetadata(
                description="non_reasoning | none | low | medium | high | xhigh | max.",
                required=True,
                type=ParameterType.STRING,
            ),
            "capability_score": ParameterMetadata(
                description="Artificial Analysis Intelligence Index for this model at this effort, 0-100.",
                required=True,
                type=ParameterType.FLOAT,
            ),
            "cost_per_task_usd": ParameterMetadata(
                description="Artificial Analysis cost-per-task in USD at this effort. Omit when unknown; the cell is then unpriced.",
                required=False,
                type=ParameterType.FLOAT,
            ),
            "source_ref": ParameterMetadata(
                description="Where the reading came from: a URL, or a register event or document id. Required.",
                required=True,
                type=ParameterType.STRING,
            ),
            "note": ParameterMetadata(description="Optional free text kept with the evidence.", required=False, type=ParameterType.STRING),
            "staleness_window_hours": ParameterMetadata(
                description="Hours the cell stays servable before it needs a fresh reading. Default 72.",
                required=False,
                type=ParameterType.INTEGER,
            ),
        },
        output_type="object",
        output_description=(
            "Operator ruling rul_9e7a67ba -- anyone with better information records "
            "one (runtime, model, effort) catalog cell directly as accepted, with "
            "its source kept as observation evidence, so a model released today "
            "is dispatchable today."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="record_model_capability_cell outcome",
            properties={
                "run_id": ParameterMetadata(type=ParameterType.STRING, description="The model_capability_refresh_run row (trigger=manual) this call wrote."),
                "runtime": ParameterMetadata(type=ParameterType.STRING),
                "model": ParameterMetadata(type=ParameterType.STRING),
                "effort": ParameterMetadata(type=ParameterType.STRING),
                "capability_score": ParameterMetadata(type=ParameterType.FLOAT),
                "cost_per_task_usd": ParameterMetadata(type=ParameterType.FLOAT, description="Null when the caller gave no cost."),
                "acceptance": ParameterMetadata(type=ParameterType.STRING, description="Always accepted."),
                "observation_ids": ParameterMetadata(type=ParameterType.LIST, description="The observation rows holding this reading's evidence."),
                "previous": ParameterMetadata(type=ParameterType.OBJECT, description="The cell's prior capability_score, cost_per_task_usd, acceptance and measured_at; null when the cell is new."),
            },
        ),
    )
    def record_model_capability_cell(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """rul_9e7a67ba -- record one catalog cell directly, as accepted, with its source."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(code="state_service_unavailable", message="state_service is not bound on this solet.")
        try:
            result = catalog_record_cell(state_service, dict(raw))
        except CatalogError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="record_held_authorization",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "requesting_peer": ParameterMetadata(
                description="The role or instance whose commit request was refused.",
                required=True,
                type=ParameterType.STRING,
            ),
            "owed_by_role": ParameterMetadata(
                description=(
                    "The seat/coordinator role expected to send the first-party "
                    "authorization this refusal is waiting on."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "branch_or_request_ref": ParameterMetadata(
                description="Proposed branch name or other stable request identifier.",
                required=True,
                type=ParameterType.STRING,
            ),
            "reason": ParameterMetadata(
                description="Why the request was refused (e.g. the unverifiable citation).",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "R1 held-authorization queue — record one open entry. Convention "
            "(not enforced): Git-Controller calls this at refusal time, never "
            "the requesting peer, so the entry exists mechanically regardless "
            "of any seat's memory."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="record_held_authorization outcome",
            properties={
                "entry_id": ParameterMetadata(type=ParameterType.STRING),
                "status": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def record_held_authorization(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """R1 held-authorization queue — plain state insert of one refusal
        event. No caller-identity check (seat ruling 2026-08-17):
        capability first, lockdown only after usage data; the convention is
        documented, not mechanically enforced."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        try:
            result = lifecycle_record_held_authorization(
                state_service,
                requesting_peer=str(raw.get("requesting_peer", "")),
                owed_by_role=str(raw.get("owed_by_role", "")),
                branch_or_request_ref=str(raw.get("branch_or_request_ref", "")),
                reason=str(raw.get("reason", "")),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="list_held_authorizations",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "owed_by_role": ParameterMetadata(
                description="Filter to entries owed by this role. Omit for all roles.",
                required=False,
                type=ParameterType.STRING,
            ),
            "requesting_peer": ParameterMetadata(
                description="Filter to entries requested by this peer. Omit for all peers.",
                required=False,
                type=ParameterType.STRING,
            ),
            "include_retired": ParameterMetadata(
                description="Include already-retired entries. Default false (open only).",
                required=False,
                type=ParameterType.BOOLEAN,
            ),
        },
        output_type="object",
        output_description=(
            "R1 held-authorization queue — the 'what is blocked on <role>?' "
            "answer for a freshly-booted session with no memory of a prior "
            "seat. Open entries only unless include_retired is set. Read-only."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="list_held_authorizations outcome",
            properties={
                "entries": ParameterMetadata(type=ParameterType.LIST),
                "count": ParameterMetadata(type=ParameterType.INTEGER),
            },
        ),
    )
    def list_held_authorizations(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """R1 held-authorization queue — trivial state read, no side effects.
        Never filters by staleness itself (no silent TTL in this queue) —
        `created_at` is returned as-is so the caller judges age directly."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        try:
            result = lifecycle_list_held_authorizations(
                state_service,
                owed_by_role=_opt_str(raw.get("owed_by_role")),
                requesting_peer=_opt_str(raw.get("requesting_peer")),
                include_retired=bool(_opt_bool(raw.get("include_retired")) or False),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="retire_held_authorization",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "entry_id": ParameterMetadata(
                description="The entry to retire (its id from record/list_held_authorizations).",
                required=True,
                type=ParameterType.STRING,
            ),
            "retired_reason": ParameterMetadata(
                description="e.g. 'authorized', 'superseded', 'withdrawn'.",
                required=True,
                type=ParameterType.STRING,
            ),
            "retired_by": ParameterMetadata(
                description="Role or instance calling this verb.",
                required=True,
                type=ParameterType.STRING,
            ),
            "retired_at": ParameterMetadata(
                description="ISO-8601 timestamp of the retirement.",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "R1 held-authorization queue — retire one entry. Convention (not "
            "enforced): Git-Controller retires on receiving the matching "
            "first-party authorization; the owed_by_role holder may also "
            "retire directly. No silent TTL — every retirement is explicit."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="retire_held_authorization outcome",
            properties={
                "entry_id": ParameterMetadata(type=ParameterType.STRING),
                "status": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def retire_held_authorization(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """R1 held-authorization queue — predicated state update
        (`retired_at IS NULL` -> set), so a double-retire is a loud
        `entry_not_found_or_already_retired` rather than a silent
        overwrite of an earlier retirement's provenance."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        try:
            result = lifecycle_retire_held_authorization(
                state_service,
                entry_id=str(raw.get("entry_id", "")),
                retired_reason=str(raw.get("retired_reason", "")),
                retired_by=str(raw.get("retired_by", "")),
                retired_at=str(raw.get("retired_at", "")),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="peer_holds_role",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "name": ParameterMetadata(
                description="Role name to re-check ownership of (e.g. 'Git-Controller').",
                required=True,
                type=ParameterType.STRING,
            ),
            "agent_instance_id": ParameterMetadata(
                description=(
                    "The caller's own agent_instance_id (agi-...). Its STABLE session "
                    "id is sourced server-side from the peer_binding — a caller-supplied "
                    "session id is never trusted."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "Act-time role-ownership re-check (§5.0): whether the caller's session "
            "still holds the role, its resolved stable session id, and whether the "
            "role's CURRENT holder has a delivery route attached."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="peer_holds_role outcome (§5.0 act-time ownership re-check)",
            properties={
                "holds": ParameterMetadata(type=ParameterType.BOOLEAN),
                "name": ParameterMetadata(type=ParameterType.STRING),
                "agent_session_id": ParameterMetadata(type=ParameterType.STRING),
                "delivery_route_attached": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                ),
            },
        ),
    )
    def peer_holds_role(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> dict[str, Any]:
        """READ-ONLY act-time ownership re-check (§5.0) — does the caller's session
        STILL hold ``name``?

        The §9 cutover makes this the Git-Controller Step-9.5 re-check: ``holds_role``
        is PULL-TRUTH over the v4 ``role_binding`` table (the prior notice-drain was a
        best-effort push signal). Sources the caller's STABLE session id from its OWN
        ``peer_binding`` row (by ``agent_instance_id`` — the REL-07 pattern), never a
        caller-supplied session id, then compares it to the live holder's. It NEVER
        writes: the anti-pattern is a self-re-claim (a WRITE that would STEAL the role
        back from a legitimate new holder) — this is a pure read.
        """
        raw = params.get("parameters", params)
        name = str(raw.get("name", "")).strip()
        agent_instance_id = str(raw.get("agent_instance_id", "")).strip()
        if not name or not agent_instance_id:
            return _failure_result(
                code="missing_argument",
                message="peer_holds_role requires non-empty 'name' and 'agent_instance_id'.",
            )
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        agent_session_id = self._claimant_session_id(agent_instance_id)
        holds = holds_role(state_service, name, agent_session_id)
        return _success_result(
            data={
                "holds": holds,
                "name": name,
                "agent_session_id": agent_session_id,
                "delivery_route_attached": self._role_delivery_route_attached(
                    state_service,
                    name,
                ),
            },
        )

    @platform_process(
        name="peer_mark_role_covered",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "name": ParameterMetadata(
                description="Role name to advance the covered mark for.",
                required=True,
                type=ParameterType.STRING,
            ),
            "message_id": ParameterMetadata(
                description=(
                    "The arm-... message_id of the NEWEST role message this "
                    "session has processed for 'name'. The server looks this "
                    "row up and attests ITS OWN (created_at, id) — a caller "
                    "can only ever name a pair that corresponds to a row "
                    "that exists (pull-surface boundary design §2)."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "The stored role_covered_mark after this attestation — the "
            "PRE-EXISTING mark unchanged on a monotonic no-op."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="peer_mark_role_covered outcome (design §2).",
            properties={
                "recipient_key": ParameterMetadata(type=ParameterType.STRING),
                "covered_created_at": ParameterMetadata(type=ParameterType.STRING),
                "covered_id": ParameterMetadata(type=ParameterType.STRING),
                "covered_message_id": ParameterMetadata(type=ParameterType.STRING),
                "attested_at": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def peer_mark_role_covered(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Attest ``name`` covered through ``message_id`` (design §2, R1).

        **R1 — REGISTERED-ROUTE-ONLY, no exceptions.** This is a WRITE whose
        wrong advance is silent loss for the NEXT holder (the strong class,
        per Architect's ruling — the same shape as the measured
        ``peer_claim_role`` instance-id finding). Unlike ``peer_holds_role``
        (a READ, whose mistake-cost falls on the caller), this verb takes NO
        caller-supplied identity argument at all, ever — not even one
        resolved server-side. Identity is sourced EXCLUSIVELY from
        ``state["inference_vertex_session_id"]``: the calling BRIDGE's own
        ``agent_instance_id``, stamped by ``ActionProcessor
        ._lift_inference_vertex_identity`` ONLY for a call dispatched through
        a registered bridge's ``process_call`` (``PlatformSurface
        ._build_process_call_trigger_data``). A caller arriving over an
        unregistered route — a one-shot ``solet-bridge call`` from the local
        CLI, which stamps the DIFFERENT ``caller_attribution_*`` family
        instead (§34.6) — is refused loud with ``unregistered_route``. That
        family is deliberately NEVER consulted here, even as a fallback.

        Role ownership is re-checked LIVE, at attestation time (not claim
        time) — a displaced prior holder attesting after being displaced
        could otherwise advance the mark past mail the NEW holder never saw,
        the identical silent-loss shape the watch-spool mark was ruled out
        for.
        """
        raw = params.get("parameters", params)
        name = str(raw.get("name", "")).strip()
        message_id = str(raw.get("message_id", "")).strip()
        if not name or not message_id:
            return _failure_result(
                code="missing_argument",
                message="peer_mark_role_covered requires non-empty 'name' and 'message_id'.",
            )
        caller_instance_id = str(state.get("inference_vertex_session_id") or "").strip()
        if not caller_instance_id:
            return _failure_result(
                code="unregistered_route",
                message=(
                    "peer_mark_role_covered requires a call dispatched through "
                    "a registered bridge's process_call; a one-shot solet "
                    "call carries no registered-route identity to attest with."
                ),
            )
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        agent_session_id = self._claimant_session_id(caller_instance_id)
        if not agent_session_id:
            return _failure_result(
                code="identity_not_registered",
                message=f"no live peer_binding for instance {caller_instance_id!r}.",
            )
        if not holds_role(state_service, name, agent_session_id):
            return _failure_result(
                code="peer_role_not_held",
                message=(
                    f"the calling session does not currently hold role {name!r} "
                    "— re-check ownership before attesting."
                ),
            )
        try:
            binding = resolve_role_binding(state_service, name)
            session_label = binding.session_label
        except Exception:  # noqa: BLE001 — audit field only, best-effort
            session_label = ""
        service = self._require_service()
        try:
            mark = service.mark_role_covered(
                recipient_key=name,
                message_id=message_id,
                attested_by_agent_instance_id=caller_instance_id,
                attested_by_agent_session_id=agent_session_id,
                attested_by_session_label=session_label,
            )
        except AgentRequestInvalidError as exc:
            return _failure_result(code="role_message_not_found", message=str(exc))
        return _success_result(
            data={
                "recipient_key": mark.recipient_key,
                "covered_created_at": mark.covered_created_at,
                "covered_id": mark.covered_id,
                "covered_message_id": mark.covered_message_id,
                "attested_at": mark.attested_at,
            },
        )

    def _role_delivery_route_attached(
        self,
        state_service: Any,
        name: str,
    ) -> bool:
        """Does the role's CURRENT holder have a live bridge bound right now?

        A role binding outlives the session that claimed it, so
        ``holds=True`` can be reported for a role whose holder has no receiver
        left — the claim is durable, the route is not. This measures the route:
        the holder's stable session id resolves to a ``peer_binding`` row, and
        that row's bridge is open (an MCP bridge session or an armed ``watch``
        long-poll — both are the same kind of attachment here).

        Named for what it measures. NOT ``receiving``: on MCP transport a route
        can be attached while no waker ever fires, so a truthful name is the
        narrow one. False is also the honest answer for a vacant role and for a
        holder whose binding is gone.

        **Total by construction.** Every fault this lookup can raise —
        a duplicate binding for one session id, a malformed role row — is
        answered ``False`` rather than propagated. ``peer_holds_role`` is
        Git-Controller's Step-9.5 pre-commit ownership re-check: ``holds`` is
        the safety answer and must survive anything the route lookup does. An
        additive truth-in-reporting field that can convert that boolean into an
        exception would be a regression wearing an addition's clothes.

        Caveat, stated rather than engineered away: this reads the role binding
        a second time (``holds_role`` read it first), so a displacement landing
        between the two reads would report ``holds`` for one holder and the
        route of another. Fixing that would mean re-implementing ``holds_role``
        inline, and changing the Step-9.5 safety computation to improve an
        advisory field is the wrong trade. The window is one state read wide.
        """
        if self._peer_registry is None or self._bridge_manager is None:
            return False
        try:
            resolved = resolve_role_binding(state_service, name)
            binding = self._peer_registry.resolve_by_agent_session_id(
                resolved.agent_session_id,
            )
        except (
            RoleBindingVacantError,
            RoleBindingMalformedError,
            PeerSessionAmbiguousError,
        ):
            return False
        if binding is None:
            return False
        bridge = self._bridge_manager.get(binding.bridge_id)
        return bridge is not None and not bridge.closed

    @platform_process(
        name="peer_inbox",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_session_id": ParameterMetadata(
                description=(
                    "The caller's OWN stable session id (ases-...), exported by "
                    "the fleet launcher as $AGENT_SESSION_ID and echoed by "
                    "peer_register / current_identity / the watcher's armed line. "
                    "The agent_id and agent_instance_id whose mail is read are "
                    "resolved server-side from this session's live peer_binding "
                    "row — a caller cannot name someone else's inbox."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "after": ParameterMetadata(
                description=(
                    "Instance-section cursor ONLY: an ISO-8601 timestamp, echo "
                    "back the previous page's next_after_created_at. It does "
                    "NOT page the role section — that is 'role_after'."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "role_after": ParameterMetadata(
                description=(
                    "Role-section cursor ONLY: the opaque token from the "
                    "previous page's next_role_cursor, echoed back verbatim. "
                    "Independent of 'after'; the two are never mixed. A "
                    "malformed token fails the role section closed."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "observer": ParameterMetadata(
                description="Read operational pending state without issuing a display-receipt page.",
                required=False,
                type=ParameterType.BOOLEAN,
                default=False,
            ),
            "limit": ParameterMetadata(
                description=(
                    "Maximum entries per section, clamped to 1..100. Default 5 "
                    "— entries carry full message content (~4KB each measured "
                    "2026-08-01), so this is a page size, not a backlog size: "
                    "page with the two cursors instead of raising it."
                ),
                required=False,
                type=ParameterType.INTEGER,
                default=PEER_INBOX_DEFAULT_LIMIT,
            ),
        },
        output_type="object",
        output_description=(
            "One page of the caller's peer inbox: an instance section (entries "
            "+ next_after_created_at + instance_exhausted) and an independently-cursored role "
            "section (role_entries + next_role_cursor + its fault-domain "
            "status), plus role_limit and truncation metadata. A successful "
            "page is not a drain; continue until next_role_cursor is "
            "null. Pull-surface "
            "boundary (design §5): role_floor_applied is True when the "
            "default drain's mark-bounded floor removed already-covered "
            "rows this call; role_history_cursor, populated only on a "
            "genuine floor-stop, is a deliberate-deep-read token."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description=(
                "peer_inbox page — the exact shape emitted by "
                "peer_inbox_view.serialize_peer_inbox_page (the same payload the "
                "localhost /peer/inbox route and the MCP peer_inbox tool return)"
            ),
            properties={
                "recipient_agent_id": ParameterMetadata(type=ParameterType.STRING),
                "recipient_agent_instance_id": ParameterMetadata(
                    type=ParameterType.STRING,
                ),
                "entries": ParameterMetadata(type=ParameterType.LIST),
                "next_after_created_at": ParameterMetadata(
                    type=ParameterType.STRING,
                ),
                "instance_exhausted": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description=(
                        "Whether the newest-first instance section is exhausted "
                        "under its backward timestamp cursor."
                    ),
                ),
                "role_entries": ParameterMetadata(type=ParameterType.LIST),
                "next_role_cursor": ParameterMetadata(type=ParameterType.STRING),
                "role_section_status": ParameterMetadata(type=ParameterType.STRING),
                "role_section_error": ParameterMetadata(type=ParameterType.STRING),
                "role_limit": ParameterMetadata(type=ParameterType.INTEGER),
                "role_page_truncated": ParameterMetadata(type=ParameterType.BOOLEAN),
                "role_truncation_reason": ParameterMetadata(
                    type=ParameterType.STRING,
                ),
                "role_byte_ceiling": ParameterMetadata(type=ParameterType.INTEGER),
                "role_floor_applied": ParameterMetadata(type=ParameterType.BOOLEAN),
                "role_history_cursor": ParameterMetadata(type=ParameterType.STRING),
                "role_read_page_token": ParameterMetadata(type=ParameterType.STRING),
                "role_read_page_status": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def peer_inbox_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> dict[str, Any]:
        """PULL receive path — read this session's own peer mail on demand.

        Named ``peer_inbox_action``, not ``peer_inbox``: this plugin already
        carries the typed ``AgentMessagingServiceInterface.peer_inbox``
        delegation, so this is the platform's documented split-verb shape —
        ``ActionProcessor._execute_plugin_method`` resolves ``peer_inbox`` from
        the process key and then prefers the ``<verb>_action`` wrapper when it
        carries the decorator's ``_platform_process_metadata`` marker. The
        process key stays ``plugin::agent_messaging_plugin::peer_inbox``.

        Before this verb the ONLY read of the durable inbox was
        the ``GET .../peer/inbox`` bridge route, whose identity comes from the
        CALLING bridge's peer registration. ``solet-bridge call`` opens a fresh,
        unregistered bridge, so a no-MCP session had no pull path at all —
        streaming ``solet-bridge watch`` was the only receive, and a session
        without a live watcher simply could not read its backlog.

        Identity is therefore an explicit argument, but a caller may only name
        its OWN session: ``agent_session_id`` is looked up in ``peer_binding``
        and the recipient triple is taken from that row (the same three fields
        the route reads off its own binding). An unknown or duplicated session
        id is a loud error — never a silent read of an empty inbox.

        Deliberately NOT done here: this read does not retire the re-emit /
        escalation insurance on the rows it returns (the watcher long-poll ack
        and the MCP ``/peer/drain`` reconcile remain the two consumption
        authorities). Retiring insurance on a read that might not reach a model
        turn risks destroying content, which is the worse failure; a session
        that drains here may still see the same IMPORTANT rows re-emitted.
        """
        if not self._active or self._peer_registry is None:
            return _failure_result(
                code="bridge.not_running",
                message=(
                    "The agent messaging bridge is not active on this "
                    "solet, so no inbox can be read. This is NOT an empty "
                    "inbox — start the interface and retry."
                ),
            )
        raw = params.get("parameters", params)
        agent_session_id = str(raw.get("agent_session_id", "")).strip()
        if not agent_session_id:
            return _failure_result(
                code="missing_argument",
                message=(
                    "peer_inbox requires the caller's own non-empty "
                    "'agent_session_id' (the launcher exports it as "
                    "$AGENT_SESSION_ID)."
                ),
            )
        try:
            binding = self._peer_registry.resolve_by_agent_session_id(
                agent_session_id,
            )
        except PeerSessionAmbiguousError as exc:
            return _failure_result(
                code="peer_session_ambiguous",
                message=str(exc),
            )
        if binding is None:
            return _failure_result(
                code="identity_not_registered",
                message=(
                    f"no live peer_binding for agent_session_id "
                    f"{agent_session_id!r}. This usually means this session's "
                    f"watcher or bridge is no longer registered — re-arm it "
                    f"('<solet> watch --role <role>', or peer_register "
                    f"over MCP) and retry. Read this as 'the reader is "
                    f"unknown', never as 'the reader has no mail': the "
                    f"messages are durable and still waiting. A wrong "
                    f"agent_session_id produces this same error, so check the "
                    f"value came from $AGENT_SESSION_ID and not a stale note."
                ),
            )
        try:
            request = _build_peer_inbox_request(raw, binding)
        except ValueError as exc:
            return _failure_result(code="invalid_after", message=str(exc))
        try:
            page = self._require_service().peer_inbox(request)
        except AgentMessagingError as exc:
            return _failure_result(
                code="peer_inbox_rejected",
                message=str(exc),
            )
        # The caller proved liveness by reading; keep "last active" in step with
        # the delivery path, exactly as the /peer/inbox route does.
        self._peer_registry.touch_binding(binding.agent_instance_id)
        return _success_result(
            data=serialize_peer_inbox_page(page, binding.agent_instance_id),
        )

    @platform_process(
        name="peer_ack_role_read_page",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_session_id": ParameterMetadata(required=True, type=ParameterType.STRING,
                description="The acknowledging caller's own stable session id."),
            "page_token": ParameterMetadata(required=True, type=ParameterType.STRING,
                description="Opaque token returned by peer_inbox after role rows were output."),
        },
        output_type="object",
        output_description="Acknowledge a successfully rendered role inbox page.",
    )
    def peer_ack_role_read_page(
        self, params: dict[str, Any], state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        raw = params.get("parameters", params)
        session_id = str(raw.get("agent_session_id", "")).strip()
        token = str(raw.get("page_token", "")).strip()
        if not session_id or not token:
            return _failure_result(code="missing_argument", message="peer_ack_role_read_page requires agent_session_id and page_token.")
        if self._peer_registry is None:
            return _failure_result(code="bridge.not_running", message="agent messaging bridge is not active.")
        try:
            binding = self._peer_registry.resolve_by_agent_session_id(session_id)
        except PeerSessionAmbiguousError as exc:
            return _failure_result(code="peer_session_ambiguous", message=str(exc))
        if binding is None:
            return _failure_result(code="identity_not_registered", message="no live peer binding for agent_session_id.")
        try:
            result = self._require_service().acknowledge_role_read_page(
                agent_session_id=session_id, agent_instance_id=binding.agent_instance_id, token=token,
            )
        except AgentMessagingError as exc:
            return _failure_result(code="role_read_page_rejected", message=str(exc))
        return _success_result(data={"status": result.status, "receipt_count": result.receipt_count})

    @platform_process(
        name="peer_role_read_receipts",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_session_id": ParameterMetadata(required=True, type=ParameterType.STRING,
                description="The querying caller's own stable session id."),
            "candidates": ParameterMetadata(required=True, type=ParameterType.LIST,
                description="Exact recipient_key/role_row_id pairs from a wake spool record."),
        },
        output_type="object",
        output_description="Read exact acknowledged role display receipts for the caller's held roles.",
    )
    def peer_role_read_receipts(
        self, params: dict[str, Any], state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        raw = params.get("parameters", params)
        session_id = str(raw.get("agent_session_id", "")).strip()
        raw_candidates = raw.get("candidates")
        if not session_id or not isinstance(raw_candidates, list):
            return _failure_result(code="missing_argument", message="peer_role_read_receipts requires agent_session_id and candidates.")
        try:
            candidates = _coerce_role_receipt_candidates(raw_candidates)
        except ValueError as exc:
            return _failure_result(code="invalid_candidates", message=str(exc))
        if self._peer_registry is None:
            return _failure_result(code="bridge.not_running", message="agent messaging bridge is not active.")
        try:
            binding = self._peer_registry.resolve_by_agent_session_id(session_id)
        except PeerSessionAmbiguousError as exc:
            return _failure_result(code="peer_session_ambiguous", message=str(exc))
        if binding is None:
            return _failure_result(code="identity_not_registered", message="no live peer binding for agent_session_id.")
        try:
            results = self._require_service().role_read_receipts(
                agent_instance_id=binding.agent_instance_id, candidates=candidates,
            )
        except AgentMessagingError as exc:
            return _failure_result(code="role_read_receipts_rejected", message=str(exc))
        return _success_result(data={
            "agent_instance_id": binding.agent_instance_id,
            "results": [{"recipient_key": item.recipient_key, "role_row_id": item.role_row_id,
                         "served": item.served} for item in results],
        })

    @platform_process(
        name="peer_list",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        output_type="object",
        output_description=(
            "A snapshot of every live peer registered on this solet: "
            "the sorted list of distinct agent_ids present, and per agent_id "
            "the list of its live instances (agent_instance_id, "
            "session_label, parent_pid, registered_at, created_at, "
            "updated_at)."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description=(
                "peer_list snapshot — the exact shape emitted by "
                "peer_list_view.serialize_peer_list (the same payload the "
                "localhost /peer/list route and the MCP peer_list tool "
                "return)"
            ),
            properties={
                "agent_ids": ParameterMetadata(type=ParameterType.LIST),
                "instances": ParameterMetadata(type=ParameterType.DICT),
            },
        ),
    )
    def peer_list(
        self,
        params: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
        state: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> dict[str, Any]:
        """No-MCP peer enumeration — closes the peer-enumeration asymmetry.

        WS-1a's ``peer_inbox`` gave a no-MCP session (``solet-bridge call``, no
        registered bridge) a way to read its OWN mail. It left a companion
        gap open: that same session had no way to see who ELSE was live —
        ``peer_list`` existed only as an MCP-bridge HTTP route and its
        Streamable/stdio MCP mirrors, all of which resolve identity from the
        CALLING bridge's registration, something ``solet-bridge call`` never
        has. This verb needs no such resolution: it is a global, unfiltered
        registry snapshot, identical for every caller regardless of identity
        — there is nothing to scope BY, so unlike ``peer_inbox`` it takes no
        arguments and does no per-caller lookup.

        No fencing beyond "reached this solet at all": localhost is the
        existing trust boundary for enumerating peers on this MCP surface
        (the pre-existing route and tool have never required more), and this
        verb decides that explicitly rather than inheriting it silently —
        see ``peer_list_view``'s module docstring for the field-set decision
        that keeps the same boundary from silently widening (``bridge_id``
        and ``agent_session_id`` stay unexposed, exactly as on the two
        pre-existing surfaces).
        """
        if not self._active or self._peer_registry is None:
            return _failure_result(
                code="bridge.not_running",
                message=(
                    "The agent messaging bridge is not active on this "
                    "solet, so no peer registry can be read. This is "
                    "NOT an empty registry — start the interface and retry."
                ),
            )
        snapshot = self._peer_registry.list_agent_ids()
        return _success_result(data=serialize_peer_list(snapshot))

    @platform_process(
        name="peer_identity",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        output_type="object",
        output_description="Bounded caller identity qualification without exposing its stable key.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Caller-scoped bridge identity qualification.",
            properties={
                "caller_identity_available": ParameterMetadata(type=ParameterType.BOOLEAN),
                "registered_bridge": ParameterMetadata(type=ParameterType.BOOLEAN),
                "bridge_identity": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def peer_identity(
        self,
        params: dict[str, Any],  # noqa: ARG002
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Qualify only identity facts the bridge supplied for this call."""
        registered = bool(str(state.get("inference_vertex_session_id") or "").strip())
        attributed = bool(str(state.get("caller_attribution_instance_id") or "").strip())
        available = registered or attributed
        return _success_result(
            data={
                "caller_identity_available": available,
                "registered_bridge": registered,
                "bridge_identity": "registered_bridge"
                if registered
                else ("one_shot_attributed" if attributed else "unavailable"),
            }
        )

    @platform_process(
        name="resolve_caller_provenance",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        output_type="object",
        output_description="Server-derived caller provenance for an application adapter.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Managed-session provenance resolved without caller identity input.",
            properties={
                "managed_session_id": ParameterMetadata(type=ParameterType.STRING),
                "agent_session_id": ParameterMetadata(type=ParameterType.STRING),
                "directed_by": ParameterMetadata(type=ParameterType.STRING),
                "directed_by_encoding": ParameterMetadata(type=ParameterType.STRING),
                "session_role_claim_id": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def resolve_caller_provenance(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Resolve the calling bridge's managed-session provenance or refuse."""
        del params
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this solet.",
            )
        try:
            provenance = lifecycle_resolve_caller_provenance(state_service, state)
        except CallerProvenanceError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=provenance.as_dict())

    @platform_process(
        name="qualify_fleet",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        output_type="object",
        output_description="Run the bounded no-worktree fleet lifecycle qualification.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Fleet lifecycle qualification outcome.",
            properties={
                "host": ParameterMetadata(type=ParameterType.STRING),
                "spawned": ParameterMetadata(type=ParameterType.BOOLEAN),
                "role_claimed": ParameterMetadata(type=ParameterType.BOOLEAN),
                "addressed_delivery_confirmed": ParameterMetadata(type=ParameterType.BOOLEAN),
                "retired": ParameterMetadata(type=ParameterType.BOOLEAN),
            },
        ),
    )
    def qualify_fleet(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """Exercise the entire bounded no-worktree fleet lifecycle."""
        del params
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state service is not bound",
            )
        request = SpawnSessionRequest(
            role_class="ephemeral",
            lane_id="qualify-fleet",
            brief_ref="internal:qualify_fleet",
            work_class="read_only",
            budget_line="qualification",
            host="qualification",
            role_name="qualify-fleet-worker",
            local_name="qualify-fleet-worker",
            directed_by=format_directed_by(state.get("call_context")),
            synthetic_qualification_no_worktree=True,
            dispatch_kind="infrastructure",
        )
        try:
            spawned = lifecycle_spawn_session(state_service, request)
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        agent_instance_id = str(spawned.get("agent_instance_id") or "")
        agent_session_id = f"ases-{agent_instance_id}"
        role_name = "qualify-fleet-worker"
        role_claimed, delivery_confirmed, failure = self._qualify_fleet_worker(
            state_service,
            state,
            agent_instance_id,
            agent_session_id,
            role_name,
        )
        try:
            if role_claimed:
                release = self.peer_release_role({"name": role_name}, state)
                if release.get("action_status") != "completed" and failure is None:
                    failure = self._qualification_action_error(
                        release,
                        "qualification_release_failed",
                        "qualification role release failed",
                    )
        finally:
            failure = self._retire_qualification_worker(
                state_service,
                state,
                agent_instance_id,
                failure,
            )
        if failure is not None:
            return _failure_result(code=failure[0], message=failure[1])
        return _success_result(
            data={
                "host": "qualification",
                "spawned": True,
                "role_claimed": role_claimed,
                "addressed_delivery_confirmed": delivery_confirmed,
                "retired": True,
            }
        )

    def _qualify_fleet_worker(
        self,
        state_service: Any,
        state: dict[str, Any],
        agent_instance_id: str,
        agent_session_id: str,
        role_name: str,
    ) -> tuple[bool, bool, tuple[str, str] | None]:
        if self._peer_registry is None:
            return (
                False,
                False,
                ("bridge.not_running", "qualification requires an active peer registry"),
            )
        binding = self._wait_for_qualification_binding(agent_session_id)
        if binding is None:
            return (
                False,
                False,
                (
                    "qualification_registration_timeout",
                    "qualification watch did not register its expected stable session identity",
                ),
            )
        claim = claim_role_for_session(
            origin=RoleClaimOrigin.INFRA,
            name=role_name,
            agent_id=binding.agent_id,
            agent_instance_id=binding.agent_instance_id,
            agent_session_id=binding.agent_session_id,
            session_label=binding.session_label,
            state_service=state_service,
            bridge_manager=self._bridge_manager,
            peer_registry=self._peer_registry,
            agent_messaging_service=self._handover_service(),
            call_context=state.get("call_context"),
        )
        if isinstance(claim, RoleClaimFailure):
            return False, False, (claim.code, claim.message)
        sent = self.peer_send_by_name(
            {"name": role_name, "content": "fleet qualification delivery probe"},
            state,
        )
        sent_data = sent.get("data") if sent.get("action_status") == "completed" else None
        if not isinstance(sent_data, dict):
            return (
                True,
                False,
                self._qualification_action_error(
                    sent,
                    "qualification_delivery_failed",
                    "role delivery failed",
                ),
            )
        if sent_data.get("resolved_agent_instance_id") != agent_instance_id:
            return (
                True,
                False,
                (
                    "qualification_delivery_unobserved",
                    "role delivery resolved to a different qualification worker",
                ),
            )
        if sent_data.get("delivery") != "queued_watcher":
            return (
                True,
                False,
                (
                    "qualification_delivery_unobserved",
                    "role delivery did not reach the registered qualification watcher",
                ),
            )
        return True, True, None

    def _wait_for_qualification_binding(self, agent_session_id: str) -> Any | None:
        assert self._peer_registry is not None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            binding = self._peer_registry.resolve_by_agent_session_id(agent_session_id)
            if binding is not None:
                return binding
            time.sleep(0.05)
        return None

    @staticmethod
    def _qualification_action_error(
        result: dict[str, Any],
        default_code: str,
        default_message: str,
    ) -> tuple[str, str]:
        error = result.get("error")
        if not isinstance(error, dict):
            return default_code, default_message
        return str(error.get("code") or default_code), str(error.get("message") or default_message)

    @staticmethod
    def _retire_qualification_worker(
        state_service: Any,
        state: dict[str, Any],
        agent_instance_id: str,
        failure: tuple[str, str] | None,
    ) -> tuple[str, str] | None:
        try:
            lifecycle_retire_session(
                state_service,
                agent_instance_id=agent_instance_id,
                directed_by=format_directed_by(state.get("call_context")),
            )
        except VerbError as exc:
            return failure if failure is not None else (exc.code, exc.message)
        return failure

    # dead post-S3; removed with the full AB-role-WRITE retirement follow-up
    def _get_address_book_service(self) -> Any:
        """Return the bound address_book_service, or ``None`` if unavailable."""
        orchestrator = getattr(self, "orchestrator_ref", None)
        if orchestrator is None:
            return None
        get_service = getattr(orchestrator, "get_service", None)
        if get_service is None:
            return None
        try:
            return get_service("address_book_service")
        except Exception:
            return None

    def _get_state_service(self) -> Any:
        """Return the bound state_service, or ``None`` if unavailable.

        The v10 Control #2.C resolution + claim authority — the role verbs
        (``peer_send_by_name`` / ``peer_claim_role`` / ``peer_release_role``)
        go through the ``agent_role_binding`` state table via this handle.
        """
        orchestrator = getattr(self, "orchestrator_ref", None)
        if orchestrator is None:
            return None
        get_service = getattr(orchestrator, "get_service", None)
        if get_service is None:
            return None
        try:
            return get_service("state_service")
        except Exception:
            return None

    def _run_startup_backfills(self) -> None:
        """Run the ONE-SHOT, durable-marker-gated GAP-2 ``agent_message``
        important-column projection off the load-bearing ``state_service``.

        (The Control #2 ``agent_role_binding`` legacy-role seed that used to run
        here was RETIRED at the §9 cutover — see the inline note below. It wrote the
        legacy table AFTER readiness set the v4-migration marker, which would strand
        rows out of v4 and break the parity-proof — Codex BLOCKER-2.)

        ``state_service`` is resolved DIRECTLY (not via the catch-all-None
        ``_get_*`` helpers) so a genuine lookup fault PROPAGATES instead of
        masquerading as "unbound" (Codex MAJOR-1); a partial/failed backfill leaves
        its marker unset so the next boot re-runs.
        """
        orchestrator = getattr(self, "orchestrator_ref", None)
        if orchestrator is None:
            raise RuntimeError(
                f"{self.name}: orchestrator_ref not injected — cannot run the startup backfills",
            )
        state_service = orchestrator.get_service("state_service")
        if state_service is None:
            raise RuntimeError(
                f"{self.name}: state_service unbound — it is the load-bearing "
                "authority for the startup backfills; refusing to proceed",
            )
        # Control #2 B5: seed agent_role_binding from legacy address-book
        # RETIRED at the §9 cutover (slice-D): the legacy address-book →
        # agent_role_binding seed is GONE. It wrote the LEGACY table, but readiness
        # now runs the v4 migration (agent_role_binding → role_binding) + sets its
        # one-shot marker AHEAD of this startup step — a legacy write here would
        # strand rows out of v4 while the marker reads 'done' (parity never
        # re-converges), breaking the parity-proof the §9 quiesce-equivalent relies
        # on (Codex BLOCKER-2). v10 deliberately left the address book behind for
        # role resolution; a zombie legacy writer post-flip re-warms that retired
        # path, so it dies here. Roles are now claimed at runtime via peer_claim_role
        # into the v4 table; the migration copies any pre-existing agent_role_binding
        # rows forward.
        # GAP-2 SQL-lockdown: project metadata.important onto the new
        # core__agent_message.important column for pre-migration rows (after the
        # critical seed, so its unbounded read cannot defer the cutover).
        msg = backfill_message_important(state_service)
        msg_updated = msg.get("updated")
        logger.info(
            "%s: agent_message important backfill status=%s (%d row(s) flipped)",
            self.name,
            msg.get("status"),
            len(msg_updated) if isinstance(msg_updated, list) else 0,
        )
        # REL-05 F2: grandfather delivered role-message history so the new
        # consumption-gated drain predicate cannot flood-re-emit it. Rides this
        # same injected authenticated state_service (never opens its own
        # connection), so it is immune to the JOS-02 migration-credential class.
        consumed = backfill_role_message_consumed(state_service)
        consumed_updated = consumed.get("updated")
        logger.info(
            "%s: agent_role_message consumed backfill status=%s (%d row(s) grandfathered)",
            self.name,
            consumed.get("status"),
            len(consumed_updated) if isinstance(consumed_updated, list) else 0,
        )
        # Fleet session-management Phase B, D1 (§3.1): stamp role_class on
        # pre-Phase-B role rows + report (never auto-fix) any pre-existing
        # >1-named-role holder for the operator cleanup pass (Dawn ruling).
        role_class = backfill_role_class(state_service)
        role_class_stamped = role_class.get("stamped")
        role_class_violations = role_class.get("cardinality_violations")
        logger.info(
            "%s: role_class backfill status=%s (%d row(s) stamped, %d "
            "cardinality violation(s) reported)",
            self.name,
            role_class.get("status"),
            len(role_class_stamped) if isinstance(role_class_stamped, list) else 0,
            len(role_class_violations) if isinstance(role_class_violations, list) else 0,
        )

    # ------------------------------------------------------------------
    # IO interface lifecycle — start_interface / stop_interface
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # D-IF7 / D-IF8 — SessionInferenceProvider sidecar (v4 §4-5)
    # ------------------------------------------------------------------

    def get_inference_provider(
        self,
        agent_instance_id: str,
    ) -> SessionInferenceProvider | None:
        """Public accessor for the per-bridge inference vertex sidecar.

        Returns the :class:`SessionInferenceProvider` registered for
        ``agent_instance_id``, or ``None`` when no provider is bound
        (streamable peers, stdio peers that did not pass
        ``provides_inference=True``, or peers that have since
        unregistered). The wrapper at
        ``inference_service/__init__.py`` falls back to the bound
        default plugin on ``None`` per v4 §7.

        INF-01 bridge-open gate (option b): the sidecar is cleared only on the
        UNREGISTER path (``_clear_inference_providers_for_bridge``), so an
        idle-swept / closed bridge leaves a STALE entry whose ``append_event``
        would raise ``BridgeNotFoundError`` (bridge popped from ``_bridges``).
        Cross-check the provider's bridge is OPEN before returning it, so the
        Phase-5 resolver DEFERs (never routes to a dead bridge) instead of
        erroring. REL-09 removes the stale entry at the source
        (``sweep_idle``/``close`` → the full unregister cleanup); this gate makes
        the resolver's PROVIDER verdict robust to swept holders meanwhile.
        """
        with self._inference_providers_lock:
            provider = self._inference_providers.get(agent_instance_id)
        if provider is None or self._bridge_manager is None:
            return provider
        bridge = self._bridge_manager.get(provider.bridge_id)
        if bridge is None or bridge.closed:
            return None
        return provider

    def get_autonomic_provider(self) -> SessionInferenceProvider | None:
        """Resolve the ``sys:autonomic`` system-slot holder's LIVE provider.

        INF-01 fault-edge: the DEFAULT verdict (the organism's own error/result
        turn, no per-flow vertex binding) routes to the frontier session holding
        ``sys:autonomic`` instead of the local default model. Resolution is
        v4-NATIVE — the ``role_binding`` table via
        :func:`resolve_role_binding_v4` — NOT ``resolve_role_to_instance``,
        which reads the LEGACY ``agent_role_binding`` table where the
        system-slot constant does not live (until the slice-D cutover). The
        holder's ``agent_instance_id`` then maps to the bridge-open-gated
        provider.

        D2-window ruling (2026-08-04, pulled forward on measured runaway —
        ~25 redeliveries/hr into the seat vs the ~4/hr the earmark accepted):
        a PEER-SESSION holder has no protocol surface for raw vertex turns
        (the serve verb needs an ``icr-`` completion request id these lack),
        so while a peer session holds the slot the VERTEX lane always answers
        ``None`` — ``resolve_autonomic``'s existing ``None`` → DEFER flip then
        lands the turn in the durable no-loss queue instead of destroying it
        against a session that cannot act. Completion-request forwarding
        (``_forward_completion_request`` → ``get_inference_provider``) is a
        DIFFERENT path and still reaches peer-session holders — that class IS
        servable. Every provider this plugin can mint today is a peer session
        (``SessionInferenceProvider``), so the guard is unconditional here;
        the ad hoc inference capability that will properly own this slot
        registers a different provider kind and reworks this accessor when it
        lands (its designed home, per the 08-03 sitting).

        Returns ``None`` when the slot is VACANT
        (:class:`RoleBindingVacantError`) — the sub-slice-2 vacancy → DEFER
        flip, unchanged — and now also for a HELD slot, per the ruling above.
        """
        state = self._get_state_service()
        try:
            resolved = resolve_role_binding_v4(state, SYS_AUTONOMIC_SLOT)
        except RoleBindingVacantError:
            return None
        logger.info(
            "sys:autonomic vertex turn: slot held by peer session agi=%s — "
            "vertex lane DEFERs to the durable queue (peer sessions cannot "
            "serve raw vertex turns; D2-window ruling 2026-08-04)",
            resolved.agent_instance_id,
        )
        return None

    def _has_live_inference_provider(self, agent_instance_id: str) -> bool:
        """Bridge-open-gated provider presence — the §D.9 candidate filter (D1)."""
        return self.get_inference_provider(agent_instance_id) is not None

    def _forward_completion_request(
        self,
        agent_instance_id: str,
        row: dict[str, object],
    ) -> None:
        """Carry one durable completion-request row to a holder's bridge.

        The ``forward_completion`` collaborator injected into
        :class:`AutonomicAssignment` — resolves the holder's live
        :class:`SessionInferenceProvider` and emits the typed
        ``inference_completion_request`` event. Raises on a missing
        provider or malformed row: the CALLER owns the stamp-clear
        (the row returns to the unassigned backlog, never lost).
        """
        import json

        provider = self.get_inference_provider(agent_instance_id)
        if provider is None:
            raise FrameworkError(
                f"completion forward: instance {agent_instance_id!r} has no "
                "live inference provider",
            )
        messages = json.loads(str(row.get(COL_ICR_MESSAGES) or "[]"))
        correlation = json.loads(str(row.get(COL_ICR_CORRELATION) or "{}"))
        provider.forward_completion_request(
            request_id=str(row.get(COL_ICR_REQUEST_ID) or ""),
            purpose=str(row.get(COL_ICR_PURPOSE) or ""),
            messages=messages,
            correlation=correlation,
        )

    def _resubmit_vertex(self, flow_id: str, method: str) -> bool:
        """SUB-05 RESUBMIT primitive — re-drive one un-consumed vertex flow.

        The ``resubmit_vertex`` collaborator injected into
        :class:`AutonomicAssignment` (INF-06 reliability). Re-enters the flow's
        owning session with a FRESH ``process_results`` initial vertex
        (:func:`build_initial_vertex_action` — observation removed, instructions
        emptied → a fresh decode of the session's CURRENT durable state, per
        Architect §2d/§6-bis; NEVER a replay of the recorded decode). ``method``
        is observability-only: whether the original forward was a
        ``process_results`` or ``process_error`` vertex, the only coherent
        re-entry is the same fresh initial vertex (re-entering ``process_error``
        WITHOUT its ephemeral error observation is incoherent; WITH it is the
        forbidden replay). The failure's consequence is already durable — the
        plan's ``[>]`` marker stays on the failed step and the failed action
        result is stored — so the fresh decode is not blind to it.

        Reuses the SAME ``flow_id`` so a re-forward / re-defer of the re-driven
        vertex upserts the SAME ``core__inference_deferred_vertex`` row (the
        clear-on-reentry site). Returns True iff the fresh vertex was submitted;
        NEVER raises (the sweep and drain are per-row fault-isolated) — a missing
        collaborator, an unknown flow, or a submit fault logs loud and returns
        False so the row stays durably queued for the next tick.
        """
        try:
            flow_manager = self._flow_manager
            orchestrator = getattr(self, "orchestrator_ref", None)
            action_factory = getattr(self, "action_factory", None)
            builder = self._compilation_context_builder
            if (
                flow_manager is None
                or orchestrator is None
                or action_factory is None
                or builder is None
            ):
                logger.warning(
                    "INF-06 RESUBMIT flow=%s (method=%s): collaborators not "
                    "injected — cannot re-drive; row stays queued.",
                    flow_id,
                    method,
                )
                return False
            session_id = flow_manager.get_flow_session_id(flow_id)
            if not session_id:
                logger.warning(
                    "INF-06 RESUBMIT flow=%s (method=%s): no owning session "
                    "(flow unknown) — cannot re-drive; row stays queued.",
                    flow_id,
                    method,
                )
                return False
            action_def = build_initial_vertex_action(
                session_id=session_id,
                flow_id=flow_id,
                orchestrator=orchestrator,
            )
            context = builder.build_context(session_id=session_id, flow_id=flow_id)
            action_factory.submit_action_definition(
                action_definition=action_def,
                context=context,
            )
            logger.info(
                "INF-06 RESUBMIT flow=%s session=%s method=%s: fresh vertex "
                "submitted (fresh decode of current durable state).",
                flow_id,
                session_id,
                method,
            )
        except Exception:  # noqa: BLE001 — per-row isolation: never abort the sweep/drain
            logger.exception(
                "INF-06 RESUBMIT flow=%s (method=%s) FAULTED — row stays "
                "durably queued for the next tick.",
                flow_id,
                method,
            )
            return False
        return True

    @platform_process(
        name="submit_autonomic_completion",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "request_id": ParameterMetadata(
                description=(
                    "The completion request id (icr-...) from the "
                    "inference_completion_request bridge event this serve "
                    "answers."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "text": ParameterMetadata(
                description=(
                    "The completion text the holder produced for the request's messages payload."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "Serve outcome: the CAS verdict (served / already_served / "
            "already_failed / unknown_request) and the request id."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description=("submit_autonomic_completion outcome (INF-02 serve verb)"),
            properties={
                "status": ParameterMetadata(type=ParameterType.STRING),
                "request_id": ParameterMetadata(type=ParameterType.STRING),
                "resume_process_key": ParameterMetadata(
                    type=ParameterType.STRING,
                ),
            },
        ),
    )
    def submit_autonomic_completion(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> dict[str, Any]:
        """Serve one INF-02 completion request (holder → platform callback).

        CAS ``pending→served`` is the idempotency gate: exactly one serve
        wins and submits the consumer's resume continuation (returned in
        ``actions`` — the platform's Pattern-6a submission; the resume
        action_def carries NO result_processor, so its completion is
        terminal). A second serve reports ``already_served`` and submits
        nothing; an unknown request id is a typed rejection.

        Empty/whitespace ``text`` is rejected DELIBERATELY (Reviewer-A N2):
        an empty planning completion is degenerate — the row stays pending
        and the serve-timeout sweep re-forwards it. Do not relax this to
        accept ''.
        """
        raw = params.get("parameters", params)
        request_id = str(raw.get("request_id", "")).strip()
        text = str(raw.get("text", ""))
        if not request_id or not text.strip():
            return _failure_result(
                code="missing_argument",
                message=("submit_autonomic_completion requires non-empty 'request_id' and 'text'."),
            )
        verdict, row = serve_completion_request(
            self._get_state_service(),
            request_id=request_id,
            result_text=text,
        )
        if verdict != SERVE_SERVED or row is None:
            return _failure_result(
                code=verdict,
                message=(f"completion request {request_id!r} not served: {verdict}"),
            )
        resume_action = _build_resume_action(row)
        self.logger.info(
            "completion request %s SERVED (purpose=%s) — submitting resume continuation %s",
            request_id,
            row.get(COL_ICR_PURPOSE),
            row.get(COL_ICR_RESUME_PROCESS_KEY),
        )
        return _success_result(
            data={
                "status": verdict,
                "request_id": request_id,
                "resume_process_key": str(
                    row.get(COL_ICR_RESUME_PROCESS_KEY) or "",
                ),
            },
            actions=[resume_action],
        )

    @platform_process(
        name="set_autonomic_slot",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description=(
                    "Target session's agent_instance_id (agi-...) to bind as "
                    "the sys:autonomic holder. Must be live and registered "
                    "with provides_inference=True."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "sys:autonomic manual-set outcome: the claim action and the bound agent_instance_id."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="set_autonomic_slot outcome (sys:autonomic manual override)",
            properties={
                "action": ParameterMetadata(type=ParameterType.STRING),
                "name": ParameterMetadata(type=ParameterType.STRING),
                "agent_instance_id": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def set_autonomic_slot(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> dict[str, Any]:
        """Manually bind ``sys:autonomic`` to a session (INF-01 §D.9 override).

        The SANCTIONED manual lane for the reserved slot: ``peer_claim_role``
        rejects ``sys:*`` for user-facing callers (§6.1), so operator/manual
        rebinding goes through here. Not principal-gated — any bridge session
        may re-point the slot (override-then-resume; the auto-assignment
        triggers keep it filled afterwards). Fails fast on a target that
        cannot serve the lane (no live inference provider).
        """
        raw = params.get("parameters", params)
        agent_instance_id = str(raw.get("agent_instance_id", "")).strip()
        if not agent_instance_id:
            return _failure_result(
                code="missing_argument",
                message="set_autonomic_slot requires a non-empty 'agent_instance_id'.",
            )
        assignment = self._autonomic_assignment
        if assignment is None:
            return _failure_result(
                code="bridge.not_started",
                message="Bridge interface is not running; no autonomic lifecycle.",
            )
        outcome = assignment.set_slot(agent_instance_id=agent_instance_id)
        if not outcome.get("success"):
            return _failure_result(
                code=str(outcome.get("code") or "set_autonomic_slot_failed"),
                message=str(outcome.get("message") or "set_autonomic_slot failed"),
            )
        return _success_result(
            data={
                "action": outcome.get("action"),
                "name": outcome.get("name"),
                "agent_instance_id": outcome.get("agent_instance_id"),
            }
        )

    def resolve_role_to_instance(self, role: str) -> str | None:
        """◆R2 resolve-by-role: current instance holding ``role``, or ``None``.

        The durable ``agent_role_binding`` table (via ``peer_claim_role``) is
        the sole resolution authority — ``agent_instance_id`` is minted fresh
        per bridge launch, so the Phase-5 vertex resolver binds by role and
        maps role → current instance here at delivery time. Returns ``None``
        when the role has no binding (vacant / never claimed) or the bound
        row carries no instance id. This is the read-only reverse of the
        outbound tag write in ``platform_surface`` and reuses the same
        ``role_binding_store`` authority as ``peer_send_by_name`` — never a
        parallel resolution path.
        """
        if not role:
            return None
        state_service = self._get_state_service()
        if state_service is None:
            return None
        try:
            resolved = resolve_role_binding(state_service, role)
        except RoleBindingVacantError:
            return None
        return resolved.agent_instance_id or None

    def _register_inference_provider(
        self,
        *,
        bridge_id: str,
        agent_instance_id: str,
        agent_id: str,
        session_label: str | None,
    ) -> None:
        """Sidecar mutation: bind a provider for the (bridge, agent_instance) pair.

        Pop-then-insert semantics per v4 §4 — the post-register hook
        replaces any prior entry under the same ``agent_instance_id`` so
        a stale binding from a crashed predecessor cannot survive a
        legitimate re-register.
        """
        if self._bridge_manager is None:
            logger.warning(
                "_register_inference_provider called before start_interface; "
                "skipping for agent_instance_id=%s",
                agent_instance_id,
            )
            return
        provider = SessionInferenceProvider(
            bridge_id=bridge_id,
            agent_instance_id=agent_instance_id,
            agent_id=agent_id,
            session_label=session_label,
            bridge_manager=self._bridge_manager,
        )
        with self._inference_providers_lock:
            self._inference_providers.pop(agent_instance_id, None)
            self._inference_providers[agent_instance_id] = provider
            # Live again — drop any disconnected tombstone for this instance.
            self._inference_provider_tombstones.pop(agent_instance_id, None)

    def _clear_inference_providers_for_bridge(
        self,
        bridge_id: str,
    ) -> int:
        """Sidecar cleanup: drop every provider tied to ``bridge_id``.

        Caller MUST snapshot the per-bridge bindings via
        :meth:`PeerRegistry.list_by_bridge` BEFORE invoking
        :meth:`PeerRegistry.unregister`; this method walks that snapshot
        and removes any matching sidecar entry. Silent no-op for
        agent_instance_ids that have no provider (streamable peers,
        stdio peers that did not pass ``provides_inference=True``).
        """
        if self._peer_registry is None:
            return 0
        bindings = self._peer_registry.list_by_bridge(bridge_id)
        cleared = 0
        with self._inference_providers_lock:
            for binding in bindings:
                if self._inference_providers.pop(binding.agent_instance_id, None) is not None:
                    cleared += 1
                    # ◆R2 case 3b: tombstone the disconnected instance so the
                    # resolver DEFERs (not silent-Qwen) a flow explicitly bound
                    # to it. LRU-evict the oldest tombstone past the cap.
                    self._inference_provider_tombstones.pop(
                        binding.agent_instance_id,
                        None,
                    )
                    self._inference_provider_tombstones[binding.agent_instance_id] = None
                    while len(self._inference_provider_tombstones) > _INFERENCE_TOMBSTONE_CAP:
                        evicted, _ = self._inference_provider_tombstones.popitem(last=False)
                        # N1 (Rev-C): LOUD eviction — a roleless bound instance
                        # aging out means its stale flows now route DEFAULT.
                        logger.warning(
                            "inference-provider tombstone evicted roleless bound "
                            "instance %s (cap=%d): its stale in-flight flows will "
                            "now route DEFAULT (default model), not DEFER. "
                            "Role-bound sessions are immune (◆R2 durable path). "
                            "Claim a role to get durable vertex protection.",
                            evicted,
                            _INFERENCE_TOMBSTONE_CAP,
                        )
        return cleared

    def _full_bridge_cleanup(self, bridge_id: str) -> int:
        """REL-09: the sweeper's per-bridge cleanup — identical to the close route.

        Sidecar clear (+ ◆R2 tombstone) → sys:autonomic Trigger-2 hook →
        registry unregister, all keyed on ``bridge_id``. Returns rows removed.
        """
        peer_registry = self._peer_registry
        if peer_registry is None:
            return 0
        return run_full_bridge_cleanup(
            bridge_id,
            inference_provider_clear=self._clear_inference_providers_for_bridge,
            autonomic_on_close=(
                self._autonomic_assignment.on_bridge_close
                if self._autonomic_assignment is not None
                else None
            ),
            unregister=peer_registry.unregister,
        )

    def _on_sweep_tick(self) -> None:
        """Composed REL-09 sweeper on_tick rider: INF-02 serve-timeout sweep +
        INF-06 forwarded-vertex re-drive + terminal-row GC + D1 session sweep.

        Each rider is fault-isolated so one fault never skips the rest of the
        tick: the INF-02 sweep can raise, so it is wrapped HERE (the sweeper's
        single outer guard would otherwise abort the tick before later riders
        run); the two INF-06 riders self-isolate (internal try/except → never
        raise, return counts) so they are called directly. Every rider runs
        every tick.

        A4 (2026-08-04): the REL-05 deaf-wake escalation rider (DirectWakeReconciler)
        retired here — sweep_overdue_sessions + _notify_steward_of_overdue
        (session_sweep.py, D1) is its sole successor, keyed off the recipient's
        own report_by promise instead of a message-level heuristic.
        """
        autonomic = self._autonomic_assignment
        if autonomic is not None:
            try:
                autonomic.completions.sweep_serve_timeouts()
            except Exception:  # noqa: BLE001 — one rider's fault must not skip the other
                logger.exception(
                    "serve-timeout sweep rider FAULTED; continuing",
                )
            # INF-06 reliability: re-drive forwarded vertices whose holder died /
            # timed out, then reap aged terminal 'failed' rows. Both self-isolate.
            autonomic.forwarded.sweep_serve_timeouts()
            autonomic.forwarded.gc_terminal_rows()
        try:
            self._run_session_lifecycle_sweep()
        except Exception:  # noqa: BLE001 — one rider's fault must not skip the others
            logger.exception("D1 session-lifecycle sweep FAULTED; sweeper continues")
        try:
            self._run_rotation_surface_sweep()
        except Exception:  # noqa: BLE001 — one rider's fault must not skip the others
            logger.exception("L4 rotation-surface sweep FAULTED; sweeper continues")

    def _run_rotation_surface_sweep(self) -> None:
        """L4 rotation-surface rider — the two notice legs that watch the
        context gauge rather than the lifecycle ledger.

        Separate from :meth:`_run_session_lifecycle_sweep` and separately
        fault-isolated, because they answer a different question from a
        different table: the D1 sweep reads ``managed_session`` deadlines and
        MUTATES state (marking rows overdue, firing dependency edges), while
        these two only READ (``session_context_status`` beside the lifecycle
        row) and notify. A fault in a read-only notice leg must not cost the
        tick its overdue marking, and vice versa.

        ★ Composition is what makes L4 LIVE. Both legs landed unreachable
        (a0a517afb / merge e883d3158): tested, and invoked by nothing, so they
        ran never in production. Until this rider existed, "L4a delivers
        notices" was a statement about capability, not about behaviour.

        Both legs are latched (see :class:`NoticeLatch`) because neither
        condition is an edge — a rotation-due session stays rotation-due until
        it rotates, and a dark session stays dark until someone fixes it, so
        unlatched they would re-deliver every tick for the duration.

        ★ THE SEAT IS NOW REACHED — BY THE THIRD LEG, AND ONLY BY IT. This
        paragraph used to say the seat was not reachable from this rider at
        all, and until 2026-08-17 that was true: the first two legs enumerate
        ``managed_session``, which structurally has no row for an
        operator-launched seat, and they notify a STEWARD resolved from
        ``spawned_by_instance_id``, which a seat does not have either. Both
        remain true OF THOSE TWO LEGS — do not read this correction as making
        them seat-capable.

        What changed is that ``sweep_rotation_self_notice`` scans
        ``session_context_status`` directly (no FK to the lifecycle ledger, so
        ``host=operator`` rows are representable) and appends to the measured
        session's OWN bridge instead of a steward's. The seat's
        ``UserPromptSubmit`` marker path still exists and is still the right
        surface for a session an operator is actively typing at; it is not a
        second detection, and it is not a substitute for this leg. It fires on
        the operator's prompt, so it is silent during exactly the autonomous
        runs in which context grows — which is the gap the third leg closes and
        the reason it delivers on the tick instead.
        """
        state_service = self._get_state_service()
        if state_service is None:
            return
        # ★ PER-LEG FAULT ISOLATION (added 2026-08-17 with the third leg).
        # Until then the three legs shared one fault domain: the rider as a
        # whole is wrapped by `_on_sweep_tick`, but nothing stood between one
        # leg and the next, so a fault in the FIRST leg skipped every leg after
        # it for that tick — silently, since the tick's handler logs the rider
        # and not the leg. Found by the third leg's own reachability guard,
        # which drives this method with the steward legs raising and asserts
        # the self-notice leg still runs; it did not.
        #
        # This widens isolation for the two pre-existing legs as well as the
        # new one. That is deliberate rather than incidental: these are three
        # independent read-only notices about three different conditions, and
        # there is no reading of this rider's purpose on which one leg's fault
        # should cost another leg its delivery.
        # ★ GAU-02: every leg records its own outcome here, healthy or not, so
        # the rider can emit ONE all-clear line at the end naming which legs
        # actually ran. Before this the rider logged only on faults and only on
        # non-zero results, which made a healthy quiet tick byte-identical to a
        # rider that never ran — and "never ran" is the reading a reader
        # defaults to. Collected as text rather than counts because a FAULTED
        # leg has no count to report and must still appear in the line; a leg
        # missing from it would otherwise read as healthy-and-quiet.
        legs: list[str] = []
        # ★ GAU-25, 2026-08-19: EVERY LEG BELOW REPORTS DETECTIONS AND
        # DELIVERIES AS SEPARATE NUMBERS, and none of them may collapse back
        # into one. Until this change each leg printed a single count, and that
        # count was DELIVERIES -- so the tick that caught a real arrested gauge
        # printed "L4d=0" while a durable gauge_notice_record for the same tick
        # carried delivery_outcome=no_steward_binding. An operator reading
        # "L4d=0" could not distinguish "the detector found nothing" from "the
        # detector found something and nobody could be told", which are opposite
        # situations with opposite remedies. The sink each leg is handed here is
        # what makes the difference printable; see StewardNoticeCounts.
        #
        # THE LAMBDAS BELOW RETURN `actionable` (delivered + undelivered), NOT
        # the sweep's int return. That is deliberate and it fixes the SAME
        # defect one level up: _run_counted_leg gates its on_finding warning on
        # the value returned here, so returning the delivered count would leave
        # a tick with detections and zero deliveries emitting no warning at all.
        # Returning `detected` instead would warn every tick for the whole of a
        # long outage, which is what each leg's latch exists to prevent.
        # TRACKED DEBT (GAU-25): the helper itself still carries the defect for
        # any FUTURE leg whose author does not read this comment. Not fixed here
        # -- _run_counted_leg is shared code and this landed mid-wave with three
        # lanes holding surgical leases on this file.
        due_counts = StewardNoticeCounts()
        _run_counted_leg(
            legs,
            "L4a",
            "rotation-due",
            lambda: (
                sweep_rotation_due_sessions(
                    state_service,
                    peer_registry=self._peer_registry,
                    bridge_manager=self._bridge_manager,
                    latch=self._rotation_due_latch,
                    counts=due_counts,
                ),
                due_counts.actionable,
            )[1],
            lambda _n: (
                f"{due_counts.detected} detected"
                f"({due_counts.delivered} delivered/"
                f"{due_counts.undelivered} undelivered) session(s) due to rotate"
            ),
            lambda _n: logger.warning(
                "L4 sweep: %d session(s) due to rotate; %d steward(s) notified, "
                "%d NOT reached — an undelivered rotation notice is an alarm "
                "nobody received, not a quiet tick",
                due_counts.detected,
                due_counts.delivered,
                due_counts.undelivered,
            ),
        )
        dark_counts = StewardNoticeCounts()
        _run_counted_leg(
            legs,
            "L4b",
            "gauge-coverage",
            lambda: (
                sweep_gauge_coverage(
                    state_service,
                    peer_registry=self._peer_registry,
                    bridge_manager=self._bridge_manager,
                    latch=self._gauge_coverage_latch,
                    counts=dark_counts,
                ),
                dark_counts.actionable,
            )[1],
            lambda _n: (
                f"{dark_counts.detected} detected"
                f"({dark_counts.delivered} delivered/"
                f"{dark_counts.undelivered} undelivered) session(s) with no gauge row"
            ),
            # ★ GAU-13(b), the SAME overclaim one level up. This line used
            # to name a "likeliest cause" the sweep never measured, exactly
            # as the per-session notice did. The notice now states what it
            # measured and diagnoses only as far as the report_alive
            # evidence carries; a rider summary that kept asserting the
            # cause would have re-introduced the inference the notice just
            # dropped, on the surface a reader hits FIRST.
            lambda _n: logger.warning(
                "L4 sweep: %d LIVE session(s) past the startup grace have no "
                "context-gauge row; %d steward(s) notified, %d NOT reached; "
                "each notice names what was measured for that session and how "
                "far the evidence identifies the cause",
                dark_counts.detected,
                dark_counts.delivered,
                dark_counts.undelivered,
            ),
        )
        # L4c is fault-isolated INSIDE the rider rather than promoted to a
        # fourth rider. Its axis is this rider's axis exactly — same table,
        # read-only, same question ("is this session getting expensive") — so a
        # separate rider would force this docstring to explain why one question
        # lives in two places, which is the rationale-rot the R4 rider's
        # docstring warns about. What a shared rider does NOT give away for
        # free is isolation, so it is taken explicitly here: a self-notice
        # fault must not cost the two steward legs their tick, or vice versa.
        try:
            counts = sweep_rotation_self_notice(
                state_service,
                peer_registry=self._peer_registry,
                bridge_manager=self._bridge_manager,
                # GAU-06 (G2): the leg's DURABLE half. This one argument is
                # what the whole item waited on -- nothing else in this rider
                # or in session_sweep holds a messaging-service reference, so
                # the self-notice could not persist anything until the supplier
                # was wired from here.
                agent_messaging_service=self._require_service(),
                latch=self._rotation_self_latch,
            )
        except Exception:  # noqa: BLE001 — one leg's fault must not skip the others
            logger.exception("L4c self-notice leg FAULTED; rider continues")
            legs.append("L4c=FAULTED")
        else:
            legs.append(
                f"L4c={counts.appended} appended"
                f"({counts.watcher_held} watcher-held)/"
                f"{counts.unroutable} unroutable/"
                f"{counts.undeliverable} undeliverable/"
                f"{counts.gauge_silent} gauge-silent",
            )
            self._log_self_notice_counts(counts)
        # L4d (GAU-01(b)): the gauge row that STOPPED, as distinct from the one
        # never written. Fault-isolated like every sibling. It is a FOURTH leg
        # rather than a branch inside L4b because the two produce different
        # counts and different remedies -- folding them would leave the reader
        # unable to tell "N sessions never reported" from "N sessions stopped
        # reporting", which are opposite operational situations.
        # ★ GAU-25's FILED SPECIMEN IS THIS LEG. On 2026-08-19 17:47:33Z this
        # line printed "L4d=0 session(s) with an arrested gauge row" on the tick
        # that emitted a gauge_stale_notice with observed_s 1031.8 against
        # threshold_s 900.0 and delivery_outcome=no_steward_binding. The
        # detection was real, the delivery failed, and the count that reached
        # the operator was the DELIVERY count. Both numbers now print.
        stale_counts = StewardNoticeCounts()
        _run_counted_leg(
            legs,
            "L4d",
            "gauge-staleness",
            lambda: (
                sweep_gauge_staleness(
                    state_service,
                    peer_registry=self._peer_registry,
                    bridge_manager=self._bridge_manager,
                    latch=self._gauge_stale_latch,
                    counts=stale_counts,
                ),
                stale_counts.actionable,
            )[1],
            lambda _n: (
                f"{stale_counts.detected} detected"
                f"({stale_counts.delivered} delivered/"
                f"{stale_counts.undelivered} undelivered) session(s) with an "
                f"arrested gauge row"
            ),
            lambda _n: logger.warning(
                "L4 sweep: %d LIVE session(s) are still reporting while their "
                "context-gauge row has stopped advancing; %d steward(s) "
                "notified, %d NOT reached; each notice carries the two "
                "timestamps it compared and the gap between them",
                stale_counts.detected,
                stale_counts.delivered,
                stale_counts.undelivered,
            ),
        )
        # ★ The rider's REACHABLE ALL-CLEAR. Emitted unconditionally, after
        # every leg, INCLUDING the all-zero tick that is the normal overnight
        # case — that is the whole point. "Did the rotation surface sweep?" was
        # unanswerable from the logs for twenty minutes during the 2026-08-18
        # post-deploy verification, because a rider that never ran, one that ran
        # and found nobody, and one whose every leg was healthy all produced
        # byte-identical silence, and the discriminator had to be found outside
        # the instrument entirely. INFO, not DEBUG: a signal only reachable by
        # raising the log level is not reachable on the tick a reader is
        # actually asking about.
        logger.info("rotation surface swept: %s", "; ".join(legs))

    @staticmethod
    def _log_self_notice_counts(counts: SelfNoticeCounts) -> None:
        """ALL FIVE counts, always, and never one without the others.

        ``appended`` replaced ``notified`` on 2026-08-19 (GAU-06 G1) because the
        old word claimed something this leg cannot observe: whether anybody read
        it. What it can assert is that the durable row was accepted.
        ``watcher_held`` is a SUBSET of ``appended``, printed beside it because
        the two have different delivery stories and one averaged number
        supported neither.

        A count of failures alone reads identically to a healthy run, and a
        count of successes alone hides the leg's own blind spot. A zero that
        prints its denominator is the only kind a reader can trust.

        The two failure counts are reported SEPARATELY because they have
        different causes and different owners: `unroutable` is the known
        watch-id join gap (a worker's gauge row is keyed on its ledger id while
        its binding is keyed on its watch id), while `undeliverable` is a live
        binding whose append raised — a transport fault, already logged at
        WARNING with a traceback. Naming only the first, as an earlier draft
        did, would have attributed every delivery fault to the join gap.
        """
        if not (
            counts.appended or counts.unroutable or counts.undeliverable or counts.gauge_silent
        ):
            # ★ GAU-02, the half found first. This used to `return` and log
            # NOTHING, which made a healthy leg that found nobody identical in
            # the log to a leg that never ran. All-zero is not an edge case
            # here: SELF_NOTICE_STALENESS_S excludes any session quiet for an
            # hour, so it is the normal overnight state of a small fleet. The
            # line states the denominators rather than merely asserting health,
            # for the same reason the non-zero line below names all three
            # counts — a zero that prints what it counted is the only kind a
            # reader can trust.
            logger.info(
                "L4c sweep: 0 session(s) appended (0 watcher-held), 0 unroutable, "
                "0 undeliverable, 0 gauge-silent "
                "— the leg RAN and had no eligible subject (every gauge row was "
                "stale, below band, or already latched for this episode); this "
                "is the expected quiet-fleet result, not a failure",
            )
            return
        logger.info(
            "L4c sweep: %d session(s) had a durable notice of their own context "
            "band APPENDED to their inbox, %d of them watcher-held (a subset, "
            "not an extra population — those surface when the session next "
            "looks, and no turn starts either way); "
            "%d unroutable (gauge row present, and neither its own key nor its "
            "stored agent_session_id join resolves to a live binding — since "
            "2026-08-18 this means a row written by a reporter predating the "
            "join column, and it should decay to 0 as reporters upgrade); "
            "%d undeliverable (binding resolved, the durable write raised — see "
            "the WARNING above for each); %d gauge-silent since registration "
            "(detected before their first context-gauge row landed)",
            counts.appended,
            counts.watcher_held,
            counts.unroutable,
            counts.undeliverable,
            counts.gauge_silent,
        )

    def _run_session_lifecycle_sweep(self) -> None:
        """D1 platform sweep rider (§3.4/§6 rule 3, Architect ratification #3):
        marks overdue sessions (+ best-effort steward notice, D2-lane-tail
        follow-up #3), fires+delivers armed 'deadline'/'lane_closed'
        dependency edges, and prunes stale session_role_claim rows. See
        ``session_sweep.py``'s module docstring for the exact scope boundary
        ('session_terminal' firing stays retire_session's job)."""
        state_service = self._get_state_service()
        # peer_registry/bridge_manager are OPTIONAL on this call (unlike the
        # two dependency sweeps below, which require them and are gated by
        # the `if` below) -- sweep_overdue_sessions must still mark overdue
        # rows on an early-boot tick before the bridge service is up; it
        # just skips the notify step internally when either is None.
        overdue = sweep_overdue_sessions(
            state_service,
            peer_registry=self._peer_registry,
            bridge_manager=self._bridge_manager,
        )
        if overdue:
            logger.info("D1 sweep: marked %d session(s) overdue", overdue)
        managed = sweep_managed_dispatches(
            state_service,
            peer_registry=self._peer_registry,
            bridge_manager=self._bridge_manager,
        )
        _log_managed_dispatch_sweep(managed)
        # W4A registration watchdog — same optional-collaborator contract as
        # sweep_overdue_sessions above (mark always, notify when possible).
        # Runs BEFORE the mapping riders so a row that is about to register
        # this tick is not marked on the strength of a stale read.
        unregistered = sweep_unregistered_spawning_sessions(
            state_service,
            peer_registry=self._peer_registry,
            bridge_manager=self._bridge_manager,
        )
        if unregistered:
            logger.warning(
                "D1 sweep: marked %d session(s) registration-overdue — a worker "
                "that has not registered past its bound did not run its "
                "registration hook",
                unregistered,
            )
        _run_session_claude_mapping_riders(state_service)
        if self._peer_registry is not None and self._bridge_manager is not None:
            fired = sweep_deadline_dependencies(
                state_service,
                peer_registry=self._peer_registry,
                bridge_manager=self._bridge_manager,
            )
            if fired:
                logger.info("D1 sweep: fired %d 'deadline' dependency edge(s)", fired)
            lane_fired = sweep_lane_closed_dependencies(
                state_service,
                peer_registry=self._peer_registry,
                bridge_manager=self._bridge_manager,
            )
            if lane_fired:
                logger.info("D1 sweep: fired %d 'lane_closed' dependency edge(s)", lane_fired)
            if self._session_role_claim_pruner is not None:
                pruned = self._session_role_claim_pruner.sweep(
                    state_service,
                    peer_registry=self._peer_registry,
                )
                if pruned:
                    logger.info("D1 sweep: pruned %d stale session_role_claim row(s)", pruned)

    def was_inference_provider_bound(self, agent_instance_id: str) -> bool:
        """◆R2 case 3b: True if ``agent_instance_id`` held a provider earlier
        in this process lifetime but its bridge has since disconnected.

        Lets the vertex resolver distinguish an *explicitly-bound-but-absent*
        roleless session (→ DEFER, never silent-Qwen) from a never-bound /
        post-restart / streamable instance (→ DEFAULT).

        GOVERNING-RULE SCOPE (N1, Rev-C ruling 2026-07-02) — the precise
        limit of "never silent-Qwen anything explicitly bound this lifetime":
        - For ROLE-BOUND sessions the rule holds ABSOLUTELY — the ◆R2 durable
          ``agent_role_binding`` path (``resolve_role_to_instance``) never
          returns DEFAULT, so a role-bound flow always PROVIDER-or-DEFERs
          regardless of this tombstone.
        - For ROLELESS sessions the rule holds WITHIN tombstone capacity
          (``_INFERENCE_TOMBSTONE_CAP``): a roleless instance is LRU-evicted
          after that many subsequent inference-provider disconnects, after
          which its stale flows degrade to DEFAULT. This is the principled,
          leak-free tradeoff — a roleless session has no durable identity, and
          "never silent-Qwen a bound session" vs "never-bound/streamable MUST
          go DEFAULT" (R6 / D-IF11) are irreconcilable under bounded memory.
          Eviction is LOUD (WARNING) so the rare aged-out case is visible;
          claiming a role is the escape from the bound. Loud eviction is
          OBSERVABILITY, not a routing fix.
        """
        with self._inference_providers_lock:
            return agent_instance_id in self._inference_provider_tombstones

    def _router_is_declared(self) -> bool:
        """True if this solet's active manifest declares the router.

        D11 ruling R1: the "router present" predicate is MANIFEST-DECLARED,
        never runtime-probed (live plugin registry start-order is a race;
        port/pid probing is defensive and stale-file-prone). Both colors of
        a router topology see the identical declared set during a swap, so
        this predicate agrees for both — the load-bearing property that
        keeps router topology's "neither child ever writes" invariant intact.

        Fails loud (never guesses) when the declared set cannot be
        determined — an absent ``orchestrator_ref``/``APP_HOME`` or an
        absent manifest.yaml (``load_manifest_plugin_set`` returns
        ``None``, its "no gating" sentinel) means we cannot tell whether
        the router is in this solet's topology, and D11's routerless
        write path must never guess.
        """
        orchestrator = getattr(self, "orchestrator_ref", None)
        if orchestrator is None:
            raise RuntimeError(
                f"{self.name}: orchestrator_ref not injected at start_interface "
                "— cannot resolve the D11 router-presence predicate",
            )
        app_home = getattr(orchestrator, "APP_HOME", None)
        if app_home is None:
            raise RuntimeError(
                f"{self.name}: orchestrator.APP_HOME unavailable at "
                "start_interface — cannot resolve the D11 router-presence "
                "predicate",
            )
        declared_plugins = load_manifest_plugin_set(app_home)
        if declared_plugins is None:
            raise RuntimeError(
                f"{self.name}: {app_home}/config/manifest.yaml is absent — "
                "the D11 router-presence predicate cannot be determined "
                "(never guessed). A solet without a manifest must "
                "still declare its topology before the bridge can decide "
                "whether it owns its own port-discovery file.",
            )
        return _ROUTER_PLUGIN_NAME in declared_plugins

    def _build_peer_registry(self) -> PeerRegistry:
        """Wire a :class:`PeerRegistry` over the platform persistent backend.

        PeerRegistry's persistent backend depends on a vault plugin having
        imported its ``postgres_backend`` module before this point (see
        peer_registry.py + 2026-06-01 reconnect-UX design §4). vault's
        ``prepare_for_readiness`` runs earlier, so the ``"postgres"``
        backend factory is registered by the time ``start_interface``
        fires. Raises ``RuntimeError`` if the orchestrator or
        ``state_service`` is missing — neither is recoverable mid-startup.
        """
        orchestrator = getattr(self, "orchestrator_ref", None)
        if orchestrator is None:
            raise RuntimeError(
                f"{self.name}: orchestrator_ref not injected at start_interface",
            )
        state_service = orchestrator.get_service("state_service")
        if state_service is None:
            raise RuntimeError(
                f"{self.name}: state_service unavailable at start_interface — "
                "PeerRegistry persistence cannot initialize",
            )
        peer_registry = PeerRegistry(state_service=state_service)
        peer_registry.register_native_wake_adapter("claude_code", self)
        return peer_registry

    @platform_process(
        name="start_interface",
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        parameters={},
        output_type="object",
        output_description="Bridge API startup confirmation.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Bridge API host, port, and endpoint prefix.",
            properties={
                "host": ParameterMetadata(
                    type=ParameterType.STRING,
                    required=True,
                ),
                "port": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    required=True,
                ),
                "bridge_url": ParameterMetadata(
                    type=ParameterType.STRING,
                    required=True,
                ),
                "started_at": ParameterMetadata(
                    type=ParameterType.STRING,
                    required=True,
                ),
            },
        ),
    )
    def start_interface(
        self,
        params: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
        state: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> dict[str, Any]:
        if self._server_thread is not None and self._server_thread.is_alive():
            return _failure_result(
                code="bridge.already_running",
                message="Bridge API server is already running",
            )
        bridge_config = self._build_bridge_runtime_config()
        self._host = bridge_config.host
        self._max_message_chars = bridge_config.max_message_chars
        self._bridge_manager = BridgeSessionManager(
            session_id_factory=self._mint_session_id,
            idle_timeout_s=bridge_config.bridge_idle_timeout_seconds,
            max_pending_events=bridge_config.max_pending_events,
            long_poll_timeout_s=bridge_config.long_poll_timeout_seconds,
            binding_liveness_window_s=bridge_config.binding_liveness_window_seconds,
            # M5 §14.4: lazy resolver — vault + ledger may not be live at
            # bridge-construction time (startup ordering); the closure
            # looks them up at session-open time via orchestrator.
            policy_resolver=self._resolve_oauth_session_policy,
        )
        self._peer_registry = self._build_peer_registry()
        # REL-09 startup reconciliation: at boot ZERO live bridges exist, so
        # every persisted peer_binding row is a pre-restart zombie (SIGTERM
        # swaps never close bridges). Purge them; live sessions re-register
        # on reconnect within seconds.
        purge_preboot_bindings(self._peer_registry)
        # ONE-SHOT startup backfills (Control #2 agent_role_binding cutover seed
        # + GAP-2 agent_message important-column projection) BEFORE the FastAPI
        # surface comes up: every legacy role RESOLVES at cutover (a send queues
        # for replay instead of rejecting), and the silent peer-inbox stays
        # correct after the read-path cutover. Services are initialized by the
        # time start_interface runs, so they are resolvable here.
        self._run_startup_backfills()
        # INF-01 sub-slice-2: the sys:autonomic lifecycle rides the live
        # bridge collaborators; its Trigger-1/2 hooks are handed to
        # register_routes below (seam §b: this plugin owns the hook bodies).
        self._autonomic_assignment = AutonomicAssignment(
            state_service=self._get_state_service,
            list_active_bridges=self._bridge_manager.list_active,
            bindings_for_bridge=self._peer_registry.list_by_bridge,
            live_binding_for_session=self._peer_registry.resolve_by_agent_session_id,
            has_live_provider=self._has_live_inference_provider,
            send_notice=self._send_handover_notice,
            grace_seconds=bridge_config.autonomic_grace_seconds,
            forward_completion=self._forward_completion_request,
            serve_window_seconds=bridge_config.completion_serve_window_seconds,
            resubmit_vertex=self._resubmit_vertex,
            forward_serve_window_seconds=bridge_config.forward_serve_window_seconds,
            forward_attempts_cap=bridge_config.forward_attempts_cap,
            terminal_gc_after_seconds=bridge_config.terminal_gc_after_seconds,
        )
        # D1 §3.4/§2 rule 4: the session-role-claim staleness pruner (Architect
        # ratification #3) rides the on_tick cadence — stateful (grace-window
        # tracking), so it is constructed once and held.
        self._session_role_claim_pruner = SessionRoleClaimPruner(
            clock=lambda: datetime.now(UTC),
        )
        # REL-09: drive the idle sweep — every expired bridge gets the SAME
        # full cleanup the close route runs (sidecar + tombstone + Trigger-2
        # + registry unregister), so swept and closed are indistinguishable.
        # INF-02 + REL-05 ride the same cadence via _on_sweep_tick: each tick
        # runs the completion serve-timeout sweep AND the deaf-wake escalation,
        # each fault-isolated so one failing does not skip the other.
        sweeper = BridgeLifecycleSweeper(
            bridge_manager=self._bridge_manager,
            cleanup=self._full_bridge_cleanup,
            interval_seconds=bridge_config.bridge_sweep_interval_seconds,
            on_tick=self._on_sweep_tick,
        )
        self._bridge_sweeper = sweeper
        sweeper.start()
        self._platform_surface = self._build_platform_surface(
            bridge_manager=self._bridge_manager,
            bridge_config=bridge_config,
        )
        self._app = self._build_fastapi_app(
            bridge_manager=self._bridge_manager,
            peer_registry=self._peer_registry,
            platform_surface=self._platform_surface,
            bridge_config=bridge_config,
        )
        if bridge_config.streamable_enabled:
            self._mount_streamable_transport(
                app=self._app,
                bridge_manager=self._bridge_manager,
                peer_registry=self._peer_registry,
                platform_surface=self._platform_surface,
                bridge_config=bridge_config,
            )
        # Bridge port is in-process only — no file write per Slice 3 of
        # the bridge-port-routing design. The macos_self_deployment_plugin
        # heartbeat reads the bound port from ``self.bridge_port`` via
        # cross-plugin lookup and passes it to ``router.register_color``.
        self._port = find_available_port(preferred=bridge_config.port)
        self._server_started_event.clear()
        self._server_thread = threading.Thread(
            target=self._run_server,
            name=f"{PLUGIN_NAME}-server",
            daemon=True,
        )
        self._server_thread.start()
        if not self._server_started_event.wait(timeout=_SERVER_START_TIMEOUT_S):
            self._shutdown_server()
            return _failure_result(
                code="bridge.startup_failed",
                message=(
                    f"Bridge API server did not signal startup within {_SERVER_START_TIMEOUT_S}s"
                ),
            )
        # D11 (workbench/2026-07-13_d11_bridge_port_discovery_routerless_ruling.md):
        # in router-less topology this plugin IS the bridge's front door, so
        # it is the sanctioned writer of its own discovery file — never in
        # router topology (R4), only after bind is confirmed above (R3),
        # rewritten on every start to self-heal port re-roll staleness (R3).
        if not self._router_is_declared():
            write_routerless_bridge_port_file(self._port)
        if bridge_config.streamable_enabled:
            streamable_failure = self._start_streamable_server(bridge_config)
            if streamable_failure is not None:
                self._shutdown_server()
                return streamable_failure
        started_at = _now_iso()
        bridge_url = f"http://{self._host}:{self._port}"
        result_data: dict[str, Any] = {
            "host": self._host,
            "port": self._port,
            "bridge_url": bridge_url,
            "started_at": started_at,
        }
        if bridge_config.streamable_enabled:
            result_data["streamable_url"] = (
                f"http://{self._streamable_host}:{self._streamable_port}/api/v1/mcp/streamable"
            )
        logger.info(
            "%s: bridge API started on %s:%s%s",
            self.name,
            self._host,
            self._port,
            (
                f" + streamable HTTP MCP on {self._streamable_host}:{self._streamable_port}"
                if bridge_config.streamable_enabled
                else ""
            ),
        )
        return _success_result(data=result_data)

    @platform_process(
        name="stop_interface",
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        parameters={},
        output_type="object",
        output_description="Bridge API shutdown confirmation.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Shutdown confirmation.",
            properties={
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    required=True,
                ),
            },
        ),
    )
    def stop_interface(
        self,
        params: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
        state: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> dict[str, Any]:
        self._shutdown_server()
        return _success_result(data={"status": "stopped"})

    def _is_full_surface_ready(self) -> bool:
        """Are both uvicorn listeners bound and ready to serve?

        Used by ``/api/v1/bridge/health`` to gate the 200-vs-503 answer
        when streamable transport is enabled. The bridge uvicorn signals
        readiness via ``_server_started_event``; the streamable uvicorn
        signals via ``_streamable_server_started_event``. Cloud ALB
        target-group health checks probe ``/api/v1/bridge/health`` on
        the streamable port (9000); honest readiness means the smoke /
        connector that uses health=200 as the OAuth-ready gate doesn't
        race against an in-flight ``_start_streamable_server`` (see
        ``workbench/2026-06-12_aws_swap_smoke_run_report.md`` §3 Bug 2,
        iter 9 — vince's slow boot exposed the race for the first time).

        Only wired in when ``streamable_enabled=True``; local dev mode
        keeps the unconditional-200 contract because no streamable
        listener exists to race against.
        """
        return (
            self._server_started_event.is_set() and self._streamable_server_started_event.is_set()
        )

    # ------------------------------------------------------------------
    # IO interface — post_message
    # ------------------------------------------------------------------

    @platform_process(
        name="post_message",
        context_handling=ContextHandling.SESSION_AWARE,
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        parameters={
            "message": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="Message body to deliver to the bound MCP session.",
            ),
        },
        output_type="object",
        output_description="Message queueing confirmation.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.DICT,
            description="Contains message queueing confirmation.",
            properties={
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description=(
                        "'queued' when appended, or 'dropped_bridge_gone' "
                        "when the originating bridge has already closed."
                    ),
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True,
        ),
        requires_result_processor=False,
    )
    def post_message(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        # Text-only channel — silently strip attachment hints.
        params.pop("attachments", None)
        params.pop("job_result_ref", None)
        session_id = state.get("session_id") or params.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return _failure_result(
                code=_ERR_SESSION_NOT_BOUND,
                message="session_id missing from state",
            )
        manager = self._bridge_manager
        if manager is None:
            return _failure_result(
                code=_ERR_SESSION_NOT_BOUND,
                message="bridge interface is not started",
            )
        bridge = _find_bridge_by_session(manager, session_id)
        if bridge is None:
            return _failure_result(
                code=_ERR_SESSION_NOT_BOUND,
                message=f"No active bridge for session {session_id}",
            )
        message = _extract_message(params)
        if len(message) > self._max_message_chars:
            return _failure_result(
                code=_ERR_VALIDATION,
                message=f"Message exceeds {self._max_message_chars} char limit",
            )
        try:
            manager.append_event(
                bridge.bridge_id,
                "post_message",
                message,
                meta={
                    "flow_id": state.get("flow_id"),
                    "session_id": session_id,
                },
            )
        except BridgeNotFoundError:
            return _failure_result(
                code=_ERR_SESSION_NOT_BOUND,
                message=f"Bridge {bridge.bridge_id} is closed or missing",
            )
        except BridgeQueueFullError:
            return _failure_result(
                code="APIError",
                message="Bridge event queue is full",
            )
        if self._memory_service is not None:
            self._memory_service.store_interaction(
                session_id=session_id,
                source_namespace=PLUGIN_NAME,
                event_type="assistant_response",
                content=message,
                metadata={"source_namespace": PLUGIN_NAME},
            )
        return _success_result(data={"status": "queued"})

    # ------------------------------------------------------------------
    # Bridge delivery — deliver_result / deliver_error EDGE_SINK pair
    # ------------------------------------------------------------------

    @platform_process(
        name="deliver_result",
        context_handling=ContextHandling.SESSION_AWARE,
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        parameters={
            "result_payload": ParameterMetadata(
                type=ParameterType.DICT,
                required=True,
                description=(
                    "Raw structured result payload to deliver to the originating bridge channel."
                ),
            ),
            "source_process_key": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=(
                    "Process key of the action whose result is being "
                    "delivered (informational; surfaced to the MCP caller)."
                ),
            ),
            "bridge_id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ID of the originating bridge session.",
            ),
        },
        output_type="object",
        output_description="Delivery confirmation.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.DICT,
            description="Contains delivery confirmation.",
            properties={
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description=(
                        "'queued' when appended, or 'dropped_bridge_gone' "
                        "when the originating bridge has already closed."
                    ),
                ),
            },
        ),
        requires_result_processor=False,
    )
    def deliver_result(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        return self._deliver_payload(
            event_type="bridge_delivery_result",
            payload_key="result_payload",
            params=params,
            state=state,
        )

    @platform_process(
        name="deliver_error",
        context_handling=ContextHandling.SESSION_AWARE,
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        parameters={
            "error_payload": ParameterMetadata(
                type=ParameterType.DICT,
                required=True,
                description=(
                    "Raw structured error payload to deliver to the originating bridge channel."
                ),
            ),
            "source_process_key": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=("Process key of the action whose failure is being delivered."),
            ),
            "bridge_id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ID of the originating bridge session.",
            ),
        },
        output_type="object",
        output_description="Delivery confirmation.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.DICT,
            description="Contains delivery confirmation.",
            properties={
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="'queued' on success.",
                ),
            },
        ),
        requires_result_processor=False,
    )
    def deliver_error(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        return self._deliver_payload(
            event_type="bridge_delivery_error",
            payload_key="error_payload",
            params=params,
            state=state,
        )

    # ------------------------------------------------------------------
    # NativeWakeAdapter — claude_code self-wake
    # ------------------------------------------------------------------

    def wake(
        self,
        *,
        recipient_parent_pid: int | None,
        delivered_prose: str,
        sender_agent_id: str,
        sender_agent_instance_id: str,
        sender_session_label: str,
        thread_id: str,
        message_id: str,
        reply_to_role: str = "",
        sender_agent_session_id: str = "",
        delivery_meta: Mapping[str, object] | None = None,
    ) -> str:
        """Implement NativeWakeAdapter for agent_id='claude_code'.

        Pairs the recipient's bridge by ``parent_pid`` and appends a
        post_message event carrying an envelope that embeds the sender's
        identity (native channel is text-only — no meta field rides
        along, so the receiver reconstructs targeted-reply args from the
        prose).  ``delivery_meta`` (v10 Control #5 / Q3-revised) is merged onto
        the bridge-event meta — for a role send it carries the role keys so the
        holder's forwarder confirms delivery (this wake is the SAME bridge queue
        as queued_notification, NOT a direct push). Raises on failure — the loop-prevention
        contract treats IMPORTANT delivery as a hard promise, so silent drops
        are not acceptable.
        """
        manager = self._bridge_manager
        registry = self._peer_registry
        if manager is None or registry is None:
            raise RuntimeError(
                "claude_code wake: bridge interface is not started",
            )
        if recipient_parent_pid is None:
            raise RuntimeError(
                "claude_code wake requires recipient parent_pid; the "
                "recipient bridge has no parent_pid (older client build)",
            )
        bridge = _find_claude_code_bridge_by_parent_pid(
            manager=manager,
            peer_registry=registry,
            parent_pid=recipient_parent_pid,
        )
        if bridge is None:
            raise RuntimeError(
                f"no open claude_code bridge with parent_pid={recipient_parent_pid}",
            )
        label_segment = f' "{sender_session_label}"' if sender_session_label else ""
        reply_hint = build_wake_reply_hint(
            reply_to_role=reply_to_role,
            sender_agent_id=sender_agent_id,
            sender_agent_instance_id=sender_agent_instance_id,
            thread_id=thread_id,
            message_id=message_id,
            # A2: the sender's stable session key, so a reply still resolves once
            # the instance id in this hint has rotated.
            sender_agent_session_id=sender_agent_session_id,
        )
        envelope = (
            f"[peer:{sender_agent_id}{label_segment} "
            f"instance={sender_agent_instance_id}] "
            f"{delivered_prose}\n\n"
            f"{reply_hint}"
        )
        # Synthetic flow_id keeps the bridge subprocess emitting a
        # non-empty meta.flow_id on the resulting channel notification.
        meta: dict[str, object] = {
            "flow_id": f"peer-wake-{message_id}",
            "thread_id": thread_id,
            "message_id": message_id,
        }
        # v10 Control #5: a role send merges its role keys so the holder's
        # forwarder recognises the role delivery on /events and confirms it.
        if delivery_meta:
            meta.update(delivery_meta)
        manager.append_event(
            bridge.bridge_id,
            EVENT_POST_MESSAGE,
            envelope,
            meta=meta,
        )
        manager.touch(bridge.bridge_id)
        return bridge.bridge_id

    # ------------------------------------------------------------------
    # Internals — service construction
    # ------------------------------------------------------------------

    def _require_service(self) -> AgentMessagingService:
        if self._service is None:
            self._service = self._build_service()
        return self._service

    def _build_service(self) -> AgentMessagingService:
        orchestrator = getattr(self, "orchestrator_ref", None)
        if orchestrator is None:
            raise RuntimeError(
                f"{self.name}: orchestrator_ref not injected",
            )
        state_service = orchestrator.get_service("state_service")
        if state_service is None:
            raise RuntimeError(
                f"{self.name}: state_service unavailable",
            )
        config = self._build_config()
        repository = AgentMessagingRepository(state_service)
        logger.info(
            "%s service constructed (allowed_backends=%s, max_message_bytes=%d)",
            self.name,
            list(config.allowed_backends),
            config.max_message_bytes,
        )
        return AgentMessagingService(
            repository=repository,
            state_service=state_service,
            config=config,
        )

    def _build_platform_surface(
        self,
        *,
        bridge_manager: BridgeSessionManager,
        bridge_config: _BridgeRuntimeConfig,
    ) -> PlatformSurface:
        orchestrator = getattr(self, "orchestrator_ref", None)
        if orchestrator is None:
            raise RuntimeError(
                f"{self.name}: orchestrator_ref not injected",
            )
        action_factory = getattr(self, "action_factory", None) or getattr(
            orchestrator,
            "action_factory",
            None,
        )
        flow_manager = self._flow_manager or orchestrator.get_service(
            "flow_service",
        )
        compilation_context_builder = self._compilation_context_builder or getattr(
            orchestrator, "compilation_context_builder", None
        )
        process_registry = self._resolve_process_registry(orchestrator)
        export_policy = self._build_export_policy()
        return PlatformSurface(
            action_factory=action_factory,
            flow_manager=flow_manager,
            compilation_context_builder=compilation_context_builder,
            bridge_manager=bridge_manager,
            # §34.6: the surface resolves an unregistered caller's attribution
            # key against the SAME registry peer_send_by_name routes over.
            # Built at _build_peer_registry, before this call site.
            peer_registry=self._peer_registry,
            process_registry=process_registry,
            discovery_service=orchestrator.get_service("discovery_service"),
            state_service=orchestrator.get_service("state_service"),
            blob_storage_service=orchestrator.get_service(
                "blob_storage_service",
            ),
            memory_service=self._memory_service,
            plugin_manager=getattr(orchestrator, "plugin_manager", None),
            export_policy=export_policy,
            max_message_chars=bridge_config.max_message_chars,
        )

    @staticmethod
    def _resolve_process_registry(
        orchestrator: object,
    ) -> dict[str, object] | None:
        get_registry = getattr(orchestrator, "get_process_registry", None)
        if not callable(get_registry):
            return None
        registry = get_registry()
        return registry if isinstance(registry, dict) else None

    def _build_export_policy(self) -> ProcessExportPolicy:
        provider = getattr(self, "config_provider", None)
        if provider is None:
            self._populate_config_provider_from_orchestrator()
            provider = getattr(self, "config_provider", None)
        enabled = _as_bool(
            _provider_get(provider, "process_export_enabled"),
            True,
        )
        allow = _as_str_tuple(
            _provider_get(provider, "process_export_allow_patterns"),
            default=(),
        )
        deny = _as_str_tuple(
            _provider_get(provider, "process_export_deny_patterns"),
            default=(),
        )
        promote = _as_str_tuple(
            _provider_get(provider, "process_export_promote_patterns"),
            default=(),
        )
        max_promoted = _as_int(
            _provider_get(provider, "process_export_max_promoted_tools"),
            40,
        )
        return ProcessExportPolicy(
            enabled=enabled,
            allow_patterns=allow,
            deny_patterns=deny,
            promote_patterns=promote,
            max_promoted_tools=max_promoted,
        )

    def _mint_session_id(self, solet_name: str) -> str:  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
        if self._session_manager is None:
            raise RuntimeError(
                f"{self.name}: session_manager not injected; "
                "cannot mint session_id for bridge open",
            )
        session_id = self._session_manager.create_session(
            namespace=PLUGIN_NAME,
            context_type="bridge",
        )
        return session_id

    # ------------------------------------------------------------------
    # Internals — bridge-delivery shared body
    # ------------------------------------------------------------------

    def _deliver_payload(
        self,
        *,
        event_type: str,
        payload_key: str,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        manager = self._bridge_manager
        if manager is None:
            return _failure_result(
                code=_ERR_NO_ACTIVE_BRIDGE,
                message="bridge interface is not started",
            )
        bridge_id = str(params.get("bridge_id") or "")
        if not bridge_id:
            return _failure_result(
                code=_ERR_NO_ACTIVE_BRIDGE,
                message="bridge_id missing from delivery params",
            )
        payload = params.get(payload_key)
        if not isinstance(payload, dict):
            return _failure_result(
                code=_ERR_PROCESS_CALL_FAILED,
                message=(f"{payload_key} must be a dict; got {type(payload).__name__}"),
            )
        # ``QueuedEvent.content`` is a string field; structured payloads
        # are JSON-serialized so the MCP client can decode the
        # event_type-discriminated body on receipt.
        import json  # noqa: PLC0415 — keep heavy imports local to the path

        content_json = json.dumps(
            {
                "payload": dict(payload),
                "source_process_key": str(
                    params.get("source_process_key") or "",
                ),
            },
            default=str,
        )
        try:
            manager.append_event(
                bridge_id,
                event_type,
                content_json,
                meta={
                    "flow_id": state.get("flow_id"),
                    "session_id": state.get("session_id"),
                    "source_process_key": str(
                        params.get("source_process_key") or "",
                    ),
                },
            )
        except BridgeNotFoundError:
            # A bridge-delivery action is terminal by contract. Once the
            # originating bridge is gone there is no caller left to receive
            # either this payload or an inference-formatted explanation.
            # Returning a failure here used to route the EDGE_SINK through
            # process_error, assign it to sys:autonomic, and mint a durable
            # forwarded vertex that INF-06 re-drove forever. Record the
            # irreversible transport drop loudly, then complete terminally
            # with zero continuation actions.
            logger.warning(
                "%s: dropping %s for closed or missing bridge %s (source_process_key=%s)",
                self.name,
                event_type,
                bridge_id,
                str(params.get("source_process_key") or ""),
            )
            return _success_result(data={"status": "dropped_bridge_gone"})
        except BridgeQueueFullError:
            return _failure_result(
                code=_ERR_QUEUE_FULL,
                message="Bridge event queue is full",
            )
        return _success_result(data={"status": "queued"})

    # ------------------------------------------------------------------
    # Internals — FastAPI server lifecycle
    # ------------------------------------------------------------------

    def _build_fastapi_app(
        self,
        *,
        bridge_manager: BridgeSessionManager,
        peer_registry: PeerRegistry,
        platform_surface: PlatformSurface,
        bridge_config: _BridgeRuntimeConfig,
    ) -> FastAPI:
        from fastapi import FastAPI  # noqa: PLC0415

        app = FastAPI(
            title="Solet Bridge API",
            version="1.0.0",
        )
        # REL-05: stamp last_model_activity_at on every MODEL-INITIATED bridge
        # route (never forwarder/infra — F1 keeps peer/register out) so the
        # consumption reconciler can tell a session that entered a turn from a
        # deaf one whose forwarder merely keeps polling.
        app.middleware("http")(
            make_model_activity_middleware(bridge_manager, peer_registry),
        )
        register_routes(
            app,
            bridge_manager=bridge_manager,
            peer_registry=peer_registry,
            platform_surface=platform_surface,
            agent_messaging_service=self._require_service(),
            config=bridge_config,
            state_service=self._get_state_service(),
            readiness_probe=(
                self._is_full_surface_ready if bridge_config.streamable_enabled else None
            ),
            # D-IF7 / D-IF8 sidecar wiring (v4 §4-5). Streamable transport
            # paths (mcp_streamable/{session,dispatch}.py) DO NOT receive
            # these callbacks per D-IF11 scope-out — streamable peers
            # fall back to default_inference_plugin via the wrapper's
            # None-handling path.
            inference_provider_register=self._register_inference_provider,
            inference_provider_clear=self._clear_inference_providers_for_bridge,
            # INF-01 sub-slice-2 (seam §b): Trigger-1 vacancy-fill/crash-heal
            # + Trigger-2 grace-delayed succession hook bodies. Streamable
            # paths stay scoped out with the sidecar (D-IF11).
            autonomic_on_register=(
                self._autonomic_assignment.on_register
                if self._autonomic_assignment is not None
                else None
            ),
            autonomic_on_close=(
                self._autonomic_assignment.on_bridge_close
                if self._autonomic_assignment is not None
                else None
            ),
        )
        # M5 §13.6 ONE-TIME EXCEPTION to the no-edits-to-god-file-plugins
        # Boy Scout rule: mount the session-ledger pairing routes umbrella
        # exported by session_shipper_bootstrap_plugin. ImportError fallback
        # is the explicit profile contract per §13.6 — profiles that don't
        # load the bootstrap plugin (e.g., minimal cloud test harnesses) skip
        # the mount entirely.
        try:
            from session_shipper_bootstrap_plugin.pairing_routes import (  # noqa: PLC0415
                make_pairing_ledger_facade,
                register_session_ledger_pairing_routes,
            )
        except ImportError:
            logger.info(
                "session_shipper_bootstrap_plugin not installed; skipping "
                "session-ledger pairing route mount (spec §13.6 profile contract)",
            )
            return app
        ledger_service = self._maybe_get_session_ledger_service()
        vault_registry = self._maybe_get_vault_oauth_registry()
        if ledger_service is None or vault_registry is None:
            logger.info(
                "session_ledger_service or vault_oauth_registry unavailable; "
                "skipping pairing route mount (will reattach next startup if both land)",
            )
            return app
        facade = make_pairing_ledger_facade(
            session_ledger_service=ledger_service,
            vault_oauth_registry=vault_registry,
        )
        register_session_ledger_pairing_routes(app, ledger=facade)
        # M5 §13.3: wire the service's operator-equivalent check to the
        # vault's is_operator_equivalent. This unlocks the §13.3 second
        # ownership-binding branch (operator_equivalent clients can
        # approve any pending deployment in addition to the initiator).
        ledger_service.set_operator_equivalent_check(
            vault_registry.is_operator_equivalent,
        )
        # M4 chatgpt-export + M9 claude_ai-export HTTP upload routes were
        # retired 2026-06-15 per the unified URL-walker design v3 §3.
        # The replacement is the per-plugin ``ingest_export`` EDGE verb
        # on chatgpt_export_session_source_plugin + claude_ai_export_session_source_plugin
        # invoked via ``process_call``. The session_shipper pairing routes
        # (different module, same function name) stay mounted above.
        return app

    def _oauth_client_is_operator_equivalent(self, client_id: str) -> bool:
        vault_registry = self._maybe_get_vault_oauth_registry()
        if vault_registry is None:
            return False
        try:
            return bool(vault_registry.is_operator_equivalent(client_id))
        except Exception:  # noqa: BLE001
            logger.exception(
                "policy resolver: is_operator_equivalent threw for client_id=%s",
                client_id,
            )
            return False

    def _oauth_client_is_management_client(self, client_id: str) -> bool:
        management_client_ids = set(
            self._build_bridge_runtime_config().oauth_management_client_ids,
        )
        return client_id in management_client_ids

    def _oauth_client_is_paired_shipper(self, client_id: str) -> bool:
        ledger = self._maybe_get_session_ledger_service()
        if ledger is None:
            return False
        try:
            repo = getattr(ledger, "_repository", None)
            if repo is None:
                return False
            return repo.get_deployment_by_oauth_client_id(client_id) is not None
        except Exception:  # noqa: BLE001
            logger.exception(
                "policy resolver: shipper deployment lookup threw for client_id=%s",
                client_id,
            )
            return False

    def _resolve_oauth_session_policy(self, claim: Any) -> tuple[str, ...]:
        """M5 §14.4 policy resolver. Closure captured by BridgeSessionManager.

        Returns one of:
        * ``_UNRESTRICTED`` — caller is operator_equivalent in the vault.
        * ``MANAGEMENT_ALLOWLIST`` — caller is a configured operator
          management client (for example ChatGPT over the secure MCP tunnel).
        * ``SHIPPER_ALLOWLIST`` — caller is a paired shipper (deployment
          row with matching oauth_client_id in pairing_status='paired').
        * ``EMPTY_ALLOWLIST`` — neither (fail-closed).

        Late binding: vault + session_ledger_service may not be live at
        BridgeSessionManager construction time (bridge starts via
        ``start_interface`` action, after the orchestrator wires the
        services). The closure looks them up at open-bridge time.
        """
        from .bridge_sessions import (  # noqa: PLC0415
            _UNRESTRICTED,
            EMPTY_ALLOWLIST,
            MANAGEMENT_ALLOWLIST,
            SHIPPER_ALLOWLIST,
        )

        client_id = getattr(claim, "client_id", "") or ""
        if not client_id:
            # Stdio bridges land here with empty client_id; the surface
            # short-circuits the policy check anyway (M5.B hot-fix), but
            # fail-closed at the resolver level is correct.
            return EMPTY_ALLOWLIST
        if self._oauth_client_is_operator_equivalent(client_id):
            return _UNRESTRICTED
        if self._oauth_client_is_management_client(client_id):
            return MANAGEMENT_ALLOWLIST
        if self._oauth_client_is_paired_shipper(client_id):
            return SHIPPER_ALLOWLIST
        return EMPTY_ALLOWLIST

    def _oauth_client_exists(self, client_id: str) -> bool:
        """M5 §14.3 BearerVerifier cross-check. True iff client is in the vault."""
        vault_registry = self._maybe_get_vault_oauth_registry()
        if vault_registry is None:
            # No registry means we can't validate; fail-closed by saying
            # the client doesn't exist. (Production: vault is always
            # bound before the bridge starts, so this path is dead.)
            return False
        try:
            return vault_registry.lookup_client(client_id) is not None
        except Exception:  # noqa: BLE001
            logger.exception(
                "client_exists_check: lookup_client threw for client_id=%s",
                client_id,
            )
            return False

    def _maybe_get_session_ledger_service(self) -> Any | None:
        """Reach the live SessionLedgerService via the orchestrator.

        Returns None if either the orchestrator_ref isn't injected yet
        or the ledger service hasn't been initialized (M1 ledger
        wiring runs in startup_sequence; bridge starts AFTER startup
        per starting_actions ordering, so this should always resolve
        in production).
        """
        orchestrator = getattr(self, "orchestrator_ref", None)
        if orchestrator is None:
            return None
        get_service = getattr(orchestrator, "get_service", None)
        if get_service is None:
            return None
        return get_service("session_ledger_service")

    def _maybe_get_blob_storage_service(self) -> Any | None:
        """Reach the live blob_storage_service via the orchestrator.

        Required by the M4 chatgpt_export + M9 claude_ai_export upload
        route facades — both call ``blob_storage_service.store_blob(...)``
        to persist the uploaded ZIP before registering a ledger source row.
        Same shape as :meth:`_maybe_get_session_ledger_service`; returns
        None when the orchestrator is not yet injected or the service
        binding is unbound (e.g., minimal cloud test harnesses without
        a blob-storage plugin loaded).
        """
        orchestrator = getattr(self, "orchestrator_ref", None)
        if orchestrator is None:
            return None
        get_service = getattr(orchestrator, "get_service", None)
        if get_service is None:
            return None
        return get_service("blob_storage_service")

    def _make_upload_route_auth(
        self,
        bridge_config: _BridgeRuntimeConfig,
    ) -> _UploadRouteAuth:
        """Build the AuthCheckProtocol callable for M4/M9 upload routes.

        The wiring follows the streamable transport's pattern so the same
        operator-opt-in outer-boundary contract applies to the upload
        surface as to the MCP streamable surface:

        * ``streamable_enabled=False`` (no streamable listener exists) OR
          ``streamable_no_auth=True`` (operator-approved outer boundary) →
          no-op auth callback (returns None for any header). Matches the
          :class:`PermissiveBearerVerifier` semantics used inside
          :meth:`_mount_streamable_transport`; local-dev curl works.
        * Otherwise → production gate per M5 §13.6 docstring on the
          chatgpt routes module (``BearerVerifier + operator_equivalent``):
          verify the bearer token via the cached
          :class:`BearerVerifier`, then assert the claim's ``client_id``
          is flagged operator-equivalent in the vault OAuth registry.

        The closure resolves both dependencies lazily because
        ``_build_fastapi_app`` runs BEFORE ``_mount_streamable_transport``;
        the verifier is constructed and cached on ``self`` later in
        ``start_interface``. By the time a request actually hits an
        upload route, both are wired.

        Raises ``PermissionError`` on any failure — the upload-route
        handler catches every exception and maps it to HTTP 401.
        """
        if not bridge_config.streamable_enabled or bridge_config.streamable_no_auth:

            def _no_op(authorization_header: str | None) -> object:  # noqa: ARG001  # pyright: ignore[reportUnusedParameter]
                return None

            return _no_op

        def _verify(authorization_header: str | None) -> object:
            verifier = self._streamable_bearer_verifier
            vault_registry = self._maybe_get_vault_oauth_registry()
            if verifier is None or vault_registry is None:
                raise PermissionError(
                    "upload-route auth unavailable: bearer verifier or "
                    "vault OAuth registry not bound at request time",
                )
            claim = verifier.verify(authorization_header)
            client_id = getattr(claim, "client_id", "")
            if not vault_registry.is_operator_equivalent(client_id):
                raise PermissionError(
                    f"client_id {client_id!r} is not operator-equivalent; "
                    "upload routes require an operator-equivalent bearer "
                    "per M5 §13.6",
                )
            return claim

        return _verify

    def _maybe_get_vault_oauth_registry(self) -> Any | None:
        """Pull the vault plugin's VaultOAuthRegistry via the injected proxy.

        W-VAULT-INTERFACE-EXTEND Phase D-2: vault is no longer fetched
        via ``orchestrator.get_service`` — the injected
        ``VaultServiceProxy`` exposes ``_oauth_registry`` as a
        transitional property (removal target W-OAUTH-EXTRACT). Returns
        None when no vault is bound (e.g., mock-vault test profiles).
        """
        vault = self._vault_service
        if vault is None:
            return None
        return getattr(vault, "_oauth_registry", None)

    def _run_server(self) -> None:
        import uvicorn  # noqa: PLC0415

        app = self._app
        if app is None:
            logger.error("%s: FastAPI app not constructed", self.name)
            return
        self._server_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._server_loop)
        cfg = uvicorn.Config(
            app,
            host=self._host or "127.0.0.1",
            port=self._port or 0,
            log_level="warning",
            loop="asyncio",
        )
        server = uvicorn.Server(cfg)
        self._server_started_event.set()
        try:
            self._server_loop.run_until_complete(server.serve())
        except Exception:
            logger.exception("%s: bridge API server crashed", self.name)
        finally:
            self._server_loop.close()

    def _shutdown_server(self) -> None:
        # Stop the REL-09 idle sweeper before its collaborators go away.
        if self._bridge_sweeper is not None:
            self._bridge_sweeper.stop()
            self._bridge_sweeper = None
        # Cancel outstanding sys:autonomic grace timers before the bridge
        # collaborators they close over are torn down.
        if self._autonomic_assignment is not None:
            self._autonomic_assignment.cancel_all()
            self._autonomic_assignment = None
        # Tear streamable down first so SSE streams unblock before the
        # underlying BridgeSessionManager goes away.
        self._teardown_streamable_server()
        self._teardown_bridge_server()
        self._app = None
        self._bridge_manager = None
        self._peer_registry = None
        self._platform_surface = None

    def _teardown_streamable_server(self) -> None:
        if self._streamable_session_manager is not None:
            self._streamable_session_manager.close_all()
            self._streamable_session_manager = None
        streamable_loop = self._streamable_server_loop
        if streamable_loop is not None and streamable_loop.is_running():
            streamable_loop.call_soon_threadsafe(streamable_loop.stop)
        streamable_thread = self._streamable_server_thread
        if streamable_thread is not None and streamable_thread.is_alive():
            streamable_thread.join(timeout=_SERVER_JOIN_TIMEOUT_S)
        self._streamable_server_thread = None
        self._streamable_server_loop = None
        self._streamable_host = None
        self._streamable_port = None

    def _teardown_bridge_server(self) -> None:
        loop = self._server_loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        thread = self._server_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=_SERVER_JOIN_TIMEOUT_S)
        self._server_thread = None
        self._server_loop = None
        self._port = None

    # ------------------------------------------------------------------
    # Internals — config provider plumbing
    # ------------------------------------------------------------------

    def _populate_config_provider_from_orchestrator(self) -> None:
        """Re-bind the config provider against the live orchestrator.

        Plugin discovery may rebuild the plugins dict, leaving the
        active instance distinct from the one that received
        ``initialize(config)``.
        """
        from ananta.core.config.config_provider import ConfigProvider  # noqa: PLC0415

        orchestrator = getattr(self, "orchestrator_ref", None)
        if orchestrator is None:
            return
        config_manager = getattr(orchestrator, "config", None)
        if config_manager is None or not hasattr(
            config_manager,
            "get_plugin_config_provider",
        ):
            return
        provider = config_manager.get_plugin_config_provider(self.name)
        if isinstance(provider, ConfigProvider):
            self.config_provider = provider

    def _build_config(self) -> AgentMessagingConfig:
        provider = self._resolve_config_provider()
        return AgentMessagingConfig(
            enabled=_as_bool(_provider_get(provider, "enabled"), True),
            allowed_backends=_as_str_tuple(
                _provider_get(provider, "allowed_backends"),
                default=("codex", "claude_code"),
            ),
            allowed_working_directory_roots=_as_str_tuple(
                _provider_get(provider, "allowed_working_directory_roots"),
                default=(),
            ),
            max_message_bytes=_as_int(
                _provider_get(provider, "max_message_bytes"),
                65_536,
            ),
            max_thread_messages=_as_int(
                _provider_get(provider, "max_thread_messages"),
                1_000,
            ),
            default_timeout_seconds=_as_int(
                _provider_get(provider, "default_timeout_seconds"),
                600,
            ),
            max_timeout_seconds=_as_int(
                _provider_get(provider, "max_timeout_seconds"),
                1_800,
            ),
        )

    def _build_bridge_runtime_config(self) -> _BridgeRuntimeConfig:
        provider = self._resolve_config_provider()
        return _BridgeRuntimeConfig(
            host=_as_str(
                _provider_get(provider, "host"),
                "127.0.0.1",
            ),
            port=_as_optional_int(_provider_get(provider, "port")),
            long_poll_timeout_seconds=_as_int(
                _provider_get(provider, "long_poll_timeout_seconds"),
                25,
            ),
            bridge_idle_timeout_seconds=_as_int(
                _provider_get(provider, "bridge_idle_timeout_seconds"),
                3_600,
            ),
            max_pending_events=_as_int(
                _provider_get(provider, "max_pending_events"),
                200,
            ),
            max_message_chars=_as_int(
                _provider_get(provider, "max_message_chars"),
                _DEFAULT_MAX_MESSAGE_CHARS,
            ),
            autonomic_grace_seconds=_as_int(
                _provider_get(provider, "autonomic_grace_seconds"),
                120,
            ),
            bridge_sweep_interval_seconds=_as_int(
                _provider_get(provider, "bridge_sweep_interval_seconds"),
                300,
            ),
            completion_serve_window_seconds=_as_int(
                _provider_get(provider, "completion_serve_window_seconds"),
                900,
            ),
            forward_serve_window_seconds=_as_int(
                _provider_get(provider, "forward_serve_window_seconds"),
                900,
            ),
            forward_attempts_cap=_as_int(
                _provider_get(provider, "forward_attempts_cap"),
                5,
            ),
            terminal_gc_after_seconds=_as_int(
                _provider_get(provider, "terminal_gc_after_seconds"),
                172_800,
            ),
            re_emit_window_seconds=_as_int(
                _provider_get(provider, "re_emit_window_seconds"),
                300,
            ),
            re_emit_cap=_as_int(
                _provider_get(provider, "re_emit_cap"),
                3,
            ),
            streamable_enabled=_as_bool(
                _provider_get(provider, "streamable_enabled"),
                False,
            ),
            streamable_host=_as_str(
                _provider_get(provider, "streamable_host"),  # noqa: S104
                "0.0.0.0",  # noqa: S104
            ),
            streamable_port=_as_int(
                _provider_get(provider, "streamable_port"),
                9000,
            ),
            streamable_allowed_origins=_as_str_tuple(
                _provider_get(provider, "streamable_allowed_origins"),
                default=(),
            ),
            streamable_bearer_max_age_seconds=_as_int(
                _provider_get(provider, "streamable_bearer_max_age_seconds"),
                300,
            ),
            oauth_enabled=_as_bool(
                _provider_get(provider, "oauth_enabled"),
                False,
            ),
            oauth_issuer_url=_as_str(
                _provider_get(provider, "oauth_issuer_url"),
                "",
            ),
            oauth_resource_aliases=_as_str_tuple(
                _provider_get(provider, "oauth_resource_aliases"),
                default=(),
            ),
            oauth_management_client_ids=_as_str_tuple(
                _provider_get(provider, "oauth_management_client_ids"),
                default=(),
            ),
            oauth_token_ttl_seconds=_as_int(
                _provider_get(provider, "oauth_token_ttl_seconds"),
                DEFAULT_TOKEN_TTL_SECONDS,
            ),
            oauth_auth_code_ttl_seconds=_as_int(
                _provider_get(provider, "oauth_auth_code_ttl_seconds"),
                600,
            ),
            oauth_refresh_token_ttl_seconds=_as_int(
                _provider_get(provider, "oauth_refresh_token_ttl_seconds"),
                30 * 24 * 60 * 60,
            ),
            oauth_require_audience=_as_bool(
                _provider_get(provider, "oauth_require_audience"),
                True,
            ),
            oauth_refresh_tokens_enabled=_as_bool(
                _provider_get(provider, "oauth_refresh_tokens_enabled"),
                True,
            ),
            streamable_cors_origins=_as_str_tuple(
                _provider_get(provider, "streamable_cors_origins"),
                default=(),
            ),
            streamable_no_auth=_as_bool(
                _provider_get(provider, "streamable_no_auth"),
                False,
            ),
        )

    def _register_unit_client(self) -> PsoletRegisterUnitClient:
        """The register client the managed-dispatch Unit mint calls (unt_57725090)."""
        return PsoletRegisterUnitClient(
            resolve_psolet_cli(_provider_get(self._resolve_config_provider(), CONFIG_PSOLET_CLI)),
            solet_name=os.environ["SOLET_NAME"].strip(),
        )

    def _build_session_lifecycle_policy_config(self) -> _SessionLifecyclePolicyConfig:
        provider = self._resolve_config_provider()
        return _SessionLifecyclePolicyConfig(
            work_class_defaults=_as_work_class_defaults(
                _provider_get(provider, "work_class_defaults"),
            ),
            work_class_tool_allowlists=_as_work_class_tool_allowlists(
                _provider_get(provider, "work_class_tool_allowlists"),
            ),
            headless_permission_mode=_as_str(
                _provider_get(provider, "headless_permission_mode"),
                "bypassPermissions",
            ),
            default_fleet_transport=_as_str(
                _provider_get(provider, "default_fleet_transport"),
                "watch",
            ),
        )

    # ------------------------------------------------------------------
    # Internals — Streamable HTTP MCP transport
    # ------------------------------------------------------------------

    def _build_oauth_surface(
        self,
        bridge_config: _BridgeRuntimeConfig,
    ) -> tuple[OAuthEndpoints | None, str, tuple[str, ...]]:
        """Derive OAuth endpoints + resource-metadata URL + audiences.

        Returns ``(None, "", ())`` when OAuth is disabled or the
        issuer URL is unset; the bearer verifier and the streamable
        router both treat those as "OAuth not mounted".
        """
        if not (bridge_config.oauth_enabled and bridge_config.oauth_issuer_url):
            return None, "", ()
        oauth_endpoints = build_endpoints(
            issuer=bridge_config.oauth_issuer_url,
            streamable_path=STREAMABLE_PATH,
        )
        resource_metadata_url = oauth_endpoints.issuer + "/.well-known/oauth-protected-resource"
        accepted_audiences: tuple[str, ...] = ()
        if bridge_config.oauth_require_audience:
            # The streamable router answers at both the primary path
            # and the alias; tokens whose ``aud`` claim matches either
            # canonical URI are accepted. /authorize + /token stamp
            # the primary URI; the alias exists for phone tokens
            # minted with --audience pointing at the alias path.
            accepted_audiences = (
                oauth_endpoints.resource,
                oauth_endpoints.issuer + STREAMABLE_ALIAS_PATH,
            )
        return oauth_endpoints, resource_metadata_url, accepted_audiences

    def _mount_streamable_transport(
        self,
        *,
        app: FastAPI,
        bridge_manager: BridgeSessionManager,
        peer_registry: PeerRegistry,
        platform_surface: PlatformSurface,
        bridge_config: _BridgeRuntimeConfig,
    ) -> None:
        """Register the Streamable HTTP router on ``app``.

        Owns the construction of the session manager + bearer
        verifier so the streamable router gets a fully-wired set of
        collaborators.  No-op if the vault plugin is unreachable —
        the streamable transport hard-fails on first request rather
        than silently degrading; the failure mode is clear from
        ``bearer.vault_unavailable`` in the error response body.
        """
        # An OAuth login surface is ALWAYS mounted now (static when an
        # issuer is pinned, dynamic origin-following otherwise — see
        # _mount_oauth_routers), and both variants offer refresh-token
        # rotation when it's enabled. So the vault must expose the
        # refresh-token methods whenever refresh tokens are enabled at
        # all, independent of streamable_no_auth.
        require_refresh = bridge_config.oauth_refresh_tokens_enabled
        vault = self._resolve_vault_plugin(
            require_refresh_token_methods=require_refresh,
        )
        oauth_endpoints, resource_metadata_url, accepted_audiences = self._build_oauth_surface(
            bridge_config
        )
        bearer_verifier, hmac_key = self._build_streamable_bearer_verifier(
            bridge_config=bridge_config,
            vault=vault,
            accepted_audiences=accepted_audiences,
        )
        # Cache the verifier so the M4/M9 upload-route auth closure can
        # share it (same audience binding, same client-exists check) per
        # the dispatch's lazy-resolver pattern.
        self._streamable_bearer_verifier = bearer_verifier
        # B1 Finding-B: wire the operator-equivalent propagation on the shared
        # platform surface — a VERIFIED operator_equivalent OAuth client keeps
        # operator authority (for_operator_equivalent) once the no-auth flip
        # lands. Reads the SAME vault ``is_operator_equivalent`` the policy
        # resolver + session-ledger use. Absent registry (mock-vault profiles)
        # → unwired → non-operator default (safe).
        oauth_registry = self._maybe_get_vault_oauth_registry()
        if oauth_registry is not None:
            platform_surface.set_operator_equivalent_check(
                oauth_registry.is_operator_equivalent,
            )
        session_manager = StreamableSessionManager(
            bridge_manager=bridge_manager,
            peer_registry=peer_registry,
        )
        self._streamable_session_manager = session_manager
        solet_name = _resolve_solet_name()
        router = build_streamable_router(
            bridge_manager=bridge_manager,
            peer_registry=peer_registry,
            platform_surface=platform_surface,
            agent_messaging_service=self._require_service(),
            state_service=self._get_state_service(),
            session_manager=session_manager,
            bearer_verifier=bearer_verifier,
            allowed_origins=bridge_config.streamable_allowed_origins,
            resource_metadata_url=resource_metadata_url,
            cors_origins=bridge_config.streamable_cors_origins,
            path_aliases=(STREAMABLE_ALIAS_PATH,),
            solet_name=solet_name,
        )
        app.include_router(router)
        self._mount_oauth_routers(
            app=app,
            bridge_config=bridge_config,
            vault=vault,
            hmac_key=hmac_key,
            oauth_endpoints=oauth_endpoints,
            accepted_audiences=accepted_audiences,
        )
        logger.info(
            "%s: Streamable HTTP MCP transport mounted at "
            "/api/v1/mcp/streamable (bearer max-age=%ds, dns-rebinding-origins=%s, "
            "cors-origins=%s, oauth=%s)",
            self.name,
            bridge_config.streamable_bearer_max_age_seconds,
            list(bridge_config.streamable_allowed_origins) or "any",
            list(bridge_config.streamable_cors_origins) or "none",
            "on" if bridge_config.oauth_enabled else "off",
        )

    def _build_streamable_bearer_verifier(
        self,
        *,
        bridge_config: _BridgeRuntimeConfig,
        vault: Any,
        accepted_audiences: tuple[str, ...],
    ) -> tuple[BearerVerifier, bytes]:
        """Build the bearer verifier + HMAC key for the streamable router.

        ``streamable_no_auth`` swaps in the permissive verifier (the
        outer boundary owns auth) but still provisions the HMAC key so
        the dynamic OAuth surface can mint tokens.
        """
        hmac_key = _load_or_create_bearer_hmac_key(vault)
        if bridge_config.streamable_no_auth:
            logger.warning(
                "streamable_no_auth=true: MCP streamable endpoint relies on "
                "an outer security boundary (tunnel-client + runtime API "
                "key, mTLS, or network isolation) for auth; per-request "
                "bearer enforcement is DISABLED. Ensure your outer boundary "
                "is active.",
            )
            return PermissiveBearerVerifier(), hmac_key
        verifier = BearerVerifier(
            hmac_key=hmac_key,
            max_age_seconds=bridge_config.streamable_bearer_max_age_seconds,
            accepted_audiences=accepted_audiences,
            # M5 §14.3: revoked-client cross-check via vault registry.
            # Lazy lookup keeps the verifier decoupled from vault wiring
            # ordering; bridge plugin already requires vault to construct
            # the verifier (HMAC key load earlier in this function), so
            # the registry is always available at first call time.
            client_exists_check=self._oauth_client_exists,
        )
        return verifier, hmac_key

    def _mount_oauth_routers(
        self,
        *,
        app: FastAPI,
        bridge_config: _BridgeRuntimeConfig,
        vault: Any,
        hmac_key: bytes,
        oauth_endpoints: OAuthEndpoints | None,
        accepted_audiences: tuple[str, ...],
    ) -> None:
        """Mount the OAuth 2.1 login surface — always exactly one variant.

        Static surface when a stable issuer is pinned
        (``oauth_endpoints`` present — cloud ALB); origin-following
        dynamic surface otherwise (local tunnel, ephemeral origin).

        This selection is INDEPENDENT of ``streamable_no_auth``: bearer
        *enforcement* (which verifier the streamable router uses) and the
        OAuth *login* surface (how a client obtains a bearer) are
        orthogonal concerns. A local tunnel deployment with enforcement
        ON still needs /authorize + /oauth/token so external clients
        (ChatGPT / claude.ai) can complete OAuth and mint a token the
        real verifier accepts. Gating the login surface on
        ``streamable_no_auth`` is what stranded the connector at a 404 on
        the enforcement cutover.
        """
        refresh_token_store = vault if bridge_config.oauth_refresh_tokens_enabled else None
        if oauth_endpoints is not None:
            oauth_router = build_oauth_router(
                endpoints=oauth_endpoints,
                client_store=vault,
                refresh_token_store=refresh_token_store,
                hmac_key=hmac_key,
                token_ttl_seconds=bridge_config.oauth_token_ttl_seconds,
                auth_code_ttl_seconds=bridge_config.oauth_auth_code_ttl_seconds,
                refresh_token_ttl_seconds=(bridge_config.oauth_refresh_token_ttl_seconds),
            )
            app.include_router(oauth_router)
            logger.info(
                "%s: OAuth 2.1 surface mounted (issuer=%s, "
                "token_ttl=%ds, auth_code_ttl=%ds, accepted_audiences=%s)",
                self.name,
                oauth_endpoints.issuer,
                bridge_config.oauth_token_ttl_seconds,
                bridge_config.oauth_auth_code_ttl_seconds,
                list(accepted_audiences) or "any",
            )
            return
        oauth_router = build_dynamic_oauth_router(
            streamable_path=STREAMABLE_PATH,
            client_store=vault,
            refresh_token_store=refresh_token_store,
            hmac_key=hmac_key,
            resource_aliases=bridge_config.oauth_resource_aliases,
            token_ttl_seconds=bridge_config.oauth_token_ttl_seconds,
            auth_code_ttl_seconds=bridge_config.oauth_auth_code_ttl_seconds,
            refresh_token_ttl_seconds=(bridge_config.oauth_refresh_token_ttl_seconds),
        )
        app.include_router(oauth_router)
        logger.info(
            "%s: dynamic (origin-following) OAuth 2.1 login surface mounted "
            "(bearer_enforcement=%s)",
            self.name,
            "off" if bridge_config.streamable_no_auth else "on",
        )

    def _resolve_vault_plugin(
        self,
        *,
        require_refresh_token_methods: bool = False,
    ) -> Any:
        """Return the plugin bound to ``vault_service`` via the injected proxy.

        Reads ``self._vault_service`` (set by ``set_vault_service`` during
        lifecycle injection) so the profile's ``service_bindings`` decides
        which concrete plugin backs the interface (e.g.
        ``macos_vault_plugin`` for local, ``secrets_manager_vault_plugin``
        for cloud).  Hardcoding to ``macos_vault_plugin`` violated the
        Interface->Plugin rule and bypassed the cloud vault entirely
        (Task #31 §3.4).

        The streamable bearer-token verifier reads its HMAC secret
        from the vault via ``retrieve`` / ``store`` (Task #53 HS256
        migration); OAuth client lookup uses
        ``lookup_oauth_client`` / ``verify_oauth_client_credentials``.
        None of these are exposed on the LLM-visible registry; they
        are reachable only through this in-process handoff. Raises
        ``RuntimeError`` with a specific missing-method list if the
        bound plugin does not satisfy the required surface so
        misconfiguration fails fast at start-up rather than at the
        first phone request.

        When ``require_refresh_token_methods`` is True (the OAuth
        refresh-token rotation flag is enabled in bridge_config), the
        bound vault must additionally expose
        ``issue_oauth_refresh_token`` and
        ``consume_oauth_refresh_token``. Both default plugins do; the
        gate exists so a future vault implementation cannot be wired
        as the refresh-token store without satisfying that contract.
        """
        vault = self._vault_service
        if vault is None:
            raise RuntimeError(
                f"{self.name}: no plugin is bound to vault_service in "
                "the active profile's service_bindings; Streamable HTTP "
                "MCP transport requires a vault for bearer-token "
                "decryption + OAuth client lookup",
            )
        required_methods: list[str] = [
            "retrieve",
            "store",
            "lookup_oauth_client",
            "verify_oauth_client_credentials",
        ]
        if require_refresh_token_methods:
            required_methods.extend(
                [
                    "issue_oauth_refresh_token",
                    "consume_oauth_refresh_token",
                ]
            )
        missing = [m for m in required_methods if not hasattr(vault, m)]
        if missing:
            plugin_name = getattr(vault, "name", type(vault).__name__)
            raise RuntimeError(
                f"{self.name}: vault_service binding {plugin_name!r} is "
                f"missing required structural methods: {missing}. "
                "Streamable HTTP MCP transport requires HMAC key "
                "storage (retrieve/store) + OAuth client lookup"
                + (" + refresh-token rotation" if require_refresh_token_methods else "")
                + ". Bind a different plugin via service_bindings or "
                "extend the current one with the missing methods.",
            )
        return vault

    def _start_streamable_server(
        self,
        bridge_config: _BridgeRuntimeConfig,
    ) -> dict[str, Any] | None:
        """Start the streamable HTTP listener; return failure dict on error.

        BLG-04: tries ``bridge_config.streamable_port`` (default 9000) FIRST
        via ``find_available_port(preferred=...)``, falling back to an
        OS-assigned ephemeral port only when that's already taken. This is
        deliberately NOT "always ephemeral":

        - Container deployments run one solet per container (isolated
          network namespace) with a HOST port mapped onto this exact
          container-internal port (see the ``streamable_port`` field's own
          comment: host 9001 -> container 9000 via Caddy/mkcert). There the
          preferred port is always free, so this is byte-identical to
          today — no behavior change for that topology.
        - Local macOS blue-green steady state (only one color running) is
          the same: preferred is free, binds exactly as before.
        - Local macOS blue-green DURING an overlap window is the ONE case
          that changes: green's preferred bind now loses the race
          (EADDRINUSE) exactly as it does today, but instead of that
          failing the color's boot, it falls back to an ephemeral port.
          External reachability at the stable, well-known port becomes the
          local blue-green router's job in that topology: it binds the
          fixed port once (never itself color-duplicated) and proxies to
          whichever color's ephemeral port it has on file, via
          `register_color`'s optional `streamable_port`
          (`macos_self_deployment_plugin.heartbeat_lifecycle`,
          `_lookup_streamable_port`/`streamable_bound_port` below) — the
          same pattern already proven for the main bridge port at line
          ~7141.
        """
        self._streamable_host = bridge_config.streamable_host
        self._streamable_port = find_available_port(
            preferred=bridge_config.streamable_port,
        )
        self._streamable_server_started_event.clear()
        self._streamable_server_thread = threading.Thread(
            target=self._run_streamable_server,
            name=f"{PLUGIN_NAME}-streamable-server",
            daemon=True,
        )
        self._streamable_server_thread.start()
        if not self._streamable_server_started_event.wait(
            timeout=_SERVER_START_TIMEOUT_S,
        ):
            return _failure_result(
                code="bridge.streamable_startup_failed",
                message=(
                    f"Streamable HTTP server did not signal startup "
                    f"within {_SERVER_START_TIMEOUT_S}s"
                ),
            )
        return None

    def _run_streamable_server(self) -> None:
        """Uvicorn entry point for the streamable HTTP listener thread."""
        import uvicorn  # noqa: PLC0415

        app = self._app
        if app is None:
            logger.error(
                "%s: streamable server thread started without FastAPI app",
                self.name,
            )
            return
        streamable_port = self._streamable_port
        if streamable_port is None:
            logger.error(
                "%s: streamable server thread started before _start_streamable_server bound a port",
                self.name,
            )
            return
        self._streamable_server_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._streamable_server_loop)
        # BLG-04: host stays configurable (loopback-only is a legitimate
        # local-dev choice), but the port is always the ephemeral value
        # `_start_streamable_server` already bound via `find_available_port`
        # — never a fixed fallback, since a fixed value is exactly what
        # caused the cross-color collision this fix removes.
        cfg = uvicorn.Config(
            app,
            host=self._streamable_host or "0.0.0.0",  # noqa: S104
            port=streamable_port,
            log_level="warning",
            loop="asyncio",
        )
        server = uvicorn.Server(cfg)
        self._streamable_server_started_event.set()
        try:
            self._streamable_server_loop.run_until_complete(server.serve())
        except Exception:
            logger.exception(
                "%s: streamable HTTP server crashed",
                self.name,
            )
        finally:
            self._streamable_server_loop.close()

    def _resolve_config_provider(self) -> object:
        provider = getattr(self, "config_provider", None)
        if provider is None:
            self._populate_config_provider_from_orchestrator()
            provider = getattr(self, "config_provider", None)
        return provider


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _log_managed_dispatch_sweep(result: Mapping[str, Any]) -> None:
    """Log only actionable reconciliation outcomes, never a false all-clear."""
    if not (result["dead"] or result["unknown"] or result["conditions"]):
        return
    logger.warning(
        "managed-dispatch sweep: attempts=%d alive=%d dead=%d unknown=%d "
        "dispatches=%d conditions=%d notices=%d",
        result["attempts_evaluated"],
        result["alive"],
        result["dead"],
        result["unknown"],
        result["dispatches_evaluated"],
        len(result["conditions"]),
        result["notices_emitted"],
    )


def _run_session_claude_mapping_riders(state_service: Any) -> None:
    """Split out of ``_run_session_lifecycle_sweep`` to keep it under the
    radon cc threshold (mirrors ``session_sweep.py``'s own
    ``_mark_one_overdue`` split and ``_spawn_session_request_from_params``
    above -- same "thin dispatch" rationale) -- the two T1 usage-capture
    riders that share the same no-peer_registry/no-bridge_manager-dependency
    call-site pattern as the overdue marking: ``drain_session_claude_mapping_spool``
    (ruling 2026-08-05, Q1(d)) and ``detect_hook_absent_sessions`` (S2c,
    named follow-up) both run every tick regardless of bridge availability.
    A verb nobody calls is bound-in-name-only. A module-level function
    (not a method) -- it touches no instance state, only ``state_service``."""
    drain_result = lifecycle_drain_session_claude_mapping_spool(state_service)
    if drain_result["upserted"] or drain_result["skipped_malformed"]:
        logger.info(
            "D1 sweep: session_claude_mapping spool drain -- files_seen=%d "
            "upserted=%d skipped_malformed=%d",
            drain_result["files_seen"],
            drain_result["upserted"],
            drain_result["skipped_malformed"],
        )
    # S2c: detects a genuinely BROKEN SessionStart hook installation (as
    # opposed to the cross-check's not-yet-fired case, which is not an error).
    hook_absent = lifecycle_detect_hook_absent_sessions(state_service)
    if hook_absent:
        logger.warning("D1 sweep: hook-absence detected for %d session(s)", hook_absent)


def _spawn_dispatch_overrides_from_params(raw: dict[str, Any]) -> dict[str, Any]:
    """The dispatch-override subset of ``spawn_session``'s params (host,
    model, effort, allowed_tools, permission_mode, allow_askuserquestion) —
    split out of ``_spawn_session_request_from_params`` to keep it under the
    radon cc threshold; these six fields share one purpose (per-spawn
    dispatch overrides), so grouping them is a real seam, not an arbitrary
    split."""
    return {
        "host": (str(raw["host"]) if raw.get("host") else None),
        "agent_runtime": str(raw.get("agent_runtime", "") or "claude_code"),
        "model": str(raw.get("model", "") or ""),
        "effort": str(raw.get("effort", "") or ""),
        "allowed_tools": _as_str_tuple(raw.get("allowed_tools"), default=()),
        "permission_mode": str(raw.get("permission_mode", "") or ""),
        "allow_askuserquestion": bool(raw.get("allow_askuserquestion", False)),
    }


def _param_text(raw: dict[str, Any], name: str) -> str:
    value = raw.get(name)
    return str(value) if value else ""


def _param_positive_int(raw: dict[str, Any], name: str) -> int:
    value = raw.get(name)
    return int(value) if value else 0


def _spawn_session_identity_params(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "role_class": _param_text(raw, "role_class"),
        "lane_id": _param_text(raw, "lane_id"),
        "brief_ref": _param_text(raw, "brief_ref"),
        "unit_id": _param_text(raw, "unit_id"),
        "repository_root": _param_text(raw, "repository_root"),
        "work_class": _param_text(raw, "work_class"),
        "budget_line": _param_text(raw, "budget_line"),
        "dispatch_id": _param_text(raw, "dispatch_id"),
        "role_name": _param_text(raw, "role_name"),
        "visibility": _param_text(raw, "visibility"),
    }


def _spawn_session_lifecycle_params(raw: dict[str, Any], directed_by: str) -> dict[str, Any]:
    return {
        "report_by_seconds": _param_positive_int(raw, "report_by_seconds"),
        "spawned_by_instance_id": _param_text(raw, "spawned_by_instance_id"),
        "spawned_by_role": _param_text(raw, "spawned_by_role"),
        "directed_by": directed_by,
        "local_name": _param_text(raw, "local_name"),
        "degraded_hooks_acknowledged": bool(raw.get("degraded_hooks_acknowledged")),
        "dispatch_kind": _param_text(raw, "dispatch_kind"),
        "reviewed_report_vendor": _param_text(raw, "reviewed_report_vendor"),
        "pair_id": _param_text(raw, "pair_id"),
        "scope_tags": _as_str_tuple(raw.get("scope_tags"), default=()),
        "difficulty_score": raw.get("difficulty_score"),
        "selection_receipt": raw.get("selection_receipt"),
        "enforce_selection_receipt": True,
    }


def _spawn_session_request_from_params(
    raw: dict[str, Any],
    directed_by: str,
) -> SpawnSessionRequest:
    """Build the ``spawn_session`` verb's typed request from raw transport
    params. Split out of the ``spawn_session`` method so the transport shim
    stays a thin dispatch (radon cc)."""
    return SpawnSessionRequest(
        **_spawn_session_identity_params(raw),
        **_spawn_session_lifecycle_params(raw, directed_by),
        **_spawn_dispatch_overrides_from_params(raw),
    )


def _dispatch_spec_from_params(
    raw: dict[str, Any],
    req: SpawnSessionRequest,
    directed_by: str,
) -> DispatchSpec:
    """Build the immutable contract from policy-resolved spawn inputs."""
    return DispatchSpec(
        dispatch_id=mint_dispatch_id(),
        lane_id=req.lane_id,
        role_name=req.role_name,
        role_class=req.role_class,
        work_class=req.work_class,
        budget_line=req.budget_line,
        brief_ref=req.brief_ref,
        unit_id=req.unit_id,
        repository_root=req.repository_root,
        brief_sha256=str(raw.get("brief_sha256") or ""),
        expected_path=str(raw.get("expected_path") or ""),
        completion_contract=_as_object(raw.get("completion_contract")),
        model=req.model,
        effort=req.effort,
        agent_runtime=req.agent_runtime,
        allowed_hosts=list(_as_str_tuple(raw.get("allowed_hosts"))),
        host=str(req.host or ""),
        visibility=req.visibility,
        local_name=req.local_name,
        report_by_seconds=req.report_by_seconds,
        allowed_tools=req.allowed_tools,
        permission_mode=req.permission_mode,
        transport=req.transport,
        allow_askuserquestion=req.allow_askuserquestion,
        degraded_hooks_acknowledged=req.degraded_hooks_acknowledged,
        spawned_by_instance_id=req.spawned_by_instance_id,
        spawned_by_role=req.spawned_by_role,
        directed_by=directed_by,
        uptake_due_at=str(raw.get("uptake_due_at") or ""),
        report_by=str(raw.get("report_by") or ""),
        watchdog_due_at=str(raw.get("watchdog_due_at") or ""),
        dispatch_kind=req.dispatch_kind,
        reviewed_report_vendor=req.reviewed_report_vendor,
        pair_id=req.pair_id,
        scope_tags=req.scope_tags,
        difficulty_score=req.difficulty_score,
        selection_receipt=req.selection_receipt or {},
        selection_receipt_enforced=req.enforce_selection_receipt,
        repository_id=_param_text(raw, "repository_id"),
        unit_key=_param_text(raw, "unit_key"),
        addresses=_as_str_tuple(raw.get("addresses"), default=()),
        reference_basis=_param_text(raw, "reference_basis"),
        reference_basis_reason=_param_text(raw, "reference_basis_reason"),
        brief_repository_root=_param_text(raw, "brief_repository_root"),
    )


def _apply_work_class_defaults(
    req: SpawnSessionRequest,
    defaults: Mapping[str, Mapping[str, str]],
) -> SpawnSessionRequest:
    """§6 L3 rule 1 — fill an OMITTED ``model``/``effort`` from the
    operator-configured per-``work_class`` default (``plugin.yaml``'s
    ``work_class_defaults`` block). Never overrides a value the caller
    explicitly passed; an unconfigured ``work_class`` (the shipped default —
    empty block) leaves both fields exactly as ``spawn_session`` received
    them, i.e. today's behavior."""
    entry = defaults.get(req.work_class)
    if not entry:
        return req
    model = req.model or str(entry.get("model", ""))
    effort = req.effort or str(entry.get("effort", ""))
    if model == req.model and effort == req.effort:
        return req
    return replace(req, model=model, effort=effort)


def _apply_tool_allowlist(
    req: SpawnSessionRequest,
    allowlists: Mapping[str, tuple[str, ...]],
) -> SpawnSessionRequest:
    """§6 permission-mode ruling (2026-08-03) — fill an OMITTED
    ``allowed_tools`` from the operator-configured per-``work_class``
    allowlist (``plugin.yaml``'s ``work_class_tool_allowlists`` block).
    Never overrides an explicit caller value. An unconfigured ``work_class``
    resolves to the shipped-empty default (``()``) — the spawn is STILL
    gated (``headless_adapter.py`` always injects the PreToolUse hook),
    just with nothing extra allowed, never an open default."""
    if req.allowed_tools:
        return req
    configured = allowlists.get(req.work_class)
    if not configured:
        return req
    return replace(req, allowed_tools=configured)


def _resolve_permission_mode(req: SpawnSessionRequest, policy_mode: str) -> SpawnSessionRequest:
    """§6 permission-mode design — fill an OMITTED ``permission_mode`` from
    ``policy_mode`` (``plugin.yaml``'s ``headless_permission_mode``), never
    overriding an explicit caller value. Per the 2026-08-03 operator ruling
    ("we don't have any restrictions now"), no resolved value is rejected
    here — ``headless_adapter.py.verify_config()`` still refuses a spawn if
    this and the config both resolve to nothing at all, which is
    operational sanity (the process needs SOME argv value), not a
    restriction on which value."""
    if req.permission_mode:
        return req
    return replace(req, permission_mode=policy_mode)


def _resolve_transport(req: SpawnSessionRequest, policy_transport: str) -> SpawnSessionRequest:
    """fleet-watch-transport-migration phase 2 slice 1 (2026-08-06) — fill
    an OMITTED ``transport`` from ``policy_transport`` (``plugin.yaml``'s
    ``default_fleet_transport``), never overriding an explicit caller
    value. Mirrors :func:`_resolve_permission_mode` exactly: the same
    fill-never-override shape, one field later."""
    if req.transport:
        return req
    return replace(req, transport=policy_transport)


def _apply_spawn_session_policy(
    req: SpawnSessionRequest,
    policy: _SessionLifecyclePolicyConfig,
) -> SpawnSessionRequest:
    """The four spawn-config-resolution steps every ``spawn_session``
    dispatch must run — model/effort defaults, tool allowlist, permission
    mode, transport — factored out so ``spawn_session()`` (the API path)
    and the ``restart_session`` choreography (the background-worker path)
    share exactly ONE resolution path. Before this (2026-08-10), the
    choreography built its own ``SpawnSessionRequest`` directly and skipped
    all four steps; ``permission_mode``/``allowed_tools``/``transport`` are
    not columns on ``managed_session``, so restarting a worker silently lost
    them every time (measured live: ``host_cannot_spawn`` — no permission
    mode configured — on the fresh spawn). A fifth policy step added here
    now reaches both callers automatically instead of needing to be copied
    into a second, easily-forgotten call site."""
    if not req.model or not req.effort:
        req = _apply_work_class_defaults(req, policy.work_class_defaults)
    req = _apply_tool_allowlist(req, policy.work_class_tool_allowlists)
    req = _resolve_permission_mode(req, policy.headless_permission_mode)
    req = _resolve_transport(req, policy.default_fleet_transport)
    return req


def _success_result(
    *,
    data: dict[str, object],
    actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a successful ``ActionResult`` dict with all required keys.

    Returns a plain ``dict[str, Any]`` (rather than the
    :class:`ActionResult` TypedDict) so this helper is assignable to
    both the IO interface contract (``dict[str, Any]``) and the
    EDGE/EDGE_SINK ``ActionResult`` callers — the platform validator
    cares about the runtime keys, not the static type. ``actions`` ride
    the poller's Pattern-6a returned-action submission (the INF-02 serve
    verb's resume continuation).
    """
    return {
        "action_status": "completed",
        "data": data,
        "actions": [] if actions is None else actions,
        "error": None,
        "timestamp": _now_iso(),
    }


def _build_resume_action(row: dict[str, object]) -> dict[str, Any]:
    """Build the served request's resume continuation action_def.

    The row's ``resume_process_key`` names the consumer's re-entry verb
    (this plugin stays consumer-agnostic); arguments are platform-owned —
    just the ``request_id``, the consumer re-reads its correlation and the
    served text from the durable row. The def carries NO result_processor,
    so its completion is terminal (no spurious inference turn); the
    consumer's own returned actions continue the flow (Pattern 6a).
    """
    import json

    resume_key = str(row.get(COL_ICR_RESUME_PROCESS_KEY) or "")
    segments = resume_key.split("::")
    if len(segments) != 3 or not all(segments):
        raise FrameworkError(
            f"completion request {row.get(COL_ICR_REQUEST_ID)!r} carries a "
            f"malformed resume_process_key {resume_key!r} "
            "(expected provider_type::provider::function_name)",
        )
    provider_type, provider, function_name = segments
    request_id = str(row.get(COL_ICR_REQUEST_ID) or "")
    action_def: dict[str, Any] = {
        "name": f"resume_completion_{function_name}",
        "description": (
            f"Resume {provider}::{function_name} with the served completion "
            f"for request {request_id}"
        ),
        "process": {
            "provider_type": provider_type,
            "provider": provider,
            "function_name": function_name,
        },
        "arguments": {"request_id": request_id},
    }
    correlation = json.loads(str(row.get(COL_ICR_CORRELATION) or "{}"))
    context_id = correlation.get("context_id") if isinstance(correlation, dict) else None
    if isinstance(context_id, str) and context_id:
        action_def["context_id"] = context_id
    return action_def


def _coerce_role_receipt_candidates(raw_candidates: object) -> list[tuple[str, str]]:
    """Validate bounded, exact wake receipt lookup candidates."""
    if not isinstance(raw_candidates, list):
        raise ValueError("candidates must be a list.")
    encoded = json.dumps(raw_candidates, separators=(",", ":"))
    if len(raw_candidates) > 1000 or len(encoded.encode()) > 256 * 1024:
        raise ValueError("receipt candidates exceed fixed bounds.")
    candidates: list[tuple[str, str]] = []
    for item in raw_candidates:
        if not isinstance(item, dict):
            raise ValueError("every candidate must be an object.")
        key, row_id = item.get("recipient_key"), item.get("role_row_id")
        if not isinstance(key, str) or not key or not isinstance(row_id, str) or not row_id:
            raise ValueError("candidates require recipient_key and role_row_id.")
        candidates.append((key, row_id))
    return candidates


def _build_peer_inbox_request(
    raw: dict[str, Any],
    binding: BridgeBinding,
) -> PeerInboxRequest:
    """Coerce caller args + the resolved binding into one ``PeerInboxRequest``.

    The recipient triple comes from ``binding`` and never from ``raw`` — a
    caller names only its own session, and the identity it reads with is the one
    the registry holds for that session. The two cursors are read independently
    and neither ever feeds the other. Raises ``ValueError`` for a malformed
    ``after``: a broken cursor means the caller's paging is wrong, and silently
    restarting from page one would turn that into an unbounded re-read.
    """
    after_raw = raw.get("after")
    try:
        after_created_at = (
            datetime.fromisoformat(str(after_raw)) if after_raw not in (None, "") else None
        )
    except ValueError as exc:
        message = (
            f"'after' must be an ISO-8601 datetime (the previous newest-first page's "
            f"next_after_created_at): {exc}"
        )
        raise ValueError(message) from exc
    role_after_raw = raw.get("role_after")
    return PeerInboxRequest(
        recipient_agent_id=binding.agent_id,
        recipient_agent_instance_id=binding.agent_instance_id,
        recipient_agent_session_id=binding.agent_session_id,
        after_created_at=after_created_at,
        limit=_clamp_peer_inbox_limit(raw.get("limit")),
        # A4 (2026-08-04): the silent/important split at send time is
        # retired, so the catch-up view is the only meaningful one — never
        # read from the caller. This closes the hatch the same way Amendment
        # 3 closes send_peer_message's: the schema entry AND the
        # read-and-branch code both go, not just one.
        include_important=True,
        role_after=(str(role_after_raw) if role_after_raw not in (None, "") else None),
        observer=bool(raw.get("observer", False)),
    )


def _clamp_peer_inbox_limit(raw: object) -> int:
    """Coerce a caller's ``limit`` to the supported page size.

    Absent or non-numeric → the modest default (the flood guard is what makes
    an unqualified ``peer_inbox`` call safe to advertise). Out-of-range values
    clamp rather than error: the caller asked for "as much as you'll give me",
    and a page size is not a correctness argument — unlike ``after`` /
    ``role_after``, where a malformed value means the caller's paging is broken
    and must fail loud.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return PEER_INBOX_DEFAULT_LIMIT
    return max(PEER_INBOX_MIN_LIMIT, min(raw, PEER_INBOX_MAX_LIMIT))


def _opt_int(raw: object) -> int | None:
    """An optional integer parameter: absent stays absent.

    ``None`` must survive as ``None`` all the way to the column. Coercing it
    to 0 would turn "the reporter did not measure the cache" into "the cache
    read zero tokens", which is the strongest possible cold signal — an
    un-upgraded reporter would start asserting a cold cache on every tick.

    A non-numeric value is also treated as NOT REPORTED rather than coerced:
    a caller sending junk has not measured anything either, and inventing a 0
    from it would produce the same false cold signal by a different route.
    """
    if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _opt_bool(raw: object) -> bool | None:
    """An optional boolean parameter: absent stays absent, never False.

    The inverse hazard to :func:`_opt_int` and just as silent: coercing None
    to False would have every un-upgraded reporter assert a WARM cache it
    never looked at.
    """
    return None if raw is None else bool(raw)


def _opt_str(raw: object) -> str | None:
    """An optional string parameter: absent stays absent, never "".

    Same family as :func:`_opt_int`/:func:`_opt_bool`. Used for
    ``reporter_surface``, where the hazard is specific: coercing None to ""
    would store an empty surface that reads as "reported, but blank" rather
    than "this reporter predates attribution", and the verb's surface
    validation would then have to accept "" to avoid failing every
    un-upgraded reporter — quietly re-admitting the unattributable row the
    column exists to eliminate. A non-string is treated as NOT REPORTED
    rather than stringified, so a caller bug cannot manufacture a surface
    like "None" that passes for a real one.
    """
    return raw if isinstance(raw, str) and raw.strip() else None


def _heartbeat_failure_first_at(*, status: str, raw: object) -> str | None:
    """Preserve an omitted healthy failure timestamp as typed ``NULL``.

    A supplied empty string is distinct from omission: it is an invalid
    timestamp that must fail before it can reach the database serializer.
    """
    if status != "heartbeat" or raw is None:
        return None
    if isinstance(raw, str) and raw.strip():
        return raw
    raise VerbError(
        "invalid_heartbeat_failure_first_at",
        "heartbeat_failure_first_at must be omitted for a healthy heartbeat or be a "
        "non-empty failure timestamp; empty strings are not timestamps.",
    )


def _failure_result(
    *,
    code: str,
    message: str,
    data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a failed ``ActionResult`` dict with all required keys."""
    timestamp = _now_iso()
    error: ErrorDetail = {
        "type": "agent_messaging_error",
        "code": code,
        "message": message,
        "details": {},
        "severity": "error",
        "timestamp": timestamp,
    }
    return {
        "action_status": "failed",
        "data": dict(data or {}),
        "actions": [],
        "error": error,
        "timestamp": timestamp,
    }


def _extract_message(params: dict[str, Any]) -> str:
    raw = params.get("message")
    return str(raw) if raw is not None else ""


def _find_bridge_by_session(
    manager: BridgeSessionManager,
    session_id: str,
) -> BridgeSessionState | None:
    for bridge in manager.list_active():
        if bridge.session_id == session_id:
            return bridge
    return None


def _find_claude_code_bridge_by_parent_pid(
    *,
    manager: BridgeSessionManager,
    peer_registry: PeerRegistry,
    parent_pid: int,
) -> BridgeSessionState | None:
    """Locate the open ``claude_code`` bridge bound to ``parent_pid``.

    Walks the peer registry to find every binding registered under
    ``agent_id="claude_code"`` whose live bridge has the matching
    ``parent_pid``.  On multiple matches (reconnect leak), prefer the
    most recently created bridge — the older one is almost certainly
    stale.
    """
    bindings = peer_registry.list_agent_ids().get("claude_code", [])
    matches: list[BridgeSessionState] = []
    for binding in bindings:
        bridge = manager.get(binding.bridge_id)
        if bridge is None or bridge.closed:
            continue
        if bridge.parent_pid != parent_pid:
            continue
        matches.append(bridge)
    if not matches:
        return None
    return max(matches, key=lambda b: b.created_at)


@dataclass(frozen=True, slots=True)
class _RoleSendSender:
    """Sender identity for a role-addressed (``peer_send_by_name``) dispatch.

    ``reply_to_role`` is the load-bearing field: when non-empty the recipient's
    envelope surfaces a role reply-to (``peer_send_by_name name=<role>``) so the
    return leg is durable (reconnect-surviving), closing the KB-08 §4 wart. The
    other fields are sender provenance (envelope display + persisted message).
    """

    agent_id: str
    agent_instance_id: str
    session_label: str
    bridge_id: str
    reply_to_role: str


def _str_field(value: object) -> str:
    """Return ``value`` if it is a non-empty string, else ``""``."""
    return value if isinstance(value, str) and value else ""


def _sender_principal_kind_from_state(state: dict[str, Any]) -> str:
    """Classify a role send from server-authenticated process context only."""
    try:
        extract_authenticated_principal(state)
    except PermissionError:
        return SENDER_PRINCIPAL_KIND_STDIO_AGENT
    return SENDER_PRINCIPAL_KIND_OAUTH_CLIENT


def _sender_from_role(
    role_name: str,
    origin_instance: str,
    state_service: Any,
    *,
    fallback_agent_id: str = SYSTEM_AGENT_ID,
    fallback_label: str = "",
) -> _RoleSendSender:
    """Sender identity for a role-stamped send: role reply-to + best-effort provenance.

    The role NAME is the durable reply-to address (survives a holder reconnect).
    The current binding supplies honest sender provenance when resolvable;
    resolution is best-effort (degrade-silent) so a provenance fault never breaks
    the send — ``reply_to_role`` is set regardless, so two-way still works.

    The fallbacks matter on the §34.6 attribution rung: there the caller's
    ``agent_id`` and label were ALREADY resolved out of the peer registry, so
    degrading them to the ``system`` sentinel when the separate role-binding
    read faults would throw away identity we hold. Callers with no better
    material keep the pre-existing defaults.
    """
    agent_id = fallback_agent_id or SYSTEM_AGENT_ID
    instance = origin_instance
    label = fallback_label or role_name
    try:
        binding = resolve_role_binding(state_service, role_name)
    except Exception:  # noqa: BLE001 — provenance is best-effort; never break the send
        binding = None
    if binding is not None:
        agent_id = binding.agent_id or agent_id
        instance = binding.agent_instance_id or origin_instance
        label = binding.session_label or label
    return _RoleSendSender(
        agent_id=agent_id,
        agent_instance_id=instance,
        session_label=label,
        bridge_id=SYSTEM_SCHEDULER_ID,
        reply_to_role=role_name,
    )


JOB_COMPLETION_PAYLOAD_CHAR_BUDGET: Final[int] = 2000
"""How much attached payload travels inside the delivered message.

A completion payload is unbounded (a Sheets dump, a query result), and an
inbox entry carries the whole message — the same arithmetic that put
``PEER_INBOX_DEFAULT_LIMIT`` at 5. Past this budget the message carries a
truncated head plus the exact verb that returns the whole payload, so the
recipient is never left guessing whether it saw everything.
"""


def _format_job_completion_message(
    *,
    job_id: str,
    provider_name: str,
    status: str,
    payload: dict[str, Any] | None,
) -> str:
    """Render the completion envelope the recipient acts on.

    Names route, content binds: the message states the job id, what produced
    it, its terminal status and the payload itself, so acting on it needs no
    second lookup. Truncation is disclosed IN the message with the verb that
    retrieves the rest — a silently clipped payload would read as a complete
    one.
    """
    origin = provider_name or "unknown provider"
    header = f"Job {job_id} finished with status '{status or 'unknown'}' (from {origin})."
    if not payload:
        body = (
            "No payload was attached. "
            f"Full job row: service_interface::job_service::get_job "
            f'{{"job_id": "{job_id}"}}'
        )
        return f"{header}\n{body}"
    rendered = json.dumps(payload, indent=2, default=str, sort_keys=True)
    label = "Result" if status == "completed" else "Error"
    if len(rendered) > JOB_COMPLETION_PAYLOAD_CHAR_BUDGET:
        head = rendered[:JOB_COMPLETION_PAYLOAD_CHAR_BUDGET]
        return (
            f"{header}\n{label} payload (TRUNCATED at "
            f"{JOB_COMPLETION_PAYLOAD_CHAR_BUDGET} of {len(rendered)} chars — "
            f"retrieve the whole payload with "
            f'service_interface::job_service::get_job {{"job_id": "{job_id}"}}'
            f"):\n{head}"
        )
    return f"{header}\n{label} payload:\n{rendered}"


def _decode_job_metadata(raw: object) -> dict[str, object]:
    """A job row's ``metadata`` column as a dict (it is TEXT holding JSON)."""
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    return {}


def _read_job_metadata(state_service: Any, job_id: str) -> dict[str, object] | None:
    """Current job metadata, or None when the row itself cannot be read.

    None and ``{}`` are deliberately different answers: an unreadable row must
    not be stamped, while a readable row with empty metadata is a fine thing to
    stamp onto.
    """
    result = state_service.read_state(
        namespace=FRAMEWORK_NAMESPACE,
        query={
            "table": FRAMEWORK_ASYNC_JOBS_TABLE,
            "filters": {"id": job_id},
            "limit": 1,
        },
    )
    data = result.get("data") if isinstance(result, dict) else None
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list) or not records:
        return None
    record = records[0]
    return _decode_job_metadata(record.get("metadata") if isinstance(record, dict) else None)


def _stamp_role_inbox_delivered(state_service: Any, job_id: str) -> bool:
    """Upgrade the job's completion_reach to role_inbox_delivered.

    Returns whether the stamp was written. Best-effort and LOUD, mirroring
    ``record_completion_reach``: a stamp that cannot be written leaves the
    pre-stamped unreached value in place, so a delivered-but-unstamped job is
    listed in the drain a second time rather than being lost. Over-reporting an
    unreached job is recoverable; silently dropping one is not.
    """
    try:
        metadata = _read_job_metadata(state_service, job_id)
        if metadata is None:
            logger.warning(
                "job %s not readable after delivery; completion_reach left "
                "unreached (it will be listed by the drain again)",
                job_id,
            )
            return False
        write_job_metadata(
            state_service,
            FRAMEWORK_NAMESPACE,
            FRAMEWORK_ASYNC_JOBS_TABLE,
            job_id,
            {**metadata, COMPLETION_REACH_KEY: REACH_ROLE_INBOX_DELIVERED},
        )
    except Exception:  # noqa: BLE001 — a failed stamp must never undo a delivery
        logger.error(
            "failed to stamp %s for job %s after a successful delivery; the "
            "unreached stamp stands and the drain will list it again",
            REACH_ROLE_INBOX_DELIVERED,
            job_id,
            exc_info=True,
        )
        return False
    return True


def _resolve_role_send_sender(
    state: dict[str, Any],
    state_service: Any,
) -> _RoleSendSender:
    """REL-01 Fork 4 resolution ladder for a role-addressed send's sender identity.

    Prefers the caller's DURABLE role (lifted into ``state`` from the flow
    trigger_data by ``ActionProcessor._lift_inference_vertex_identity``) so a role
    reply routes back to whoever holds it, surviving a holder reconnect. Ladder:

      1. role present → :func:`_sender_from_role` (role reply-to + provenance).
      2. else originating agent_instance_id present → fire-and-forget by instance,
         honestly labelled, no reply-to-role.
      3. else CALLER ATTRIBUTION present (§34.6) → an unregistered caller — the
         local CLI — whose opaque session key the SERVER already resolved
         against the peer registry in
         ``PlatformSurface._resolve_caller_attribution``. Its role rung is
         preferred over its instance rung for the same reconnect-survival
         reason as rung 1. Nothing here is caller-asserted: an unresolvable
         key arrives all-empty and falls through to rung 4.
      4. else → genuine scheduler-originated send → the system scheduler sentinel
         (pre-REL-01 behaviour, now reached ONLY when no caller identity rode the
         flow, e.g. scheduler / heartbeat-originated sends).
    """
    role_name = _str_field(state.get("inference_vertex_role"))
    origin_instance = _str_field(state.get("inference_vertex_session_id"))
    if role_name:
        return _sender_from_role(role_name, origin_instance, state_service)
    if origin_instance:
        return _RoleSendSender(
            agent_id=SYSTEM_AGENT_ID,
            agent_instance_id=origin_instance,
            session_label="",
            bridge_id=SYSTEM_SCHEDULER_ID,
            reply_to_role="",
        )
    attributed_role = _str_field(state.get("caller_attribution_role"))
    attributed_instance = _str_field(state.get("caller_attribution_instance_id"))
    if attributed_role:
        return _sender_from_role(
            attributed_role,
            attributed_instance,
            state_service,
            fallback_agent_id=_str_field(state.get("caller_attribution_agent_id")),
            fallback_label=_str_field(state.get("caller_attribution_label")),
        )
    if attributed_instance:
        return _RoleSendSender(
            agent_id=(_str_field(state.get("caller_attribution_agent_id")) or SYSTEM_AGENT_ID),
            agent_instance_id=attributed_instance,
            session_label=_str_field(state.get("caller_attribution_label")),
            bridge_id=SYSTEM_SCHEDULER_ID,
            reply_to_role="",
        )
    return _RoleSendSender(
        agent_id=SYSTEM_AGENT_ID,
        agent_instance_id=SYSTEM_SCHEDULER_ID,
        session_label=SYSTEM_SCHEDULER_LABEL,
        bridge_id=SYSTEM_SCHEDULER_ID,
        reply_to_role="",
    )


def _provider_get(provider: object, key: str) -> object | None:
    """Read ``key`` from a ConfigProvider-shaped object.

    Returns ``None`` when the provider is absent, has no ``.get`` method,
    or the key is missing. Callers wrap with ``_as_int`` / ``_as_bool`` /
    ``_as_str`` / ``_as_str_tuple`` which carry their own typed defaults.

    The third positional ``default`` parameter was dropped 2026-05-30 as
    part of the plugin-config-defaults unification: yaml's ``config:``
    block now lands as the lowest merge layer in
    ``ConfigManager.get_plugin_config``, so a hardcoded default at the
    ``_provider_get`` callsite duplicates the yaml entry and creates a
    drift surface (see Plugin Authoring Traps §10).
    """
    if provider is None:
        return None
    getter = getattr(provider, "get", None)
    if not callable(getter):
        return None
    return getter(key)


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return default


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool):  # bool is a subclass of int — treat as default
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return default


def _as_optional_int(value: object) -> int | None:
    """Like :func:`_as_int` but returns ``None`` when no usable value was given.

    Used for fields whose semantics differ between "unset" (dynamic
    behavior) and "set to N" (explicit override).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _as_str(value: object, default: str) -> str:
    if isinstance(value, str) and value:
        return value
    return default


def _as_str_tuple(
    value: object,
    *,
    default: tuple[str, ...] = (),
) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, (list, tuple)):
        items = tuple(str(v) for v in value)
        return items or default
    return (str(value),)


def _as_object(value: object) -> dict[str, Any]:
    """Return a shallow object mapping or an empty mapping for validation."""
    return dict(value) if isinstance(value, dict) else {}


def _as_work_class_defaults(value: object) -> dict[str, dict[str, str]]:
    """Coerce ``plugin.yaml``'s ``work_class_defaults`` block (§6 L3 rule 1)
    into ``{work_class: {"model": ..., "effort": ...}}``. Tolerant of a
    malformed entry (skips it, logs, keeps the rest) rather than failing the
    whole config build over one operator typo — matches ``_as_bool``/
    ``_as_int``'s "fall back rather than crash" posture. Absent/wrong-typed
    input returns ``{}``, which is exactly today's behavior (no defaults
    applied, spawn_session leaves model/effort as the caller passed them)."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for work_class, entry in value.items():
        if not isinstance(work_class, str) or not isinstance(entry, dict):
            logger.warning(
                "Skipping malformed work_class_defaults entry for %r: not a mapping",
                work_class,
            )
            continue
        result[work_class] = {str(k): str(v) for k, v in entry.items() if k in {"model", "effort"}}
    return result


def _as_work_class_tool_allowlists(value: object) -> dict[str, tuple[str, ...]]:
    """Coerce ``plugin.yaml``'s ``work_class_tool_allowlists`` block (§6
    permission-mode ruling, 2026-08-03) into ``{work_class: (tool_name, ...)}``.
    Tolerant of a malformed entry (skips it, logs, keeps the rest) — same
    posture as ``_as_work_class_defaults``. Absent/wrong-typed input returns
    ``{}``: every headless spawn is STILL gated (the hook is always injected,
    per ``headless_adapter.py``'s ``_spawn_env``), just with an empty
    allowlist — never an open default."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, tuple[str, ...]] = {}
    for work_class, entry in value.items():
        if not isinstance(work_class, str) or not isinstance(entry, (list, tuple)):
            logger.warning(
                "Skipping malformed work_class_tool_allowlists entry for %r: not a list",
                work_class,
            )
            continue
        result[work_class] = tuple(str(t) for t in entry)
    return result


def _resolve_solet_name() -> str:
    """Return the solet identity from ``$SOLET_NAME``.

    Single source of truth across the platform: every plugin that
    surfaces a deployment label to external clients reads the same
    env var the bootstrap script sets.  Empty string when unset (laptop
    dev mode); downstream callers apply their own fallback.
    """
    import os  # noqa: PLC0415 — kept local so the import is greppable here

    return os.environ.get("SOLET_NAME", "").strip()


# R4 seed-packaging audit, Package B (2026-08-10): root_manifest.yaml's own
# unwritten placeholder for `solet_name:` -- the midwife rewrites this
# field ONLY at genesis, so a raw/pre-genesis checkout keeps this exact
# literal, which rung 2 below must skip rather than treat as a real name.
_ROOT_MANIFEST_PLACEHOLDER = "solet"


def _resolve_solet_name_for_memory_tags() -> str:
    """Three-rung origin-resolution ladder for memory-tag scoping ONLY.

    SEPARATE from :func:`_resolve_solet_name` by deliberate ruling
    (coordinator seat, arm-8491e1ba, 2026-08-10): ``_resolve_solet_name`` has
    an unrelated third caller (the MCP streamable router's own identity
    label) whose consumers were never traced, so it stays byte-identical
    here -- containment over elegance on mint night. This function is
    called ONLY from the two M2.2 memory-tag verbs
    (``generate_curation_report``/``reinforce_by_slug``); unifying the two
    resolvers, after tracing the router-identity string's actual
    consumers, is a named post-mint backlog item, not this change.

    The SAME ladder as the hooks' own ``_journal.solet_name()``
    (``.claude/hooks/memory_passthrough/_journal.py`` and its vendored
    plugin copy), a parity test asserts the two agree on OUTPUT across a
    matrix of env/file/dirname combinations -- never on implementation
    approach, since this side runs inside the venv (real ``yaml.safe_load``)
    while the hooks' side runs outside it (a minimal regex line-scan,
    since PyYAML is a venv-only dependency there, measured 2026-08-10).

    1. ``SOLET_NAME`` env var, if set.
    2. ``root_manifest.yaml``'s own ``solet_name:`` field, placeholder-
       skipped.
    3. ``CLAUDE_PROJECT_DIR``-basename -- :func:`_resolve_solet_name`'s
       existing sole behavior, preserved here as the final fallback.

    Empty string only when every rung is exhausted (no env var AND no
    resolvable ``CLAUDE_PROJECT_DIR``) -- callers apply their own
    fail-loud contract on an empty result, same as the existing function.
    """
    import os  # noqa: PLC0415 — kept local, mirrors _resolve_solet_name's own style

    import yaml  # noqa: PLC0415

    env_name = os.environ.get("SOLET_NAME", "").strip()
    if env_name:
        return env_name
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if not project_dir:
        return ""
    try:
        with open(os.path.join(project_dir, "root_manifest.yaml"), encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        candidate = str((data or {}).get("solet_name", "")).strip()
        if candidate and candidate != _ROOT_MANIFEST_PLACEHOLDER:
            return candidate
    except (OSError, yaml.YAMLError):
        pass  # unreadable, absent, or malformed -- fall through, never raise
    return os.path.basename(os.path.normpath(os.path.abspath(project_dir)))


__all__ = ["AgentMessagingPlugin"]
