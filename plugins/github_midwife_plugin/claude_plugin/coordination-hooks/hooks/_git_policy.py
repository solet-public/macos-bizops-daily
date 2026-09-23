"""Runner-neutral Git-Controller mistake-prevention policy.

This module owns the policy shared by the Claude Code and Codex hook adapters.
It does not read runner identity or project environment variables: callers
resolve those bindings and pass them in explicitly.  The module is stdlib-only
because hook handlers run outside the platform virtualenv.

This is not an adversarial security boundary.  It prevents trusted peer
sessions from accidentally mutating git state or bypassing the designated
controller workflow.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# ruff: noqa: I001, E402
# pyright: reportMissingImports=false
from _git_controller_walker import heredoc_body_is_script_source, walk_git_invocations


EXPLORE_SUBAGENT = "Explore"

# Claude Code currently routes these names.  A runner adapter may expose only
# the subset whose live wire shape it has measured.
SUBAGENT_TOOL_NAMES = frozenset({"Task", "Agent"})
FILE_PATH_TOOL_NAMES = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})
BASH_TOOL_NAME = "Bash"
GATED_TOOL_NAMES = frozenset({BASH_TOOL_NAME}) | FILE_PATH_TOOL_NAMES | SUBAGENT_TOOL_NAMES
# The historical constant name remains the four-copy smoke's stable surface,
# but its content is now an automatic recovery instruction. No local liveness
# signal can safely prove a cross-runner deployment is solo, so no exemption is
# offered. Held as its own constant so the gate's copies can be asserted
# byte-equal on the clause SECTION, while the per-copy parameterized parts of
# the surrounding message stay free to differ.
EXEMPTION_CLAUSE = (
    "Ask your coordinator to arrange a bounded, authorized handoff to "
    "Git-Controller. Do not provision or start a Git-Controller session "
    "yourself."
)

# Operator-ruled wording, 2026-08-01 (A5). Verbatim but for two typo
# normalizations, disclosed in the WS-3b operator decision sheet — do not
# re-normalize further.
POLICY_MESSAGE = (
    "Issuing git commands in a multi-session environment can lead to data "
    "loss. Policy is to delegate all repository-impacting git commands to a "
    "designated 'git controller' session. " + EXEMPTION_CLAUSE
)

ALLOWED_NO_FLAG_CHECK = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
        "blame",
        "shortlog",
        "describe",
        "name-rev",
        "for-each-ref",
        "reflog",
        "rev-parse",
        "rev-list",
        "merge-base",
        "ls-files",
        "ls-tree",
        "ls-remote",
        "cat-file",
        "check-ignore",
        "verify-commit",
        "verify-tag",
        "grep",
        "var",
        "help",
        "version",
        "--version",
    }
)

DUAL_MODE_ALLOWED: dict[str, frozenset[str]] = {
    "branch": frozenset(
        {
            "-v",
            "-l",
            "-r",
            "--show-current",
            "--list",
            "--contains",
            "--no-contains",
            "-a",
            "--all",
        }
    ),
    "tag": frozenset({"-l", "-n", "--list", "--contains", "--no-contains"}),
    "stash": frozenset({"list", "show"}),
    "remote": frozenset({"-v", "show"}),
    "submodule": frozenset({"status", "summary"}),
    "worktree": frozenset({"list"}),
    "bisect": frozenset({"log", "view"}),
    "config": frozenset({"--get", "--list", "--get-all", "--get-regexp"}),
}

UNIVERSAL_BANNED_FLAGS = frozenset({"--no-verify"})
CONTROLLER_CONFIRMATION_ENV = "GIT_CONTROLLER_OPERATOR_CONFIRMATION"
CONTROLLER_CONFIRMATION_REQUIRED_SENTENCE = (
    "Even Git-Controller must stop for explicit operator confirmation before a "
    "force-push, `reset --hard`, `clean -fd`, rebase of a shared branch, branch "
    "deletion (`-d` or `-D`), `--no-verify`, or `checkout --` / `restore` that "
    "discards path contents."
)
CONTROLLER_CONFIRMATION_CONTRACT = (
    "Prefix exactly one target git invocation with "
    "GIT_CONTROLLER_OPERATOR_CONFIRMATION=<rul_<8 hex>, arm-<32 hex>, or "
    "agm-_<8-32 hex>>. "
    "The citation is command-local and cannot cover a second git invocation."
)
DANGEROUS_C_KEY_PREFIXES: tuple[str, ...] = (
    "commit.gpgsign",
    "core.hooksPath",
    "gc.auto",
    "alias.",
)
DANGEROUS_GIT_GLOBALS = frozenset({"--git-dir", "--work-tree"})

_MUTATING_DUAL_FLAGS = frozenset({"-d", "-D", "-m", "-M", "--delete", "--force"})
_NOARG_READONLY_SUBS = frozenset(
    {"branch", "tag", "remote", "submodule", "worktree", "bisect"},
)
_BOOLEAN_GIT_GLOBALS = frozenset(
    {
        "-p",
        "--paginate",
        "--no-pager",
        "--no-replace-objects",
        "--bare",
        "--literal-pathspecs",
        "--no-optional-locks",
        "--no-advice",
    }
)
_VALUE_GIT_GLOBALS = frozenset(
    {
        "-C",
        "-c",
        "--exec-path",
        "--html-path",
        "--man-path",
        "--info-path",
        "--namespace",
        "--super-prefix",
        "--config-env",
    }
)
_FS_MUTATING_VERBS = frozenset(
    {
        "rm",
        "mv",
        "cp",
        "ln",
        "dd",
        "install",
        "chmod",
        "chown",
        "mkdir",
        "rmdir",
        "touch",
        "tee",
        "truncate",
        "shred",
        "unlink",
    }
)
_SHELL_REDIRECT_TOKENS = frozenset({">", ">>", ">|"})


def _check_universal_banned(invocation: list[str]) -> tuple[bool, str]:
    """Universal flag bans, dangerous globals, and inline config keys."""
    rest = invocation[2:]
    for flag in UNIVERSAL_BANNED_FLAGS:
        if flag in rest:
            return True, f"banned flag {flag!r}"
    for global_opt in DANGEROUS_GIT_GLOBALS:
        if global_opt in invocation:
            return True, f"banned global git option {global_opt!r}"
    for index in range(len(invocation) - 1):
        if invocation[index] != "-c" or "=" not in invocation[index + 1]:
            continue
        key, _, value = invocation[index + 1].partition("=")
        blocked = _key_blocked(key, value)
        if blocked is not None:
            return True, blocked
    return False, ""


def _key_blocked(key: str, value: str) -> str | None:
    """Return a block reason when a ``-c key=value`` pair is dangerous."""
    for prefix in DANGEROUS_C_KEY_PREFIXES:
        if not key.startswith(prefix):
            continue
        if prefix == "alias." and not value.startswith("!"):
            return None
        return f"banned inline config {key!r}"
    return None


def _check_dual_mode_subcommand(subcommand: str, rest: list[str]) -> tuple[bool, str]:
    """Evaluate a dual-mode subcommand against its explicit read-only set."""
    if not rest:
        if subcommand == "stash":
            return False, "bare `git stash` defaults to push — banned"
        if subcommand in _NOARG_READONLY_SUBS:
            return True, f"{subcommand!r} with no args is read-only"
        return False, f"{subcommand!r} requires explicit read-only flag"
    first = rest[0]
    if first not in DUAL_MODE_ALLOWED[subcommand]:
        return False, f"{subcommand!r} with non-allowlisted first arg {first!r}"
    for token in rest:
        if token in _MUTATING_DUAL_FLAGS:
            return False, f"mutating flag {token!r} after read-only {first!r}"
    return True, f"{subcommand!r} {first!r} is read-only"


def _find_subcommand_index(invocation: list[str]) -> int | None:
    """Return the real subcommand index after recognized git global options."""
    index = 1
    while index < len(invocation):
        token = invocation[index]
        if not token.startswith("-"):
            return index
        if token in _BOOLEAN_GIT_GLOBALS:
            index += 1
            continue
        base = token.split("=", 1)[0]
        if base in _VALUE_GIT_GLOBALS:
            index += 1 if "=" in token else 2
            continue
        return index
    return None


def is_invocation_allowed(invocation: list[str]) -> tuple[bool, str]:
    """Evaluate one git invocation against the read-only allowlist."""
    if len(invocation) < 2:
        return False, "bare 'git' with no subcommand"
    blocked, reason = _check_universal_banned(invocation)
    if blocked:
        return False, reason
    subcommand_index = _find_subcommand_index(invocation)
    if subcommand_index is None:
        return False, "bare 'git' with no subcommand"
    subcommand = invocation[subcommand_index]
    rest = invocation[subcommand_index + 1 :]
    if subcommand in ALLOWED_NO_FLAG_CHECK:
        return True, f"read-only subcommand {subcommand!r}"
    if subcommand in DUAL_MODE_ALLOWED:
        return _check_dual_mode_subcommand(subcommand, rest)
    return False, f"subcommand {subcommand!r} is not in the read-only allowlist"


def _is_broad_or_directory_pathspec(pathspec: str) -> bool:
    """Return true for a broad pathspec or one resolving to a directory."""
    if pathspec in {".", ":/", "*"} or any(char in pathspec for char in "*?["):
        return True
    if pathspec.endswith("/"):
        return True
    try:
        return Path(pathspec).is_dir()
    except OSError:
        return True


def _has_explicit_file_paths(pathspecs: list[str]) -> bool:
    """Return true only for a non-empty, explicit list of file paths."""
    return bool(pathspecs) and not any(
        _is_broad_or_directory_pathspec(pathspec) for pathspec in pathspecs
    )


def _restore_pathspecs(rest: list[str]) -> list[str]:
    """Extract restore pathspecs without treating ``--source``'s ref as one."""
    if "--" in rest:
        return rest[rest.index("--") + 1 :]
    pathspecs: list[str] = []
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--source":
            index += 2
            continue
        if token.startswith("--source=") or token.startswith("-"):
            index += 1
            continue
        pathspecs.append(token)
        index += 1
    return pathspecs


