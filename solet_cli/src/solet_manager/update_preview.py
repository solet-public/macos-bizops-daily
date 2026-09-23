"""Closed, non-mutating Step-4 update preview and approval carrier."""

from __future__ import annotations

from dataclasses import dataclass

from solet_setup_contracts import canonical_sha256

from .models import CommandResult, ExitCode, JsonValue
from .update_candidate import UpdateCandidate
from .update_topology import UpdateTopology

DEFAULT_PLANNED_ACTIONS = ("target.fetch_exact_candidate", "target.fast_forward_exact_candidate")
#: Step 6 section 4.8: the zero-delta source path fetches and merges nothing.
VERIFY_PLANNED_ACTIONS = ("manager.acquire_update_candidate", "target.verify_exact_candidate")
VERIFY_PREVIEW_STATUS = "verify_preview_ready"
VERIFY_PREVIEW_MESSAGE = "source: no change (already at {tag}); runtime: verify by probe, apply only what is missing; then final doctor and promotion."


@dataclass(frozen=True, slots=True)
class UpdatePreview:
    candidate: UpdateCandidate
    topology: UpdateTopology
    planned_actions: tuple[str, ...]
    approval_fingerprint: str | None

    def to_command_result(self) -> CommandResult:
        return CommandResult(
            "update_preview",
            "preview_ready" if self.approval_fingerprint else "awaiting_user",
            "Update preview completed.",
            ExitCode.OK if self.approval_fingerprint else ExitCode.HUMAN_ACTION,
            data={"candidate_descriptor_digest": self.candidate.descriptor_digest, "planned_actions": list(self.planned_actions), "topology_reasons": list(self.topology.reasons), "approval_fingerprint": self.approval_fingerprint, "target_byte_writes": 0, "manager_state_writes": 0},
        )


def preview_update(
    candidate: UpdateCandidate,
    topology: UpdateTopology,
    *,
    baseline_commit: str,
    bound_identity: dict[str, JsonValue] | None = None,
    planned_actions: tuple[str, ...] = DEFAULT_PLANNED_ACTIONS,
    source_mode: str = "advance",
) -> UpdatePreview:
    """Render the closed preview; the fingerprint covers every action-driving input.

    ``bound_identity`` carries the instance, filesystem, baseline, candidate,
    collision, and Manager-path identities the executor revalidates under the
    instance lock.  Timestamps and cache hit/miss are deliberately excluded.
    ``source_mode`` is part of the preimage so a ``verify`` fingerprint can
    never be accepted by an ``advance`` apply or vice versa (Step 6 F-ZD-2).
    """
    if not topology.actionable:
        return UpdatePreview(candidate, topology, planned_actions, None)
    fingerprint = canonical_sha256(
        {
            "kind": "update",
            "descriptor_digest": candidate.descriptor_digest,
            "baseline_commit": baseline_commit,
            "candidate_commit": candidate.fields.commit,
            "candidate_tree": candidate.fields.tree_hash,
            "receipt_digest": candidate.receipt_digest,
            "topology_reasons": list(topology.reasons),
            "planned_actions": list(planned_actions),
            "source_mode": source_mode,
            "bound_identity": {} if bound_identity is None else bound_identity,
        }
    )
    return UpdatePreview(candidate, topology, planned_actions, fingerprint)
