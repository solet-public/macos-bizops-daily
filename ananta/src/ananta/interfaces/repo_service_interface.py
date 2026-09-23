"""Repo Service Interface — read-only primitives over the local platform repo.

Lets an external MCP client (or the run_joseki driver) inspect the platform's
git WORKTREE without any mutation path. Every verb is READ-ONLY and repo-root
confined:

- ``search`` / ``read_file`` / ``list_files`` — repo-root-confined, secret-shape
  scrubbed (read_file refuses on a hit; search redacts snippets), denylist-
  excluded (.git/, profile/ secrets, key material), bounded with explicit
  truncation markers.
- ``git_status`` / ``git_diff`` — the read-only git surface the GIT-CONTROLLER
  policy explicitly permits; argv baked server-side, never mutating.
- ``propose_patch`` — stores a unified-diff ARTIFACT (state-interface, own
  namespace) plus a read-only ``git apply --check`` flag, and returns a
  patch_id. APPLY IS NOT A VERB — the operator / Git-Controller applies the
  artifact through the normal handoff. There is no model-callable apply path.

Plugins implementing this interface should:
1. Define ``service_interfaces`` returning a tuple containing ``RepoServiceInterface``.
2. Define ``supported_interface_versions`` mapping the interface to its version.
3. Confine + denylist every path argument; bake every git/rg argv server-side.

See: ``workbench/2026-07-05_b3_repo_and_gates_primitives_design.md`` §2.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from ananta.core.services.call_context import CallContext


class RepoServiceInterface(ABC):
    """Read-only repo inspection: search / read_file / list_files / git_status / git_diff / propose_patch."""

    INTERFACE_VERSION: ClassVar[str] = "1.0.0"

    @abstractmethod
    def search(
        self,
        query: str,
        path_glob: str | None = None,
        max_results: int = 50,
        *,
        call_context: CallContext | None = None,
    ) -> dict[str, Any]:
        """Ranked ``rg``-backed content search, repo-root confined; denylisted paths
        excluded, snippets secret-redacted, results capped. Fails loud if ``rg`` is absent."""
        ...

    @abstractmethod
    def read_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        *,
        call_context: CallContext | None = None,
    ) -> dict[str, Any]:
        """Read a repo-root-confined file (optional line range); byte/line-capped with an
        explicit truncation marker. REFUSES the whole file if it carries a credential shape."""
        ...

    @abstractmethod
    def list_files(
        self,
        path: str | None = None,
        depth: int = 1,
        glob: str | None = None,
        *,
        call_context: CallContext | None = None,
    ) -> dict[str, Any]:
        """List entries under a repo-root-confined directory (bounded depth), excluding
        denylisted paths; entry-count capped with an explicit truncation marker."""
        ...

    @abstractmethod
    def git_status(self, *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Porcelain-parsed working-tree status (branch, staged/unstaged/untracked). Read-only."""
        ...

    @abstractmethod
    def git_diff(
        self,
        ref: str | None = None,
        path: str | None = None,
        staged: bool = False,
        *,
        call_context: CallContext | None = None,
    ) -> dict[str, Any]:
        """Read-only ``git diff`` (optional ref/path, staged flag); diff-size capped with stat."""
        ...

    @abstractmethod
    def propose_patch(
        self, unified_diff: str, *, call_context: CallContext | None = None
    ) -> dict[str, Any]:
        """Store a unified-diff ARTIFACT + a read-only apply-check flag; return a patch_id.
        Apply stays with the operator/Git-Controller — there is no apply verb."""
        ...
