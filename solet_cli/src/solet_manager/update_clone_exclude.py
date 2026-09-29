"""Whether the clone-exclude block an update adds would make one in-clone destination ignored (design section 6.3).

The runtime plan refuses an in-target destination the clone does not ignore.  The fleet launcher sits
under ``client/``, which existing clones do not ignore, so the update itself adds that ignore rule in the same
plan; the refusal is lifted only for a path the rule would really cover.  Only literal lines count: a line
naming the path, or a directory (trailing ``/``) that contains it.  A glob or a negation covers nothing.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from .existing_install_bundle import CLONE_EXCLUDE_DESTINATION, ManagedArtifact

__all__ = ["exclude_covers", "planned_exclude_covers"]

_NOT_LITERAL = re.compile(r"[*?\[\\]")


def exclude_covers(template_text: str, relative: str) -> bool:
    """Whether a literal line of ``template_text`` ignores ``relative``."""
    lines = (line.strip() for line in template_text.splitlines())
    literal = [line for line in lines if line and not line.startswith(("#", "!")) and _NOT_LITERAL.search(line) is None]
    return any(relative == line or (line.endswith("/") and relative.startswith(line)) for line in literal)


def planned_exclude_covers(artifacts: Iterable[ManagedArtifact], read_template: Callable[[str], str], relative: str) -> bool:
    """Whether the bundle's clone-exclude artifact, once written, would ignore ``relative``."""
    return any(exclude_covers(read_template(artifact.template_ref), relative) for artifact in artifacts if artifact.logical_destination == CLONE_EXCLUDE_DESTINATION)
