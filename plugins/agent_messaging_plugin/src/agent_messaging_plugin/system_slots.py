"""System-slot declarations + the §6.1 reserved-keyspace claim gate.

A SYSTEM slot is a capability slot the *platform* declares (first + only today:
the ``sys:autonomic`` inference-of-last-resort, INF-01). Its identity is a
platform CODE CONSTANT living behind the reserved ``sys:`` prefix — the
no-role-name-literal rule scopes to USER names only (§6/§D.2). User and system
identities occupy DISJOINT ``external_id`` keyspaces, so collision is
structurally impossible.

Two fill kinds, discriminated by the declaration's ``owner_plugin`` + ``holder_kind``:

* **plugin-owned** (``owner_plugin`` set) — only the declared owner plugin may
  claim it, verified against the SERVER-BUILT :class:`CallContext` principal
  (never caller-supplied). No production plugin-owned slot exists yet; the gate
  machinery is exercised via a fixture owned-slot in the §6.1 smokes.
* **session-filled** (``owner_plugin=None``, e.g. ``sys:autonomic``) — ungated,
  assigned by the §D.9 auto-assignment policy (INF-01 lane: vacancy-fill +
  succession over the live bridge set), NEVER the general ``peer_claim_role``.

This module is the SUBSTRATE INF-01 consumes (per the seam agreement
``workbench/2026-07-03_inf01_slicec_seam_boundary.md``): it imports
``SYS_AUTONOMIC_SLOT`` + calls the slice-B ``claim_role_binding_v4`` primitive
from its §D.9 hooks. The gate here governs the general ``peer_claim_role`` path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from ananta.core.services.call_context import CallContext, PrincipalKind
from ananta.llm.agent_messaging.role_binding import (
    HOLDER_KIND_INFERENCE_PROVIDER,
    HOLDER_KIND_SESSION,
    SYS_AUTONOMIC_SLOT,
    SYSTEM_ROLE_PREFIX,
    is_system_role,
)

# The ``PrincipalKind`` domain value for a plugin-bound principal (call_context.py
# stamps it via CallContext.for_plugin). Named to avoid a magic string.
_PRINCIPAL_KIND_PLUGIN: PrincipalKind = "plugin"
_VALID_HOLDER_KINDS = frozenset({HOLDER_KIND_SESSION, HOLDER_KIND_INFERENCE_PROVIDER})


@dataclass(frozen=True, slots=True)
class SystemSlotDeclaration:
    """A platform-declared system slot (§6.1/§D.2) — a slot CONSTANT, not a user name.

    ``owner_plugin`` set → PLUGIN-OWNED (owner gate); ``owner_plugin=None`` →
    SESSION-FILLED (ungated, §D.9 auto-assignment). ``holder_kind`` is a code
    constant, never a role name.
    """

    slot_name: str
    owner_plugin: str | None
    holder_kind: str


# The platform's declaration registry — the code-level list of system-slot
# constants, fixed at import (startup). Dict keys ARE ``slot_name``, so a
# duplicate declaration is structurally impossible. ``sys:autonomic`` is the only
# slot today (session-filled); plugin-owned slots are future headroom.
SYSTEM_SLOT_DECLARATIONS: Mapping[str, SystemSlotDeclaration] = {
    SYS_AUTONOMIC_SLOT: SystemSlotDeclaration(
        slot_name=SYS_AUTONOMIC_SLOT,
        owner_plugin=None,
        holder_kind=HOLDER_KIND_SESSION,
    ),
}


class SystemSlotDeclarationError(Exception):
    """A system-slot declaration is malformed (fail-startup integrity check)."""


def validate_system_slot_declarations(
    declarations: Mapping[str, SystemSlotDeclaration] = SYSTEM_SLOT_DECLARATIONS,
) -> None:
    """Fail-loud integrity check of the declared system slots (called at startup).

    Enforces the registry key matches ``slot_name``, the name is in the reserved
    keyspace, and ``holder_kind`` is a known code constant. Duplicate declarations
    are structurally impossible (dict keys). Raises
    :class:`SystemSlotDeclarationError` — a malformed platform declaration is a
    startup-blocking bug, never a silently-tolerated state.
    """
    for key, slot in declarations.items():
        if key != slot.slot_name:
            raise SystemSlotDeclarationError(
                f"declaration registry key {key!r} does not match slot_name {slot.slot_name!r}",
            )
        if not is_system_role(slot.slot_name):
            raise SystemSlotDeclarationError(
                f"system slot {slot.slot_name!r} is not in the reserved "
                f"{SYSTEM_ROLE_PREFIX!r} keyspace",
            )
        if slot.holder_kind not in _VALID_HOLDER_KINDS:
            raise SystemSlotDeclarationError(
                f"system slot {slot.slot_name!r} has unknown holder_kind {slot.holder_kind!r}",
            )


def get_system_slot(
    name: str,
    declarations: Mapping[str, SystemSlotDeclaration] = SYSTEM_SLOT_DECLARATIONS,
) -> SystemSlotDeclaration | None:
    """The declaration for ``name`` (or ``None`` if not a declared system slot)."""
    return declarations.get(name)


class SystemSlotClaimDecision(Enum):
    """Outcome of the §6.1 reserved-keyspace claim gate for ``peer_claim_role``."""

    NOT_SYSTEM = "not_system"   # a normal user role → proceed with the ordinary claim
    ALLOW = "allow"             # plugin-owned slot, verified declared owner → proceed
    REJECT = "reject"           # denied (see reason)


@dataclass(frozen=True, slots=True)
class SystemSlotClaimVerdict:
    """The gate's decision + a human-facing reason for the REJECT path."""

    decision: SystemSlotClaimDecision
    reason: str = ""