def _checkout_confirmation_reason(subcommand: str, rest: list[str]) -> str | None:
    if subcommand != "checkout":
        return None
    pathspecs = rest[rest.index("--") + 1 :] if "--" in rest else []
    if not _has_explicit_file_paths(pathspecs):
        return "`git checkout` requires an explicit file-path list"
    return None


def _restore_confirmation_reason(subcommand: str, rest: list[str]) -> str | None:
    if subcommand == "restore" and not _has_explicit_file_paths(_restore_pathspecs(rest)):
        return "`git restore` requires an explicit file-path list"
    return None


def _reset_confirmation_reason(subcommand: str, rest: list[str]) -> str | None:
    if subcommand == "reset" and any(token in {"--hard", "--merge", "--keep"} for token in rest):
        return "destructive `git reset` mode requires explicit operator confirmation"
    return None


def _clean_confirmation_reason(subcommand: str, rest: list[str]) -> str | None:
    if subcommand == "clean" and any(
        token in {"--force", "-x", "-X"}
        or (token.startswith("-") and not token.startswith("--") and "f" in token[1:])
        for token in rest
    ):
        return "forceful `git clean` requires explicit operator confirmation"
    return None


def _stash_confirmation_reason(subcommand: str, rest: list[str]) -> str | None:
    if subcommand != "stash":
        return None
    if not rest or rest[0] in {"drop", "clear"}:
        return "destructive `git stash` form requires explicit operator confirmation"
    if rest[0] == "push" and not _has_explicit_file_paths(
        rest[rest.index("--") + 1 :] if "--" in rest else []
    ):
        return "`git stash push` requires an explicit file-path list"
    return None


