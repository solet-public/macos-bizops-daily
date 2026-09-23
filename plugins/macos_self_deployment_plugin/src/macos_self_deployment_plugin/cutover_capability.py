"""Fail-closed activation gate for the reconciliation cutover channel.

The adjudicated design (codex §12, adopted as D1) makes this channel
*inactivatable* until audit findings D-1.1 through D-1.10 are closed or proven
structurally unreachable in this path, plus the eleventh property: no SIGKILL
reachable from any sequence this channel drives.  The build brief states the
production capability version REMAINS ABSENT until then.

So this module is a gate, not a declaration.  It requires an attestation module
that the D-track will add when those properties actually close, and refuses
when it is missing or incomplete.  Today it always refuses, and that is the
correct behaviour rather than a placeholder: ``green_candidate.kill`` still
issues ``os.kill(pid, 9)`` and is reachable from the candidate-cleanup path the
shared orchestrator uses, so a channel that executed today would violate the
never-kill-9 ruling that binds it.

Two things this gate deliberately does NOT do:

* It does not scan source for signal-9 call sites.  Reachability is a
  behavioural property of a path, and a keyword scan over source cannot
  establish it — it would report clean on a call reached through an alias, and
  report dirty on a docstring.  The attestation is produced by the work that
  actually closes each property, with its own negative smoke per property; this
  gate checks that the attestation exists and is complete.
* It reads no environment variable, config key, or file path.  An activation
  switch that a caller can set is not a gate.  The only way to satisfy it is to
  land a module that names every closed property.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "CUTOVER_PROTOCOL_VERSION",
    "REQUIRED_PROPERTIES",
    "CutoverCapability",
    "CutoverCapabilityError",
    "load_cutover_capability",
    "require_cutover_capability",
]

#: Bumping this is a deliberate act: it invalidates every prior attestation,
#: which is what should happen when the property set changes.
CUTOVER_PROTOCOL_VERSION: Final[int] = 1

#: The eleven properties codex §12 enumerates.  Named individually rather than
#: counted, so a partial attestation says WHICH closure is missing — "9 of 11"
#: is not an actionable refusal.
REQUIRED_PROPERTIES: Final[frozenset[str]] = frozenset({
    "d_1_1_application_level_candidate_readiness",
    "d_1_2_rollback_restores_prior_gate_on_prior_instance",
    "d_1_3_active_instance_proven_to_execute_current",
    "d_1_4_failed_symlink_compensation_leaves_recovery_marker",
    "d_1_5_durable_rollback_restores_or_refuses_manifest_mismatch",
    "d_1_6_rollback_selects_captured_prior_instance_id",
    "d_1_7_activation_refusal_isolates_rejected_candidate",
    "d_1_8_candidate_boot_cannot_scrub_live_runtime_files",
    "d_1_9_candidate_intent_durable_before_spawn",
    "d_1_10_instance_port_color_uniqueness_enforced",
    "no_sigkill_reachable_from_this_channel",
})


class CutoverCapabilityError(Exception):
    """The channel is not activatable; carries exactly what is missing."""

    def __init__(self, reason_code: str, message: str, *, missing: frozenset[str]) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.missing = missing


@dataclass(frozen=True, slots=True)
class CutoverCapability:
    """A complete, versioned attestation that this channel may execute."""

    protocol_version: int
    attested: frozenset[str]

    def missing(self) -> frozenset[str]:
        return REQUIRED_PROPERTIES - self.attested


def load_cutover_capability() -> CutoverCapability | None:
    """Return the landed attestation, or ``None`` when none exists.

    The attestation lives in a module the D-track lands when the properties
    actually close.  Its absence is the normal state today and is reported as
    ``None`` rather than raised, so callers distinguish "not activatable yet"
    from "attestation present but incomplete" — those are different problems
    with different owners.
    """
    try:
        from macos_self_deployment_plugin import (  # noqa: PLC0415
            cutover_capability_attestation as attestation,
        )
    except ImportError:
        return None
    version = getattr(attestation, "PROTOCOL_VERSION", None)
    attested = getattr(attestation, "ATTESTED_PROPERTIES", None)
    if not isinstance(version, int) or not isinstance(attested, (frozenset, set)):
        return None
    return CutoverCapability(
        protocol_version=version,
        attested=frozenset(str(item) for item in attested),
    )


def require_cutover_capability() -> CutoverCapability:
    """Raise unless this channel is fully activatable.

    Called before ANY controller execution.  Refusing here rather than inside
    the swap is deliberate: once the orchestrator has been entered a refusal is
    no longer free, and the whole point of an activation gate is that it costs
    nothing to obey.
    """
    capability = load_cutover_capability()
    if capability is None:
        raise CutoverCapabilityError(
            "cutover_capability_absent",
            "reconciliation cutover is not activatable: no capability attestation is "
            "landed (D-1.1 through D-1.10 and no-SIGKILL closure are outstanding)",
            missing=REQUIRED_PROPERTIES,
        )
    if capability.protocol_version != CUTOVER_PROTOCOL_VERSION:
        raise CutoverCapabilityError(
            "cutover_capability_version_mismatch",
            f"capability attests protocol {capability.protocol_version}, this channel "
            f"requires {CUTOVER_PROTOCOL_VERSION}",
            missing=REQUIRED_PROPERTIES,
        )
    missing = capability.missing()
    if missing:
        raise CutoverCapabilityError(
            "cutover_capability_incomplete",
            f"capability is missing {len(missing)} of {len(REQUIRED_PROPERTIES)} required "
            f"closures: {', '.join(sorted(missing))}",
            missing=missing,
        )
    return capability