def evaluate_system_slot_claim(
    name: str,
    call_context: CallContext | None,
    declarations: Mapping[str, SystemSlotDeclaration] = SYSTEM_SLOT_DECLARATIONS,
) -> SystemSlotClaimVerdict:
    """§6.1 claim gate for a name arriving at the general ``peer_claim_role``.

    * not a ``sys:`` name → ``NOT_SYSTEM`` (the caller proceeds with the normal claim).
    * ``sys:`` name not declared → ``REJECT`` (unknown slot in the reserved keyspace).
    * SESSION-FILLED (``owner_plugin`` None, e.g. ``sys:autonomic``) → ``REJECT``:
      assigned by the §D.9 auto-assignment policy, NOT this user-facing verb.
    * PLUGIN-OWNED → ``ALLOW`` iff the principal is the declared owner
      (``principal_kind=='plugin'`` AND ``calling_plugin==owner_plugin``); else ``REJECT``.

    ``call_context`` MUST be the SERVER-BUILT context (lifted into ``state`` by the
    action processor — never read from caller ``params``), so slot ownership cannot
    be forged.
    """
    if not is_system_role(name):
        return SystemSlotClaimVerdict(SystemSlotClaimDecision.NOT_SYSTEM)
    slot = declarations.get(name)
    if slot is None:
        return SystemSlotClaimVerdict(
            SystemSlotClaimDecision.REJECT,
            f"{name!r} is in the reserved {SYSTEM_ROLE_PREFIX!r} keyspace but is "
            f"not a declared system slot",
        )
    if slot.owner_plugin is None:
        return SystemSlotClaimVerdict(
            SystemSlotClaimDecision.REJECT,
            f"system slot {name!r} is session-filled (auto-assigned per INF-01 "
            f"§D.9); it is not claimable via peer_claim_role",
        )
    if (
        call_context is None
        or call_context.principal_kind != _PRINCIPAL_KIND_PLUGIN
        or call_context.calling_plugin != slot.owner_plugin
    ):
        return SystemSlotClaimVerdict(
            SystemSlotClaimDecision.REJECT,
            f"system slot {name!r} is owned by plugin {slot.owner_plugin!r}; only "
            f"that plugin (server-verified principal) may claim it",
        )
    return SystemSlotClaimVerdict(SystemSlotClaimDecision.ALLOW)


__all__ = [
    "SYSTEM_SLOT_DECLARATIONS",
    "SystemSlotClaimDecision",
    "SystemSlotClaimVerdict",
    "SystemSlotDeclaration",
    "SystemSlotDeclarationError",
    "evaluate_system_slot_claim",
    "get_system_slot",
    "validate_system_slot_declarations",
]
