"""Minimal token-walker for the Git-Controller gate.

Sibling module to ``git_controller_gate.py``. Scope: detect direct
``git <subcommand>`` invocations from trusted peer sessions, including
when wrapped in ``bash -c`` / ``sh -c`` / ``eval`` or ``$(...)`` substitution.
NOT an adversarial obfuscation defense — peer mistakes don't ANSI-C-encode or
quote-splice.

Stdlib-only.
"""

from __future__ import annotations

import posixpath
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# ruff: noqa: I001, E402
# pyright: reportMissingImports=false
from _git_controller_lex import (
    CHAIN_SEPARATORS as _CHAIN_SEPARATORS,
    extract_subst_pieces as _extract_subst_pieces,
    punctuation_tokenize as _punctuation_tokenize,
    split_heredoc_bodies as _split_heredoc_bodies,
)

MAX_RECURSION = 5

SHELL_EVAL_BINARIES = frozenset({"eval", "bash", "sh", "zsh", "dash", "ksh"})

# Long shell options that take NO argument — skip when walking toward -c.
_SHELL_LONG_OPTS_NOARG = frozenset({
    "--noprofile", "--norc", "--login", "--posix", "--restricted",
})
# Short opt-cluster letters that consume the NEXT token (e.g. `bash -O extglob`).
_SHELL_SHORT_OPTS_WITH_ARG = frozenset({"O", "T", "D", "o"})
_COMMAND_PREFIXES = frozenset({"command", "env", "exec"})
_ENV_OPTIONS_WITH_ARG = frozenset({"-C", "-u", "--chdir", "--unset"})
_NON_EXECUTING_ARGUMENT_CONSUMERS = frozenset({
    "[", ":", "awk", "basename", "cat", "cut", "dirname", "echo", "false",
    "file", "grep", "head", "jq", "ls", "printf", "read", "realpath", "rg",
    "sed", "sort", "stat", "tail", "test", "tr", "true", "uniq", "wc",
})


def _token_is_git_command(token: str) -> bool:
    """True if a token is ``git`` or has posix basename ``git``.

    Catches path-qualified forms like ``/usr/bin/git`` that a peer might
    type out of habit.
    """
    return token == "git" or posixpath.basename(token) == "git"


def _collect_git_invocation(tokens: list[str], i: int) -> tuple[list[str], int]:
    """Collect a ``git ...`` chain up to the next chain separator.

    The first collected token is normalized to literal ``"git"`` so the
    downstream allowlist check works for path-qualified forms.
    """
    j = i + 1
    collected = ["git"]
    while j < len(tokens) and tokens[j] not in _CHAIN_SEPARATORS:
        collected.append(tokens[j])
        j += 1
    return collected, j


def _is_shell_assignment(token: str) -> bool:
    """Whether ``token`` is a leading shell variable assignment."""
    name, separator, _ = token.partition("=")
    return bool(separator) and name.isidentifier()


def _is_command_segment_token(tokens: list[str], index: int) -> bool:
    """Whether ``index`` is still inside the current simple command."""
    return index < len(tokens) and tokens[index] not in _CHAIN_SEPARATORS


def _is_command_prefix(token: str) -> bool:
    """Whether ``token`` precedes, rather than names, an executable."""
    return _is_shell_assignment(token) or (
        token in _COMMAND_PREFIXES and token != "env"
    )


def _env_option_width(option: str) -> int:
    """Return how many tokens one supported ``env`` option consumes."""
    return 2 if option in _ENV_OPTIONS_WITH_ARG else 1


def _skip_env_prefix(tokens: list[str], index: int) -> int:
    """Skip leading ``env`` assignments and options to its executable."""
    while _is_command_segment_token(tokens, index):
        option = tokens[index]
        if not (_is_shell_assignment(option) or option.startswith("-")):
            return index
        index += _env_option_width(option)
    return index


def _command_token_index(tokens: list[str], start: int) -> int | None:
    """Find one simple command's executable token after safe prefixes.

    A token spelling ``git`` is an invocation only when it is the executable
    after leading assignments or command-selection wrappers. Treating every
    argument token as an executable turns ``printf ... git reset --hard`` into
    a false mutation while preserving direct and wrapped git commands.
    """
    index = start
    while _is_command_segment_token(tokens, index):
        token = tokens[index]
        if token == "env":
            index = _skip_env_prefix(tokens, index + 1)
            continue
        if _is_command_prefix(token):
            index += 1
            continue
        return index
    return None


def _consume_shell_short_cluster(tok: str, j: int) -> tuple[int, bool] | None:
    """Walk a short-option cluster after a shell-evaluator.

    Returns ``(new_j, saw_c)`` or None if not a short cluster.
    """
    if not (len(tok) >= 2 and tok[0] == "-" and tok[1] != "-"):
        return None
    letters = tok[1:]
    if "c" in letters:
        return j + 1, True
    if letters[-1] in _SHELL_SHORT_OPTS_WITH_ARG:
        return j + 2, False
    return j + 1, False


def _step_shell_option(tok: str, j: int) -> tuple[int, bool, bool] | None:
    """One iteration of the shell-option walker.

    Returns ``(new_j, saw_c, stop)`` or None to break the walk.
    ``saw_c`` true means -c was found at new_j-1; caller returns new_j.
    ``stop`` true means break the walk.
    """
    if tok == "--":
        return j, False, True
    if tok == "-c":
        return j + 1, True, False
    if tok in _SHELL_LONG_OPTS_NOARG:
        return j + 1, False, False
    cluster = _consume_shell_short_cluster(tok, j)
    if cluster is None:
        return None
    new_j, saw_c = cluster
    return new_j, saw_c, False


