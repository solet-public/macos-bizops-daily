#!/usr/bin/env python3
"""Smoke: a built release's interpreter cannot be changed by a later brew upgrade.

A release's ``venv/`` is a CoW clone of the checkout's ``.venv``, so it inherits
a base-interpreter symlink pointing at Homebrew's FLOATING alias
(``/opt/homebrew/opt/python@3.13/bin/python3.13``). Homebrew repoints that alias
on every patch upgrade, which means an unpinned release executes whatever brew
most recently installed — resolved afresh at each spawn, and free to change
under a release that was built and probed hours earlier. On 2026-08-31 a
3.13.13_1 → 3.13.15 bump did exactly that between two cutovers.

The subsmokes and the mutation each one catches:

* ``SC-1 floating-link-is-pinned`` — the pin not running at all.
* ``SC-2 pin-survives-a-brew-upgrade`` — the property the whole change exists to
  buy, and the only subsmoke that actually proves it. Repoints the floating
  alias AFTER pinning, exactly as ``brew upgrade`` would, and asserts the venv
  still resolves to the original interpreter. A pin that resolved the alias but
  wrote the alias back would pass SC-1 and fail here.
* ``SC-3 intra-bin-hop-preserved`` — pinning ``bin/python3`` itself instead of
  the link that leaves ``bin/``. That would move the invoked path out of the
  venv, orphan it from ``pyvenv.cfg``, and silently start a release with the
  BASE interpreter's site-packages instead of its own.
* ``SC-4 idempotent`` — a rebuild over an already-pinned venv must be a no-op,
  not a change or an error.
* ``SC-5 nothing-to-pin-is-not-a-failure`` — a venv whose launcher is a real
  file (build fixtures, and any venv with a copied-in binary) must build
  normally. Raising here turns a healthy venv into a failed release.
* ``SC-6 broken-target-fails-loud`` — the fail-loud path that must survive SC-5's
  tolerance: a link that EXISTS but resolves to a non-executable must still
  raise, or the pin would ship a release whose interpreter is already broken.

Hermetic: builds fake venv trees in a temp dir. No release, no deploy, no brew.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from macos_self_deployment_plugin.venv_interpreter_pin import (  # noqa: E402
    VenvInterpreterPinError,
    pin_venv_interpreter,
)


def _make_venv(root: Path, *, floating: bool = True) -> tuple[Path, Path, Path]:
    """Build a fake venv mirroring the real symlink shape.

    ``bin/python3 -> bin/python3.13 -> <alias> -> <cellar>/python3.13`` when
    ``floating``; the alias hop is what Homebrew rewrites on upgrade.

    Returns ``(venv_dir, alias, cellar_binary)``.
    """
    cellar = root / "Cellar" / "python@3.13" / "3.13.13_1" / "bin"
    cellar.mkdir(parents=True)
    real = cellar / "python3.13"
    real.write_text("#!/bin/sh\nexit 0\n")
    real.chmod(0o755)

    alias = root / "opt" / "python@3.13" / "bin" / "python3.13"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(real)

    venv_bin = root / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python3.13").symlink_to(alias if floating else real)
    (venv_bin / "python3").symlink_to("python3.13")
    (root / "venv" / "pyvenv.cfg").write_text("home = whatever\n")
    return root / "venv", alias, real


def _sc1_floating_link_is_pinned(root: Path) -> None:
    venv, _alias, real = _make_venv(root)
    pin = pin_venv_interpreter(venv)
    assert pin.changed, "a floating base link was not pinned"
    assert pin.pinned_target == real, f"pinned to {pin.pinned_target}, want {real}"
    assert Path(os.readlink(venv / "bin" / "python3.13")) == real, (
        "the base link still names the floating alias on disk"
    )


def _sc2_pin_survives_a_brew_upgrade(root: Path) -> None:
    venv, alias, real = _make_venv(root)
    pin_venv_interpreter(venv)

    # Exactly what `brew upgrade python@3.13` does: install a new Cellar version
    # and repoint the alias at it. Nothing touches the release.
    new_cellar = root / "Cellar" / "python@3.13" / "3.13.15" / "bin"
    new_cellar.mkdir(parents=True)
    new_real = new_cellar / "python3.13"
    new_real.write_text("#!/bin/sh\nexit 0\n")
    new_real.chmod(0o755)
    alias.unlink()
    alias.symlink_to(new_real)

    resolved = Path(os.path.realpath(venv / "bin" / "python3"))
    assert resolved == real, (
        f"a brew upgrade moved the built release onto {resolved}; the pin must "
        f"keep it on {real} — this is the entire point of R4"
    )


def _sc3_intra_bin_hop_preserved(root: Path) -> None:
    venv, _alias, real = _make_venv(root)
    pin_venv_interpreter(venv)
    launcher = venv / "bin" / "python3"
    assert launcher.is_symlink(), "bin/python3 stopped being a symlink"
    assert Path(os.readlink(launcher)) == Path("python3.13"), (
        "bin/python3 was repointed out of bin/; the invoked path would no "
        "longer sit beside pyvenv.cfg and venv activation would break"
    )
    assert Path(os.path.realpath(launcher)) == real


def _sc4_idempotent(root: Path) -> None:
    venv, _alias, real = _make_venv(root)
    pin_venv_interpreter(venv)
    again = pin_venv_interpreter(venv)
    assert again.pinnable, "second pin lost track of the link"
    assert not again.changed, "re-pinning an already-pinned venv reported a change"
    assert again.pinned_target == real


def _sc5_nothing_to_pin_is_not_a_failure(root: Path) -> None:
    venv_bin = root / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    launcher = venv_bin / "python3"
    launcher.write_text("#!/bin/sh\nexit 0\n")  # a real file, not a symlink
    launcher.chmod(0o755)
    pin = pin_venv_interpreter(root / "venv")
    assert not pin.pinnable, "a copied-in launcher was treated as pinnable"
    assert not pin.changed
    # And the absent case: a venv with no launcher at all must not raise either.
    empty = root / "empty_venv"
    (empty / "bin").mkdir(parents=True)
    assert not pin_venv_interpreter(empty).pinnable


def _sc6_broken_target_fails_loud(root: Path) -> None:
    venv, alias, real = _make_venv(root)
    # The link exists and points outside bin/, but its target is gone.
    real.unlink()
    alias.unlink()
    alias.symlink_to(real)
    try:
        pin_venv_interpreter(venv)
    except VenvInterpreterPinError:
        return
    raise AssertionError(
        "a base link resolving to a missing interpreter was pinned silently; "
        "the release would ship with an already-broken interpreter"
    )


def _check(name: str, fn: Any, failures: list[str]) -> None:
    # .resolve() matters: on macOS /var is itself a symlink to /private/var, so
    # an unresolved temp root would make every realpath comparison below fail on
    # the prefix rather than on the property under test.
    root = Path(tempfile.mkdtemp(prefix="venvpin-smoke-")).resolve()
    try:
        fn(root)
    except Exception as exc:  # noqa: BLE001 — the smoke reports, never propagates
        print(f"  FAIL {name}: {exc}")
        failures.append(name)
    else:
        print(f"  ok   {name}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    failures: list[str] = []
    print("release venv interpreter pin:")
    _check("SC-1 floating-link-is-pinned", _sc1_floating_link_is_pinned, failures)
    _check("SC-2 pin-survives-a-brew-upgrade", _sc2_pin_survives_a_brew_upgrade, failures)
    _check("SC-3 intra-bin-hop-preserved", _sc3_intra_bin_hop_preserved, failures)
    _check("SC-4 idempotent", _sc4_idempotent, failures)
    _check(
        "SC-5 nothing-to-pin-is-not-a-failure",
        _sc5_nothing_to_pin_is_not_a_failure,
        failures,
    )
    _check("SC-6 broken-target-fails-loud", _sc6_broken_target_fails_loud, failures)

    if failures:
        print(f"\nFAIL — {len(failures)} case(s): {', '.join(failures)}")
        return 1
    print("\nPASS — a built release's interpreter is immutable against brew.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
