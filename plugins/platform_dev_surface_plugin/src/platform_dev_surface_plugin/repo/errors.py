"""Typed rejections for repo_service — every failure is loud and specific.

Fail-closed posture (design §2.3): a path outside root, a denylisted path, a
credential-shaped byte sequence, or an absent tool is a TYPED error, never a
best-effort clamp or a silently-empty result. The distinction matters — an
empty result reads as "absent" to the caller; a typed rejection says
"excluded" / "refused" / "escaped".
"""

from __future__ import annotations


class RepoServiceError(ValueError):
    """Base for repo_service typed rejections (deterministic — not retryable)."""


class RepoPathError(RepoServiceError):
    """A path argument resolved OUTSIDE the repo root (traversal / symlink-escape / absolute-outside)."""


class RepoDenylistError(RepoServiceError):
    """A path argument targets a denylisted location (.git/, profile/ secrets, key material, runtime)."""


class RepoSecretError(RepoServiceError):
    """Returned bytes carried a credential shape; read_file refuses the whole file (Q2 ruling)."""


class RepoToolError(RuntimeError):
    """A required external tool (rg) is absent or errored — fail loud, never silent-fallback (B-N2)."""