def _branch_confirmation_reason(subcommand: str, rest: list[str]) -> str | None:
    if subcommand == "branch" and any(token in {"-d", "-D", "--delete"} for token in rest):
        return "branch deletion requires explicit operator confirmation"
    return None


def _push_confirmation_reason(subcommand: str, rest: list[str]) -> str | None:
    if subcommand == "push" and any(
        token.startswith("--force") or token.startswith("+") for token in rest
    ):
        return "forceful `git push` requires explicit operator confirmation"
    return None


def _worktree_confirmation_reason(subcommand: str, rest: list[str]) -> str | None:
    if subcommand == "worktree" and (
        rest[:1] == ["prune"] or (rest[:1] == ["remove"] and "--force" in rest)
    ):
        return "destructive `git worktree` form requires explicit operator confirmation"
    return None


def _controller_destructive_reason(invocation: list[str]) -> str | None:
    """Classify controller-only forms that require an operator citation."""
    subcommand_index = _find_subcommand_index(invocation)
    if subcommand_index is None:
        return None
    subcommand = invocation[subcommand_index]
    rest = invocation[subcommand_index + 1 :]
    if "--no-verify" in rest:
        return "`--no-verify` requires explicit operator confirmation"
    if subcommand == "rebase":
        return "`git rebase` requires explicit operator confirmation"
    classifiers = (
        _checkout_confirmation_reason,
        _restore_confirmation_reason,
        _reset_confirmation_reason,
        _clean_confirmation_reason,
        _stash_confirmation_reason,
        _branch_confirmation_reason,
        _push_confirmation_reason,
        _worktree_confirmation_reason,
    )
    for classifier in classifiers:
        reason = classifier(subcommand, rest)
        if reason is not None:
            return reason
    return None


