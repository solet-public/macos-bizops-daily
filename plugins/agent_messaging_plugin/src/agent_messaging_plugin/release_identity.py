"""GAU-21 — which RELEASE is this code running from, self-identified.

A recorded alarm that does not say which release's detector produced it cannot
be re-read later, and this is not hypothetical: measured 2026-08-19, the
running release evaluated gauge coverage at 300s while master's source said
600s, and carried no staleness detector at all while master had one. Anyone
reasoning about a stored notice from master's constants would have dated the
evidence wrongly in both directions.

★ SELF-IDENTIFYING, ON PURPOSE — read from the running module's OWN tree
rather than from an environment variable. The obvious alternative,
``SOLET_RELEASE_ID``, is a real convention elsewhere in this platform but is
not necessarily set for a given deployment: measured on a supervised install,
the launch agent's ``EnvironmentVariables`` carried only ``PATH`` and
``SOLET_NAME``. Reading it here would have recorded an empty string on every
row — a phantom that reads as "no release" while the code plainly ran from one.
Walking up from ``__file__`` cannot disagree with the code that is executing,
because it IS the code that is executing.

★ ABSENT IS ABSENT. A checkout run has no release identity at all (the repo
root carries no ``VERSION``), and this returns ``None`` for it rather than
inventing a label. The column it feeds is nullable for exactly this reason:
a guessed release id is worse than a missing one, since only the missing one
announces itself.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Final

VERSION_FILENAME: Final = "VERSION"

MAX_PARENTS_SEARCHED: Final = 8
"""How far up from this module to look for a release identity file.

The deployed layout puts this module at
``<release_root>/code/plugins/agent_messaging_plugin/src/agent_messaging_plugin/``
— six levels below the ``VERSION`` beside the release root — so eight bounds
the real layout with room for one nesting change. Bounded rather than
unbounded because an unbounded walk ends at ``/`` and will happily adopt any
unrelated ``VERSION`` file it passes on the way.
"""


def _identity_from(version_path: Path) -> str | None:
    """The ``release_id`` in this file, or ``None`` if it is not usable.

    Every ``None`` here is a deliberate refusal to guess: an unreadable file, a
    non-object payload, and a payload with no ``release_id`` are all "this is
    not a release identity", and none of them justifies substituting the
    directory name that happened to contain the file.
    """
    try:
        payload = json.loads(version_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    release_id = payload.get("release_id")
    if not isinstance(release_id, str) or not release_id:
        return None
    return release_id


@lru_cache(maxsize=1)
def running_release_id() -> str | None:
    """The release this module is running from, or ``None`` for a checkout.

    Cached because it cannot change within a process: the answer is a property
    of where this file was imported from, and a re-read per notice would put
    filesystem I/O on the sweep's emit path for a value that is fixed at
    import.
    """
    for parent in list(Path(__file__).resolve().parents)[:MAX_PARENTS_SEARCHED]:
        release_id = _identity_from(parent / VERSION_FILENAME)
        if release_id is not None:
            return release_id
    return None


__all__ = [
    "MAX_PARENTS_SEARCHED",
    "VERSION_FILENAME",
    "running_release_id",
]
