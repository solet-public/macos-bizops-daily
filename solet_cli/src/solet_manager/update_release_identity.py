"""The exact-candidate-release-identity postcondition, with every failed fact named (iss_414f2f74, Problem wgr_a737ee5b).

After the fast-forward and at every re-entry the target must sit at the approved candidate: seven facts, all of which must hold.
``release_identity_mismatches`` returns the facts that do not, each with its measured value and the value it must have, and the
postcondition refuses exactly when that list is not empty -- the conditions are the seven that the single boolean held, nothing
widened and nothing narrowed.  ``release_identity_refusal`` renders them as one refusal whose repair fits the causes present.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from .adapter_validation import FORMULA_MARKER, bounded_redacted
from .errors import SourceTransitionIncompleteError
from .existing_install_inspection import ExistingInstallFacts, InspectionAnchorKind, InspectionStatus, ObservedBoolean
from .origin_identity import names_repository

LEAD = "target is not at the exact candidate release identity"
_VALUE_LIMIT = 120
_USERINFO = re.compile(r"(?<=://)[^/\s]*@")


@dataclass(frozen=True, slots=True)
class IdentityMismatch:
    """One failed fact: its name, the measured value and the value the candidate requires."""

    fact: str
    observed: str
    expected: str

    def sentence(self) -> str:
        return f"{self.fact} is {self.observed}, expected {self.expected}"


def _public(text: str) -> str:
    """Text the Manager may print: secret-shaped text redacted, a Homebrew keg path read ``[keg path]``, every ``public_string`` rule met."""
    return bounded_redacted(text).replace(FORMULA_MARKER, "[keg path]")


def _shown(value: str | None) -> str:
    """A measured value for a human: absent reads ``none``, a URL loses its credentials, then the public rules and a length bound apply."""
    if value is None:
        return "none"
    text = _public(_USERINFO.sub("", value))
    return text if len(text) <= _VALUE_LIMIT else f"{text[:_VALUE_LIMIT]}..."


def _origins_shown(origins: tuple[str, ...]) -> str:
    return "none" if not origins else f"{len(origins)} ({', '.join(_shown(origin) for origin in origins[:3])}{', ...' if len(origins) > 3 else ''})"


def release_identity_mismatches(facts: ExistingInstallFacts, *, commit: str, tree: str, branch: str | None, canonical_repository: str) -> tuple[IdentityMismatch, ...]:
    """Every one of the seven conjuncts that fails, in the order they are checked; empty exactly when the target is at the candidate."""
    found = (
        (facts.head_commit == commit, "head_commit", _shown(facts.head_commit), commit),
        (facts.head_tree == tree, "head_tree", _shown(facts.head_tree), tree),
        (facts.identity_status is InspectionStatus.VERIFIED, "identity_status", facts.identity_status.value, InspectionStatus.VERIFIED.value),
        (facts.anchor_kind is InspectionAnchorKind.CURRENT_CHANNEL, "anchor_kind", facts.anchor_kind.value, InspectionAnchorKind.CURRENT_CHANNEL.value),
        (facts.detached is ObservedBoolean.FALSE, "detached", facts.detached.value, ObservedBoolean.FALSE.value),
        (facts.branch == branch, "branch", _shown(facts.branch), _shown(branch)),
        (len(facts.origins) == 1 and names_repository(facts.origins[0], canonical_repository), "origins", _origins_shown(facts.origins), f"exactly one, naming {_shown(canonical_repository)}"),
    )
    return tuple(IdentityMismatch(fact, observed, expected) for holds, fact, observed, expected in found if not holds)


def _branch_repair(fact: str, quoted_target: str, branch: str | None) -> str:
    if branch is None:
        return f"The update recorded no branch to return to; inspect HEAD with `git -C {quoted_target} status`."
    if _shown(branch) != branch:
        return f"The recorded branch name cannot be printed; inspect HEAD with `git -C {quoted_target} status`."
    cause = "HEAD is not on a branch" if fact == "detached" else "The checked-out branch changed during the update"
    return f"{cause}; once HEAD is at the approved commit, switch to {_shown(branch)} with `git -C {quoted_target} switch {shlex.quote(branch)}`."


def _repair_for(mismatch: IdentityMismatch, target: Path, name: str, branch: str | None) -> str:
    quoted = shlex.quote(str(target))
    match mismatch.fact:
        case "head_commit" | "head_tree":
            return f"HEAD does not carry the approved bytes; inspect it with `git -C {quoted} log -1` and resume once it matches the approved candidate."
        case "identity_status" | "anchor_kind":
            return f"The target's sealed identity no longer verifies as its current channel; read which check failed with `solet-manager doctor {name}`."
        case "detached" | "branch":
            return _branch_repair(mismatch.fact, quoted, branch)
        case _:
            return f"The origin remote must be exactly one URL naming the canonical repository; read it with `git -C {quoted} remote -v` and correct it."


def release_identity_refusal(mismatches: tuple[IdentityMismatch, ...], *, target: Path, name: str, branch: str | None) -> SourceTransitionIncompleteError:
    """One refusal naming each failed fact with its measured value; the repair carries one sentence per cause present.

    Both texts are neutralised whole: each value is safe alone, but a value that ends in a secret keyword and separator becomes
    secret-shaped once the text that follows it is joined on.  A detached HEAD reads as no branch, so ``detached`` already carries
    the one repair for it and ``branch`` adds none.
    """
    failed = {item.fact for item in mismatches}
    causes = tuple(item for item in mismatches if not (item.fact == "branch" and "detached" in failed))
    message = _public(f"{LEAD}: {'; '.join(item.sentence() for item in mismatches)}")
    repairs = tuple(dict.fromkeys(_repair_for(item, target, name, branch) for item in causes))
    return SourceTransitionIncompleteError(message, repair=_public(f"Do not reset. {' '.join(repairs)}"))
