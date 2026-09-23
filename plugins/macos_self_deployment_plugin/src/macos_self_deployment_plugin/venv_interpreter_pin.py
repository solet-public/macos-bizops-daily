"""Freeze a materialized release's base interpreter to a concrete path.

A release's ``venv/`` is a copy-on-write clone of the development checkout's
``.venv``, so it inherits that venv's base-interpreter symlink — and on a
Homebrew host that link points at the FLOATING alias
``/opt/homebrew/opt/python@3.13/bin/python3.13``, which Homebrew repoints on
every patch upgrade. The practical consequence is that a release's interpreter
is not a property of the release at all: it is whatever ``brew`` last decided,
resolved afresh at each spawn, and it can change under a release that was built,
probed, and shipped hours earlier.

On 2026-08-31 it did. A ``python@3.13`` bump from 3.13.13_1 to 3.13.15 landed
between one cutover and the next; because both interpreters are ad-hoc signed
with different code-directory hashes, the new binary was a different application
to the macOS Keychain, and the credential read the solet performs during startup
was refused.

Pinning resolves the base link ONCE, at build time, and rewrites it to the
concrete versioned path it resolved to. What the pin does and does not buy is
worth being exact about:

* It does **not** fix the underlying ACL fragility. A pinned release whose
  credential ACL is revoked still fails — it just fails the same way twice
  instead of differently each time. The fail-fast gate in
  :mod:`macos_vault_plugin.keychain_interaction` is the fix; this is defence in
  depth, additive to it and never a substitute for it.
* It **does** make the interpreter an immutable property of the built release,
  so a silent background upgrade can never again reach a deploy path, and a
  release that probed green stays byte-identical in what it executes.
* It trades one failure mode for a louder one: when the pinned Cellar version is
  eventually removed (``brew cleanup``), the release fails to spawn with a
  missing-interpreter error instead of silently drifting onto a new binary. That
  is the better failure — it names its own cause.

The venv's ``bin/python3`` → ``bin/python3.13`` hop is deliberately left alone.
Only the first link that leaves ``bin/`` is rewritten, so ``pyvenv.cfg`` stays
adjacent to the invoked path and venv activation semantics are untouched.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

#: The venv-relative launcher every spawn path invokes.
_VENV_BIN = "bin"
_VENV_PYTHON = "python3"
#: Bounds the symlink walk; a venv's bin/ hop chain is 1-2 links in practice.
_MAX_LINK_HOPS = 16


class VenvInterpreterPinError(RuntimeError):
    """The base interpreter could not be resolved or pinned."""


@dataclass(frozen=True, slots=True)
class InterpreterPin:
    """What :func:`pin_venv_interpreter` found and what it changed.

    Three outcomes, and only one of them is a change:

    * ``link is None`` — there was nothing to pin. The venv's launcher is a real
      file rather than a symlink (or is absent entirely), so no host interpreter
      is being referenced by name and none can drift. This is a normal, healthy
      result, NOT a degraded one: a copied-in binary is already as pinned as a
      binary gets. Build fixtures look like this.
    * ``changed is False`` with a ``link`` — the link already named a concrete
      path. Pinning is idempotent, so rebuilding over a pinned venv is a no-op.
    * ``changed is True`` — the link named a floating alias and was rewritten.
    """

    link: Path | None
    previous_target: str | None
    pinned_target: Path | None
    changed: bool

    @property
    def pinnable(self) -> bool:
        """Whether this venv had a host-interpreter link at all."""
        return self.link is not None


NOTHING_TO_PIN = InterpreterPin(
    link=None, previous_target=None, pinned_target=None, changed=False,
)


def _base_interpreter_link(venv_dir: Path) -> Path | None:
    """The first symlink in ``bin/python3``'s chain that leaves ``bin/``.

    That link — typically ``bin/python3.13`` — is the one naming the host
    interpreter. The intra-``bin`` hops before it are the venv's own aliases and
    must be preserved.

    Returns ``None`` when the chain never leaves ``bin/``: the launcher is a
    real file, or is absent. Neither can drift with a Homebrew upgrade, so
    neither is an error — this function refuses to invent a failure for a venv
    that simply has nothing to pin.
    """
    bin_dir = (venv_dir / _VENV_BIN).resolve()
    current = venv_dir / _VENV_BIN / _VENV_PYTHON
    for _hop in range(_MAX_LINK_HOPS):
        if not current.is_symlink():
            return None
        target = Path(os.readlink(current))
        resolved = target if target.is_absolute() else current.parent / target
        if resolved.parent.resolve() != bin_dir:
            return current
        current = resolved
    raise VenvInterpreterPinError(
        f"venv launcher chain under {bin_dir} exceeded {_MAX_LINK_HOPS} hops",
    )


def pin_venv_interpreter(venv_dir: Path) -> InterpreterPin:
    """Rewrite ``venv_dir``'s base-interpreter link to its concrete target.

    Idempotent: a link already naming a concrete path is left untouched and
    reported with ``changed=False``. A venv with no host-interpreter link at all
    returns :data:`NOTHING_TO_PIN` rather than raising — there is nothing there
    to drift.

    Raises:
        VenvInterpreterPinError: a host-interpreter link EXISTS but resolves to
            something that is not an executable file, or the launcher chain does
            not terminate. Fails loud — a release whose named interpreter is
            already broken must not be shipped as if it were fine.
    """
    link = _base_interpreter_link(venv_dir)
    if link is None:
        return NOTHING_TO_PIN
    previous_target = os.readlink(link)
    concrete = Path(os.path.realpath(link))
    if not concrete.is_file() or not os.access(concrete, os.X_OK):
        raise VenvInterpreterPinError(
            f"{link} resolves to {concrete}, which is not an executable file",
        )
    if previous_target == str(concrete):
        return InterpreterPin(
            link=link, previous_target=previous_target,
            pinned_target=concrete, changed=False,
        )
    # A symlink cannot be repointed in place; create-then-rename is atomic.
    tmp = link.with_name(f"{link.name}.pin-tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(concrete, tmp)
    os.replace(tmp, link)
    return InterpreterPin(
        link=link, previous_target=previous_target,
        pinned_target=concrete, changed=True,
    )


def pin_and_report(venv_dir: Path, logger: logging.Logger) -> InterpreterPin:
    """:func:`pin_venv_interpreter`, with the outcome logged.

    Lives here rather than at the call site so the release builder stays a thin
    caller: which of the three outcomes occurred, and how each is worded, is a
    property of pinning, not of the build choreography.
    """
    pin = pin_venv_interpreter(venv_dir)
    if not pin.pinnable:
        logger.info(
            "release venv has no host-interpreter link to pin (%s) — a "
            "copied-in launcher cannot drift", venv_dir,
        )
    elif pin.changed:
        logger.info(
            "pinned release interpreter: %s -> %s (was %s)",
            pin.link, pin.pinned_target, pin.previous_target,
        )
    else:
        logger.info(
            "release interpreter already concrete: %s -> %s",
            pin.link, pin.pinned_target,
        )
    return pin


__all__ = [
    "NOTHING_TO_PIN",
    "InterpreterPin",
    "VenvInterpreterPinError",
    "pin_and_report",
    "pin_venv_interpreter",
]
