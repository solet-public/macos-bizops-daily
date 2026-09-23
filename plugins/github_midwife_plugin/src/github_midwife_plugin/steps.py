"""Slice D — genesis step machine (Layer 1).

Own-copy of `birther.py`'s steps-as-data shape (Coordinator-Dawn's
composability rider, 2026-07-09): a tuple of `(name, runner)` callables
over an explicit `GenesisContext` (target path + profile info — no cwd
assumptions), executed by `run_steps`, which accepts an INJECTED
sequence defaulting to `GENESIS_STEP_RUNNERS`. The injected-sequence seam
keeps the step machine composable without restructuring this module. (The
original verb-mode use — prepending target-acquisition steps, a fresh
`git clone` of the public repo — was RETIRED with acquisition mode
2026-07-18; the Seed Factory supplies an already-assembled clone, so
genesis runs the standard sequence against it.)

Genesis's 6-step sequence — `validate_name` -> `resolve_target` ->
`materialize_configs` -> `seed_root_manifest` -> `materialize_kb_symlinks` ->
`write_manifest_marker` — is deliberately SHORTER than `birther.py`'s 8, all
adjudicated 2026-07-09 (the KB-symlink step added 2026-07-12; the
root-manifest step added 2026-07-12):
  * `seed_root_manifest` IS a step (2026-07-12) — the seed ships the
    MINTING solet's `root_manifest.yaml` verbatim (it is in the
    assemble `copy:` allowlist), so without a rewrite a seed-born
    newborn's `solet_name:` still names the minting solet —
    exactly the identity pollution the seed factory exists to prevent.
    Mirrors `macos_midwife_plugin`'s birth-time `seed_for_newborn`
    (which fires inside its `clone_and_copy`); genesis has no copy step,
    so it is a spine step here (see `root_manifest_seed.py`).
  * `clone_and_copy` is N/A — genesis runs in-place on the user's own
    clone; there is nothing to copy INTO (cloning the repo into itself
    is nonsense).
  * `materialize_kb_symlinks` IS a step (2026-07-12) — earlier believed
    N/A because a full `git clone` ships the git-tracked `knowledge_bases/`
    symlinks. FALSIFIED for the seed path: the MINT seed's `assemble` BANS
    symlinks from the published seed, so a SEED-BORN clone has NONE and the
    auto-installer finds an empty `knowledge_bases/` (a dead KB). This step
    derives + idempotently repairs them from the clone's own KB dirs
    (see `kb_symlinks.py`), safely never touching a real content directory.
  * `venv_setup` is owned by Layer 0 (create the venv + install the
    seed) and Slice B's `profile_install.py` (allowlist install) — not
    a member of this step machine.
  * `asset_downloads` is dropped entirely, no no-op placeholder — the
    free profile ships zero asset-bearing plugins; fast-fail philosophy
    says no dead steps for a hypothetical future need.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config_materialize import ConfigMaterializeError, materialize_profile
from .constants import (
    MANIFEST_MARKER_PATH,
    NAME_PATTERN,
    REQUIRED_CLONE_MARKERS,
    is_valid_solet_name,
)
from .kb_symlinks import KbSymlinkError, materialize_kb_symlinks
from .manifest_marker import build_marker_payload, write_marker
from .root_manifest_seed import RootManifestSeedError, seed_for_newborn


@dataclass
class GenesisContext:
    """Mutable orchestration ledger threaded through each step.

    `target` and `kb_root` are caller-supplied — NO cwd assumption; a
    verb-mode entrypoint (Slice G) resolves its own freshly-cloned
    target and passes it in the exact same way.
    """

    name: str
    profile_name: str
    target: Path
    kb_root: Path
    steps: list[dict[str, Any]] = field(default_factory=list)
    manifest_path: Path | None = None


_StepRunner = Callable[[GenesisContext], dict[str, Any]]


def _run_validate_name(ctx: GenesisContext) -> dict[str, Any]:
    # `is_valid_solet_name` uses `fullmatch` (see constants) -- `NAME_PATTERN.match`
    # would let a trailing-newline name slip past `$`, and this name flows on to
    # every downstream SQL/path derivation.
    if not is_valid_solet_name(ctx.name):
        return {
            "step_name": "validate_name", "status": "failed",
            "error": (
                f"invalid solet name {ctx.name!r}: must match "
                f"{NAME_PATTERN.pattern} (lowercase letter start, 1-62 "
                "trailing chars from [a-z0-9_-])"
            ),
        }
    return {"step_name": "validate_name", "status": "completed"}


def _run_resolve_target(ctx: GenesisContext) -> dict[str, Any]:
    missing = [m for m in REQUIRED_CLONE_MARKERS if not (ctx.target / m).exists()]
    if missing:
        return {
            "step_name": "resolve_target", "status": "failed",
            "error": (
                f"{ctx.target} does not look like a platform clone — missing: "
                f"{', '.join(missing)}. Run genesis from the repo root of a "
                "full `git clone` of the platform."
            ),
        }
    return {"step_name": "resolve_target", "status": "completed", "target": str(ctx.target)}


def _run_materialize_configs(ctx: GenesisContext) -> dict[str, Any]:
    try:
        written = materialize_profile(
            target=ctx.target, kb_root=ctx.kb_root,
            profile_name=ctx.profile_name, name=ctx.name,
        )
    except ConfigMaterializeError as exc:
        return {"step_name": "materialize_configs", "status": "failed", "error": str(exc)}
    return {
        "step_name": "materialize_configs", "status": "completed",
        "files_written": {k: [str(p) for p in v] for k, v in written.items()},
    }


def _run_seed_root_manifest(ctx: GenesisContext) -> dict[str, Any]:
    try:
        manifest_path = seed_for_newborn(ctx.target, ctx.name)
    except RootManifestSeedError as exc:
        return {"step_name": "seed_root_manifest", "status": "failed", "error": str(exc)}
    return {
        "step_name": "seed_root_manifest", "status": "completed",
        "root_manifest_path": str(manifest_path),
    }


def _run_materialize_kb_symlinks(ctx: GenesisContext) -> dict[str, Any]:
    try:
        report = materialize_kb_symlinks(ctx.target)
    except KbSymlinkError as exc:
        return {"step_name": "materialize_kb_symlinks", "status": "failed", "error": str(exc)}
    return {"step_name": "materialize_kb_symlinks", "status": "completed", "symlinks": report}


def _run_write_marker(ctx: GenesisContext) -> dict[str, Any]:
    # "spine_complete", NOT "success": the post-spine phases (credential seed,
    # vault passphrase, autostart) haven't run yet -- run_genesis rewrites the
    # marker with the phase records and the final success/failed status once
    # they have (cold-run finding D4, 2026-07-13: a spine-end "success" read as
    # whole-genesis success while the newborn crash-looped).
    payload = build_marker_payload(
        name=ctx.name, profile_name=ctx.profile_name, steps=ctx.steps, status="spine_complete",
    )
    ctx.manifest_path = write_marker(ctx.target, payload)
    return {
        "step_name": "write_manifest_marker", "status": "completed",
        "manifest_path": str(ctx.manifest_path),
    }


GENESIS_STEP_RUNNERS: tuple[tuple[str, _StepRunner], ...] = (
    ("validate_name", _run_validate_name),
    ("resolve_target", _run_resolve_target),
    ("materialize_configs", _run_materialize_configs),
    ("seed_root_manifest", _run_seed_root_manifest),
    ("materialize_kb_symlinks", _run_materialize_kb_symlinks),
    ("write_manifest_marker", _run_write_marker),
)


def run_steps(
    ctx: GenesisContext, step_runners: Sequence[tuple[str, _StepRunner]] = GENESIS_STEP_RUNNERS,
) -> list[dict[str, Any]]:
    """Execute `step_runners` in order against `ctx`; abort on the first failure.

    Returns `ctx.steps` (also mutated in place as each step runs, so a
    caller inspecting `ctx.steps` mid-failure sees the same list).
    `step_runners` is injectable — genesis's own entrypoint uses the
    6-step default; a future verb-mode entrypoint passes its own
    (longer) sequence without this module changing.
    """
    for _step_name, runner in step_runners:
        record = runner(ctx)
        ctx.steps.append(record)
        if str(record.get("status", "")) == "failed":
            break
    return ctx.steps


__all__ = [
    "GENESIS_STEP_RUNNERS",
    "MANIFEST_MARKER_PATH",
    "GenesisContext",
    "run_steps",
]
