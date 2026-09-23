"""Exact runtime values of statically composed Python string literals.

A "statically composed" value is one the interpreter would produce from
constant operands alone, so the census can report the assembled value that a
raw-text scan of the source never sees. The forms folded (iss_271f81af widened
the original ``+`` / implicit-adjacent / interpolation-free f-string set):

* ``str`` and ``bytes`` constants, and ``+`` between two of the same type;
* interpolation-free f-strings, including ``{"literal"}`` parts;
* ``"sep".join([...])`` / ``b"sep".join([...])`` over a list or tuple whose
  every element folds to the separator's type (a set is unordered, so it is
  never folded);
* percent formatting, ``str`` or ``bytes``, with a constant, a tuple of
  constants or a constant-keyed mapping of constants on the right;
* ``str.format`` with constant positional and keyword arguments.

Bytes values are reported decoded as UTF-8 with ``surrogateescape``, which
keeps every ASCII byte verbatim and never raises, so a token assembled from
bytes is matched by the same text pattern as one assembled from text. Anything
outside these forms (a name, a call on a non-constant, a starred or ``**``
argument, a format operation the interpreter would reject) resolves to
``None`` and the raw source is what the scanners see.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from types import EllipsisType

type _StaticText = str | bytes
type _StaticOperand = str | bytes | int | float | complex | bool | EllipsisType | None
# What the interpreter raises on a formatting operation it rejects. Each one
# means "not a static value", never a census failure.
_FORMAT_ERRORS = (TypeError, ValueError, KeyError, IndexError, AttributeError, OverflowError)
# A width or precision this long is never a real static value; it is how a
# twenty-character template makes the census allocate gigabytes. Such a
# template is left unfolded rather than evaluated.
_RUNAWAY_WIDTH = re.compile(rb"[0-9]{7,}")


class _Dynamic:
    """Marker for an operand the census cannot evaluate statically."""

    __slots__ = ()


_DYNAMIC = _Dynamic()


@dataclass(frozen=True, slots=True)
class ResolvedPythonString:
    """One complete, statically known Python string expression."""

    value: str
    line_number: int
    column: int
    source: str


@dataclass(frozen=True, slots=True)
class ResolvedPythonPatternMatch:
    """One regex match introduced only by resolving a static expression."""

    literal: ResolvedPythonString
    token: str


def _joined_string_value(node: ast.JoinedStr) -> str | None:
    parts: list[str] = []
    for part in node.values:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            parts.append(part.value)
            continue
        if (
            isinstance(part, ast.FormattedValue)
            and part.conversion == -1
            and part.format_spec is None
            and isinstance(value := _static_text_value(part.value), str)
        ):
            parts.append(value)
            continue
        return None
    return "".join(parts)


def _static_operand(node: ast.AST) -> _StaticOperand | _Dynamic:
    """A constant of any scalar type, or a folded text; ``_DYNAMIC`` otherwise."""

    if isinstance(node, ast.Constant):
        return node.value
    text = _static_text_value(node)
    return _DYNAMIC if text is None else text


def _static_operands(elements: Sequence[ast.expr]) -> tuple[_StaticOperand, ...] | None:
    operands: list[_StaticOperand] = []
    for element in elements:
        operand = _static_operand(element)
        if isinstance(operand, _Dynamic):
            return None
        operands.append(operand)
    return tuple(operands)


def _static_mapping(node: ast.Dict) -> dict[_StaticOperand, _StaticOperand] | None:
    mapping: dict[_StaticOperand, _StaticOperand] = {}
    for key, value in zip(node.keys, node.values, strict=True):
        operand = _static_operand(value)
        if key is None or not isinstance(key, ast.Constant) or isinstance(operand, _Dynamic):
            return None
        mapping[key.value] = operand
    return mapping


def _has_runaway_width(template: _StaticText) -> bool:
    encoded = template.encode("utf-8", errors="surrogateescape") if isinstance(template, str) else template
    return _RUNAWAY_WIDTH.search(encoded) is not None


def _percent_value(left: _StaticText, right: ast.AST) -> _StaticText | None:
    """``left % right`` when the right side is entirely constant."""

    if _has_runaway_width(left):
        return None
    operand: object
    if isinstance(right, ast.Tuple):
        operand = _static_operands(right.elts)
    elif isinstance(right, ast.Dict):
        operand = _static_mapping(right)
    else:
        operand = _static_operand(right)
    if operand is None or isinstance(operand, _Dynamic):
        return None
    try:
        return left % operand
    except _FORMAT_ERRORS:
        return None


def _typed_pieces[T: (str, bytes)](
    elements: Sequence[ast.expr], kind: type[T]
) -> list[T] | None:
    pieces: list[T] = []
    for element in elements:
        piece = _static_text_value(element)
        if not isinstance(piece, kind):
            return None
        pieces.append(piece)
    return pieces


def _join_value(separator: _StaticText, call: ast.Call) -> _StaticText | None:
    """``sep.join([...])`` over a list or tuple of the separator's own type."""

    if len(call.args) != 1 or call.keywords:
        return None
    (argument,) = call.args
    if not isinstance(argument, ast.List | ast.Tuple):
        return None
    if isinstance(separator, str):
        texts = _typed_pieces(argument.elts, str)
        return None if texts is None else separator.join(texts)
    chunks = _typed_pieces(argument.elts, bytes)
    return None if chunks is None else separator.join(chunks)


