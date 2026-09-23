#!/usr/bin/env python3
"""Structural drift smoke for the R4 Package C worker-hook resolution
ladder (2026-08-10) — ``headless_adapter._WORKER_INJECTED_HOOK_FILENAMES``
is the single declared list both host adapters resolve against (rung 1:
the origin checkout's own ``.claude/hooks/``; rung 2: this plugin's own
shipped ``coordination-hooks/hooks/`` fallback, see
``headless_adapter._resolve_worker_hook_path``'s own docstring).

This smoke is the guard against the exact defect the ladder itself was
built to fix, recurring in a NEW shape: a future adapter change that
starts injecting a hook filename into a spawned worker's generated
``--settings`` without also vendoring that file into this plugin's
shipped fallback rung. A missing rung-2 file would pass every existing
adapter smoke (which runs from wherever it happens to be invoked) while
silently bricking every worker spawned from a born clone — exactly the
class of gap `manifest_consistency_smoke.py`'s own header epigraph
describes ("the failure mode ... has already happened twice"). This file
is that same discipline applied to the adapter side of the ladder, not
the plugin-document side.

R4 gate-fix (2026-08-10, coordinator-seat un-park dispatch): the first version of
this smoke hard-asserted rung 1's own presence (``REPO_ROOT/.claude/hooks``)
as if it were a universal truth. It isn't — it's a DEV-CHECKOUT-ONLY
truth. A born clone (the seed's own gate target) ships NO
``.claude/hooks/`` at all, correctly, by design; asserting its presence
there is not catching drift, it's asserting the wrong invariant against
the exact tree this smoke exists to protect, and it blocked the born-clone
gate on the re-mint. Fixed: every rung-1-dependent assertion is now
SHAPE-AWARE, keyed on the MEASURED tree shape
(``(REPO_ROOT/".claude"/"hooks").is_dir()``) — never an environment
variable, which could disagree with what's actually on disk. Two
directions, unconditionally checked either way:

  - direction 1 (the born-clone guarantee): every declared filename
    resolves in the plugin's shipped hooks/ dir (rung 2) — unconditional,
    every tree shape, never weakened.
  - direction 2 (the dev-checkout mirror-drift guard): every declared
    filename ALSO exists in the checkout's own ``.claude/hooks/`` (rung 1)
    — only meaningful, and only asserted, when rung 1 is actually present
    in this tree; a born clone prints an explicit N/A classification line
    instead of silently shrinking the check count.

Plus an end-to-end resolution check against the real repo root, itself
shape-aware: a dev checkout must resolve every filename to its rung-1
copy (rung 1 always wins when present); a born clone must resolve every
filename to its rung-2 copy instead (the SAME born-clone guarantee,
restated at the resolution layer rather than the bare-file-presence
layer). Every assertion states which shape it ran under, in its own
label, so a failure is never ambiguous about which tree produced it.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/worker_hook_shipping_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin.headless_adapter import (  # noqa: E402
    _PLUGIN_HOOKS_DIR_RELATIVE,
    _WORKER_INJECTED_HOOK_FILENAMES,
    _resolve_worker_hook_path,
)

_passed = 0
_failed: list[str] = []

_CHECKOUT_HOOKS_DIR = REPO_ROOT / ".claude" / "hooks"
_PLUGIN_HOOKS_DIR = REPO_ROOT / _PLUGIN_HOOKS_DIR_RELATIVE


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _rung1_present() -> bool:
    """The measured tree shape this whole smoke keys on -- never an env
    var, which could disagree with what's actually on disk."""
    return _CHECKOUT_HOOKS_DIR.is_dir()


def test_declared_list_is_non_empty() -> None:
    """A silently-emptied list would make every check below vacuously
    pass -- the population is asserted before anything is concluded from it."""
    _check(
        bool(_WORKER_INJECTED_HOOK_FILENAMES),
        "the declared worker-injected hook filename list is non-empty",
    )


def test_every_declared_filename_ships_in_the_plugin_fallback_rung() -> None:
    """The born-clone guarantee, direction 1: a spawned worker with NO
    ``.claude/hooks/`` at all must still find every one of these files at
    rung 2. Unconditional -- every tree shape, never weakened, never
    gated on rung 1's presence."""
    for name in _WORKER_INJECTED_HOOK_FILENAMES:
        path = _PLUGIN_HOOKS_DIR / name
        _check(
            path.is_file(),
            f"{name} ships in the plugin's shipped hooks/ dir (rung 2): {path}",
        )


def test_every_declared_filename_exists_in_the_checkout_origin_rung() -> None:
    """The mirror-image drift, direction 2: a filename added to the
    declared list without ever having existed as a real checkout hook at
    all. Shape-aware: this leg is a DEV-CHECKOUT-ONLY truth -- a born
    clone shipping no ``.claude/hooks/`` is the correct, expected shape,
    not a drift to catch, so this leg prints an explicit N/A
    classification there instead of silently shrinking the check count."""
    if not _rung1_present():
        print(
            "  N/A   rung-1 mirror-drift leg: no .claude/hooks/ in this tree "
            "(born-clone shape) -- dev-checkout-only truth, not applicable here",
        )
        return
    for name in _WORKER_INJECTED_HOOK_FILENAMES:
        path = _CHECKOUT_HOOKS_DIR / name
        _check(
            path.is_file(),
            f"[dev-checkout shape] {name} exists in the checkout's own .claude/hooks/ (rung 1): {path}",
        )


def test_resolution_against_the_real_repo_root_matches_the_measured_shape() -> None:
    """End-to-end against the REAL repo tree (not a synthetic fixture),
    shape-aware. Dev checkout (rung 1 present): every declared filename
    must resolve to the rung-1 copy (rung 1 always wins when present).
    Born clone (rung 1 absent): every declared filename must resolve to
    the rung-2 copy instead -- the SAME born-clone guarantee as direction
    1 above, restated at the resolution layer rather than the
    bare-file-presence layer, so a ladder bug that resolved to the wrong
    rung despite both files existing would still be caught."""
    if _rung1_present():
        for name in _WORKER_INJECTED_HOOK_FILENAMES:
            resolved = _resolve_worker_hook_path(REPO_ROOT, name)
            _check(
                resolved == _CHECKOUT_HOOKS_DIR / name,
                f"[dev-checkout shape] {name} resolves to the checkout's rung-1 copy against the real repo root",
            )
        return
    for name in _WORKER_INJECTED_HOOK_FILENAMES:
        resolved = _resolve_worker_hook_path(REPO_ROOT, name)
        _check(
            resolved == _PLUGIN_HOOKS_DIR / name,
            f"[born-clone shape] {name} resolves to the plugin's rung-2 fallback against the real repo root",
        )


def main() -> int:
    print("worker-hook-shipping — adapter ladder vs. the real repo tree")
    print("=" * 68)
    shape = "dev-checkout (rung 1 present)" if _rung1_present() else "born-clone (rung 1 absent)"
    print(f"Measured tree shape: {shape}")
    print("-" * 68)
    test_declared_list_is_non_empty()
    test_every_declared_filename_ships_in_the_plugin_fallback_rung()
    test_every_declared_filename_exists_in_the_checkout_origin_rung()
    test_resolution_against_the_real_repo_root_matches_the_measured_shape()
    print("-" * 68)
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
