"""Minimal lex helpers for the Git-Controller gate.

Sibling module to ``git_controller_gate.py``. Scope: trusted peer
mistake-prevention, NOT adversarial obfuscation defense. Only the structural
pieces needed to recognize a direct ``git <subcommand>`` invocation, including
when wrapped in ``bash -c`` / ``sh -c`` / ``eval`` or ``$(...)`` substitution.

Stdlib-only.
"""

from __future__ import annotations

import re
import shlex

_SUBST_PLACEHOLDER_PREFIX = "__GCG_SUBST_"
_CMD_SUBST_RE = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")

CHAIN_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")"})


def punctuation_tokenize(command: str) -> list[str]:
    """Punctuation-aware shlex tokenization.

    Treats ``&&``, ``||``, ``;``, ``|``, ``&``, ``(``, ``)`` as token
    boundaries so the walker can detect compound commands like
    ``git status && git push``.
    """
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    return list(lex)


def extract_subst_pieces(command: str) -> tuple[str, list[tuple[str, str]]]:
    """Replace ``$(...)`` and ``` `...` ``` with placeholders.

    Returns ``(outer_command, [(placeholder, inner_piece), ...])``. The
    walker recurses on each inner piece so ``echo $(git push)`` catches
    the inner git invocation.
    """
    pieces: list[tuple[str, str]] = []

    def repl(match: re.Match[str]) -> str:
        inner = match.group(1) or match.group(2) or ""
        placeholder = f"{_SUBST_PLACEHOLDER_PREFIX}{len(pieces)}__"
        pieces.append((placeholder, inner))
        return placeholder

    outer = _CMD_SUBST_RE.sub(repl, command)
    return outer, pieces


# A heredoc opener: ``<<WORD``, ``<<-WORD``, ``<<'WORD'``, ``<<"WORD"``. The
# ``(?<!<)`` lookbehind keeps a ``<<<WORD`` here-string from matching on its
# second and third ``<`` — a here-string's data sits on the SAME line and is
# already an ordinary token, so treating it as a heredoc opener would swallow
# the following lines as a body that shell never reads that way.
_HEREDOC_START_RE = re.compile(
    r"(?<!<)<<(-?)[ \t]*(?:'([^']*)'|\"([^\"]*)\"|([A-Za-z_][A-Za-z0-9_]*))",
)


def _heredoc_starts(line: str) -> list[tuple[bool, str]]:
    """Every heredoc opener on one line, in the order the shell consumes them.

    Returns ``[(strip_leading_tabs, delimiter), ...]``; the flag is the
    ``<<-`` variant, whose terminator may be indented with tabs.
    """
    starts: list[tuple[bool, str]] = []
    for match in _HEREDOC_START_RE.finditer(line):
        delimiter = match.group(2) or match.group(3) or match.group(4) or ""
        if delimiter:
            starts.append((match.group(1) == "-", delimiter))
    return starts


def _consume_heredoc_body(
    lines: list[str], index: int, delimiter: str, strip_tabs: bool,
) -> tuple[list[str], int, bool]:
    """Read one heredoc body from ``index`` up to its terminator line.

    Returns ``(body_lines, next_index, terminated)``. ``terminated`` false
    means the body ran off the end of the command with no closing delimiter;
    the caller must then keep those lines VISIBLE rather than treat them as
    data — see ``split_heredoc_bodies``.
    """
    body: list[str] = []
    while index < len(lines):
        candidate = lines[index]
        probe = candidate.lstrip("\t") if strip_tabs else candidate
        index += 1
        if probe.rstrip() == delimiter:
            return body, index, True
        body.append(candidate)
    return body, index, False


def split_heredoc_bodies(command: str) -> tuple[str, list[tuple[str, str]]]:
    """Separate heredoc BODIES from the command text that surrounds them.

    Returns ``(retained_command, [(owner_line, body), ...])``. A heredoc body
    is DATA — file content, a message, prose — and walking it for command
    tokens is what made ``cat > note.md <<'EOF'`` carrying two ordinary words
    about this gate parse as a command plus an unrecognized subcommand.

    What stays in ``retained_command`` is deliberate and is the reason this is
    not a widened hole: the owning command, the ``<<`` operator itself, the
    delimiter word, and every line after the terminator all remain visible to
    the walker. Only body lines move out, and the caller still decides what to
    do with them — a body fed to a shell evaluator is script source, not data
    (``heredoc_body_is_script_source`` in the walker).

    An UNTERMINATED body fails VISIBLE: its lines are retained as command text
    rather than silently swallowed. The opposite choice would let a runaway
    delimiter hide the rest of a command by accident, not just by design.

    Scope, unchanged from the rest of this module: peer mistake-prevention.
    A ``<<`` written inside a quoted string is read as an opener here; that
    costs a false negative, never a false block.
    """
    lines = command.split("\n")
    retained: list[str] = []
    bodies: list[tuple[str, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        retained.append(line)
        index += 1
        for strip_tabs, delimiter in _heredoc_starts(line):
            body, index, terminated = _consume_heredoc_body(
                lines, index, delimiter, strip_tabs,
            )
            if terminated:
                bodies.append((line, "\n".join(body)))
            else:
                retained.extend(body)
    return "\n".join(retained), bodies