# iss_ceebab20 / unt_304d827b: an operator-confirmation citation is matched as a
# WHOLE TOKEN against an explicit id grammar, never as a substring. The previous
# rule accepted any citation merely CONTAINING "turn"/"message"/"msg"/"rul", so
# "rules", "turnip" and "msgs" each authorized one destructive git invocation.
# Co-occurrence anywhere in a blob cannot tell mention from meaning; position and
# shape can. Extend by adding one alternative here — keep all four copies equal.
_CITATION_ID_PATTERN = re.compile(
    r"^(?:"
    r"rul_[0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?"
    r"|arm-[0-9a-f]{32}"
    r"|agm-_[0-9a-f]{8,32}"
    r")$",
    re.IGNORECASE,
)
_STANDALONE_GIT_TOKEN = re.compile(r"(?<![A-Za-z0-9_])git(?![A-Za-z0-9_])")


def _unparseable_source_names_git(command: str) -> bool:
    """Search retained shell source, never a heredoc data body, for ``git``."""
    try:
        from _git_controller_lex import split_heredoc_bodies

        retained, heredocs = split_heredoc_bodies(command)
    except (ImportError, ValueError):
        return _STANDALONE_GIT_TOKEN.search(command) is not None
    sources = [retained]
    sources.extend(
        body for owner_line, body in heredocs if heredoc_body_is_script_source(owner_line)
    )
    return any(_STANDALONE_GIT_TOKEN.search(source) is not None for source in sources)


def _unparseable_git_block_reason(command: str, parsed_ok: bool) -> str | None:
    """Return the fail-closed reason when unparseable shell source names git."""
    if parsed_ok or not _unparseable_source_names_git(command):
        return None
    return "Bash command containing `git` could not be safely inspected"


def _check_noncontroller_bash(command: str) -> tuple[bool, str]:
    """Apply direct-.git, fail-closed, and invocation policy to peer shell."""
    if _command_targets_dot_git(command):
        return True, "command appears to mutate `.git/` directly"
    invocations, parsed_ok = walk_git_invocations(command)
    unparseable_reason = _unparseable_git_block_reason(command, parsed_ok)
    if unparseable_reason is not None:
        return True, unparseable_reason
    for invocation in invocations:
        allowed, reason = is_invocation_allowed(invocation)
        if not allowed:
            joined = " ".join(invocation[:5])
            return True, f"banned git invocation `{joined}...`: {reason}"
    return False, ""


def _has_single_invocation_confirmation(command: str, invocation_count: int) -> bool:
    """Return true for one visible, command-local operator citation prefix."""
    if invocation_count != 1:
        return False
    try:
        from _git_controller_lex import punctuation_tokenize

        tokens = punctuation_tokenize(command)
    except (ValueError, ImportError):
        return False
    for index, token in enumerate(tokens[:-1]):
        if not token.startswith(f"{CONTROLLER_CONFIRMATION_ENV}="):
            continue
        citation = token.partition("=")[2]
        if _CITATION_ID_PATTERN.match(citation) is None:
            continue
        next_token = tokens[index + 1]
        if next_token == "git" or next_token.endswith("/git"):
            return True
    return False


