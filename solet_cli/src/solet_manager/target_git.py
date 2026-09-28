"""The one hardened form of every Git invocation the Manager runs against a target repository.

A target checkout is operator-writable, so its own ``.git/config`` (and anything it includes), its hooks
and its replace refs are untrusted input.  Every Manager Git vector against a target -- inspect, import,
update, create's resume verification, the doctor's seed-integrity census -- is built here
(iss_836499b3 review B1; iss_6a8d03a3):

- ``-c`` overrides that neutralise what a repository config can arm without being named here:
  ``core.fsmonitor=false``, ``core.hooksPath=/dev/null`` (an approved target write may pin its own empty
  Manager-owned directory instead), and no signature verification (``gpg.program`` stays unreachable);
- an environment with no inherited ``GIT_*`` redirection, no system or global configuration
  (``GIT_CONFIG_NOSYSTEM=1``, ``GIT_CONFIG_GLOBAL=/dev/null``), no replace refs
  (``GIT_NO_REPLACE_OBJECTS=1`` -- a replace ref would otherwise let HEAD's commit and tree answer for
  another object during the identity proof), no external diff, no pager and no terminal prompt;
- and, before any vector that can run a content filter, a textconv or a driver (``status``, ``diff``,
  ``show``, ``checkout`` ...), a scan of the
  repository-scoped configuration that refuses every key able to execute target-supplied code
  (``git_execution_surface_unsafe``).  A clean/smudge filter or a diff/merge driver is selected by
  attributes, so no ``-c`` override can disarm it by name; refusing is the only closed answer;
- a pinned location (review R2-1, iss_9e956e39): ``GIT_DIR`` and ``GIT_WORK_TREE`` name the verified
  directory itself, so a repository's ``core.worktree``/``core.bare`` cannot move where the Manager reads
  or writes.  Both keys are refused by the scan as well, and every write vector first proves that
  ``rev-parse --show-toplevel`` is the pinned directory.  The Manager-owned candidate cache is pinned as
  a bare repository; only ``git init``, which creates a repository, runs unpinned.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Sequence
from enum import StrEnum
from pathlib import Path

from .errors import UpdateBlockedError

GIT_EXECUTION_SURFACE_UNSAFE = "git_execution_surface_unsafe"
GIT_WORKTREE_REDIRECTED = "git_worktree_redirected"


class GitLayout(StrEnum):
    """Where a vector's repository lives relative to the directory it runs in."""

    #: ``<dir>/.git`` with ``<dir>`` itself as the work tree: every target.
    WORKTREE = "worktree"
    #: ``<dir>`` is the repository: the Manager-owned bare candidate cache.
    BARE = "bare"
    #: No repository yet: only ``git init`` creating one.
    UNPINNED = "unpinned"

_HARDENING = (
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "log.showSignature=false",
    "-c", "merge.verifySignatures=false",
)
_ENVIRONMENT = {
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_EXTERNAL_DIFF": "",
    "GIT_PAGER": "cat",
}
#: Subcommands that can run a clean filter, a diff driver or a transport over target content.  Review N8 adds the
#: transports, so the in-target candidate fetch is scanned by construction rather than by call order.
_SCANNED_SUBCOMMANDS = frozenset(
    {
        "status", "diff", "diff-index", "diff-files", "diff-tree", "show", "log", "grep", "blame", "add", "stash",
        "checkout", "merge", "restore", "switch", "reset", "cherry-pick",
        "fetch", "pull", "push", "ls-remote", "clone", "submodule", "remote",
    }
)
#: Global options that precede a subcommand, with how many tokens each consumes.
_GLOBAL_OPTIONS = {"-C": 2, "-c": 2, "--no-optional-locks": 1, "--no-pager": 1, "--literal-pathspecs": 1}
EXECUTABLE_CONFIG = tuple(
    re.compile(pattern)
    for pattern in (
        r"^core\.(fsmonitor|fsmonitorhookversion|hookspath|sshcommand|gitproxy|askpass|editor|pager|attributesfile|excludesfile|alternaterefscommand)$",
        r"^filter\..*",
        r"^diff\.(external|.*\.(command|textconv))$",
        r"^merge\..*\.driver$",
        r"^(credential|url|alias|gpg|http|https|include|includeif|submodule|protocol|uploadpack|receive|ssh|difftool|mergetool|pager|browser|maintenance)\..*",
        r"^sequence\.editor$",
        r"^commit\.(gpgsign|template)$",
        r"^remote\..*\.(proxy|proxyauthmethod|vcs|uploadpack|receivepack)$",
        # Not code, but it moves where Git reads and writes (review R2-1); ``core.bare`` is refused when true.
        r"^core\.worktree$",
    )
)
_GIT_TRUE = frozenset({"true", "yes", "on", "1"})


def target_git_argv(args: Sequence[str], *, hooks_dir: Path | None = None) -> tuple[str, ...]:
    """``git`` plus the hardening overrides, then ``args`` (global options such as ``-C`` included)."""
    hooks: tuple[str, ...] = () if hooks_dir is None else ("-c", f"core.hooksPath={hooks_dir}")
    return ("git", *_HARDENING, *hooks, *args)


def target_git_env(*, read_only: bool = True, inherit: bool = False) -> dict[str, str]:
    """The hardened environment; ``inherit`` keeps the caller's non-Git variables (network, HOME)."""
    base = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")} if inherit else {}
    base.setdefault("PATH", os.environ.get("PATH", os.defpath))
    environment = {**base, **_ENVIRONMENT}
    if read_only:
        environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment


def parse_git_config_entries(raw: bytes) -> tuple[tuple[str, str, str], ...]:
    """Parse ``git config --list --show-scope -z`` into (scope, key, value) rows.

    The wire shape is ``scope NUL key NL value NUL``; anything else fails closed.
    """
    tokens = raw.split(b"\0")
    if tokens[-1] != b"":
        raise ValueError("git config listing is not NUL-terminated")
    body = tokens[:-1]
    if len(body) % 2:
        raise ValueError("git config listing has an odd token count")
    rows: list[tuple[str, str, str]] = []
    for scope, entry in zip(body[::2], body[1::2], strict=True):
        key, separator, value = entry.decode("utf-8", "strict").partition("\n")
        if not separator or not key:
            raise ValueError("git config entry lacks a key/value separator")
        rows.append((scope.decode("utf-8", "strict"), key, value))
    return tuple(rows)


def unsafe_config_keys(entries: tuple[tuple[str, str, str], ...], *, bare: bool = False) -> tuple[str, ...]:
    """Return every repository-scoped key that would let the target run its own code or move its work tree.

    ``bare`` is the Manager-owned bare cache, whose own ``core.bare=true`` is its layout, not a redirect.
    """
    found = {
        key
        for scope, key, value in entries
        if scope in {"local", "worktree"}
        and (
            any(pattern.fullmatch(key.lower()) for pattern in EXECUTABLE_CONFIG)
            or (not bare and key.lower() == "core.bare" and value.strip().lower() in _GIT_TRUE)
        )
    }
    return tuple(sorted(found))


def refuse_unsafe_config(entries: tuple[tuple[str, str, str], ...], *, bare: bool = False) -> None:
    """Raise ``git_execution_surface_unsafe`` naming every executable or redirecting repository-scoped key."""
    unsafe = unsafe_config_keys(entries, bare=bare)
    if unsafe:
        raise UpdateBlockedError(
            GIT_EXECUTION_SURFACE_UNSAFE,
            f"target Git configuration can execute target-supplied code or move the work tree: {', '.join(unsafe)}",
            repair="Remove the executable Git configuration from the target, then retry.",
        )


def run_target_git(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    layout: GitLayout = GitLayout.WORKTREE,
    read_only: bool = True,
    hooks_dir: Path | None = None,
    inherit_environment: bool = False,
    timeout: float = 600,
    pass_fds: Sequence[int] = (),
    preexec_fn: Callable[[], object] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run one closed vector against a pinned repository, scanning its configuration first when content can be filtered.

    The directory is the leading ``-C`` (absolute), else ``cwd``, else ``.`` when ``preexec_fn`` changes into it.
    A scan that cannot list the configuration is returned as the vector's own failure (fail closed).
    """
    directory = pinned_directory(args, cwd=cwd, relative_allowed=preexec_fn is not None, layout=layout)
    environment = {**target_git_env(read_only=read_only, inherit=inherit_environment), **location_env(directory, layout)}

    def run(vector: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(  # noqa: S603 - closed vectors; the hardening is the point of this module
            target_git_argv(vector, hooks_dir=hooks_dir),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=environment,
            pass_fds=tuple(pass_fds),
            preexec_fn=preexec_fn,
        )

    prefix, subcommand = split_global_options(args)
    if subcommand in _SCANNED_SUBCOMMANDS:
        listing = run((*prefix, "config", "--list", "--show-scope", "-z"))
        if listing.returncode:
            return listing
        refuse_unsafe_config(parse_git_config_entries(listing.stdout), bare=layout is GitLayout.BARE)
    if not read_only and layout is GitLayout.WORKTREE and directory is not None:
        require_toplevel(run((*prefix, "rev-parse", "--show-toplevel")), directory)
    return run(args)


def pinned_directory(args: Sequence[str], *, cwd: Path | None, relative_allowed: bool, layout: GitLayout) -> str | None:
    """The directory a vector is pinned to, or ``None`` for an unpinned ``git init``."""
    if layout is GitLayout.UNPINNED:
        return None
    prefix, _ = split_global_options(args)
    changes = [prefix[index + 1] for index in range(len(prefix) - 1) if prefix[index] == "-C"]
    if len(changes) > 1 or (changes and not Path(changes[0]).is_absolute()):
        raise ValueError("a target Git vector changes directory more than once or to a relative path")
    if changes:
        return changes[0]
    if cwd is not None:
        return str(cwd)
    if relative_allowed:
        return "."
    raise ValueError("a target Git vector names no directory to pin")


def location_env(directory: str | None, layout: GitLayout) -> dict[str, str]:
    if directory is None:
        return {}
    if layout is GitLayout.BARE:
        return {"GIT_DIR": directory}
    return {"GIT_DIR": os.path.join(directory, ".git"), "GIT_WORK_TREE": directory}


def require_toplevel(completed: subprocess.CompletedProcess[bytes], directory: str) -> None:
    """Before a write: the work tree Git would write is the pinned directory (same device and inode)."""
    reported = completed.stdout.decode("utf-8", "replace").strip()
    try:
        same = completed.returncode == 0 and os.path.samefile(reported, directory)
    except OSError:
        same = False
    if not same:
        raise UpdateBlockedError(
            GIT_WORKTREE_REDIRECTED,
            f"Git would write the work tree {reported or '<unknown>'!r}, not the target {directory!r}",
            repair="Remove core.worktree (and any work-tree redirection) from the target's Git configuration, then retry.",
        )


def split_global_options(args: Sequence[str]) -> tuple[tuple[str, ...], str | None]:
    """Split ``args`` into its leading global options and the subcommand name."""
    index = 0
    while index < len(args) and args[index] in _GLOBAL_OPTIONS:
        index += _GLOBAL_OPTIONS[args[index]]
    return tuple(args[:index]), (args[index] if index < len(args) else None)