def _format_value(template: str, call: ast.Call) -> str | None:
    """``template.format(...)`` with constant positional and keyword arguments."""

    if _has_runaway_width(template):
        return None
    positional = _static_operands(call.args)
    if positional is None:
        return None
    named: dict[str, _StaticOperand] = {}
    for keyword in call.keywords:
        operand = _static_operand(keyword.value)
        if keyword.arg is None or isinstance(operand, _Dynamic):
            return None
        named[keyword.arg] = operand
    try:
        return template.format(*positional, **named)
    except _FORMAT_ERRORS:
        return None


def _call_value(node: ast.Call) -> _StaticText | None:
    if not isinstance(node.func, ast.Attribute):
        return None
    receiver = _static_text_value(node.func.value)
    if receiver is None:
        return None
    if node.func.attr == "join":
        return _join_value(receiver, node)
    if node.func.attr == "format" and isinstance(receiver, str):
        return _format_value(receiver, node)
    return None


def _concatenation_value(left: _StaticText, right: _StaticText | None) -> _StaticText | None:
    if isinstance(left, str) and isinstance(right, str):
        return left + right
    if isinstance(left, bytes) and isinstance(right, bytes):
        return left + right
    return None


def _static_text_value(node: ast.AST) -> _StaticText | None:
    """The exact ``str`` or ``bytes`` a static expression evaluates to."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str | bytes):
        return node.value
    if isinstance(node, ast.BinOp):
        left = _static_text_value(node.left)
        if left is None:
            return None
        if isinstance(node.op, ast.Add):
            return _concatenation_value(left, _static_text_value(node.right))
        return _percent_value(left, node.right) if isinstance(node.op, ast.Mod) else None
    if isinstance(node, ast.JoinedStr):
        return _joined_string_value(node)
    return _call_value(node) if isinstance(node, ast.Call) else None


def _as_text(value: _StaticText) -> str:
    return value if isinstance(value, str) else value.decode("utf-8", errors="surrogateescape")


def literal_string_value(node: ast.AST) -> str | None:
    """Return an exact string value, or ``None`` for any dynamic expression.

    Bytes values are returned decoded (see the module docstring), so a caller
    comparing against a text token sees an assembled bytes token too.
    """

    value = _static_text_value(node)
    return None if value is None else _as_text(value)


def _source_segment(lines: list[str], node: ast.expr) -> str:
    start_line = node.lineno - 1
    end_line = (node.end_lineno or node.lineno) - 1
    start_column = node.col_offset
    end_column = node.end_col_offset or start_column
    if start_line == end_line:
        return lines[start_line].encode()[start_column:end_column].decode()
    segments = [lines[start_line].encode()[start_column:].decode()]
    segments.extend(lines[start_line + 1 : end_line])
    segments.append(lines[end_line].encode()[:end_column].decode())
    return "\n".join(segments)


class _LiteralVisitor(ast.NodeVisitor):
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.resolved: list[ResolvedPythonString] = []

    def _record(self, node: ast.expr, value: str) -> None:
        line = self._lines[node.lineno - 1]
        column = len(line.encode()[: node.col_offset].decode())
        self.resolved.append(
            ResolvedPythonString(
                value=value,
                line_number=node.lineno,
                column=column,
                source=_source_segment(self._lines, node),
            )
        )

    def _record_static(self, node: ast.expr) -> bool:
        value = literal_string_value(node)
        if value is None:
            return False
        self._record(node, value)
        return True

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Add):
            self._record_static(node)
            # A dynamic outer concatenation is not an exact literal value. Do
            # not promote a statically foldable child fragment into the whole
            # value.
            return
        if isinstance(node.op, ast.Mod) and self._record_static(node):
            return
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # ``"sep".join([...])`` / ``"{}".format(...)`` with constant operands
        # fold to one value; any other call is scanned operand by operand, as
        # it was before the fold covered calls at all.
        if not self._record_static(node):
            self.generic_visit(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        self._record_static(node)
        # As above, literal fragments of a dynamic f-string are not complete
        # values and must not be scanned independently.

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str | bytes):
            self._record(node, _as_text(node.value))


def resolved_python_strings(source: str) -> tuple[ResolvedPythonString, ...]:
    """Collect complete static string expressions from parseable Python."""

    try:
        tree = ast.parse(source)
    except (IndentationError, SyntaxError, ValueError):
        return ()
    visitor = _LiteralVisitor(source.splitlines())
    visitor.visit(tree)
    return tuple(visitor.resolved)


def newly_resolved_pattern_matches(
    source: str, pattern: re.Pattern[str]
) -> tuple[ResolvedPythonPatternMatch, ...]:
    """Return pattern hits absent from each expression's raw source bytes."""

    resolved: list[ResolvedPythonPatternMatch] = []
    for literal in resolved_python_strings(source):
        visible = Counter(match.group(0) for match in pattern.finditer(literal.source))
        for match in pattern.finditer(literal.value):
            token = match.group(0)
            if visible[token]:
                visible[token] -= 1
                continue
            resolved.append(ResolvedPythonPatternMatch(literal=literal, token=token))
    return tuple(resolved)


__all__ = [
    "ResolvedPythonPatternMatch",
    "ResolvedPythonString",
    "literal_string_value",
    "newly_resolved_pattern_matches",
    "resolved_python_strings",
]
