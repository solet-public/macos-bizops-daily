"""The installer's own interpreter pin, recognized byte-exactly (iss_f1d8cfc2).

Every real solet's coding-agent install stage rewrites the two tracked
coordination-hook manifests (``<cli>.patch_hook_interpreter``), binding each
bare ``python3`` hook command to ``<target>/.venv/bin/python3``.  Both manifests
sit under the ``plugins/github_midwife_plugin/`` roster root, so without this
module section 6.5 reports every real solet as ``executed_code_modified`` and
neither an update nor a doctor probe can run.

A modification is the installer's pin only when all of these hold: the path is
one of the two manifests the installer writes; the raw-diff row is a
content-only ``M`` of a regular file; and the working-tree bytes equal
``pin_hook_interpreter`` (the one transform the installer itself calls, from
``solet_setup_contracts``) applied to the committed blob at ``HEAD``, bound to
the record's literal canonical target joined with ``.venv/bin/python3``.  Any
other difference -- an extra edit, another interpreter, malformed JSON, a
manifest the transform leaves unchanged -- stays an executed-code modification.
A recognized pin is then an ordinary Class-T preserved tracked path: it is
committed by digest, and a fast-forward that does not touch it carries it to
the new tree byte-for-byte.  A candidate that changes a pinned manifest is still
``tracked_overlap_present``; carrying the pin across a changed manifest is a
Manager capability this release does not have (iss_c1a7df20).
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path

from solet_setup_contracts.hook_interpreter_pin import HOOK_MANIFEST_PATHS, HookManifestError, instance_interpreter, pin_hook_interpreter

from .errors import SourceError
from .existing_install_inspection import ExistingInstallFacts, RawRow
from .target_git import run_target_git

__all__ = ["BlobReader", "installer_pinned_paths", "read_target_blob", "worktree_bytes"]

#: ``<commit>:<path>`` -> the committed bytes; raises when the object is unreadable.
type BlobReader = Callable[[str], bytes]

_REGULAR_MODES = frozenset({"100644", "100755"})


def installer_pinned_paths(target: Path, facts: ExistingInstallFacts, read_blob: BlobReader) -> tuple[str, ...]:
    """Every tracked modification that is exactly the installer's interpreter pin of the ``HEAD`` blob, sorted."""
    head = facts.head_commit
    if head is None:
        return ()
    modified = frozenset(facts.tracked_paths.values)
    rows = {row.path: row for row in facts.tracked_entries.values}
    interpreter = instance_interpreter(target)
    pinned: list[str] = []
    for path in HOOK_MANIFEST_PATHS:
        row = rows.get(path)
        if path not in modified or row is None or not _content_only(row):
            continue
        try:
            expected = pin_hook_interpreter(read_blob(f"{head}:{path}"), interpreter)
        except HookManifestError:
            continue
        if expected is not None and worktree_bytes(target, path) == expected:
            pinned.append(path)
    return tuple(sorted(pinned))


def read_target_blob(target: Path) -> BlobReader:
    """A reader over the target's own object store through the hardened Git surface (``cat-file`` runs no filter)."""

    def read(spec: str) -> bytes:
        completed = run_target_git(("cat-file", "blob", spec), cwd=target)
        if completed.returncode:
            raise SourceError(f"committed blob {spec} is unreadable: {completed.stderr.decode('utf-8', 'replace').strip()}")
        return completed.stdout

    return read


def _content_only(row: RawRow) -> bool:
    return row.status == "M" and row.old_mode == row.new_mode and row.new_mode in _REGULAR_MODES


def worktree_bytes(target: Path, path: str) -> bytes | None:
    """The regular file's bytes, never through a final-component symlink; ``None`` when it is not a regular file."""
    try:
        descriptor = os.open(target / path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1_048_576):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)
