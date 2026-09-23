"""The executed-code set, derived from the declaration rather than listed (Step 7 design section 6.5).

The Manager executes target code through two vectors: the ``bootstrap`` vector
(``<base_python> <target>/bootstrap.py --operation-adapter``, which puts the
repository root on ``sys.path`` and imports ``bootstrap_adapter.*``) and the
``target_adapter``/``reconciliation`` vector (``<target>/.venv/bin/python3 -m
github_midwife_plugin.setup_adapter``, whose package imports ``ananta.*`` and
``macos_vault_plugin.*`` at module level) -- distributions that are *editable
installs from the target tree*.  A local modification under any of those roots
is executed by the next probe, so the bound is the declared editable-install
closure the dependencies stage enforces (``REQUIRED_DISTRIBUTIONS`` union the
roster union the bundle's closure additions), never a two-prefix list and never
the venv's own ``direct_url.json`` (target-owned, ignored, possibly absent or
dangling exactly when the doctor most needs the bound).
"""

from __future__ import annotations

from pathlib import Path

from .errors import UpdateBlockedError
from .update_candidate import UpdateCandidate
from .update_runtime_plan import REQUIRED_DISTRIBUTIONS, roster_plugins

__all__ = ["BOOTSTRAP_ROOTS", "RosterUnreadableError", "executed_code_roots"]

#: The bootstrap vector's own surface (``bootstrap.py:68-73``): always a root.
BOOTSTRAP_ROOTS: tuple[str, ...] = ("bootstrap.py", "bootstrap_adapter/")


class RosterUnreadableError(RuntimeError):
    """The roster could not be read, so the executed-code roots cannot be derived (doctor: ``unknown``)."""


def executed_code_roots(target: Path, candidate: UpdateCandidate | None) -> tuple[str, ...]:
    """Every tree prefix a Manager vector executes code from, sorted; directory roots end in ``/``.

    With a candidate the bundle's ``closure_additions`` join the set; without one
    (a standalone doctor) the roots are the bootstrap surface, the fixed required
    distributions and every roster plugin.  A roster plugin absent from the tree is
    still a root: there is nothing to modify under it, and naming it costs nothing.
    An unreadable roster raises :class:`RosterUnreadableError` so a doctor reports
    ``unknown`` instead of executing target code whose roots it could not derive;
    an absent roster file is an empty roster.
    """
    roots: set[str] = set(BOOTSTRAP_ROOTS)
    roots.update(f"{relative}/" for _, relative in REQUIRED_DISTRIBUTIONS)
    try:
        roster = roster_plugins(target)
    except UpdateBlockedError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            # An absent roster names no plugins (a definite, not an unknown); refusing a target without one is the runtime plan's job.
            roster = ()
        else:
            raise RosterUnreadableError(str(exc)) from exc
    roots.update(f"plugins/{plugin}/" for plugin in roster)
    if candidate is not None:
        roots.update(f"{piece.relative_path.rstrip('/')}/" for piece in candidate.bundle.closure_additions)
    return tuple(sorted(roots))
