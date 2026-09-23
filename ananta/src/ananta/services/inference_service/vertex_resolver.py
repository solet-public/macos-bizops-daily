"""Phase 5 (Seam B) — per-flow inference vertex resolver.

Resolves the inference vertex for a flow BEFORE the configured default
provider runs. An MCP-originating flow is tagged in its ``trigger_data``
with the originating session's durable role (``inference_vertex_role``,
◆R2) and ephemeral instance (``inference_vertex_session_id``); this
resolver reads those tags and decides one of three routings:

- ``PROVIDER`` — a live :class:`SessionInferenceProvider` resolves; the
  vertex method routes to it (emits a bridge-delivery channel event, does
  NOT run the local prompt pipeline).
- ``DEFER`` — the flow is *explicitly bound* to a session that is absent
  right now (role-holder disconnected, or a roleless instance that held a
  provider earlier this lifetime). Record + no-op; NEVER a silent fall
  back to the default local model. A role-bound deferral re-resolves when
  a session re-claims the role (that re-drive trigger is a documented
  follow-up; v1 records + no-ops).
- ``DEFAULT`` — no vertex binding (untagged flows), or a roleless instance
  that was never bound in this process lifetime (post-restart, streamable
  peers scoped out per D-IF11, sessions without ``provides_inference``).
  The default provider handles it unchanged.

Governing asymmetry: NEVER route an
explicitly-bound-this-lifetime vertex to the default model; DEFAULT only
for never-bound-in-this-lifetime or untagged flows. The failure modes are
asymmetric — silent-Qwen on a bound session reintroduces the hallucination
this phase exists to kill; deferring an unbound flow black-holes it.

The resolver is an InferenceService-internal collaborator (NOT a registered
verb — no create-process dual-write per the locked design). It reaches
``AgentMessagingPlugin`` via ``plugin_manager.get_plugin`` (by name, no
import) and calls its public accessors; the role→instance resolution reuses
the plugin's ``agent_role_binding`` authority, never a parallel one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ananta.llm.agent_messaging.role_binding import SYS_AUTONOMIC_SLOT

if TYPE_CHECKING:
    from ananta.core.domain.types import ActionResult
    from ananta.core.plugins.plugin_manager import PluginManager

logger = logging.getLogger(__name__)

# The registered name of the bridge plugin that owns the peer/role sidecar
# and the outbound vertex tag. Reached by name (not import) to keep the
# core → plugin dependency direction acyclic.
_AGENT_MESSAGING_PLUGIN_NAME = "agent_messaging_plugin"

_TAG_ROLE = "inference_vertex_role"
_TAG_INSTANCE = "inference_vertex_session_id"


class VertexProvider(Protocol):
    """Structural type for a per-session inference vertex.

    Satisfied by ``SessionInferenceProvider``; the two vertex-routed
    methods emit a bridge-delivery event and return an ``ActionResult``.
    """

    def process_error(
        self, params: dict[str, object], state: dict[str, object],
    ) -> ActionResult: ...

    def process_results(
        self, params: dict[str, object], state: dict[str, object],
    ) -> ActionResult: ...


@runtime_checkable
class _VertexResolutionPlugin(Protocol):
    """The subset of ``AgentMessagingPlugin`` the resolver depends on."""

    def resolve_role_to_instance(self, role: str) -> str | None: ...

    def get_inference_provider(
        self, agent_instance_id: str,
    ) -> VertexProvider | None: ...

    def was_inference_provider_bound(self, agent_instance_id: str) -> bool: ...

    def get_autonomic_provider(self) -> VertexProvider | None: ...


@runtime_checkable
class _FlowRowReader(Protocol):
    """The state-service surface the resolver reads flow ``trigger_data`` with."""

    def read_state(
        self, *, namespace: str, query: dict[str, object],
    ) -> dict[str, object]: ...


class VertexRouting(Enum):
    """How a flow's result/error processing should be routed."""

    PROVIDER = "provider"
    DEFER = "defer"
    DEFAULT = "default"


@dataclass(frozen=True, slots=True)
class VertexResolution:
    """The resolver's verdict for one flow."""

    routing: VertexRouting
    provider: VertexProvider | None
    role: str | None
    agent_instance_id: str | None

    @classmethod
    def default(cls) -> VertexResolution:
        return cls(VertexRouting.DEFAULT, None, None, None)


