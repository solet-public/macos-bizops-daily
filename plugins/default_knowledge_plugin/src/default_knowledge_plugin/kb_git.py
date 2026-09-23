"""Git subprocess helpers for the default knowledge plugin.

All functions are pure subprocess wrappers — no plugin instance state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

from ananta.core.domain.enums import ActionStatus

if TYPE_CHECKING:
    from .models import Manifest

# The local branch KB updates are committed onto. An operator-identity-free
# internal label. Create-if-missing self-migrates: on a legacy repo whose HEAD is
# on the old origin-named branch, `git checkout -b managed` forks from that tip
# (history preserved) and leaves the legacy branch inert (never-delete-branches).
_GIT_BRANCH = "managed"


def git_clone(url: str, target: Path, token: str | None = None) -> None:
    """Clone a git repository. Token used only in-memory for auth."""
    clone_url = url
    if token:
        parsed = urlparse(url)
        clone_url = f"{parsed.scheme}://{token}@{parsed.netloc}{parsed.path}"
    subprocess.run(
        ["git", "clone", clone_url, str(target)],
        check=True,
        capture_output=True,
        text=True,
    )


def git_setup_branch(repo_path: Path) -> None:
    """Create and checkout the 'managed' branch if it doesn't exist."""
    result = subprocess.run(
        ["git", "branch", "--list", _GIT_BRANCH],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    if _GIT_BRANCH not in result.stdout:
        subprocess.run(
            ["git", "checkout", "-b", _GIT_BRANCH],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        subprocess.run(
            ["git", "checkout", _GIT_BRANCH],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        )


def git_head_sha(repo_path: Path) -> str:
    """Get current HEAD commit SHA."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def git_default_branch(repo_path: Path) -> str:
    """Detect the default remote branch (main/master/etc)."""
    result = subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip().split("/")[-1]
    for branch in ("main", "master"):
        check = subprocess.run(
            ["git", "rev-parse", "--verify", f"origin/{branch}"],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        if check.returncode == 0:
            return branch
    return "main"


def git_changed_files(repo_path: Path, from_sha: str) -> list[str]:
    """Get list of changed files since a commit."""
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{from_sha}..HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return [f for f in result.stdout.strip().split("\n") if f]


def git_commit_file(kb_dir: Path, relative_path: str, message: str) -> None:
    """Stage and commit a file on the managed branch."""
    subprocess.run(
        ["git", "add", relative_path],
        cwd=kb_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=kb_dir,
        check=True,
        capture_output=True,
        text=True,
    )


def resolve_git_token(name: str, address_book_service: Any) -> str | None:
    """Resolve a git authentication token from the address book.

    Returns None when the address book is unavailable or has no token entry.
    """
    if address_book_service is None:
        return None
    try:
        resolved = address_book_service.resolve_with_secrets(name)
        if resolved.get("action_status") != ActionStatus.COMPLETED.value:
            return None
        data = resolved.get("data", {})
        if not isinstance(data, dict):
            return None
        for entry in data.get("entries", []):
            if entry.get("field_type") == "token":
                return entry.get("value")  # type: ignore[no-any-return]
    except Exception:
        pass
    return None


def fetch_and_merge_git(
    kb_dir: Path,
    record: dict[str, Any],
    token: str | None,
    manifest: Manifest,
    collect_files_fn: Any,
) -> list[Path] | dict[str, Any]:
    """Fetch origin, merge default branch into managed, detect changed files.

    Returns a list of changed Paths on success, or an error dict on merge conflict.
    ``collect_files_fn`` is ``kb_source.collect_files`` injected to avoid circular import.
    """
    if token and record.get("source"):
        parsed = urlparse(record["source"])
        auth_url = f"{parsed.scheme}://{token}@{parsed.netloc}{parsed.path}"
        original_url = record["source"]
        subprocess.run(
            ["git", "remote", "set-url", "origin", auth_url],
            cwd=kb_dir, check=True, capture_output=True, text=True,
        )
        try:
            subprocess.run(
                ["git", "fetch", "origin"],
                cwd=kb_dir, check=True, capture_output=True, text=True,
            )
        finally:
            subprocess.run(
                ["git", "remote", "set-url", "origin", original_url],
                cwd=kb_dir, check=True, capture_output=True, text=True,
            )
    else:
        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=kb_dir, check=True, capture_output=True, text=True,
        )

    default_branch = git_default_branch(kb_dir)
    merge_result = subprocess.run(
        ["git", "merge", f"origin/{default_branch}", "--no-edit"],
        cwd=kb_dir, capture_output=True, text=True,
    )

    if merge_result.returncode != 0:
        subprocess.run(
            ["git", "merge", "--abort"],
            cwd=kb_dir, capture_output=True, text=True,
        )
        return {
            "status": "error",
            "error": "merge_conflict",
            "message": merge_result.stderr,
        }

    last_commit = record.get("last_indexed_commit")
    if last_commit:
        changed_names = git_changed_files(kb_dir, last_commit)
        return [kb_dir / f for f in changed_names if (kb_dir / f).exists()]
    return cast(list[Path], collect_files_fn(kb_dir, manifest))
