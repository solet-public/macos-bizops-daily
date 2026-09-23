"""The assembled hash-pinned rulebook (W3-C C1).

The F2 rulebook ships as a committed, in-package JSON build artifact
(``assembled_rulebook.json``) assembled from its tier-tagged sources by
:mod:`assembler`, with a three-level hash manifest (per-source / per-tier /
whole-artifact) computed by :mod:`manifest`. ``verify/rulebook.py`` loads the
artifact and verifies the whole-artifact hash FAIL-LOUD at every load;
``scanners/rulebook_sync`` (W3C-1b) re-derives the same hashes to catch drift.
One shared hashing module is what keeps the assembler and the checker from
disagreeing. In-package placement is release-copy safe (it retires B3a's
worktree-anchor path); the committed artifact makes every rulebook change a
Git-Controller-gated, reviewable diff (it is never assembled at first-load).
"""

from __future__ import annotations