def check_bash(
    tool_input: dict[str, object],
    session_role: str | None,
    controller_role: str | None,
) -> tuple[bool, str]:
    """Apply the Bash policy and return ``(block, reason)``."""
    if controller_role is None:
        return False, ""
    raw_command = tool_input.get("command", "")
    command = raw_command if isinstance(raw_command, str) else ""
    if session_role == controller_role:
        invocations, _ = walk_git_invocations(command)
        for invocation in invocations:
            reason = _controller_destructive_reason(invocation)
            if reason is None:
                continue
            if _has_single_invocation_confirmation(command, len(invocations)):
                return False, ""
            return True, (
                f"{reason}. {CONTROLLER_CONFIRMATION_REQUIRED_SENTENCE} "
                f"{CONTROLLER_CONFIRMATION_CONTRACT}"
            )
        return False, ""
    return _check_noncontroller_bash(command)


def _tokens_target_dot_git(tokens: list[str], separators: frozenset[str]) -> bool:
    """Return true for an fs-mutator or redirect whose operands name ``.git/``."""
    for index, token in enumerate(tokens):
        if token not in _FS_MUTATING_VERBS and token not in _SHELL_REDIRECT_TOKENS:
            continue
        for operand in tokens[index + 1 :]:
            if operand in separators:
                break
            if ".git/" in operand or operand == ".git":
                return True
    return False


def _command_targets_dot_git(command: str) -> bool:
    """Return true for an fs-mutator or redirect targeting ``.git/``.

    Heredoc BODIES are excluded for the same reason the walker excludes them:
    they are data. Prose about repository internals — a note saying never to
    delete ``.git/index`` by hand — is not a mutation, and reading it as one
    is the identical false positive one door over. A body fed to a shell
    evaluator is still scanned, exactly as the walker scans it.
    """
    try:
        from _git_controller_lex import (
            CHAIN_SEPARATORS,
            punctuation_tokenize,
            split_heredoc_bodies,
        )

        retained, heredocs = split_heredoc_bodies(command)
        segments = [retained]
        segments.extend(
            body for owner_line, body in heredocs if heredoc_body_is_script_source(owner_line)
        )
        return any(
            _tokens_target_dot_git(punctuation_tokenize(segment), CHAIN_SEPARATORS)
            for segment in segments
        )
    except (ValueError, ImportError):
        return False


def _collect_tool_input_paths(tool_input: dict[str, object]) -> list[str]:
    """Extract every supported path field from one tool payload."""
    paths: list[str] = []
    for key in ("file_path", "notebook_path", "path", "target"):
        value = tool_input.get(key)
        if isinstance(value, str):
            paths.append(value)
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for entry in edits:
            if not isinstance(entry, dict):
                continue
            file_path = entry.get("file_path")
            if isinstance(file_path, str):
                paths.append(file_path)
    return paths


def _path_under_git_dir(raw: str, git_dir: Path) -> bool:
    """Return true when ``raw`` resolves under ``git_dir``."""
    try:
        resolved = Path(raw).resolve()
    except OSError:
        return False
    try:
        resolved.relative_to(git_dir)
    except ValueError:
        return False
    return True


def check_file_path(
    tool_input: dict[str, object],
    session_role: str | None,
    controller_role: str | None,
    repo_root: str | None,
) -> tuple[bool, str]:
    """Block a non-controller file tool from writing under ``.git/``."""
    if controller_role is None or session_role == controller_role:
        return False, ""
    paths = _collect_tool_input_paths(tool_input)
    if not paths or not repo_root:
        return False, ""
    try:
        git_dir = (Path(repo_root) / ".git").resolve()
    except OSError:
        return False, ""
    for raw in paths:
        if _path_under_git_dir(raw, git_dir):
            return True, f"writes to {str(git_dir)!r} are forbidden (path: {raw!r})"
    return False, ""


def check_task(
    tool_input: dict[str, object],
    session_role: str | None,
    controller_role: str | None,
) -> tuple[bool, str]:
    """Block non-controller sub-agent spawning except read-only Explore."""
    if controller_role is None or session_role == controller_role:
        return False, ""
    raw_subagent_type = tool_input.get("subagent_type", "")
    subagent_type = raw_subagent_type if isinstance(raw_subagent_type, str) else ""
    if subagent_type == EXPLORE_SUBAGENT:
        return False, ""
    return True, (
        f"Spawning a sub-agent (Task/Agent tool, subagent_type={subagent_type!r}) "
        "is forbidden for non-controller sessions. Sub-agents inherit your "
        "session identity and could perform git operations on your behalf, "
        "bypassing this policy."
    )
