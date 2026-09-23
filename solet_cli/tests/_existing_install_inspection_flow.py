"""Conservative control-flow predicates for the inspection call collector."""

from __future__ import annotations

import ast
from collections.abc import Callable, Sequence


def assigned_names(statements: list[ast.stmt]) -> set[str]:
    """Find bindings a non-convergent loop must conservatively forget."""
    return {
        node.id
        for statement in statements
        for node in ast.walk(statement)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }


def terminates_unconditionally(
    statement: ast.stmt,
    block_terminates: Callable[[list[ast.stmt]], bool],
) -> bool:
    """Whether every reachable path through this statement transfers control."""
    if isinstance(statement, (ast.Raise, ast.Return, ast.Break, ast.Continue)):
        return True
    if isinstance(statement, ast.If):
        return _all_blocks_terminate((statement.body, statement.orelse), block_terminates)
    if isinstance(statement, (ast.Try, ast.TryStar)):
        return _try_terminates(statement, block_terminates)
    if isinstance(statement, ast.Match):
        return _match_terminates(statement, block_terminates)
    return False


def _all_blocks_terminate(
    blocks: tuple[list[ast.stmt], ...],
    block_terminates: Callable[[list[ast.stmt]], bool],
) -> bool:
    return all(block_terminates(block) for block in blocks)


def _try_terminates(
    statement: ast.Try | ast.TryStar,
    block_terminates: Callable[[list[ast.stmt]], bool],
) -> bool:
    branches = (statement.body, *(handler.body for handler in statement.handlers))
    if statement.orelse:
        branches = (*branches, statement.orelse)
    return _all_blocks_terminate(branches, block_terminates)


def _match_terminates(
    statement: ast.Match,
    block_terminates: Callable[[list[ast.stmt]], bool],
) -> bool:
    if not statement.cases or not _has_wildcard_case(statement.cases):
        return False
    return _all_blocks_terminate(tuple(case.body for case in statement.cases), block_terminates)


def _has_wildcard_case(cases: list[ast.match_case]) -> bool:
    return any(
        isinstance(case.pattern, ast.MatchAs) and case.pattern.name is None and case.guard is None
        for case in cases
    )


def contains_finally_transfer(nodes: Sequence[ast.AST]) -> bool:
    """Find a transfer owned by this finally block, not a nested scope/loop."""
    for node in nodes:
        if isinstance(node, (ast.Return, ast.Break, ast.Continue)):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            if contains_finally_transfer(node.orelse):
                return True
            continue
        if contains_finally_transfer(list(ast.iter_child_nodes(node))):
            return True
    return False


def has_reachable_loop_break(
    statements: list[ast.stmt],
    terminates: Callable[[ast.stmt], bool],
) -> bool:
    """Find a break that reaches this loop, ignoring dead suffixes."""
    for statement in statements:
        if _contains_loop_break(statement, terminates):
            return True
        if terminates(statement):
            break
    return False


def _contains_loop_break(node: ast.AST, terminates: Callable[[ast.stmt], bool]) -> bool:
    if isinstance(node, ast.Break):
        return True
    if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
        return has_reachable_loop_break(node.orelse, terminates)
    if isinstance(node, _BREAK_SCOPE_BOUNDARIES):
        return False
    return any(has_reachable_loop_break(block, terminates) for block in _break_blocks(node))


_BREAK_SCOPE_BOUNDARIES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


def _break_blocks(node: ast.AST) -> tuple[list[ast.stmt], ...]:
    if isinstance(node, ast.If):
        return (node.body, node.orelse)
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return (node.body,)
    if isinstance(node, (ast.Try, ast.TryStar)):
        return (
            node.body,
            node.orelse,
            node.finalbody,
            *(handler.body for handler in node.handlers),
        )
    if isinstance(node, ast.Match):
        return tuple(case.body for case in node.cases)
    return ()


def nested_blocks(statement: ast.stmt) -> tuple[list[ast.stmt], ...]:
    """Return one compound layer's branches for bounded try-prefix capture."""
    if isinstance(statement, ast.If):
        return (statement.body, statement.orelse)
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        return (statement.body,)
    if isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
        return (statement.body, statement.orelse)
    if isinstance(statement, (ast.Try, ast.TryStar)):
        return (
            statement.body,
            statement.orelse,
            statement.finalbody,
            *(handler.body for handler in statement.handlers),
        )
    if isinstance(statement, ast.Match):
        return tuple(case.body for case in statement.cases)
    return ()