def _walk_to_dash_c(tokens: list[str], start: int) -> int | None:
    """Walk shell options after a shell-evaluator binary; return the -c arg index."""
    j = start
    while (
        j < len(tokens)
        and tokens[j].startswith("-")
        and tokens[j] not in _CHAIN_SEPARATORS
    ):
        step = _step_shell_option(tokens[j], j)
        if step is None:
            return None
        new_j, saw_c, stop = step
        if stop:
            return None
        j = new_j
        if saw_c:
            return j if j < len(tokens) else None
    return None


def _handle_shell_eval_token(
    tokens: list[str], i: int, depth: int,
) -> tuple[list[list[str]], int] | None:
    """Handle ``eval`` / ``bash -c`` / ``sh -c`` recursion.

    Returns ``(invocations, new_i)`` or None if this token is not a
    shell-evaluator form. The depth-cap on ``walk_git_invocations``
    prevents pathological recursion.
    """
    tok = tokens[i]
    if tok not in SHELL_EVAL_BINARIES:
        return None
    if tok == "eval" and i + 1 < len(tokens):
        j = i + 1
        collected: list[str] = []
        while j < len(tokens) and tokens[j] not in _CHAIN_SEPARATORS:
            collected.append(tokens[j])
            j += 1
        if not collected:
            return [], i + 1
        inner, _ = walk_git_invocations(" ".join(collected), depth + 1)
        return inner, j
    cmd_idx = _walk_to_dash_c(tokens, i + 1)
    if cmd_idx is None:
        return None
    inner, _ = walk_git_invocations(tokens[cmd_idx], depth + 1)
    return inner, cmd_idx + 1


def heredoc_body_is_script_source(owner_line: str) -> bool:
    """True when a heredoc opened on ``owner_line`` is fed to a shell evaluator.

    A body handed to ``bash``/``sh``/``eval`` — ``bash <<EOF`` or
    ``cat <<EOF | bash`` — is script SOURCE, not data, and stays exactly as
    visible to the walker as it was before heredoc bodies were split out. This
    is the half of the heredoc fix that keeps it from widening the hole: prose
    in a body becomes data, a body actually run by a shell does not.

    Any evaluator token ANYWHERE on the line counts, not just the leading
    command word, so the pipe-to-shell form is covered too. A line that will
    not tokenize returns True — an ambiguous owner is treated as an evaluator,
    which can only keep a body visible, never hide one.

    Note this does NOT address the runtime-script-source escape the gate's own
    docstring already declares out of scope: ``cat <<EOF > /tmp/x.sh`` written
    now and run later is invisible to any single-command check.
    """
    try:
        tokens = _punctuation_tokenize(owner_line)
    except ValueError:
        return True
    return any(posixpath.basename(tok) in SHELL_EVAL_BINARIES for tok in tokens)


def _walk_tokenized_line(tokens: list[str], depth: int) -> list[list[str]]:
    """Walk already-tokenized one-line shell source for git invocations."""
    invocations: list[list[str]] = []
    i = 0
    while i < len(tokens):
        if tokens[i] in _CHAIN_SEPARATORS:
            i += 1
            continue
        segment_end = i
        while segment_end < len(tokens) and tokens[segment_end] not in _CHAIN_SEPARATORS:
            segment_end += 1
        command_index = _command_token_index(tokens, i)
        if command_index is None:
            # An assignments-only segment has no executable of its own, but
            # must not hide a later semicolon-separated command.
            i = segment_end
            continue
        shell_eval = _handle_shell_eval_token(tokens, command_index, depth)
        if shell_eval is not None:
            inner_inv, _ = shell_eval
            invocations.extend(inner_inv)
            i = segment_end
            continue
        executable = posixpath.basename(tokens[command_index])
        if executable in _NON_EXECUTING_ARGUMENT_CONSUMERS:
            i = segment_end
            continue
        for token_index in range(i, segment_end):
            if not _token_is_git_command(tokens[token_index]):
                continue
            collected, _ = _collect_git_invocation(tokens, token_index)
            invocations.append(collected)
        i = segment_end
    return invocations


def walk_git_invocations(
    command: str, depth: int = 0,
) -> tuple[list[list[str]], bool]:
    """Walk a shell command, returning every ``git ...`` invocation.

    Returns ``(invocations, parsed_ok)``. ``parsed_ok=False`` means
    tokenization failed; the caller decides whether to fail-closed.
    Depth-cap = ``MAX_RECURSION`` prevents pathological recursion.
    """
    if depth > MAX_RECURSION:
        return [], True
    retained, heredocs = _split_heredoc_bodies(command)
    outer, subst_pieces = _extract_subst_pieces(retained)
    invocations: list[list[str]] = []
    parsed_ok = True
    for line in outer.splitlines() or [outer]:
        try:
            tokens = _punctuation_tokenize(line)
        except ValueError:
            return [], False
        invocations.extend(_walk_tokenized_line(tokens, depth))
    for _, piece in subst_pieces:
        inner_inv, inner_parsed_ok = walk_git_invocations(piece, depth + 1)
        invocations.extend(inner_inv)
        parsed_ok = parsed_ok and inner_parsed_ok
    for owner_line, body in heredocs:
        if not heredoc_body_is_script_source(owner_line):
            continue
        inner_inv, inner_parsed_ok = walk_git_invocations(body, depth + 1)
        invocations.extend(inner_inv)
        parsed_ok = parsed_ok and inner_parsed_ok
    return invocations, parsed_ok