def _str_or_none(value: object) -> str | None:
    """A non-empty string, or ``None`` (absent / empty / wrong type)."""
    return value if isinstance(value, str) and value else None


class InferenceProviderResolver:
    """Resolve the per-flow inference vertex from a flow's ``trigger_data``."""

    def __init__(
        self,
        *,
        plugin_manager: PluginManager,
        state_service: object | None,
    ) -> None:
        self._plugin_manager = plugin_manager
        self._state_service = state_service

    def resolve(self, state: dict[str, object]) -> VertexResolution:
        """Classify a flow's vertex routing from its ``state`` dict.

        ``state`` carries ``flow_id`` (injected by
        ``ActionProcessor._execute_vertex_inference``); the vertex tags live
        on the flow row's ``trigger_data`` column, not in ``state``.
        """
        flow_id = _str_or_none(state.get("flow_id"))
        if flow_id is None:
            return VertexResolution.default()
        trigger_data = self._read_trigger_data(flow_id)
        if trigger_data is None:
            return VertexResolution.default()

        role = _str_or_none(trigger_data.get(_TAG_ROLE))
        instance = _str_or_none(trigger_data.get(_TAG_INSTANCE))
        if role is None and instance is None:
            return VertexResolution.default()

        plugin = self._get_messaging_plugin()
        if plugin is None:
            # The bridge layer is unreachable — cannot route to a session at
            # all. Degrade to the default provider rather than deferring into
            # a hole; a missing messaging plugin is a whole-solet fault.
            logger.warning(
                "vertex resolver: agent_messaging_plugin unavailable; "
                "flow=%s falls back to default provider", flow_id,
            )
            return VertexResolution.default()

        if role is not None:
            return self._resolve_by_role(plugin, role, instance)
        if instance is not None:
            return self._resolve_by_instance(plugin, instance)
        return VertexResolution.default()

    def _resolve_by_role(
        self,
        plugin: _VertexResolutionPlugin,
        role: str,
        instance: str | None,
    ) -> VertexResolution:
        """Role-tagged flows resolve BY ROLE and NEVER fall to default.

        A role tag means the originating session was explicitly bound; the
        durable ``agent_role_binding`` row is the resolution authority. If it
        resolves to a live provider → route; otherwise (holder disconnected
        or binding vacant) → DEFER, so a re-claim re-resolves it. Silent-Qwen
        on a role-bound flow is exactly the prohibited path.

        N3 (Rev-C): the role→instance lookup reads ``agent_role_binding``
        and can raise (same DB-read class as B1). A role-bound flow must
        NEVER crash the vertex or fall to the default model on a transient
        read fault — degrade to DEFER (loud), symmetric with
        ``_get_messaging_plugin``. Role-tagged flows only ever return
        PROVIDER or DEFER.
        """
        try:
            current_instance = plugin.resolve_role_to_instance(role)
            if current_instance is not None:
                provider = plugin.get_inference_provider(current_instance)
                if provider is not None:
                    return VertexResolution(
                        VertexRouting.PROVIDER, provider, role, current_instance,
                    )
            return VertexResolution(
                VertexRouting.DEFER, None, role, current_instance or instance,
            )
        except Exception:  # noqa: BLE001 — role-bound: DEFER on any lookup fault, never crash/Qwen
            logger.warning(
                "vertex resolver: role→instance lookup failed for role=%r; "
                "DEFERring (role-bound path never falls to the default model)",
                role,
                exc_info=True,
            )
            return VertexResolution(VertexRouting.DEFER, None, role, instance)

    def _resolve_by_instance(
        self, plugin: _VertexResolutionPlugin, instance: str,
    ) -> VertexResolution:
        """Roleless (instance-only) flows: live → route; bound-then-gone →
        DEFER; never-bound-this-lifetime → default.

        N3 (Rev-C): if a lookup raises, the binding is unconfirmable — a
        roleless instance has no durable identity to recover, so we cannot
        safely DEFER (that would risk black-holing a genuinely never-bound /
        streamable flow that R6/D-IF11 require to route DEFAULT). Degrade to
        DEFAULT (loud). DEFER stays reserved for the case where we KNOW the
        instance was bound this lifetime (tombstone hit).
        """
        try:
            provider = plugin.get_inference_provider(instance)
            if provider is not None:  # case 3a
                return VertexResolution(
                    VertexRouting.PROVIDER, provider, None, instance,
                )
            if plugin.was_inference_provider_bound(instance):  # case 3b
                return VertexResolution(VertexRouting.DEFER, None, None, instance)
            return VertexResolution.default()  # case 3c
        except Exception:  # noqa: BLE001 — roleless + unconfirmable binding → DEFAULT (loud)
            logger.warning(
                "vertex resolver: instance lookup failed for instance=%r; "
                "binding unconfirmable, falling back to DEFAULT (roleless has "
                "no durable identity to safely DEFER)",
                instance,
                exc_info=True,
            )
            return VertexResolution.default()

    def resolve_autonomic(self) -> VertexResolution:
        """Resolve the ``sys:autonomic`` fault-edge holder for a DEFAULT flow.

        INF-01 §D.3/§D.9: the organism's own error/result turn (no per-flow
        vertex binding — the DEFAULT verdict) routes to the frontier session
        holding the ``sys:autonomic`` system slot instead of the local default
        model. A NEW call site at ``_route_vertex``'s DEFAULT fallthrough,
        reusing the role→instance→live-provider path + the
        ``SessionInferenceProvider`` forwarder as-built.

        Self-guards the §D.3 HARD EDGE: resolving the slot needs the SAME
        messaging plugin, so a plugin-unreachable solet fault → DEFAULT
        (stays LOCAL) here too — it structurally cannot reach the holder. A
        lookup FAULT (the accessor raises) also degrades to DEFAULT (loud) —
        the fault-edge's safe floor, symmetric with
        :meth:`_resolve_by_instance`: an unconfirmable slot must not
        black-hole the organism's own turn.

        ★ Sub-slice-2 FLIP (INF-01 §D.9, Day-ruled): a KNOWN-vacant / gone
        holder (the accessor answers ``None``) → DEFER, never LOCAL. The
        auto-assignment lifecycle (Trigger-1 vacancy-fill / crash-heal,
        Trigger-2 succession, manual-set) keeps the slot normally filled, so
        a vacancy is a transient window — the deferral lands in the durable
        NO-LOSS queue and the first-claim drain re-lights the lane. The
        sub-slice-1 interim (vacant → DEFAULT → local model) is DEAD: that
        path was the permanent-silent-qwen-on-vacancy defect INF-01 exists
        to kill. The flip-assertion smoke fails the build if vacancy ever
        falls LOCAL again.
        """
        plugin = self._get_messaging_plugin()
        if plugin is None:
            return VertexResolution.default()
        try:
            provider = plugin.get_autonomic_provider()
        except Exception:  # noqa: BLE001 — fault-edge degrades to LOCAL (loud), never crashes the turn
            logger.warning(
                "vertex resolver: sys:autonomic resolution failed; falling "
                "back to the default local model (fault-edge safe floor)",
                exc_info=True,
            )
            return VertexResolution.default()
        if provider is None:
            return VertexResolution(
                VertexRouting.DEFER, None, SYS_AUTONOMIC_SLOT, None,
            )
        return VertexResolution(
            VertexRouting.PROVIDER, provider, SYS_AUTONOMIC_SLOT, None,
        )

    def _get_messaging_plugin(self) -> _VertexResolutionPlugin | None:
        try:
            plugin = self._plugin_manager.get_plugin(_AGENT_MESSAGING_PLUGIN_NAME)
        except Exception:  # noqa: BLE001 — any lookup fault → default fallback
            return None
        if isinstance(plugin, _VertexResolutionPlugin):
            return plugin
        return None

    def _read_trigger_data(self, flow_id: str) -> dict[str, object] | None:
        """Read the flow row's parsed ``trigger_data`` dict, or ``None``.

        Mirrors ``action_queue_poller._extract_trigger_data_from_flow_row``
        so the resolver reads the tag anchor the same way the poller does,
        without threading a new dependency.
        """
        if not isinstance(self._state_service, _FlowRowReader):
            return None
        result = self._state_service.read_state(
            namespace="core",
            query={"table": "flows", "filters": {"id": flow_id}},
        )
        data = result.get("data")
        if not isinstance(data, dict):
            return None
        records = data.get("records")
        if not isinstance(records, list) or not records:
            return None
        first = records[0]
        if not isinstance(first, dict):
            return None
        raw = first.get("trigger_data")
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return None
            return parsed if isinstance(parsed, dict) else None
        return None


__all__ = [
    "InferenceProviderResolver",
    "VertexProvider",
    "VertexResolution",
    "VertexRouting",
]
