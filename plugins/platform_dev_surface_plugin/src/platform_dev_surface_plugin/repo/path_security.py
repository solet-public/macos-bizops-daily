"""Repo-root confinement + exclusion denylist (design §2.3 controls 1 & 2).

The confinement root is the app_home-resolved WORKTREE root — which CONTAINS
``profile/`` (vault/secrets) and ``.git/``. So the denylist is LOAD-BEARING,
not defense-in-depth (Rev-A ASK-4): without it, a confined-but-denylisted read
would leak credential material or git internals.

Two fail-closed gates, applied to EVERY path argument before any read:

1. **Confinement.** ``resolve()`` collapses ``..`` and follows symlinks, then
   the resolved real path MUST be inside the resolved root. Rejects ``..``
   traversal, absolute-outside paths, and symlinks whose target escapes root —
   with a typed :class:`RepoPathError`, never a clamp.
2. **Denylist.** A resolved path under an excluded directory (``.git``,
   ``profile``, bundled venvs, runtime dirs) or matching secret-file globs
   (``.env*``, ``*.key``/``*.pem``/…) is a typed :class:`RepoDenylistError` —
   NOT an empty result, so the caller learns it was EXCLUDED, not ABSENT.
"""

from __future__ import annotations

import unicodedata
from fnmatch import fnmatchcase
from pathlib import Path

from platform_dev_surface_plugin.repo.errors import RepoDenylistError, RepoPathError


def _norm(text: str) -> str:
    """Fold a path component to the filesystem's case+unicode-insensitive form.

    The confinement root's filesystem (APFS on macOS) is case-INSENSITIVE and
    unicode-normalizing, but ``realpath`` PRESERVES the requested case — so a
    request for ``Profile/`` / ``.GIT`` / ``SECRET.PEM`` resolves to the real
    ``profile/`` / ``.git`` / secret file yet a case-SENSITIVE denylist would
    miss it. Normalizing (NFC + casefold) BOTH the path parts and the denylist
    entries closes that bypass (F2)."""
    return unicodedata.normalize("NFC", text).casefold()

# Directory names that are excluded wherever they appear under the root.
_DENYLIST_DIR_PARTS: frozenset[str] = frozenset(
    {".git", "profile", ".ananta", "node_modules", "__pycache__"}
)
# Directory-name prefixes (bundled virtualenvs ship vendored third-party code
# + would themselves hold no first-party secrets, but are out of the dev surface).
_DENYLIST_DIR_PREFIXES: tuple[str, ...] = (".venv", "venv_", "venv.")
# Secret-bearing file-name globs (credential material never leaves the boundary).
_DENYLIST_FILE_GLOBS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_rsa.*",
    "id_ed25519",
    "id_ed25519.*",
    "*.keychain*",
    "*.keystore",
)

# Pre-normalized denylist (NFC + casefold) — compared against normalized path
# parts so the case+unicode-insensitive filesystem boundary cannot be dodged.
_DENYLIST_DIR_PARTS_NORM: frozenset[str] = frozenset(_norm(p) for p in _DENYLIST_DIR_PARTS)
_DENYLIST_DIR_PREFIXES_NORM: tuple[str, ...] = tuple(_norm(p) for p in _DENYLIST_DIR_PREFIXES)
_DENYLIST_FILE_GLOBS_NORM: tuple[str, ...] = tuple(_norm(g) for g in _DENYLIST_FILE_GLOBS)


def resolve_within_root(root: Path, raw_path: str) -> Path:
    """Resolve ``raw_path`` (real path, symlinks + ``..`` collapsed) inside ``root``.

    A relative ``raw_path`` is taken relative to ``root``; an absolute one is
    used as-is. The resolved real path must be inside the resolved root or a
    :class:`RepoPathError` is raised (never a clamp). Works on not-yet-existing
    targets — ``resolve()`` still collapses the existing prefix + symlinks.
    """
    root_resolved = root.resolve()
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root_resolved / candidate
    resolved = candidate.resolve()
    if resolved != root_resolved and not resolved.is_relative_to(root_resolved):
        raise RepoPathError(
            f"path {raw_path!r} resolves to {resolved} which is OUTSIDE the repo "
            f"root {root_resolved} (traversal, symlink-escape, or absolute-outside)"
        )
    return resolved


def assert_not_denylisted(root: Path, resolved: Path) -> None:
    """Raise :class:`RepoDenylistError` if ``resolved`` is under a denylisted path.

    ``resolved`` must already be confirmed inside ``root`` (call
    :func:`resolve_within_root` first). Checks every path component against the
    excluded directory set/prefixes and the final name against the secret-file
    globs. A denylisted path is a typed rejection — never a silent empty result.
    """
    relative = resolved.relative_to(root.resolve())
    for part in relative.parts:
        npart = _norm(part)
        if npart in _DENYLIST_DIR_PARTS_NORM or npart.startswith(_DENYLIST_DIR_PREFIXES_NORM):
            raise RepoDenylistError(
                f"path {relative} is under the denylisted location {part!r} "
                "(excluded, not absent — .git/profile/venv/runtime never readable)"
            )
    if any(fnmatchcase(_norm(resolved.name), glob) for glob in _DENYLIST_FILE_GLOBS_NORM):
        raise RepoDenylistError(
            f"file {resolved.name!r} matches a secret-file denylist pattern (excluded, not absent)"
        )


def confine(root: Path, raw_path: str) -> Path:
    """Confine + denylist-check ``raw_path`` against ``root``; return the real path.

    The single entry point every verb calls before touching a path. Raises
    :class:`RepoPathError` (outside root) or :class:`RepoDenylistError`
    (excluded) — both fail-closed.
    """
    resolved = resolve_within_root(root, raw_path)
    assert_not_denylisted(root, resolved)
    return resolved
