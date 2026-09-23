"""Canonical ``root_uri`` helpers for the session-ledger ingest layer.

A ``session_ledger__source`` row is keyed by ``(source_kind, root_uri)``.
For filesystem-backed pulling sources the ``root_uri`` is the walk root; for
exports it is a blob id; for pushed sources it is a ``pushed:<kind>`` sentinel;
for symbolic sources it is ``local:<name>``. Two spellings of the same
filesystem root (``~/x`` vs ``file:///abs/x`` vs ``/abs/x``) must collapse to
ONE row, or polling double-ingests (P1.1.B).

This module is the single home for the two halves of that contract:

* :func:`normalize_root_uri` — lexical canonicalization to ``file:///<abs>``
  form (pure; safe when the path does not exist). Applied as the first step
  at every registration write seam, and as defense-in-depth inside the
  repository's ``insert_source`` / ``find_source_id_by_kind_and_root_uri``.
* :func:`canonicalize_root_uri_for_storage` — :func:`normalize_root_uri` plus
  a ``realpath`` collapse when the path EXISTS (symlink aliases converge —
  Codex round-4 C2). Applied only at the storage/apply seam.
* :func:`root_uri_to_path` — the inverse: resolve a filesystem ``root_uri`` to
  a concrete :class:`~pathlib.Path`. Used by the pulling plugins (P1.1.E) to
  walk the per-source root instead of a config singleton.

Determinism scope (Codex C1): deterministic WITHIN a solet, NOT across —
``expanduser`` / ``expandvars`` resolve against THIS host's HOME/env, so the
same logical file is ``/Users/alice`` on one box and ``/home/x`` on another. That
is correct: a ``__source`` row is solet-local.

Module is import-pure (no platform deps) so callers retain their RELOAD_SAFE
posture.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

__all__ = [
    "canonicalize_root_uri_for_storage",
    "normalize_root_uri",
    "root_uri_to_path",
]


def _filesystem_path_or_none(root_uri: str) -> str | None:
    """Return the raw filesystem path for a fs ``root_uri``, else ``None``.

    ``None`` signals a non-filesystem ``root_uri`` (blob id, ``pushed:*`` /
    ``local:*`` sentinel) that must pass through unchanged. Raises ``ValueError``
    for a ``file://`` uri carrying a non-empty/non-``localhost`` authority
    rather than silently dropping it (Codex C1 — ``urlsplit`` catches the
    authority that ``urlparse().path`` would discard).
    """
    if root_uri.startswith("file://"):
        parts = urlsplit(root_uri)
        if parts.netloc not in ("", "localhost"):
            raise ValueError(f"unsupported file:// authority: {root_uri!r}")
        return unquote(parts.path)
    if root_uri.startswith(("/", "~")):
        return root_uri
    return None


def normalize_root_uri(root_uri: str) -> str:
    """Canonicalize a filesystem ``root_uri`` to ``file:///<abs>`` form.

    ``/abs``, ``~/x`` and ``file:///abs`` collapse to one canonical
    ``file:///<abs>``. Non-filesystem ``root_uri`` values — blob ids
    (``bmd-*``), pushed sentinels (``pushed:*``), symbolic roots
    (``local:agent_messaging``) — pass through UNCHANGED.

    Stays lexical (no ``realpath``) so it is safe when the path does not yet
    exist; symlink convergence is handled at the apply seam by
    :func:`canonicalize_root_uri_for_storage`. Idempotent fixed point: a value
    already in ``file:///<abs>`` form returns unchanged.
    """
    path = _filesystem_path_or_none(root_uri)
    if path is None:
        return root_uri
    abs_path = os.path.abspath(os.path.expanduser(os.path.expandvars(path)))
    return f"file://{abs_path}"


def canonicalize_root_uri_for_storage(root_uri: str) -> str:
    """:func:`normalize_root_uri` + ``realpath`` collapse when the path exists.

    The storage/apply seam (registration) calls this so two symlink spellings
    of the same EXISTING directory converge to one ``__source`` row (Codex
    round-4 C2). When the resolved path does not exist the lexical form is kept
    (no ``realpath`` on a missing path). Non-filesystem ``root_uri`` values pass
    through unchanged.
    """
    normalized = normalize_root_uri(root_uri)
    if not normalized.startswith("file://"):
        return normalized
    abs_path = normalized.removeprefix("file://")
    if os.path.exists(abs_path):
        return f"file://{os.path.realpath(abs_path)}"
    return normalized


def root_uri_to_path(root_uri: str) -> Path:
    """Resolve a filesystem ``root_uri`` to a concrete :class:`~pathlib.Path`.

    Inverse of :func:`normalize_root_uri` for the filesystem forms. Accepts the
    canonical ``file:///<abs>`` form (what a ``__source`` row stores), a bare
    ``/abs`` or ``~/x`` path. A non-filesystem ``root_uri`` (blob id,
    ``pushed:*`` / ``local:*`` sentinel) raises ``ValueError`` — those sources
    never resolve a filesystem path.
    """
    path = _filesystem_path_or_none(root_uri)
    if path is None:
        raise ValueError(f"not a filesystem root_uri: {root_uri!r}")
    return Path(os.path.expanduser(os.path.expandvars(path)))
