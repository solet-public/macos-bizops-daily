"""The operator repair for an existing install the Manager will not import.

A refusal without a way out is a dead end: a hand-merged pre-Manager plain clone
(a local ``git merge`` of a listed stable seed) was reported ``import_not_allowed``
with no repair at all. Every class whose ``import_disposition`` is not ``allow``
therefore carries one repair, and the only realign it ever proposes is the
non-destructive one: ``git reset --mixed`` to a listed seed commit the clone's own
history already contains, which moves HEAD and the index and never a working file. It is
emitted as one ``&&`` chain behind a branch named for the exact HEAD, so the reset can never
run unless the operator's commit is first kept on a branch of its own; every path is
shell-quoted.
"""

from __future__ import annotations

import shlex

from .existing_install_inspection import (
    ExistingInstallClass,
    ExistingInstallInspectionResult,
    RepositoryRelation,
)


def import_refusal_repair(result: ExistingInstallInspectionResult) -> str | None:
    """The repair for a classification that refuses import; ``None`` exactly when import is allowed."""
    if result.classification.import_disposition == "allow":
        return None
    target = shlex.quote(str(result.target_identity.canonical_display))
    channel = result.channel_identity
    inspect = f"`solet-manager inspect --target {target} --channel {channel.channel_id} --json`"
    kind = result.classification.installation_class
    if kind is ExistingInstallClass.DEVELOPMENT_CHECKOUT:
        return "This is a development checkout, not a seed clone; the Manager never imports or updates it."
    if kind is ExistingInstallClass.INSPECTION_INCOMPLETE:
        return f"Some required facts could not be observed. Run {inspect}, resolve each check whose status is unknown or failed, then retry the import."
    if kind is ExistingInstallClass.SOURCE_IDENTITY_UNPROVEN:
        realign = _realign_repair(result, target, inspect)
        if realign is not None:
            return realign
    return (
        f"HEAD is neither the {channel.channel_id} release {channel.commit} nor a stable seed commit this Manager lists, "
        f"and no listed seed commit is in its history with origin {channel.repository}, so its source cannot be proven. "
        f"A development checkout or a fork is never imported. For a seed clone, run {inspect} and read the failed identity checks."
    )


def _realign_repair(result: ExistingInstallInspectionResult, target: str, inspect: str) -> str | None:
    head = result.facts.head_commit
    seed = result.listed_ancestors[-1] if result.listed_ancestors else None
    if head is None or seed is None or result.facts.repository_relation is not RepositoryRelation.CANONICAL:
        return None
    if seed == head:
        return (
            f"HEAD {head} is a listed stable seed commit, but its identity did not verify: the working PROVENANCE.json "
            f"must be byte-identical to the committed one and HEAD's seal trailers must match it. Run {inspect} and read the failed identity checks."
        )
    kept = f"solet-pre-import-{head[:12]}"
    realign = (
        f"git -C {target} branch {kept} {head} && git -C {target} reset --mixed {seed} && git -C {target} status --short"
    )
    return (
        f"HEAD {head} is not a listed seed commit, but the listed stable seed {seed} is in its history (a pre-Manager "
        "clone with local commits or a hand merge on top of it). Realign it without changing any file, then import: "
        "(1) back up the directory; "
        f"(2) run `{realign}`: it keeps your current commit on the new branch {kept}, stops before the reset if that "
        "branch cannot be created, then moves HEAD and the index only (never a working file) to the seed, and lists "
        "what your own commits changed (edited files as M, added files as ??); "
        f"(3) run {inspect} again, then `solet-manager import <name> --target {target} --channel {result.channel_identity.channel_id} --dry-run`, "
        "and update through the Manager so every migration from that seed runs."
    )
