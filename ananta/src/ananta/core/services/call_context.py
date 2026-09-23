"""Server-built CallContext for service-interface authorization.

W-VAULT-INTERFACE-EXTEND (P0 Tier 1, state-service consolidation campaign):
the CallContext carries the calling principal's identity so service
implementations can enforce per-method admin/operator gating + per-key
namespace ownership.

Construction is ALWAYS server-side. ActionProcessor builds it for queued
service-interface actions; VaultServiceProxy builds it for bound-service
calls. Caller-supplied `call_context` arguments are dropped on the floor
— never trusted, never propagated. Likewise `source_plugin` on
QueuedActionProtocol is stamped at enqueue time by the dispatching
plugin's identity; external/operator MCP calls leave it None and the
principal_kind comes from the authenticated/operator context.

`CallContext.calling_plugin` is NEVER inferred from a process key's
provider or from any caller-supplied metadata. The only sources of truth
are (a) the plugin lifecycle binding at proxy construction, (b) the
ActionProcessor's routing-table-resolved source_plugin on a queued
action, (c) the authenticated_principal injected through state for
operator-bridge calls.

Reference: workbench/2026-06-07_state_service_consolidation_master_plan.md §3.3.3;
workbench/2026-06-07_tier_1_dispatch_signoff_v2.md correction #3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PrincipalKind = Literal["plugin", "operator", "operator_equivalent", "external"]


@dataclass(frozen=True)
class CallContext:
    """Server-constructed authorization context for a service-interface call.

    Frozen + slots-less because instances flow through positional /
    keyword parameters and may need pickling for queued-action
    persistence in some flows. Fields kept narrow on purpose — adding
    state here is an invitation for downstream consumers to start
    reading from the wrong source.

    Attributes:
        calling_plugin: The plugin name this call is bound to. None for
            operator / external / unbound contexts.
        principal_kind: Discriminator for the principal class. Used by
            per-method authorization helpers (see
            VaultServiceImplementation.assert_operator_principal).
        principal_id: Optional operator / external identifier (e.g., an
            OAuth client_id or operator marker). Not used for
            plugin-bound callers.
    """

    calling_plugin: str | None
    principal_kind: PrincipalKind
    principal_id: str | None = None

    @classmethod
    def for_plugin(cls, plugin_name: str) -> CallContext:
        """Caller-bound plugin context (the common case)."""
        return cls(
            calling_plugin=plugin_name,
            principal_kind="plugin",
        )

    @classmethod
    def for_operator(cls) -> CallContext:
        """Operator-direct context (CLI / MCP from the operator's session)."""
        return cls(
            calling_plugin=None,
            principal_kind="operator",
        )

    @classmethod
    def for_operator_equivalent(cls, principal_id: str | None = None) -> CallContext:
        """Operator-equivalent context (per `oauth_client.operator_equivalent`).

        Used when an authenticated bridge principal carries the
        operator-equivalent marker — that principal can invoke admin /
        operator-only vault methods on the operator's behalf.
        """
        return cls(
            calling_plugin=None,
            principal_kind="operator_equivalent",
            principal_id=principal_id,
        )

    @classmethod
    def for_external(cls, principal_id: str | None = None) -> CallContext:
        """External-principal context (authenticated, non-operator)."""
        return cls(
            calling_plugin=None,
            principal_kind="external",
            principal_id=principal_id,
        )

    @property
    def is_operator_principal(self) -> bool:
        """True iff this principal is allowed to invoke admin/operator-only methods."""
        return self.principal_kind in ("operator", "operator_equivalent")


class VaultAccessDeniedError(Exception):
    """Raised by VaultService implementations when CallContext authz fails.

    Per master plan §3.3.4: namespace-mismatch raises at Tier 2 (wired
    here, activated at W-VAULT-CALLER-ENFORCE); operator-only method
    violations raise from Tier 1 (active now, enforced by the per-method
    helper in the concrete VaultService implementation).
    """


class VaultKeyMalformedError(Exception):
    """Raised by VaultService implementations on malformed scoped keys.

    Tier 2 W-VAULT-CALLER-ENFORCE: scoped keys MUST be in the form
    ``<solet>.<plugin>.<credential>`` (at least three segments
    separated by ``.``; the credential portion may contain further dots).
    Plugin-principal calls with a key that fails this shape raise
    ``VaultKeyMalformedError`` from ``_enforce_namespace`` BEFORE the
    namespace ownership check, so the caller sees a structural error
    rather than an authorization-denial misdiagnosis.
    """
